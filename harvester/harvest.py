#!/usr/bin/env python3
"""TNP Seagrass Nursery harvester.

Polls the Seneye cloud API for every device on the account, stores each new
reading, and exports the JSON the dashboard reads.

    python -m harvester.harvest                 poll once, store, export
    python -m harvester.harvest --export-only   rebuild the JSON from the DB
    python -m harvester.harvest --dry-run       poll and print, write nothing

Credentials come from the environment (never the config file):

    SENEYE_USER          the Seneye account e-mail
    SENEYE_PWD           the Seneye account password
    TUYA_ACCESS_ID       Tuya cloud project Access ID, for the chiller plugs
    TUYA_ACCESS_SECRET   Tuya cloud project Access Secret
    DATABASE_URL         optional; defaults to sqlite:///data/nursery.db
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

if __package__ in (None, ""):  # allow `python harvester/harvest.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harvester.export import build_payload, write_csv, write_payload
from harvester.nutrients import load as load_nutrients
from harvester.plugs import load as load_plugs
from harvester.seneye import SeneyeClient, SeneyeError
from harvester.store import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(ROOT, "config.json")


def load_config(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Harvest Seneye readings for the TNP nursery dashboard")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--out", default=os.path.join(ROOT, "dashboard", "data", "nursery.json"))
    ap.add_argument("--csv", default=os.path.join(ROOT, "dashboard", "data", "readings.csv"))
    ap.add_argument("--window-days", type=int, default=None)
    ap.add_argument("--raw-days", type=int, default=None)
    ap.add_argument("--nutrients", default=None,
                    help="read samples from this .xlsx instead of the configured sheet")
    ap.add_argument("--skip-nutrients", action="store_true")
    ap.add_argument("--skip-maintenance", action="store_true")
    ap.add_argument("--skip-plugs", action="store_true")
    ap.add_argument("--export-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    config = load_config(args.config)
    store = Store(args.database_url)
    store.migrate()

    started = int(time.time())
    polled = 0
    inserted = 0
    status = "ok"
    message = ""

    if not args.export_only:
        try:
            client = SeneyeClient(
                user=os.environ.get("SENEYE_USER", ""),
                pwd=os.environ.get("SENEYE_PWD", ""),
            )
            readings = client.poll()
            polled = len(readings)
            device_cfg = config.get("devices", {})

            if args.dry_run:
                for r in readings:
                    print(json.dumps(r.as_row(), indent=2, default=str))
                return 0

            sumps_cfg = config.get("sumps", {})
            for r in readings:
                cfg = device_cfg.get(r.device_id, {})
                sump = cfg.get("sump")
                sump_cfg = sumps_cfg.get(sump, {}) if sump else {}
                tanks = sump_cfg.get("tanks", [])
                label = cfg.get("label") or (
                    f"{sump} → tanks {', '.join(tanks)}" if sump and tanks
                    else sump or f"Seneye {r.device_id}"
                )
                if sump and sump not in sumps_cfg:
                    print(
                        f"warning: device {r.device_id} maps to sump {sump}, "
                        "which is not listed in config.json > sumps",
                        file=sys.stderr,
                    )
                store.upsert_device(
                    device_id=r.device_id,
                    description=label,
                    device_type=None,
                    sump_code=sump,
                    system_code=cfg.get("system") or sump_cfg.get("system"),
                    label=label,
                    seen_at=r.reading_time,
                )
            inserted = store.insert_readings(r.as_row() for r in readings)
            print(f"polled {polled} device(s), {inserted} new reading(s)")

        except SeneyeError as exc:
            status = "error"
            message = str(exc)
            print(f"harvest failed: {exc}", file=sys.stderr)

    # In-situ samples, re-read every run straight from the Google Sheet. A
    # failure here must never stop the Seneye harvest or wipe what is stored.
    if args.nutrients:
        config.setdefault("nutrients", {})["workbook"] = args.nutrients
        config["nutrients"].pop("sheet_url", None)
    if args.skip_nutrients:
        print("nutrients: skipped (--skip-nutrients)")
    else:
        try:
            load_nutrients(store, config, ROOT)
        except Exception as exc:
            print(f"nutrients: could not load samples: {exc}", file=sys.stderr)

    # Chiller plugs and the nursery air sensor, read from the Tuya cloud. Read
    # only: nothing here ever switches anything. A Tuya failure is reported and
    # stepped over, because a plug reading going missing must not stop the
    # water readings being collected or published.
    if args.skip_plugs:
        print("plugs: skipped (--skip-plugs)")
    else:
        try:
            summary = load_plugs(store, config)
            if summary.get("skipped"):
                pass
            else:
                print(
                    f"plugs: {summary.get('polled', 0)} socket(s)/sensor(s) read, "
                    f"{summary.get('sockets', 0)} state change(s), "
                    f"{summary.get('ambient', 0)} new air reading(s), "
                    f"{summary.get('meter', 0)} meter reading(s)"
                    + (f", {summary['offline']} device(s) not answering"
                       if summary.get("offline") else "")
                )
                # A device that has been factory reset is gone from the
                # account, not merely unreachable, and nothing else would ever
                # say so: the readings just stop. This is the one plug failure
                # worth shouting about, because it needs a decision rather than
                # a walk to the nursery.
                for did in summary.get("gone", []):
                    print(
                        f"plugs: WARNING device {did} is configured but is no "
                        "longer on the Tuya account. A factory reset gives a "
                        "device a new ID, and rejoining it to a different "
                        "Wi-Fi network does exactly that.",
                        file=sys.stderr,
                    )
                if summary.get("gone") and summary.get("candidates"):
                    print("plugs: devices on the account that config.json does "
                          "not know about, one of which is probably it:",
                          file=sys.stderr)
                    for d in summary["candidates"]:
                        print(f"    {d['id']}  {d.get('name') or '(no name)'}  "
                              f"[{d.get('product_name') or 'unknown'}]",
                              file=sys.stderr)
                    print("plugs: fix it with Actions -> Replace a device ID, "
                          "which keeps the old readings on the same chart.",
                          file=sys.stderr)
                for did in summary.get("unchecked", []):
                    print(f"plugs: {did} returned nothing, and the account "
                          "listing could not be read to say why", file=sys.stderr)

                days = summary.get("subscription_days")
                if days is not None and days <= 30:
                    print(
                        f"plugs: WARNING the Tuya IoT Core subscription "
                        + (f"lapsed {abs(days)} day(s) ago"
                           if days < 0 else f"expires in {days} day(s)")
                        + ". Extend it at iot.tuya.com under Cloud > Cloud Services "
                          "> IoT Core, then update plugs.subscription_expires in "
                          "config.json. Until then the plug readings will stop "
                          "updating without any other sign.",
                        file=sys.stderr,
                    )
        except Exception as exc:
            print(f"plugs: could not read the Tuya devices: {exc}", file=sys.stderr)

    # Maintenance issues and planned jobs. Named people appear in this log, so
    # it is written outside the published dashboard folder by default.
    # The maintenance board is imported here rather than at the top of the file
    # on purpose. It is an optional feature, off in this deployment, and a
    # module-level import made a missing or broken maintenance.py abort the
    # whole run before a single reading was collected. Nothing optional should
    # be able to stop the water readings.
    maint_cfg = config.get("maintenance", {}) or {}
    if maint_cfg.get("enabled", True) and not args.skip_maintenance:
        try:
            from harvester.maintenance import build_payload as build_board
            from harvester.maintenance import load as load_maintenance

            load_maintenance(store, config, ROOT)
            board = build_board(store, config)
            board_path = os.path.join(ROOT, maint_cfg.get("out", "board/data/maintenance.json"))
            write_payload(board, board_path)
            print(
                f"board: {board['counts']['open']} open, "
                f"{board['counts']['in_progress']} in progress, "
                f"{board['counts']['overdue']} job(s) overdue -> {board_path}"
            )
        except Exception as exc:
            print(f"maintenance: could not build the board: {exc}", file=sys.stderr)

    export_cfg = config.get("export", {})
    payload = build_payload(
        store,
        config,
        window_days=args.window_days or export_cfg.get("window_days", 365),
        raw_days=args.raw_days or export_cfg.get("raw_days", 30),
    )
    write_payload(payload, args.out)
    if args.csv:
        write_csv(store, args.csv)
    print(f"exported {len(payload['readings'])} reading(s) to {args.out}")

    store.finish_run(
        started_at=started,
        finished_at=int(time.time()),
        status=status,
        devices_polled=polled,
        readings_inserted=inserted,
        message=message,
    )
    store.close()
    return 1 if status == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Checking the nursery against the chiller schedule.

The schedule exists because of a hard constraint: one chiller may run between
12:00 and 21:00, three overnight, or the cabinet overheats. Nine chillers
rejecting 2.95 kW each into one enclosure is the thing the whole timetable is
arranged around. A dashboard that can say "four are running and the cabinet
allows one" is worth more than any amount of pretty temperature history.

Three questions, in the order they matter:

    is anything running that should not be, right now
    did each system actually get its block
    how long has each system gone without cooling

The second is not idle bookkeeping. A Smart Life schedule fires at the block
boundary and a plug that is off the network at that moment misses it outright;
nothing retries. Rows C and E sat dark for days that way in September, and
nothing anywhere said so. Losing a 2.5-hour block from a timetable built around
a 16 degree time-average is not a rounding error.

Everything here reads. Nothing in this module can switch anything: the page it
feeds is a static file on a public website and must never acquire the ability
to turn a chiller off.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from typing import Any, Iterable

DAY = 86400


def _zone(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return datetime.timezone.utc


def _hhmm(text: str) -> tuple[int, int]:
    """'09:30' or '0930' to (9, 30)."""
    t = str(text).strip().replace(":", "")
    if not t.isdigit() or len(t) not in (3, 4):
        raise ValueError(f"not a time of day: {text!r}")
    t = t.zfill(4)
    h, m = int(t[:2]), int(t[2:])
    if not (0 <= h <= 24 and 0 <= m < 60):
        raise ValueError(f"not a time of day: {text!r}")
    return h, m


def occurrences(cfg: dict[str, Any], start: int, end: int, tz) -> list[dict[str, Any]]:
    """Every run of every block between two instants, in real time.

    Blocks are written in local time and several of them cross midnight, so
    they are laid down day by day against the nursery's own clock rather than
    by adding seconds to a UTC epoch. That also keeps them honest across the
    October clock change, when one local day is 25 hours long.
    """
    blocks = [b for b in (cfg.get("blocks") or []) if b.get("sumps")]
    if not blocks:
        return []

    first = datetime.datetime.fromtimestamp(start, tz).date() - datetime.timedelta(days=1)
    last = datetime.datetime.fromtimestamp(end, tz).date() + datetime.timedelta(days=1)

    out: list[dict[str, Any]] = []
    day = first
    while day <= last:
        for b in blocks:
            fh, fm = _hhmm(b["from"])
            th, tm = _hhmm(b["to"])
            begins = datetime.datetime.combine(day, datetime.time(fh % 24, fm), tz)
            ends = datetime.datetime.combine(day, datetime.time(th % 24, tm), tz)
            if ends <= begins:  # the block runs over midnight
                ends += datetime.timedelta(days=1)
            a, z = int(begins.timestamp()), int(ends.timestamp())
            if z > start and a < end:
                out.append({
                    "from": a, "to": z,
                    "label": f"{b['from']}–{b['to']}",
                    "sumps": list(b["sumps"]),
                })
        day += datetime.timedelta(days=1)
    out.sort(key=lambda o: o["from"])
    return out


def limit_at(cfg: dict[str, Any], when: int, tz) -> int | None:
    """How many chillers the cabinet allows at this time of day."""
    local = datetime.datetime.fromtimestamp(when, tz)
    minute = local.hour * 60 + local.minute
    for lim in cfg.get("limits") or []:
        fh, fm = _hhmm(lim["from"])
        th, tm = _hhmm(lim["to"])
        a, z = fh * 60 + fm, th * 60 + tm
        inside = a <= minute < z if a < z else (minute >= a or minute < z)
        if inside:
            return int(lim["max_running"])
    return None


def spans(rows: Iterable[dict[str, Any]], now: int) -> dict[str, list[tuple[int, int, int, int]]]:
    """Turn the transition log into, per sump, a list of (start, end, on, sure).

    `sure` is 0 while the plug was off the network. The relay holds whatever
    position it was left in, so the state is probably still right, but it is
    not confirmed and the totals say how much of the day was in that condition
    rather than quietly counting it as fact.
    """
    by_socket: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_socket[(r["device_id"], r["socket"])].append(r)

    out: dict[str, list[tuple[int, int, int, int]]] = defaultdict(list)
    for key, log in by_socket.items():
        log.sort(key=lambda r: int(r["changed_at"]))
        for i, r in enumerate(log):
            sump = r.get("sump_code")
            if not sump or r.get("on_state") is None:
                continue
            begins = int(r["changed_at"])
            ends = int(log[i + 1]["changed_at"]) if i + 1 < len(log) else now
            if ends <= begins:
                continue
            out[sump].append((begins, ends, int(r["on_state"]),
                              0 if r.get("online") == 0 else 1))
    for sump in out:
        out[sump].sort()
    return out


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def running_at(by_sump: dict[str, list[tuple[int, int, int, int]]], when: int) -> list[str]:
    """What was on at this instant, from the most recent transition before it.

    Written against the transitions rather than the spans on purpose. Matching
    a span with `start <= when < end` looks right and is wrong at exactly one
    moment: the open span runs to `now`, so asking what is on *now* fell past
    the end of it and reported an idle nursery. A transition log answers this
    question by looking backwards, not by looking for a bracket.
    """
    on = []
    for sump, runs in by_sump.items():
        state = None
        for a, _z, st, _sure in runs:
            if a > when:
                break
            state = st
        if state == 1:
            on.append(sump)
    return sorted(on)


def breaches(by_sump, cfg, start: int, end: int, tz, step: int = 300) -> list[dict[str, Any]]:
    """Stretches where more chillers were on than the cabinet allows.

    Swept at five-minute resolution, which is finer than the half-hourly
    harvest can actually resolve. That is deliberate: the sweep is over the
    reconstructed on/off spans, not over the samples, so a plug that switched
    at 02:03 is placed at 02:03.
    """
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    t = start
    while t < end:
        cap = limit_at(cfg, t, tz)
        on = running_at(by_sump, t)
        over = cap is not None and len(on) > cap
        if over:
            if current is None:
                current = {"from": t, "to": t + step, "limit": cap,
                           "peak": len(on), "sumps": set(on)}
            else:
                current["to"] = t + step
                current["peak"] = max(current["peak"], len(on))
                current["sumps"].update(on)
        elif current is not None:
            current["sumps"] = sorted(current["sumps"])
            out.append(current)
            current = None
        t += step
    if current is not None:
        current["sumps"] = sorted(current["sumps"])
        out.append(current)
    return out


def build(store, config: dict[str, Any], now: int, days: int = 3) -> dict[str, Any]:
    cfg = (config.get("schedule") or {})
    if not cfg.get("enabled", False):
        return {"enabled": False}

    tz = _zone(cfg.get("timezone") or (config.get("site") or {}).get("timezone") or "UTC")
    since = now - days * DAY

    try:
        rows = store.query(
            "SELECT device_id, socket, changed_at, sump_code, on_state, online "
            "FROM plug_states ORDER BY changed_at"
        )
    except Exception:  # no plug table yet
        return {"enabled": False}
    if not rows:
        return {"enabled": True, "ready": False,
                "note": "No plug readings stored yet, so nothing can be checked."}

    by_sump = spans(rows, now)
    runs = occurrences(cfg, since, now, tz)

    # ---- what should be happening this minute, against what is
    here = [o for o in runs if o["from"] <= now < o["to"]]
    block = here[-1] if here else None
    actual = running_at(by_sump, now)
    expected = sorted(block["sumps"]) if block else []
    cap = limit_at(cfg, now, tz)

    # ---- did each block actually happen
    targets = cfg.get("daily_target_hours") or {}
    misses, per_block = [], []
    for o in runs:
        if o["to"] > now:
            continue  # still in progress, too early to judge
        length = o["to"] - o["from"]
        for sump in o["sumps"]:
            on = sum(_overlap(a, z, o["from"], o["to"])
                     for a, z, state, _ in by_sump.get(sump, []) if state == 1)
            frac = on / length if length else 0
            per_block.append({"from": o["from"], "to": o["to"], "label": o["label"],
                              "sump": sump, "fraction": round(frac, 3)})
            # Under a fifth of the block is a miss rather than a late start.
            if frac < 0.2:
                misses.append({"from": o["from"], "to": o["to"], "label": o["label"],
                               "sump": sump, "fraction": round(frac, 3)})

    # ---- hours per system per day, against the schedule's own targets
    daily: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    start_day = int(datetime.datetime.fromtimestamp(since, tz)
                    .replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    edges = []
    d = start_day
    while d < now:
        nxt = int((datetime.datetime.fromtimestamp(d, tz)
                   + datetime.timedelta(days=1)).timestamp())
        edges.append((d, min(nxt, now)))
        d = nxt
    for a, z in edges:
        for sump, spans_ in by_sump.items():
            on = sum(_overlap(s0, s1, a, z) for s0, s1, state, _ in spans_ if state == 1)
            unsure = sum(_overlap(s0, s1, a, z) for s0, s1, _, sure in spans_ if not sure)
            daily[a][sump] = {
                "hours": round(on / 3600, 2),
                "target": targets.get(sump),
                "unconfirmed_hours": round(unsure / 3600, 2),
            }

    # The longest a system went without cooling, measured across the whole
    # window rather than per calendar day. Clipping at midnight split the one
    # gap that matters most, because the overnight stretch is the long one.
    gaps: dict[str, dict[str, Any]] = {}
    for sump, spans_ in by_sump.items():
        cooling = sorted((s0, s1) for s0, s1, state, _ in spans_ if state == 1)
        worst, at, last_end = 0, None, since
        for s0, s1 in cooling:
            if s1 <= since:
                continue
            if s0 - last_end > worst:
                worst, at = s0 - last_end, last_end
            last_end = max(last_end, s1)
        if now - last_end > worst:
            worst, at = now - last_end, last_end
        gaps[sump] = {"hours": round(worst / 3600, 2), "since": at,
                      "ongoing": bool(at is not None and at + worst >= now - 60)}

    over = breaches(by_sump, cfg, since, now, tz)

    return {
        "enabled": True,
        "ready": True,
        "timezone": str(tz),
        "target_c": cfg.get("target_c"),
        "setpoints": cfg.get("setpoints") or {},
        "now": {
            "t": now,
            "block": ({"label": block["label"], "from": block["from"], "to": block["to"]}
                      if block else None),
            "expected": expected,
            "actual": actual,
            "missing": [s for s in expected if s not in actual],
            "unexpected": [s for s in actual if s not in expected],
            "running": len(actual),
            "limit": cap,
            "over_limit": bool(cap is not None and len(actual) > cap),
        },
        "blocks": per_block[-80:],
        "misses": misses[-40:],
        "daily": [{"day": a, "by_sump": v} for a, v in sorted(daily.items())],
        "gaps": gaps,
        "breaches": [{"from": b["from"], "to": b["to"], "limit": b["limit"],
                      "peak": b["peak"], "sumps": b["sumps"]} for b in over],
        "note": cfg.get("note"),
    }

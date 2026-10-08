"""Tuya cloud client for the nursery's smart plugs and air sensor.

Two kinds of device sit on the Tuya account:

* double-socket plugs, one per row of systems, each socket switching a chiller
  for one sump. The dashboard reads their state and never writes it: the page
  is a static file on a public website and cannot hold a credential, so
  switching stays in the phone app where it belongs.
* a temperature and humidity sensor inside the nursery, which measures the air
  the tanks sit in rather than the water. Worth having: a sump climbing on a
  hot afternoon with its chiller drawing power is a different fault from a
  sump climbing with its chiller switched off.

No third-party dependencies. Tuya sign requests with HMAC-SHA256 over a
canonical string, which is a dozen lines of `hmac` and `hashlib`.

Credentials come from the environment, never the config file:

    TUYA_ACCESS_ID      "Access ID/Client ID" from the cloud project
    TUYA_ACCESS_SECRET  "Access Secret/Client Secret" from the same page

Docs: https://developer.tuya.com/en/docs/cloud/
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable

USER_AGENT = "tnp-seneye-harvester/1.0 (+https://github.com/Nautilusproject-Gib)"

# Tuya route every account to one data centre and refuse calls sent to another,
# so this has to match the region the cloud project was created in. Gibraltar
# accounts are normally Central Europe.
REGIONS = {
    "eu": "https://openapi.tuyaeu.com",          # Central Europe
    "weu": "https://openapi-weaz.tuyaeu.com",    # Western Europe
    "us": "https://openapi.tuyaus.com",          # Western America
    "eus": "https://openapi-ueaz.tuyaus.com",    # Eastern America
    "cn": "https://openapi.tuyacn.com",          # China
    "in": "https://openapi.tuyain.com",          # India
}

EMPTY_BODY_SHA256 = hashlib.sha256(b"").hexdigest()

# What a socket's on/off state is called in Tuya's data points. A single-socket
# plug reports `switch` or `switch_1`; a double reports both `switch_1` and
# `switch_2`.
SWITCH_CODES = ("switch", "switch_1", "switch_2", "switch_3", "switch_4")

# Air sensor data points, with the divisor Tuya apply. Their API reports scaled
# integers: 213 means 21.3 degrees. The divisors are overridable per device in
# config.json because cheaper sensors are not consistent about it.
AMBIENT_CODES = {
    "air_temperature": (("va_temperature", "temp_current", "temperature"), 10.0),
    "humidity": (("va_humidity", "humidity_value", "humidity"), 1.0),
    "battery": (("battery_percentage", "battery_state", "battery"), 1.0),
}

# Energy monitoring, on the plugs that have it. Absent on most cheap doubles,
# which is fine: a socket's on/off state is the useful part.
POWER_CODES = {
    "power_w": (("cur_power",), 10.0),
    "voltage_v": (("cur_voltage",), 10.0),
    "current_ma": (("cur_current",), 1.0),
}


class TuyaError(RuntimeError):
    pass


@dataclass
class SocketState:
    """One socket of one plug, at one instant."""

    device_id: str
    socket: str
    reading_time: int
    # When Tuya last heard from the plug, which is NOT the same as when we
    # polled. A plug that is online was in contact just now; one that is
    # offline was last in contact whenever its record says, and that date is
    # the honest answer to "how old is this reading".
    last_contact: int | None = None
    sump_code: str | None = None
    role: str | None = None
    label: str | None = None
    on_state: int | None = None
    online: int | None = None
    power_w: float | None = None
    plug_power_w: float | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "socket": self.socket,
            "reading_time": self.reading_time,
            "last_contact": self.last_contact,
            "sump_code": self.sump_code,
            "role": self.role,
            "on_state": self.on_state,
            "online": self.online,
            "power_w": self.power_w,
            "plug_power_w": self.plug_power_w,
        }


@dataclass
class AmbientReading:
    """Air temperature and humidity inside the nursery."""

    device_id: str
    reading_time: int
    # When Tuya last saw one of this sensor's values move. Kept apart from
    # reading_time for the same reason it is on the plugs: they answer
    # different questions. reading_time says when we took this reading;
    # reported_at says how long the sensor has been saying the same thing.
    reported_at: int | None = None
    air_temperature: float | None = None
    humidity: float | None = None
    battery: float | None = None
    online: int | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "reading_time": self.reading_time,
            "reported_at": self.reported_at,
            "air_temperature": self.air_temperature,
            "humidity": self.humidity,
            "battery": self.battery,
            "online": self.online,
        }


@dataclass
class PowerSample:
    """One meter reading from one plug, kept as a series rather than a state.

    The meter is per plug, not per socket, so this is what the whole row draws.
    Splitting it between two running chillers would be arithmetic with no
    measurement behind it.
    """

    device_id: str
    reading_time: int
    power_w: float | None = None
    voltage_v: float | None = None
    current_ma: float | None = None
    add_ele: float | None = None
    online: int | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "reading_time": self.reading_time,
            "power_w": self.power_w,
            "voltage_v": self.voltage_v,
            "current_ma": self.current_ma,
            "add_ele": self.add_ele,
            "online": self.online,
        }


@dataclass
class Poll:
    sockets: list[SocketState] = field(default_factory=list)
    ambient: list[AmbientReading] = field(default_factory=list)
    power: list[PowerSample] = field(default_factory=list)
    raw: dict[str, dict[str, Any]] = field(default_factory=dict)
    offline: list[str] = field(default_factory=list)


class TuyaClient:
    def __init__(
        self,
        access_id: str,
        access_secret: str,
        region: str = "eu",
        timeout: int = 20,
        retries: int = 3,
    ):
        if not access_id or not access_secret:
            raise TuyaError(
                "Tuya credentials missing. Set TUYA_ACCESS_ID and TUYA_ACCESS_SECRET."
            )
        self.access_id = access_id
        self.access_secret = access_secret.encode("utf-8")
        self.base = REGIONS.get(str(region).lower(), region if str(region).startswith("http")
                                else REGIONS["eu"])
        self.timeout = timeout
        self.retries = retries
        self._token: str | None = None
        self._token_expires = 0

    # -- signing -----------------------------------------------------------

    def _canonical(self, method: str, path: str, query: dict[str, str] | None) -> str:
        """Tuya's URL for signing: query parameters sorted by key."""
        if not query:
            return path
        items = sorted(query.items())
        return path + "?" + urllib.parse.urlencode(items)

    def _sign(self, method: str, url_for_sign: str, body: str, token: str | None) -> dict[str, str]:
        t = str(int(time.time() * 1000))
        nonce = hashlib.md5((t + url_for_sign).encode("utf-8")).hexdigest()
        digest = (hashlib.sha256(body.encode("utf-8")).hexdigest() if body
                  else EMPTY_BODY_SHA256)
        # METHOD \n body-hash \n signature-headers \n url. The third line is
        # empty because no headers are included in the signature.
        to_sign = f"{method.upper()}\n{digest}\n\n{url_for_sign}"
        message = self.access_id + (token or "") + t + nonce + to_sign
        signature = hmac.new(
            self.access_secret, message.encode("utf-8"), hashlib.sha256
        ).hexdigest().upper()
        headers = {
            "client_id": self.access_id,
            "sign": signature,
            "t": t,
            "nonce": nonce,
            "sign_method": "HMAC-SHA256",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if token:
            headers["access_token"] = token
        return headers

    # -- transport ---------------------------------------------------------

    def _call(
        self,
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        body: Any = None,
        authed: bool = True,
    ) -> Any:
        url_for_sign = self._canonical(method, path, query)
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        token = self.token() if authed else None
        headers = self._sign(method, url_for_sign, payload, token)
        if payload:
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(
            self.base + url_for_sign,
            data=payload.encode("utf-8") if payload else None,
            headers=headers,
            method=method.upper(),
        )

        last_err: Exception | None = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    parsed = json.loads(resp.read().decode("utf-8", "replace"))
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise TuyaError(
                        f"Tuya rejected the request (HTTP {exc.code}) for {path}. "
                        "Check TUYA_ACCESS_ID / TUYA_ACCESS_SECRET and that the "
                        "cloud project's data centre matches config.json > plugs > region."
                    ) from exc
                last_err = exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_err = exc
            time.sleep(2 ** attempt)
        else:
            raise TuyaError(f"Tuya request failed for {path}: {last_err}")

        if not isinstance(parsed, dict):
            raise TuyaError(f"Unexpected Tuya payload for {path}: {parsed!r}")
        if not parsed.get("success", False):
            code = parsed.get("code")
            msg = parsed.get("msg") or "no message"
            hint = ""
            if code in (1106, 1114, 28841002, 28841105):
                # The ones that actually happen: an unsubscribed or lapsed IoT
                # Core trial, or plugs never linked to the cloud project.
                hint = (" This usually means the project's IoT Core subscription has "
                        "lapsed, or the app account is no longer linked to it. "
                        "Check Cloud > Cloud Services > IoT Core on iot.tuya.com.")
            raise TuyaError(f"Tuya error {code} for {path}: {msg}.{hint}")
        return parsed.get("result")

    def token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        result = self._call(
            "GET", "/v1.0/token", {"grant_type": "1"}, authed=False
        ) or {}
        tok = result.get("access_token")
        if not tok:
            raise TuyaError(f"Tuya returned no access token: {result!r}")
        self._token = str(tok)
        self._token_expires = time.time() + float(result.get("expire_time") or 7200)
        return self._token

    # -- endpoints ---------------------------------------------------------

    def status(self, device_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Current data points for each device, as {device_id: {code: value}}.

        Tries the batch endpoint first, because one call for six devices beats
        six calls, and falls back to per-device requests if the account's API
        version does not serve it.
        """
        ids = [str(i) for i in device_ids if i]
        if not ids:
            return {}
        try:
            result = self._call(
                "GET", "/v1.0/iot-03/devices/status", {"device_ids": ",".join(ids)}
            )
            out: dict[str, dict[str, Any]] = {}
            for entry in result or []:
                did = str(entry.get("id"))
                out[did] = {str(s.get("code")): s.get("value")
                            for s in (entry.get("status") or [])}
            if out:
                return out
        except TuyaError:
            pass

        out = {}
        for did in ids:
            try:
                result = self._call("GET", f"/v1.0/devices/{did}/status")
            except TuyaError:
                continue
            out[did] = {str(s.get("code")): s.get("value") for s in (result or [])}
        return out

    def info(self, device_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Name and online flag per device.

        Kept separate from status because "the plug says it is on" and "the
        plug has been unreachable since Tuesday" are different facts, and only
        one of them is in the data points.
        """
        ids = [str(i) for i in device_ids if i]
        out: dict[str, dict[str, Any]] = {}
        for did in ids:
            try:
                result = self._call("GET", f"/v1.0/devices/{did}") or {}
            except TuyaError:
                continue
            out[did] = {
                "name": result.get("name"),
                "online": 1 if result.get("online") else 0,
                "product_name": result.get("product_name"),
                "update_time": _as_int(result.get("update_time")),
            }
        return out

    def all_devices(self, limit: int = 200) -> list[dict[str, Any]]:
        """Every device the linked app account can see.

        Needed because a Tuya device that is factory reset comes back with a
        brand new device ID. Re-joining a plug or sensor to a different Wi-Fi
        network is enough to do it, at which point the ID written in
        config.json refers to something that no longer exists and the harvester
        is politely asking about a ghost. This lists what is actually there so
        the new ID can be read off rather than hunted for.
        """
        found: list[dict[str, Any]] = []
        cursor = ""
        while len(found) < limit:
            params = {"page_size": "100"}
            if cursor:
                params["last_row_key"] = cursor
            result = self._call(
                "GET", "/v1.0/iot-01/associated-users/devices", params
            ) or {}
            batch = result.get("devices") or []
            if not batch:
                break
            for d in batch:
                found.append({
                    "id": str(d.get("id")),
                    "name": d.get("name"),
                    "product_name": d.get("product_name"),
                    "online": bool(d.get("online")),
                    "update_time": _as_int(d.get("update_time")),
                    "active_time": _as_int(d.get("active_time")),
                })
            if not result.get("has_more"):
                break
            cursor = result.get("last_row_key") or ""
            if not cursor:
                break
        return found


# -- polling -------------------------------------------------------------------


def poll(client: TuyaClient, config: dict[str, Any], now: int | None = None) -> Poll:
    """One pass over every configured Tuya device."""
    now = int(now if now is not None else time.time())
    cfg = (config.get("plugs") or {})
    devices_cfg = {k: v for k, v in (cfg.get("devices") or {}).items()
                   if not k.startswith("_")}
    if not devices_cfg:
        return Poll()

    ids = list(devices_cfg)
    status = client.status(ids)
    info = client.info(ids)

    out = Poll(raw=status)

    for did, dcfg in devices_cfg.items():
        codes = status.get(did) or {}
        meta = info.get(did) or {}
        online = meta.get("online")
        if online is None:
            online = 1 if codes else 0
        if not codes:
            out.offline.append(did)
            online = 0

        # Tuya's `update_time` means different things on different devices, and
        # reading it as a heartbeat was wrong. On a sensor it is the last
        # measurement, which is what we want. On a switch it is the last time
        # the device record changed, so a chiller nobody has touched for a
        # fortnight reports a fortnight-old timestamp while sitting there
        # working perfectly. Taking that as "last heard from" made every plug
        # look dead. For a switch, the moment we asked and got an answer is the
        # honest freshness, and whether the plug is reachable is what `online`
        # is for.
        kind = str(dcfg.get("kind") or "switch").lower()
        reported = _as_int(meta.get("update_time"))
        sane = bool(reported and now - 365 * 86400 <= reported <= now + 300)

        if kind == "ambient":
            # This was wrong and the sensor's own history caught it. Using
            # update_time as the reading time meant every poll that found the
            # same value carried the same timestamp, the insert deduplicated it
            # away, and a fortnight of monitoring produced two stored readings.
            # The air was being measured every half hour and almost all of it
            # was thrown out.
            #
            # A poll is a reading: the air was that temperature when we asked,
            # whether or not the number had moved since last time. update_time
            # is kept alongside, where it answers the question it is actually
            # good for, which is how long the sensor has been saying the same
            # thing.
            seen = now
            contact = reported if (sane and reported <= now + 300) else now
        else:
            # A switch is polled, not pushed: the moment we asked and got an
            # answer is when the reading is from. Its update_time is only
            # meaningful once the plug has gone offline, when it says when
            # contact was lost.
            seen = now
            contact = reported if (online == 0 and sane) else now

        if kind == "ambient":
            scales = dcfg.get("scales") or {}
            values: dict[str, float | None] = {}
            for field_name, (candidates, default_div) in AMBIENT_CODES.items():
                raw = _first(codes, candidates)
                div = float(scales.get(field_name, default_div) or 1.0)
                values[field_name] = None if raw is None else _as_float(raw, div)
            out.ambient.append(
                AmbientReading(
                    device_id=did,
                    reading_time=seen,
                    reported_at=contact,
                    air_temperature=values.get("air_temperature"),
                    humidity=values.get("humidity"),
                    battery=values.get("battery"),
                    online=online,
                )
            )
            continue

        sockets_cfg = {k: v for k, v in (dcfg.get("sockets") or {}).items()
                       if not k.startswith("_")}
        if not sockets_cfg:
            # No mapping written down yet: report whatever switches the plug
            # has, unassigned, so they show up in the probe rather than
            # vanishing silently.
            sockets_cfg = {code: {} for code in SWITCH_CODES if code in codes}

        power = _first(codes, POWER_CODES["power_w"][0])
        plug_power = None if power is None else _as_float(power, POWER_CODES["power_w"][1])

        # The meter reading goes into its own series as well as onto the socket
        # states. Energy is the integral of power over time, and plug_states
        # overwrites its power column on every heartbeat, so the state table
        # can answer "what is it drawing now" but never "what has it used".
        volts = _first(codes, POWER_CODES["voltage_v"][0])
        amps = _first(codes, POWER_CODES["current_ma"][0])
        out.power.append(
            PowerSample(
                device_id=did,
                reading_time=now,
                power_w=plug_power,
                voltage_v=None if volts is None else _as_float(volts, POWER_CODES["voltage_v"][1]),
                current_ma=None if amps is None else _as_float(amps, POWER_CODES["current_ma"][1]),
                add_ele=_as_float(codes.get("add_ele")),
                online=online,
            )
        )

        def switch_value(code: str) -> Any:
            raw = codes.get(code)
            if raw is None and code == "switch_1":
                raw = codes.get("switch")
            return raw

        def is_on(raw: Any) -> int | None:
            return None if raw is None else (1 if raw in (True, 1, "true", "1") else 0)

        # The meter is per plug, not per socket. It can only be attributed when
        # exactly one of the plug's sockets is drawing: with both chillers
        # running, 857 W is the pair, and splitting it in half would be an
        # invention. The plug total is carried either way, which is still worth
        # having: a chiller reporting "on" at zero watts is a failed compressor
        # or a tripped plug, and the switch state alone would never show it.
        on_now = [c for c in sockets_cfg if is_on(switch_value(c)) == 1]

        for code, scfg in sockets_cfg.items():
            on_state = is_on(switch_value(code))
            out.sockets.append(
                SocketState(
                    device_id=did,
                    socket=code,
                    reading_time=seen,
                    sump_code=scfg.get("sump"),
                    role=scfg.get("role") or dcfg.get("role"),
                    label=scfg.get("label") or dcfg.get("label") or meta.get("name"),
                    last_contact=contact,
                    on_state=on_state,
                    online=online,
                    power_w=(plug_power if (len(on_now) == 1 and on_now[0] == code)
                             else None),
                    plug_power_w=plug_power,
                )
            )

    return out


def load(store, config: dict[str, Any], env: dict[str, str] | None = None) -> dict[str, int]:
    """Poll Tuya and store what came back. Returns a small summary.

    Raises TuyaError on a failure the caller should report; the harvester
    treats that as non-fatal, because a plug reading going missing must never
    stop the water readings being collected.
    """
    import os

    env = env if env is not None else dict(os.environ)
    cfg = (config.get("plugs") or {})
    if not cfg.get("enabled", False):
        return {"sockets": 0, "ambient": 0, "skipped": 1}

    client = TuyaClient(
        access_id=env.get("TUYA_ACCESS_ID", ""),
        access_secret=env.get("TUYA_ACCESS_SECRET", ""),
        region=cfg.get("region", "eu"),
    )
    result = poll(client, config)
    sockets = store.insert_plug_states(s.as_row() for s in result.sockets)
    ambient = store.insert_ambient(a.as_row() for a in result.ambient)
    meter = store.insert_plug_power(p.as_row() for p in result.power)
    summary = {
        "sockets": sockets,
        "ambient": ambient,
        "meter": meter,
        "polled": len(result.sockets) + len(result.ambient),
        "offline": len(result.offline),
        "subscription_days": subscription_days(cfg),
    }
    summary.update(reconcile(client, config, result))
    return summary


def reconcile(client: TuyaClient, config: dict[str, Any],
              result: "Poll") -> dict[str, Any]:
    """Work out whether a silent device is unreachable or simply gone.

    A factory reset gives a Tuya device a new ID, and rejoining one to a
    different Wi-Fi network is enough to cause it. The old ID then refers to
    nothing, the harvester asks about a device that does not exist, and the
    readings stop with no error anywhere. That is the worst kind of failure:
    everything claims to be fine.

    The two cases are easy to tell apart once you look. A plug that is merely
    off the network is still registered and still answers with its last known
    data points, as Rows C and E do. A device that has been reset returns
    nothing AND is absent from the account's own device list. Only the second
    needs a new ID written down.

    The account listing costs an extra request, so it is only fetched when
    something has actually gone quiet.
    """
    if not result.offline:
        return {"gone": [], "candidates": []}

    cfg = config.get("plugs") or {}
    configured: set[str] = set()
    for did, dcfg in (cfg.get("devices") or {}).items():
        if did.startswith("_"):
            continue
        configured.add(did)
        configured.update(str(x) for x in (dcfg.get("previous_ids") or []))

    try:
        account = client.all_devices()
    except TuyaError:
        # Not knowing is not the same as nothing being wrong, so the silent
        # devices are still reported, just without the diagnosis.
        return {"gone": [], "candidates": [], "unchecked": list(result.offline)}

    present = {d["id"] for d in account}
    gone = [did for did in result.offline if did not in present]
    candidates = [d for d in account if d["id"] not in configured]
    return {"gone": gone, "candidates": candidates}


def subscription_days(cfg: dict[str, Any], now: float | None = None) -> int | None:
    """Days until the Tuya IoT Core subscription lapses, if a date is recorded.

    When it does lapse the API stops answering and the plug column freezes
    where it stands, so this is worth shouting about a fortnight early rather
    than discovering it from a chiller that has been "on" since October.
    """
    import datetime

    raw = cfg.get("subscription_expires")
    if not raw:
        return None
    try:
        day = datetime.datetime.strptime(str(raw).strip()[:10], "%Y-%m-%d")
    except ValueError:
        return None
    expires = day.replace(tzinfo=datetime.timezone.utc).timestamp()
    return int((expires - (now if now is not None else time.time())) // 86400)


def inventory(client: TuyaClient, config: dict[str, Any]) -> str:
    """Every device on the account, flagged against what config.json expects.

    The two lines worth reading are "not in config.json", which is where a
    reset device's new ID turns up, and "configured but not on the account",
    which is the old ID it replaced.
    """
    cfg = config.get("plugs") or {}
    known: dict[str, str] = {}
    for did, dcfg in (cfg.get("devices") or {}).items():
        if did.startswith("_"):
            continue
        known[did] = dcfg.get("label") or did
        for old in dcfg.get("previous_ids") or []:
            known[str(old)] = (dcfg.get("label") or did) + " (previous ID)"

    devices = client.all_devices()
    lines = [f"{len(devices)} device(s) on the account", ""]
    unknown = []
    for d in sorted(devices, key=lambda x: (x.get("name") or "").lower()):
        mark = "  " if d["id"] in known else "NEW"
        note = known.get(d["id"], "not in config.json")
        when = ("  last update " + time.strftime(
            "%Y-%m-%d %H:%M UTC", time.gmtime(d["update_time"]))
            if d.get("update_time") else "")
        lines.append(f"{mark} {d['id']}  {d.get('name') or '(no name)'}")
        lines.append(f"      {d.get('product_name') or 'unknown product'}, "
                     f"{'online' if d['online'] else 'OFFLINE'}{when}")
        lines.append(f"      {note}")
        if d["id"] not in known:
            unknown.append(d)

    missing = [k for k in known if k not in {d["id"] for d in devices}]
    if missing:
        lines.append("")
        lines.append("Configured but NOT on the account any more:")
        for k in missing:
            lines.append(f"  {k}  {known[k]}")
        lines.append("")
        lines.append("A device that was factory reset comes back with a new ID, "
                     "so one of the NEW entries above is almost certainly the "
                     "same piece of hardware. Put the new ID in config.json and "
                     "move the old one into that device's 'previous_ids' list, "
                     "which keeps its history on the same chart.")
    return "\n".join(lines)


def describe(client: TuyaClient, device_ids: Iterable[str]) -> str:
    """Human-readable dump of every data point each device reports.

    This exists so the socket-to-sump mapping can be written from what the
    plugs actually say rather than from a guess about naming. Run it once,
    read the output, fill in config.json.
    """
    ids = [str(i) for i in device_ids if i]
    status = client.status(ids)
    info = client.info(ids)
    lines: list[str] = []
    for did in ids:
        meta = info.get(did) or {}
        codes = status.get(did) or {}
        lines.append("")
        lines.append(f"{did}  {meta.get('name') or '(no name)'}")
        lines.append(f"  product: {meta.get('product_name') or 'unknown'}")
        lines.append(f"  online:  {'yes' if meta.get('online') else 'no'}")
        if meta.get("update_time"):
            lines.append("  last update: " + time.strftime(
                "%Y-%m-%d %H:%M:%S UTC", time.gmtime(meta["update_time"])))
        if not codes:
            lines.append("  no data points returned")
            continue
        lines.append("  data points:")
        for code in sorted(codes):
            lines.append(f"    {code:<24} {codes[code]!r}")
        switches = [c for c in codes if c in SWITCH_CODES]
        if switches:
            lines.append("  switch codes to map in config.json > plugs > devices > "
                         f"{did} > sockets: " + ", ".join(sorted(switches)))
    return "\n".join(lines)


# -- helpers -------------------------------------------------------------------


def _first(codes: dict[str, Any], candidates: Iterable[str]) -> Any:
    for name in candidates:
        if name in codes and codes[name] is not None:
            return codes[name]
    return None


def _as_float(value: Any, divisor: float = 1.0) -> float | None:
    try:
        return round(float(value) / (divisor or 1.0), 4)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None

"""Read the nursery in-situ sampling sheet and load it into the database.

These are hand measurements taken at the sumps with handheld meters and test
kits, on a different instrument set from the Seneye probes and with its own
accuracy characteristics. They are stored and displayed as a separate method,
never merged into the sensor series: where the two disagree, that is two
instruments measuring, not an error to reconcile.

Three sources, same parser, tried in this order:

* the .xlsx itself in Drive, downloaded over HTTPS on every run (nutrients.
  workbook_url). This is the file the tablet writes to, so it is the one route
  that cannot go stale behind a copy.
* a published Google Sheet (nutrients.sheet_url), fetched as CSV
* an .xlsx committed to the repo at data/nutrients.xlsx (the offline fallback)

All three are read with the standard library alone. The .xlsx is parsed
straight out of its OOXML, so nothing has to be installed.

The order exists because of a failure that cost a fortnight of readings. The
sheet route reads a native Google Sheet, which an uploaded .xlsx cannot be, so
for a while the live sheet was a duplicate of the workbook. Rows typed on the
tablet went into the workbook, the harvester read the duplicate, and the
dashboard quietly showed the state of the sampling programme as of the last
time somebody remembered to copy one file into the other. Reading the workbook
directly removes the copy, and with it the chance of that happening again.

The parser handles the shape the TNP sheet is actually in: the date written
once at the top of each sampling block and blank on the rows below it, dates
stored both as real dates and as text like "21/9/26", "N/A" for parameters not
measured that round, blank separator rows between blocks, and columns located
by their headings rather than by position.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from typing import Any

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

# Columns are located by their heading, not their position, so inserting a
# column in the sheet does not silently shift every value one field across.
# Order matters: the first pattern that matches a heading wins, so "phosphate"
# is tested before the bare "ph".
HEADER_PATTERNS = [
    ("date", (r"^date$",)),
    ("time", (r"^time$",)),
    ("tank", (r"^tank", r"^sump", r"^system")),
    ("temp_c", (r"^temp",)),
    ("salinity_ppt", (r"^salinity", r"^sal$")),
    ("po4", (r"^po4", r"^phosphate")),
    ("no3", (r"^no3", r"^nitrate")),
    ("no2", (r"^no2", r"^nitrite")),
    ("nh3", (r"^nh3", r"^ammonia")),
    ("dkh", (r"^dkh$", r"^kh$", r"^alk", r"^carbonate")),
    ("ca", (r"^ca2?$", r"^calcium")),
    ("mg", (r"^mg2?$", r"^magnesium")),
    ("ph", (r"^ph$",)),
    ("observer", (r"^observer", r"^recorder", r"^who")),
    ("notes", (r"^notes?$", r"^comment")),
]

NUMERIC_FIELDS = (
    "temp_c", "salinity_ppt", "ph", "dkh", "no3", "no2", "nh3", "po4", "ca", "mg",
)

# What each analyte is called, what it is measured in, and which family it
# belongs to. Physical properties describe the state of the water; chemical
# ones are what is dissolved in it. The dashboard groups the two.
ANALYTES = [
    # physical
    ("temp_c", "Temperature", "\u00b0C", 1, "physical"),
    ("salinity_ppt", "Salinity", "ppt", 2, "physical"),
    ("ph", "pH", "", 2, "physical"),
    ("dkh", "Carbonate hardness", "\u00b0dKH", 1, "physical"),
    # chemical
    # Units are as the kits report them. In seawater 1 ppm is about 1.03 mg/L
    # (a litre weighs ~1.026 kg), so the reference ranges below are the same
    # numbers either way and no conversion is applied.
    ("no3", "Nitrate (NO\u2083\u207b)", "mg/L", 2, "chemical"),
    ("no2", "Nitrite (NO\u2082\u207b)", "mg/L", 3, "chemical"),
    ("nh3", "Ammonia (NH\u2083)", "mg/L", 3, "chemical"),
    # Read in parts per billion, not ppm: at 4 to 10 ppb the readings are
    # ordinary Strait surface water, whereas the same figures in ppm would be
    # two orders of magnitude above anything seawater does.
    ("po4", "Phosphate (PO\u2084\u00b3\u207b)", "ppb", 1, "chemical"),
    ("ca", "Calcium (Ca\u00b2\u207a)", "ppm", 0, "chemical"),
    ("mg", "Magnesium (Mg\u00b2\u207a)", "ppm", 0, "chemical"),
]

ANALYTE_GROUPS = [
    ("physical", "Physical"),
    ("chemical", "Chemical"),
]


class NutrientError(RuntimeError):
    pass


# -- workbook reading ------------------------------------------------------


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    out = []
    for si in root.findall("m:si", NS):
        out.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
    return out


def _sheet_path(zf: zipfile.ZipFile, wanted: str | None) -> str:
    """Find the worksheet part for the named sheet, or the first sheet."""
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rel_target = {
        r.get("Id"): r.get("Target")
        for r in rels
    }
    sheets = workbook.findall(".//m:sheet", NS)
    chosen = None
    for sheet in sheets:
        name = (sheet.get("name") or "").strip().lower()
        if wanted and name == wanted.strip().lower():
            chosen = sheet
            break
    if chosen is None:
        chosen = sheets[0] if sheets else None
    if chosen is None:
        raise NutrientError("workbook contains no worksheets")
    rid = chosen.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
    target = rel_target.get(rid, "worksheets/sheet1.xml")
    return "xl/" + target.lstrip("/")


def read_workbook_rows(source: Any, sheet_name: str | None = "Nutrient") -> list[dict[str, str]]:
    """Return the worksheet as a list of {column letter: cell text} rows.

    `source` is a path on disk or the bytes of an .xlsx. Taking bytes lets the
    workbook be read straight out of a Drive download without ever touching
    the filesystem, which matters on the harvest runner where the checkout is
    thrown away afterwards.
    """
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(bytes(source))
    with zipfile.ZipFile(source) as zf:
        strings = _shared_strings(zf)
        sheet = ET.fromstring(zf.read(_sheet_path(zf, sheet_name)))

    rows = []
    for row in sheet.findall(".//m:row", NS):
        cells: dict[str, str] = {}
        for cell in row.findall("m:c", NS):
            ref = cell.get("r") or ""
            column = "".join(ch for ch in ref if ch.isalpha())
            kind = cell.get("t")
            if kind == "inlineStr":
                node = cell.find("m:is", NS)
                text = "".join(t.text or "" for t in node.iter(f"{{{NS['m']}}}t")) if node is not None else ""
            else:
                node = cell.find("m:v", NS)
                if node is None:
                    continue
                text = node.text or ""
                if kind == "s":
                    index = int(text)
                    text = strings[index] if index < len(strings) else ""
            text = text.strip()
            if text:
                cells[column] = text
        if cells:
            rows.append(cells)
    return rows


# -- value coercion --------------------------------------------------------


def _number(value: str | None) -> float | None:
    """Worksheet cell to float. 'N/A', blanks and text become None.

    A value written as '<0.02' or '0.5 (est)' keeps its number, because a
    detection-limit reading is still information; the raw text is preserved in
    the notes column when the sheet has one.
    """
    if value is None:
        return None
    text = value.strip()
    if not text or text.upper() in {"N/A", "NA", "-", "--"}:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _excel_date(value: str) -> dt.date | None:
    """Excel serial or a typed date string to a date.

    The workbook has both: most blocks carry a serial number, but a date typed
    as '21/9/26' arrives as text. Day-first is assumed, which is how the sheet
    is written.
    """
    text = value.strip()
    if not text:
        return None
    try:
        serial = float(text)
        # Excel's epoch, including its deliberate 1900 leap-year bug
        return (dt.datetime(1899, 12, 30) + dt.timedelta(days=serial)).date()
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _excel_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        fraction = float(value)
    except ValueError:
        # already text: a CSV export gives "12:38:00", keep it as HH:MM
        text = value.strip()
        match = re.match(r"^(\d{1,2}):(\d{2})", text)
        return f"{int(match.group(1)):02d}:{match.group(2)}" if match else (text or None)
    seconds = int(round(fraction * 86400)) % 86400
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"


def sump_code(tank_id: str, sumps: dict[str, Any] | None = None) -> str | None:
    """'A12' -> 'SA12'. Returns None for anything not a known sump."""
    text = (tank_id or "").strip().upper().replace(" ", "")
    if not text or text in {"TANK ID", "TANKID"}:
        return None
    code = text if text.startswith("S") else "S" + text
    if sumps and code not in sumps:
        return None
    return code


# -- column matching -------------------------------------------------------


def normalise_heading(text: str) -> str:
    """Strip units, subscripts and punctuation so headings can be matched.

    'NO\u2083 (mg/L)' -> 'no3';  'Mg2+ (mg/L)' -> 'mg2';  'dKH (\u00b0dKH)' -> 'dkh'
    """
    text = re.sub(r"\([^)]*\)", " ", text or "")          # drop bracketed units
    subscripts = str.maketrans("\u2080\u2081\u2082\u2083\u2084\u2085\u2086\u2087\u2088\u2089", "0123456789")
    text = text.translate(subscripts)
    text = text.replace("\u207a", "").replace("\u207b", "")  # superscript plus/minus
    text = re.sub(r"[^a-z0-9]", "", text.lower())
    return text


def map_headings(headings: dict[str, str]) -> dict[str, str]:
    """{column key: heading text} -> {field: column key}.

    A heading that matches nothing is ignored rather than guessed at.
    """
    mapping: dict[str, str] = {}
    for key, heading in headings.items():
        clean = normalise_heading(heading)
        if not clean:
            continue
        for field, patterns in HEADER_PATTERNS:
            if field in mapping:
                continue
            if any(re.match(pattern, clean) for pattern in patterns):
                mapping[field] = key
                break
    return mapping


def _find_header_row(rows: list[dict[str, str]]) -> tuple[int, dict[str, str]]:
    """Locate the header row and its field mapping.

    The sheet has a blank first column and sometimes a title row above the
    headings, so the header is found by content: the first row that yields both
    a tank column and at least two measured fields.
    """
    for index, row in enumerate(rows[:10]):
        mapping = map_headings(row)
        measured = [f for f in mapping if f in NUMERIC_FIELDS]
        if "tank" in mapping and len(measured) >= 2:
            return index, mapping
    raise NutrientError(
        "could not find the header row; expected a 'Tank ID' column alongside "
        "columns such as Temp, Salinity, pH, NO3"
    )


# -- parsing ---------------------------------------------------------------


def build_records(
    rows: list[dict[str, str]],
    sumps: dict[str, Any] | None = None,
    date_is_serial: bool = True,
) -> list[dict[str, Any]]:
    """Turn raw spreadsheet rows into one record per sample.

    The date cell is filled in only on the first row of each sampling block, so
    it is carried down until the next one appears. The same applies to the time
    and the observer, which are recorded once per round.
    """
    header_index, columns = _find_header_row(rows)
    current_date: dt.date | None = None
    current_time: str | None = None
    current_observer: str | None = None
    records: list[dict[str, Any]] = []
    skipped: list[str] = []

    def cell(row: dict[str, str], field: str) -> str | None:
        key = columns.get(field)
        return row.get(key) if key else None

    for row in rows[header_index + 1:]:
        raw_date = cell(row, "date")
        if raw_date:
            parsed = _excel_date(raw_date)
            if parsed:
                current_date = parsed
                current_time = _excel_time(cell(row, "time"))
                current_observer = cell(row, "observer")
        else:
            raw_time = cell(row, "time")
            if raw_time:
                current_time = _excel_time(raw_time)
        observer = cell(row, "observer")
        if observer:
            current_observer = observer

        sump = sump_code(cell(row, "tank") or "", sumps)
        if sump is None:
            if cell(row, "tank"):
                skipped.append(str(cell(row, "tank")))
            continue
        if current_date is None:
            skipped.append(f"{cell(row, 'tank')} (no date above it)")
            continue

        record: dict[str, Any] = {
            "sample_date": current_date.isoformat(),
            "sample_time": current_time,
            "sump_code": sump,
            "observer": current_observer,
            "notes": cell(row, "notes"),
        }
        measured = False
        for field in NUMERIC_FIELDS:
            value = _number(cell(row, field))
            record[field] = value
            if value is not None:
                measured = True
        if measured:
            records.append(record)

    if skipped:
        print(f"nutrients: skipped {len(skipped)} row(s): {', '.join(skipped[:6])}")
    return records


def parse_workbook(source: Any, sumps: dict[str, Any] | None = None,
                   sheet_name: str | None = "Nutrient") -> list[dict[str, Any]]:
    """Read an .xlsx, given as a path or as bytes, into sample records."""
    return build_records(read_workbook_rows(source, sheet_name), sumps)


def parse_csv_rows(text: str) -> list[dict[str, str]]:
    """CSV text into positional rows, the same shape read_workbook_rows gives."""
    reader = csv.reader(io.StringIO(text))
    rows: list[dict[str, str]] = []
    for values in reader:
        row = {}
        for index, value in enumerate(values):
            value = (value or "").strip()
            if value:
                row[str(index)] = value
        if row:
            rows.append(row)
    return rows


def parse_csv(text: str, sumps: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Read a CSV export of the sheet into sample records.

    Google serves dates already formatted (13/08/2026), so no serial-number
    conversion is involved on this path; _excel_date handles both anyway.
    """
    rows = parse_csv_rows(text)
    if not rows:
        raise NutrientError("the sheet export was empty")
    return build_records(rows, sumps)


# -- fetching --------------------------------------------------------------


def fetch_sheet_csv(url: str, timeout: int = 30) -> str:
    """Download the sheet as CSV.

    Accepts either a 'Publish to web' CSV link or an ordinary sheet URL, which
    is rewritten to its CSV export endpoint. Google answers both with a
    redirect chain, which urllib follows.
    """
    url = sheet_csv_url(url)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "tnp-seneye-harvester/1.0",
            "Accept": "text/csv, */*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            final = response.geturl()
    except urllib.error.HTTPError as exc:
        hint = ""
        if exc.code == 400:
            hint = (
                " A 400 usually means the file is an uploaded .xlsx rather than "
                "a native Google Sheet (its URL carries rtpof=true). Open it and "
                "use File > Save as Google Sheets, then publish that copy and put "
                "its URL in config.json."
            )
        elif exc.code in (401, 403, 404):
            hint = (
                " Check the tab is published: File > Share > Publish to web, pick "
                "the tab, choose Comma-separated values (.csv), Publish."
            )
        raise NutrientError(
            f"Google returned HTTP {exc.code} for the sheet.{hint}"
        ) from exc
    except urllib.error.URLError as exc:
        raise NutrientError(f"could not reach Google Sheets: {exc}") from exc

    text = payload.decode("utf-8-sig", "replace")
    # A sign-in page is HTML, not CSV, and would otherwise parse as nonsense.
    if "<html" in text[:400].lower() or "accounts.google.com" in final:
        raise NutrientError(
            "the sheet returned a sign-in page rather than CSV, so it is not "
            "readable without credentials. Publish it to the web "
            "(File > Share > Publish to web > Comma-separated values)."
        )
    return text


def drive_download_url(url: str) -> str:
    """Normalise any Drive link to one that returns the file's own bytes.

    Accepts the thing you actually get from Drive's Share button
    (/file/d/<id>/view?usp=sharing), an /open?id= link, a bare file ID, or a
    link that is already a download URL.
    """
    url = url.strip()
    if not url:
        raise NutrientError("nutrients.workbook_url is empty")
    if "export=download" in url or "/uc?" in url:
        return url
    return f"https://drive.google.com/uc?export=download&id={drive_file_id(url)}"


def drive_file_id(url: str) -> str:
    """Pull the file ID out of a Drive or Sheets link.

    The ouid parameter on a share link is the trap here: a pattern looking for
    `id=` finds `ouid=118023771820086390278` and hands back an account number
    as a file ID. The `[?&]id=` anchor is what keeps that from happening.
    """
    url = url.strip()
    match = (re.search(r"/file/d/([A-Za-z0-9_-]{20,})", url)
             or re.search(r"[?&]id=([A-Za-z0-9_-]{20,})", url)
             or re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})", url)
             or re.fullmatch(r"([A-Za-z0-9_-]{20,})", url))
    if not match:
        raise NutrientError(
            f"could not find a Drive file ID in {url!r}. Use the link from "
            "Drive's Share button, which looks like "
            "https://drive.google.com/file/d/<long id>/view?usp=sharing"
        )
    return match.group(1)


def drive_candidates(url: str) -> list[str]:
    """Every endpoint worth trying for one Drive file, in order.

    Two of them, because Google has moved this twice. /uc is the old route and
    still redirects; drive.usercontent.google.com is where it redirects to and
    is the one that works when /uc answers with an interstitial instead of the
    file. Trying both costs one extra request on a day when the first fails,
    and saves a week of the sampling sheet quietly not arriving.
    """
    url = url.strip()
    if "export=download" in url or "/uc?" in url:
        return [url]
    fid = drive_file_id(url)
    return [
        f"https://drive.google.com/uc?export=download&id={fid}",
        f"https://drive.usercontent.google.com/download?id={fid}&export=download",
    ]


def fetch_workbook(url: str, timeout: int = 30) -> bytes:
    """Download the .xlsx from Drive and return its bytes.

    The checks here are all about failing with the reason rather than with a
    parse error forty lines later. An unshared file answers with an HTML
    permission page, and a sign-in redirect answers with an HTML login page;
    both are valid HTTP 200s that are not a workbook. A real .xlsx is a zip,
    so its first two bytes are PK.
    """
    reasons: list[str] = []
    for target in drive_candidates(url):
        request = urllib.request.Request(
            target,
            headers={
                "User-Agent": "tnp-seneye-harvester/1.0",
                "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, */*",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            reasons.append(f"HTTP {exc.code}")
            continue
        except urllib.error.URLError as exc:
            reasons.append(str(exc))
            continue

        if payload[:2] == b"PK":
            return payload

        head = payload[:600].decode("utf-8", "replace").lower()
        if "accounts.google.com" in head or "sign in" in head:
            reasons.append("a sign-in page")
        elif "<html" in head:
            reasons.append("a web page, not the file")
        else:
            reasons.append(f"{len(payload)} bytes starting {payload[:8]!r}")

    told = "; ".join(f"{t.split('//')[1].split('/')[0]} answered {r}"
                     for t, r in zip(drive_candidates(url), reasons))
    raise NutrientError(
        f"the workbook could not be downloaded ({told}). The usual cause is "
        "sharing: open the file in Drive, choose Share, and set General "
        "access to 'Anyone with the link' as Viewer. The link itself can stay "
        "exactly as it is."
    )


def sheet_csv_url(url: str) -> str:
    """Normalise a Google Sheets URL to something that returns CSV."""
    url = url.strip()
    if "output=csv" in url or "format=csv" in url:
        return url
    match = re.search(r"/spreadsheets/d/(?:e/)?([A-Za-z0-9_-]+)", url)
    if not match:
        raise NutrientError(f"not a Google Sheets URL: {url}")
    key = match.group(1)
    gid_match = re.search(r"[#&?]gid=(\d+)", url)
    gid = gid_match.group(1) if gid_match else "0"
    if "/spreadsheets/d/e/" in url:  # already a published link
        return f"https://docs.google.com/spreadsheets/d/e/{key}/pub?gid={gid}&single=true&output=csv"
    return f"https://docs.google.com/spreadsheets/d/{key}/export?format=csv&gid={gid}"


# -- loading ---------------------------------------------------------------


def load(store, config: dict[str, Any], root: str) -> int:
    """Load in-situ samples, preferring the live sheet over the local copy.

    Order of preference:

    1. the .xlsx in Drive, if nutrients.workbook_url is set. This is the file
       the tablet writes to, so it is the only source that is the data rather
       than a copy of it.
    2. the published Google Sheet, if a URL is configured
    3. the .xlsx committed to the repo

    A failed fetch is never fatal and never destructive: the samples already in
    the database stay exactly as they are, so a Google outage or a sheet that
    gets unpublished shows up as a stale date on the dashboard rather than an
    empty table. Every successful fetch is also written to disk as a CSV, which
    gives the repo a dated copy of what the sheet said at the time.
    """
    nutrient_cfg = config.get("nutrients", {}) or {}
    sumps = config.get("sumps")
    records: list[dict[str, Any]] = []
    source = None

    workbook_url = nutrient_cfg.get("workbook_url")
    if workbook_url:
        try:
            records = parse_workbook(
                fetch_workbook(workbook_url), sumps,
                nutrient_cfg.get("sheet", "Nutrient"),
            )
            source = "Drive workbook"
        except NutrientError as exc:
            print(f"nutrients: workbook download failed ({exc})")
        except Exception as exc:
            print(f"nutrients: the downloaded workbook could not be read ({exc})")

    url = nutrient_cfg.get("sheet_url")
    if not records and url:
        try:
            text = fetch_sheet_csv(url)
            records = parse_csv(text, sumps)
            source = "google sheet"
            cache = nutrient_cfg.get("cache", "data/nutrients_latest.csv")
            if cache:
                path = os.path.join(root, cache)
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(text)
        except NutrientError as exc:
            print(f"nutrients: sheet fetch failed ({exc})")

    if not records:
        workbook = os.path.join(root, nutrient_cfg.get("workbook", "data/nutrients.xlsx"))
        if os.path.exists(workbook):
            records = parse_workbook(workbook, sumps, nutrient_cfg.get("sheet", "Nutrient"))
            source = "workbook"
        elif url or workbook_url:
            # Be precise about which route failed: a fetch that errored is a
            # different problem from never having been set up.
            print(
                "nutrients: no configured source could be read and there is no "
                f"{nutrient_cfg.get('workbook', 'data/nutrients.xlsx')} to fall "
                "back on, so stored samples are left as they are"
            )
            return 0
        else:
            print("nutrients: no source configured and no workbook on disk, skipping")
            return 0

    written = store.upsert_nutrients(records)
    newest = max((r["sample_date"] for r in records), default=None)
    print(f"nutrients: {len(records)} sample(s) from the {source}, {written} written"
          + (f", newest {newest}" if newest else ""))

    # Say so when the source has nothing recent in it. A sampling round that
    # was typed into the wrong file looks exactly like a sampling round that
    # did not happen, and the symptom is silence: the table keeps showing the
    # last figures it had. One line in the run log turns that into a question
    # somebody can answer.
    quiet_days = int(nutrient_cfg.get("warn_after_days") or 21)
    if newest:
        age = (dt.date.today() - dt.date.fromisoformat(newest)).days
        if age > quiet_days:
            print(
                f"nutrients: WARNING the newest sample in the {source} is "
                f"{newest}, {age} days ago. If sampling has been happening, "
                "the rows are going somewhere the harvester is not reading: "
                "check that nutrients.workbook_url points at the same file "
                "the tablet writes to.",
                file=sys.stderr,
            )
    return written

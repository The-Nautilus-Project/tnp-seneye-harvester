# TNP Seagrass Nursery Monitor

In-situ water data from the Seneye monitors in the Posidonia nursery at North Mole,
collected on a schedule, stored in a database and published as a dashboard for
students and the research side of the project.

It is the tank-side counterpart to `tnp-ocean-harvester`: same shape (a Python
harvester on GitHub Actions, a database-agnostic store, a static dashboard), a
different source.

```
Seneye in sump  ->  Seneye cloud  ->  harvester (every 30 min)  ->  database
                                                                      |
                                                     JSON export -> dashboard
```

## What gets collected

Each Seneye sits in a sump, and a sump serves several tanks:

| System | Sumps | Tanks |
|---|---|---|
| A | SA12, SA345 | A1–A5 |
| B | SB12, SB34 | B1–B4 |
| C | SC12, SC34 | C1–C4 |
| D | SD12, SD345 | D1–D5 |
| E | SE12 | E1, E2 |

Per Seneye reading: temperature, pH and free ammonia (NH₃), plus the device's own
health flags (slide serial and expiry date, out-of-water, disconnected). A reading
describes the shared water of the tanks on that sump, not an individual tank, and
the dashboard says so.

The light metrics the reef units also report (PAR, lux, colour temperature) are
not collected. The probes sit in the sumps rather than in the lit tanks, so they
only ever read zero.

## Setup

**1. Find your device ids**

```bash
export SENEYE_USER="you@example.com"
export SENEYE_PWD="..."
python -m harvester.harvest --dry-run
```

This prints one block per device on the account, including its id.

**2. Map ids to sumps** in `config.json`:

```json
"devices": {
  "40281": { "sump": "SA12" },
  "40295": { "sump": "SD345" }
}
```

The sump supplies the system letter and the tank list, so that is the only line
you need per device. If you move a Seneye to a different sump, change its entry
here — readings already stored keep the mapping they were collected under.

**3. Run it**

```bash
python -m harvester.harvest
```

Nothing to install: the harvester is standard library only on the default
SQLite backend. Python 3.9+.

**4. On GitHub Actions**

Add two repository secrets, `SENEYE_USER` and `SENEYE_PWD`. The workflow in
`.github/workflows/harvest.yml` polls every 30 minutes, commits the updated
database and JSON export back to the repo, and publishes `dashboard/` to GitHub
Pages. GitHub's scheduler is best-effort, so a run is occasionally late or
skipped; the harvester is idempotent, so that only ever costs the one reading
the API was holding at that moment.

## Putting the dashboard on the TNP website

`dashboard/` is self-contained: `index.html` plus `data/nursery.json`. Copy both
onto the site (keeping the relative path), or point an iframe at the Pages URL.
The page loads no libraries and stores nothing in the browser; the only network
request it makes is for its own JSON.

For a snapshot with the data baked in — no server, no fetch — run:

```bash
python tools/build_artifact.py --out build/nursery-snapshot.html
```

### Embedding it in a page

`embed/embed-snippet.html` is a self-contained block for the website: an iframe
and a short script, no stylesheet and no library. The only line to change is the
`src`. Hand it to whoever maintains the site and it drops into a page as is.

The dashboard measures itself and posts its height to the host page, so the
frame grows to fit rather than scrolling inside itself. The host script accepts
that message only from its own frame, and only when the origin matches, then
sanity-checks the number before applying it. If the message never arrives, the
frame keeps the fallback height in its style attribute and scrolls internally,
so a strict content policy degrades rather than breaks.

## Where the data lives

`DATABASE_URL` decides, and nothing else in the code changes:

| Value | Backend |
|---|---|
| unset | `sqlite:///data/nursery.db` (default, committed by the workflow) |
| `postgresql://user:pwd@host:5432/db` | PostgreSQL — needs `psycopg` or `psycopg2` |
| `mysql://user:pwd@host:3306/db` | MySQL/MariaDB — needs `mysql-connector-python` or `PyMySQL` |

Tables are created on first run; `sql/` holds the same schema written out for
review. Readings are keyed on `(device_id, reading_time)`, so re-polling the
same last reading never duplicates a row, and re-running a harvest is always
safe.

## The export the dashboard reads

`dashboard/data/nursery.json` holds daily statistics for the whole record
(`window_days`, default 365) and raw readings for the recent window
(`raw_days`, default 30), so the file the browser downloads does not grow
without limit. Both are set in `config.json`. `dashboard/data/readings.csv` is
the full raw export for anyone who wants the numbers rather than the charts.

Daily statistics are n, minimum, maximum, mean and sample standard deviation per
device per day per parameter. The dashboard's daily view draws the mean with a
±1 SD band and a min–max envelope.

## The dashboard's two tabs

**Seneye sensors** holds the continuous record: the parameter, window and series
controls, a card per sump with its statistics, the detail chart and the daily
statistics table.

**In-situ samples** holds the hand-sampled record: the matrix table, a small
chart per sump for the chosen analyte, and its own export. The two are kept
apart because they are different measurement methods on different instruments at
different cadences, and mixing their controls made both harder to read.

### Exporting

Each tab has an export panel. Tick the sumps you want, set a date range, and
take the result as a CSV download or straight to the clipboard. The row count
updates as you change the selection, so you know what you are getting before you
ask for it.

The sensor export offers either every reading or the daily statistics, and
defaults its dates to whatever window is selected above. The in-situ export
defaults to the whole record, since those samples go back months and that tab
has no window control. Filenames carry the range, for example
`nursery-sensor-readings_2026-08-13_to_2026-09-21.csv`.

Downloads use a blob URL, which works on the published site. Where a browser
blocks it, the data goes to the clipboard instead and the button says so.

## Modelled values

Two figures on the dashboard are calculated rather than measured. Both are
labelled "modelled" on their tab and carry a note on the chart explaining what
they are. Neither is ever written into the same field as a measurement, and both
are recomputed on every export, so changing the model needs no re-harvesting.
`config.json` has a `derived` block to switch either off.

**Ammonium (NH₄⁺)** is a genuine derivation. Free ammonia and ammonium are two
sides of one equilibrium, so given the measured NH₃, pH and temperature, plus
salinity, the rest of the total ammonia pool follows:

```
I   = 19.973 S / (1000 - 1.2005109 S)
pKa = 0.0901821 + 2729.92/(T+273.2) + (0.1552 - 0.0003142 T) I
f   = 1/(10^(pKa - pH) + 1)              fraction present as free NH3
```

Total ammonia is NH₃/f and ammonium is the remainder, converted by molar mass.
At seawater pH only about 3% of the pool is free ammonia, so NH₄⁺ comes out far
larger than NH₃, which is expected rather than a fault. The fit is valid for
5–35 ppt, 5–35 °C and pH 7.8–8.3; outside that the value is withheld rather than
extrapolated.

**Oxygen at saturation** is *not* dissolved oxygen. It is how much oxygen the
water could hold at the measured temperature and the salinity in use, at
sea-level pressure. Real DO sits below this whenever respiration outpaces
exchange, and measuring it needs a probe. It is shown as a ceiling and the
dashboard says so in as many words.

```
DO0 = exp(-139.34411 + 1.575701e5/T - 6.642308e7/T^2
          + 1.243800e10/T^3 - 8.621949e11/T^4)     mg/L, fresh water, 1 atm
Fs  = exp(-S (0.017674 - 10.754/T + 2140.7/T^2))   salinity correction
DO  = DO0 * Fs                                     T in kelvin
```

Checked against the published tables: 9.08 mg/L at 20 °C fresh, 7.38 at 20 °C
and S=35, both matched to within 0.03 mg/L by the unit tests.

**Salinity** for both comes from the most recent in-situ sample for that sump.
Where a sump has no sample yet, `derived.default_salinity` is used. It is never
invented per reading, and both models return nothing rather than guess when an
input is missing.

**Sources**

Benson, B.B. and Krause, D. (1984) 'The concentration and isotopic fractionation
of oxygen dissolved in freshwater and seawater in equilibrium with the
atmosphere', *Limnology and Oceanography*, 29(3), pp. 620–632. Equation as
adopted by the U.S. Geological Survey, *Office of Water Quality Technical
Memorandum 2011.03*.

Bell, T.G., Johnson, M.T., Jickells, T.D. and Liss, P.S. (2007)
'Ammonia/ammonium dissociation coefficient in seawater: a significant numerical
correction', *Environmental Chemistry*, 4(3), pp. 183–186.
doi:10.1071/EN07032.

Florida Department of Environmental Protection, *Calculation of un-ionized
ammonia in fresh and saline water*, standard operating procedure, after
Whitfield (1974) and Bower and Bidwell (1978).

A caveat carried from Bell et al. (2007): expressions of this family, derived
from Khoo et al. (1977), can overstate free NH₃ under some conditions. Since the
harvester works the other way, from measured NH₃ to ammonium, an overstated pKa
would understate ammonium. Treat the NH₄⁺ figure as indicative.

## In-situ samples

The nursery is also sampled by hand at the sumps every week or two, with
handheld meters and test kits: nitrate, nitrite, phosphate, salinity, carbonate
hardness, calcium, magnesium, plus spot temperature and pH.

These are a different measurement method from the Seneye probes, on different
instruments with their own accuracy and resolution. They are stored in their own
table and shown as their own thing. Where both measure the same quantity the
hand sample is drawn on the sensor chart as a hollow diamond, for comparison
only: the two are never averaged, and a difference between them is two
instruments measuring, not an error to reconcile. The wording the dashboard uses
to say so lives in `config.json` under `nutrients.method_note`, so you can put it
in your own words.

### Reading the sheet

The harvester fetches the Google Sheet itself on every run. Set it up once:

1. In the sheet: **File, Share, Publish to web**. Choose the **Nutrient** tab and
   **Comma-separated values (.csv)**, then Publish.
2. Put the sheet's URL in `config.json` under `nutrients.sheet_url`. Either the
   published link or the ordinary `/edit` URL works; the harvester rewrites it to
   the CSV endpoint.

After that, anything typed into the sheet is on the dashboard within half an
hour. Nothing to export, nothing to commit.

Publishing makes that tab readable by anyone with the link. If the sampling data
has to stay private, the alternative is a Google service account with the sheet
shared to it and its key in an Actions secret, which is more setup; the code
would need a `google-auth` dependency.

### What happens when the fetch fails

Nothing destructive. Samples already in the database are left exactly as they
are, so an outage or an unpublished sheet shows as a stale date on the dashboard
rather than an empty table. If a `data/nutrients.xlsx` is present it is used as a
fallback. Every successful fetch is also written to `data/nutrients_latest.csv`
and committed, which gives the repo a dated record of what the sheet said at the
time.

### What the parser copes with

Columns are found by their headings rather than their position, so inserting a
column in the sheet will not silently shift every value one field across.
Beyond that: the date entered once at the top of each sampling block and left
blank below it, dates both as real dates and as typed text like `21/9/26`, `N/A`
for anything not measured that round, blank separator rows, and the empty first
column. Rows are matched to sumps by the `Tank ID` column, where `A12` means
sump `SA12`, and keyed on date plus sump, so a corrected sheet overwrites rather
than duplicates. An analyte with no values anywhere is dropped from the export
rather than shown as an empty chart.

### Reference ranges and colouring

Each in-situ value is compared with ordinary local seawater and coloured on
three levels: green inside the typical range, amber between typical and the
outer bound, red beyond it. Colour is not the only cue — amber cells carry a
triangle and red cells a square, so the table still reads in greyscale.

The ranges live in `config.json` under `insitu_reference`, each with the basis
it was derived from. They describe **seawater**, not what *Posidonia* requires,
and a red cell is a prompt to check the reading and the test kit rather than
evidence of a problem.

| Analyte | Typical | Outer | Derived from |
|---|---|---|---|
| Temperature | 13–20 °C | 11–22 | TNP nursery set points, not a seawater range |
| Salinity | 35–38.5 ppt | 32–41 | Atlantic inflow ~36.2 to Mediterranean outflow ~38.4 at the Strait |
| pH | 8.0–8.2 | 7.5–8.5 | surface ocean ~8.1; outer bounds are the nursery set points |
| Carbonate hardness | 6.8–8.0 °dKH | 5–11 | Mediterranean surface alkalinity |
| Nitrate | 0–0.25 mg/L | 0–1.0 | western Mediterranean surface nitrate |
| Nitrite | 0–0.05 mg/L | 0–0.2 | upper-ocean nitrite |
| Ammonia | 0–0.05 mg/L | 0–0.2 | surface ammonium, a few µmol/L at most |
| Phosphate | 0–50 ppb | 0–300 | western Mediterranean surface phosphate |
| Calcium | 400–455 ppm | 360–520 | 412 ppm at S=35, scaled with salinity |
| Magnesium | 1250–1420 ppm | 1150–1600 | 1290 ppm at S=35, scaled with salinity |

**The conversions**, so the numbers can be checked rather than taken on trust:

- Alkalinity to dKH: 2600 µmol/kg × 1.027 kg/L ÷ 1000 = 2.67 meq/L; ÷ 0.3566 =
  7.5 °dKH. The 2500–2650 µmol/kg range gives 7.2–7.6 °dKH.
- Nutrients from µmol/L to mg/L as the ion: NO₃ × 62, NO₂ × 46, PO₄ × 95,
  NH₄ × 18, all ÷ 1000. So 4 µmol/L nitrate = 0.25 mg/L, and 0.16 µmol/L
  phosphate = 0.015 mg/L, which is 15 ppb.
- Major ions scale with salinity: Ca 412 and Mg 1290 at S=35 become ~452 and
  ~1415 at S=38.4.
- Calcium and magnesium are read in ppm. In seawater 1 ppm is about 1.03 mg/L,
  because a litre weighs roughly 1.026 kg, so the same figures serve for both
  units and the harvester converts nothing.
- Phosphate is read in **parts per billion**, a thousandth of the others. TNP's
  readings run 4 to 10 ppb, which is ordinary Strait surface water; the same
  figures in ppm would be a hundred times anything seawater does. The band was
  originally set in ppm and has been multiplied by a thousand, so it describes
  the same concentrations as before.

**Sources**

Belgacem, M., Schroeder, K., Barth, A., Troupin, C., Pavoni, B., Raimbault, P.,
Garcia, N., Borghini, M. and Chiggiato, J. (2021) 'Climatological distribution
of dissolved inorganic nutrients in the western Mediterranean Sea (1981–2017)',
*Earth System Science Data*, 13, pp. 5915–5949. doi:10.5194/essd-13-5915-2021.

Gemayel, E., Hassoun, A.E.R., Benallal, M.A., Goyet, C., Rivaro, P.,
Abboud-Abi Saab, M., Krasakopoulou, E., Touratier, F. and Ziveri, P. (2015)
'Climatological variations of total alkalinity and total dissolved inorganic
carbon in the Mediterranean Sea surface waters', *Earth System Dynamics*, 6,
pp. 789–800. doi:10.5194/esd-6-789-2015.

Zakem, E.J., Al-Haj, A., Church, M.J., van Dijken, G.L., Dutkiewicz, S.,
Foster, S.Q., Fulweiler, R.W., Mills, M.M. and Follows, M.J. (2018)
'Ecological control of nitrite in the upper ocean', *Nature Communications*, 9,
1206. doi:10.1038/s41467-018-03553-w.

Major-ion concentrations at S=35 follow the standard seawater composition
reported in oceanographic reference texts and summarised by the Global Seafood
Alliance, *Typical chemical characteristics of full-strength seawater*.

Two caveats worth keeping in view. The ammonia row assumes the kit reports
total ammonia; if it reports free NH₃ the typical range should be an order of
magnitude lower. And the nursery is a closed system on collected seawater, so
nitrate and phosphate can legitimately sit above open-water values without
anything being wrong.

## Maintenance and issue tracking

A hub for anyone working at the nursery: faults people find, and the planned
jobs that fall due. Two tabs in the maintenance sheet, read the same way as the
sampling data.

**Maintenance** - one row per fault:

| Issue ID | Date | Time | System / Sump | Equipment | Fault / problem | Severity | Status | Assigned to | Action taken | Date resolved | Reported by | Notes |

**Schedule** - one row per recurring job:

| Task ID | Task | System / Sump | Equipment | Frequency (days) | Last done | Done by | Next due | Notes |

`templates/Nautilus_Maintenance_template.xlsx` has both tabs with the headings
and one example row. Import it into the maintenance sheet, delete the examples,
and publish each tab to the web as CSV as you did for the sampling data. Put
each tab's URL (or its `gid`) in `config.json` under `maintenance`.

### How the state is worked out

Status is read from the sheet and mapped onto open, in progress and resolved.
People type all sorts, so `closed`, `done`, `fixed` and `complete` all count as
resolved, `WIP`, `started` and `awaiting parts` as in progress. Anything
unrecognised is kept as written and treated as outstanding, so a typo never
hides a fault from the board. A row with a resolution date but no status counts
as resolved; a row with neither counts as open. A row with no description is not
an issue at all and is skipped, which is what lets blank separator rows through.

Severity is optional. Fill it in and the board sorts by it; leave it out and
everything sorts by age.

For planned jobs, next due comes from the sheet when it is filled in, otherwise
from last done plus the frequency. A job with neither is listed with no due date
rather than a guessed one. Overdue jobs sort to the top.

The sheet is the record of truth, so each refresh replaces the tables wholesale:
delete a row in the sheet and it disappears from the board. A tab that cannot be
read is left alone rather than emptied.

### Where the board goes

The board is a page of its own at `board/index.html`, reading
`board/data/maintenance.json`. It names the people who reported and carried out
work, so by default it is **neither committed nor published**: `board/data/` is
in `.gitignore`, and the Pages workflow only uploads `dashboard/`.

That default exists because the repo is public. Three ways to give the team
access to it:

1. **A separate private repo** with Pages, once the free nonprofit Team plan is
   in place. Pages on a private repo needs a paid or nonprofit plan. This is the
   only option that is genuinely private and still a live URL.
2. **Anonymised on the public site.** Set `maintenance.anonymise` to true and
   `publish` to true. Faults, status and due dates become public; who reported
   and who fixed them are stripped from the export.
3. **Keep it in Google.** Everyone who can see the sheet can see the log. No
   board, but no setup either.

Until you pick one, the board is generated locally by a harvest run and can be
opened straight from disk.

## Working ranges

`config.json` gives each parameter a `band` (the nursery's working range, shaded
on the charts) and `hard` limits (a reading outside them is flagged critical).
These are operating set points for this nursery, not published tolerances for
*Posidonia oceanica* — edit them to match the protocol, and remember the
dashboard states them as TNP's own.

## Slide replacement countdown

A Seneye slide lasts 30 days and is changed at the unit in the sump, so the
countdown is per sump, not per tank. The dashboard shows it in two places: a
strip above the panels giving the soonest one ("Replace slide in 23 days — all
9 sumps, due 14 Oct 2026"), and a small chip on each sump's panel. Green with
more than a week to go, amber inside the last week, red once it is overdue.

**Recording a slide change** is a two-line edit in `config.json`, and needs
nothing installed:

```json
"slides": {
  "enabled": true,
  "interval_days": 30,
  "warn_days": 7,
  "trust_sensor": true,
  "default_changed": "2026-09-14",
  "changed": {
    "SA12": "2026-10-02"
  }
}
```

`default_changed` covers every sump. An entry under `changed` overrides it for
that one sump, which is what you want when a single slide gets replaced out of
step with the rest. When the whole nursery is done in one session, move
`default_changed` to that date and empty `changed` again.

The edit can be made in GitHub's web editor. The next scheduled harvest rebuilds
the export and the dashboard picks it up — there is no need to run anything.

**Where the date comes from.** If the Seneye itself reports an expiry date for
its current slide, that is used in preference, because the device knows when its
own slide was registered and a written note can be forgotten. The logged date is
the fallback. The strip says which of the two it is using, so a slide that was
changed but not logged, or logged but not changed, is visible rather than
silently wrong. Set `trust_sensor` to `false` to go by the logged dates only.

**The days are counted in the browser**, not at export time, so the number a
student reads is right for the day they are reading it even if the last harvest
ran hours ago.

## Chiller plugs and the nursery air

Each row of systems runs its chillers from one double-socket smart plug, and a
temperature and humidity sensor sits in the nursery itself. Both are Tuya
devices, paired in the Smart Life app, and the harvester reads them through
Tuya's cloud API on the same half-hourly schedule as the water readings.

**The dashboard reads and never writes.** It is a static file on a public
website, so it can hold no credential, and a button on it would be a button any
visitor could press. Switching stays in the phone app. What the page adds is
the half of the story the temperature trace leaves out: which chiller is on,
since when, and whether the plug has stopped answering. "SC34 is 21.5 °C and
its chiller went off at 14:00" is a sentence worth reading, and it needs no
ability to switch anything.

A chiller being off is not reported as a fault, because they cycle. Three
things are: a chiller off while its own sump is above the working range, a
chiller switched on but drawing no power, and a plug that has dropped off the
network.

The middle one is only visible because these plugs meter. A relay that closes
while the compressor does not start reports a perfectly healthy "on", and the
switch state alone would show nothing wrong until the sump cooked. Where a
plug has one socket drawing, the wattage is that chiller's; where both are on
it belongs to the pair and is reported as such rather than halved.

Online and stale are kept apart. A plug Tuya can still reach was in contact at
the moment we polled. One that has dropped off the network was last in contact
whenever its record says, and its relay is still holding whatever position it
was left in, so the dashboard reports the last observed state and says plainly
that it cannot confirm it. Reading Tuya's `update_time` as a heartbeat was an
early mistake here: on a sensor it is the last measurement, but on a switch it
is the last time the device record changed, so a chiller nobody had touched
for a fortnight looked a fortnight dead.

When every plug goes quiet at once the banner says so once, as one reading
fault, rather than naming nine sumps. Nine near-identical lines would bury
whatever else needed saying, and telling a student that nine chillers have
failed when one API has stopped answering sends them running for nothing.

### Setting it up

Credentials come from the environment, never `config.json`:

| Secret | Where it comes from |
| --- | --- |
| `TUYA_ACCESS_ID` | Access ID/Client ID, on the cloud project's Overview tab |
| `TUYA_ACCESS_SECRET` | Access Secret/Client Secret, same page |

Create the project at iot.tuya.com under **Cloud → Development**, industry and
development method both **Smart Home**, data centre matching the region the
phone app account was registered in. Then **Devices → Link Tuya App Account →
Add App Account** and scan the QR code from the app's *Me* tab. The plugs
appear in the project's device list once that is done; an empty list almost
always means the wrong data centre.

`config.json > plugs > region` has to match: `eu` is Central Europe, `weu`
Western Europe, `us` Western America.

### Which socket is which

Nothing in the API says which of a double plug's two switch codes drives which
physical socket, so it is not guessed. Run **Actions → Probe smart plugs** and
note both values; switch one socket off in the app for a few seconds; run it
again and see which code changed. That is two minutes per plug against a year
of the dashboard quietly naming the wrong chiller.

The same workflow prints every data point each device reports, which is also
how to check the air sensor's scaling. Tuya send scaled integers, so 213 means
21.3 °C; if humidity comes back as 655 rather than 65, set its scale to 10 in
`config.json > plugs > devices > <id> > scales`.

### Why the air reading is often "old"

The sensor does not report on a timer. It sends a reading when a value moves
past a threshold its firmware decides, so a steady room produces nothing new
for an hour or two and the last change being old is not a fault.

Two timestamps are kept apart, because collapsing them was a mistake worth
documenting. `reading_time` is when the harvester took the reading, and there
is one every half hour: the air was that temperature when we asked, whether or
not the number had moved. `reported_at` is when Tuya last saw a value change.
Keying the stored reading on the second deduplicated almost everything away, so
a fortnight of monitoring produced two stored readings and the chart had
nothing to draw. A reading is not stored when the sensor is offline, because
writing its last value every half hour would manufacture a flat line out of
nothing. The plugs are the
opposite: they answer every poll, which is why `stale_minutes` is 90 for them
and `ambient_stale_hours` is 6 for the sensor. Sharing one threshold made a
healthy sensor look dead every time the nursery held still.

The chip says which of the three it is: a time on its own for a recent reading,
"unchanged since" once it is over an hour old, "last reported" past the
threshold, and "offline since" when Tuya cannot reach the device at all. Only
the last two are greyed.

To see what normal looks like for this particular sensor, run Actions → **Air
sensor reporting rate**. It reads the stored readings and gives the median gap,
the distribution, and the longest silences. It counts the sensor's own reports,
not the harvester's polls: "48 readings stored, 3 distinct reports" is the
shape of the answer, and quoting the first number alone would only describe how
often the harvester runs. One limit it states itself: the
harvester polls every thirty minutes, so it cannot see a sensor reporting more
often than that, and gaps at or below the harvest interval are a floor on the
true rate rather than a measurement of it. Gaps longer than thirty minutes are
real. A median of an hour or two with no long silences is a sensor reporting on
change in a stable room. Occasional gaps of many hours mixed with short ones
usually mean it dropped off the network rather than that the air stopped
moving.

## The chiller schedule

Nine chillers run off a timetable, and the timetable exists because of a hard
constraint: the cabinet takes one chiller running between 12:00 and 21:00 and
three overnight, since each DC2200 rejects about 2.95 kW into it. The schedule
lives in the plugs, through Smart Life. **This repository never switches
anything.** The dashboard is a static file on a public website and the day it
can turn a chiller off is the day a student can too.

What it does instead is check that what happened matches what was planned. The
timetable is written into `config.json > schedule`, and the strip at the top of
the dashboard answers three questions.

**Is the right thing running this minute**, and is the cabinet limit being
respected. Two chillers during the day lockout is the failure that damages
equipment, so it outranks everything else on the strip.

**Did each system actually get its block.** This is the one that matters most
in practice. A Smart Life schedule fires at the block boundary and a plug that
is off the network at that moment misses it outright; nothing retries. Rows C
and E sat dark for days in September that way and nothing anywhere said so.
Losing a 2.5-hour block from a timetable built around a 16 °C time-average is
not a rounding error.

**How long each system went without cooling**, measured across the whole window
rather than per calendar day. Clipping at midnight split the one gap that
matters most, because the overnight stretch is the long one.

Editing a block in `config.json` changes only what the dashboard expects to
see. A change made in Smart Life has to be made here too, or the page reports a
fault that is not one.

### Three corrections to the schedule document

Building the check against the timetable turned up three errors in the
*Longest gap without cooling* column of `Chiller_schedule_2026`:

| System | Document | Actual |
| --- | --- | --- |
| SE12 | 5.0 h | **6.75 h** |
| SC12 | 7.5 h | 7.25 h |
| SA345 | 9.0 h | 9.25 h |

The SE12 one is worth acting on. It runs 02:00–04:30, 09:30–12:00 and
18:45–21:00, so it sits from **12:00 to 18:45 with no cooling**, through the
hottest part of the day, on a seagrass system. The document understates that by
nearly two hours. Every runtime figure and the 54 chiller-hour total check out
exactly; it is only the gap column.

## Energy

Taken by integrating the half-hourly power readings, not from the plugs' own
`add_ele` counter. That counter resets whenever a plug loses power and two of
the five have been stuck on the same figure for over a week, so it is the
number that would look most authoritative on a dashboard while being the least
true. It is stored and never shown, only so we can tell later whether it starts
behaving.

Accuracy, measured against a simulation where the true answer was known: **0.5%
over a fortnight, and about 15% day to day.** A compressor cycles on its own
thermostat, so half-hourly spot readings of it are a sampling problem; over a
month the errors cancel, over one day they do not. Each day carries a
`coverage` figure, so a day the harvester only watched for ten hours is not
mistaken for a quiet one.

The meter is per plug, so consumption is per row rather than per sump. Your
schedule happens to separate most of them anyway: Rows A, C and E never run
both sockets at once, while Rows B and D always run both together.

Set `plugs.tariff.per_kwh` to see a cost. From the schedule's own figures, 54
chiller-hours a day at roughly 750 W is about 40.5 kWh a day, or just under
14,800 kWh a year, before the cabinet extraction fan.

### When a device is reset

A Tuya device that is factory reset comes back with a brand new device ID, and
joining one to a different Wi-Fi network is enough to do it. The ID in
`config.json` then points at nothing, the harvester asks about a device that no
longer exists, and the readings stop without any error.

**The harvest notices.** Every run that finds a device silent checks it against
the account's own device list, and the two cases are easy to tell apart once
you look: a plug merely off the network is still registered and still answers
with its last known state, as Rows C and E do, while a device that has been
reset returns nothing and is absent from the listing. Only the second needs a
new ID. The run log names it, lists the IDs on the account that `config.json`
does not know about, and says which workflow fixes it. Nothing has to be
spotted by eye.

**Fixing it takes one workflow.** Actions → **Replace a device ID**, paste the
old and the new, leave dry run ticked the first time. It rewrites `config.json`
and commits, so nobody edits JSON by hand. With **check** ticked it first asks
Tuya whether the new ID really is on the account and the old one really is not,
and refuses the swap if either is wrong.

To find the new ID: Actions → **Probe smart plugs** with **list every device**
ticked, or iot.tuya.com → Cloud → Development → your project → Devices.

The old ID is recorded under the device's `previous_ids`, which is what keeps
the readings from before the reset on the same chart rather than starting the
record again from the day the router changed. A device reset more than once
keeps the whole chain.

### The subscription

Tuya put cloud access behind an IoT Core subscription that has to be renewed.
When it lapses the API stops answering and the chiller column freezes exactly
where it stood, which looks identical to nothing having changed. Record the
expiry date in `plugs.subscription_expires`: every harvest warns in its log
from thirty days out, and the dashboard shows it from `subscription_warn_days`.

### How it is stored

`plug_states` is a transition log, not a sample every half hour. A row is
written when a socket changes state and its `last_seen` is bumped otherwise.
Eleven sockets polled every thirty minutes would be two hundred thousand rows a
year to say "still on"; this way the table holds the switching history, which
is the part anyone would want to read back, and `changed_at` is already the
answer to "since when".

Air readings go in `ambient` and are deliberately kept out of the sump table.
The air is context for what the water is doing, not a tenth sump to check.

## Importing history from Seneye

The API only ever serves the last reading, so anything from before the harvester
started has to come from Seneye directly. `tools/import_history.py` loads those
files into the same `readings` table the harvester writes to; once they are in,
the dashboard treats old and new readings identically.

### What to ask Seneye for

Ask support for a **CSV export of all readings** for the account, and be
specific about four things, because they are the four that cost a re-export if
they come back wrong:

1. **CSV**, not PDF or a screenshot of a chart.
2. **Every device**, with either one file per device or a device id column in a
   combined file. A file with neither can still be imported, one device at a
   time, with `--sump`.
3. **The full period**, with the date the record starts.
4. **Which timezone the timestamps are in** — UTC or Gibraltar local time. This
   is the one that matters most. Getting it wrong shifts a year of readings by
   an hour or two, and nothing about the result looks broken afterwards.

Columns wanted: date/time, device identifier, temperature, pH and NH₃. Anything
else in the file is ignored rather than guessed at.

### What TNP's own export turned out to be

The September 2026 export, checked against the live readings:

- **Header** `Declared,Temperature,NH3,pH`, newest row first. `Declared` is
  Seneye's name for the reading time and the importer now recognises it.
- **Timestamps** `22/09/2026 14:58`, day first, no offset marked.
- **Timezone: `Europe/London`, not `Europe/Gibraltar`.** Seneye is a UK
  company and its export uses UK time. In September its timestamps run one
  hour ahead of UTC, and the clocks change on the UK dates: the record repeats
  local hour 01 on 26 October 2025, which is the BST→GMT pattern. Gibraltar
  would have repeated hour 02. Importing these files as Gibraltar time puts
  every reading an hour late.
- **No device column.** Each file covers one sump and is identified by its
  filename, so the files must be named `SA12.csv`, `SA345.csv` and so on.
- **Repeated timestamps.** Seneye logs the same minute more than once, and the
  repeats do not always agree — one file has 02:11 three times with pH 7.94,
  7.94 and 8.62. The importer collapses repeats to their median rather than
  keeping whichever row came first, so an outlier cannot win on row order.
- **Early readings are the unit, not the water.** The record opens on 24
  October 2025 at 28.5 °C and pH 6.7, which is a sensor on a bench rather than
  a sump. November 2025 is missing entirely and December has nine days.
  Coverage is steady from January 2026 at about 45 readings per sump per day.

### Loading it, without installing anything

The **Import Seneye history** workflow does the whole thing in GitHub, so no
Python is needed on your own machine:

1. On the repo's **Code** tab, open `data/history/` and use *Add file → Upload
   files* to put the CSVs Seneye sent there. Commit.
2. Go to **Actions → Import Seneye history → Run workflow**.
3. Set the timezone Seneye confirmed. Leave **dry run** ticked. Run it.
4. Open the run's log. It lists the columns it matched, how many rows it could
   read, the period they span, how much of it the database already holds, and
   the reason for anything it would skip. Nothing has been written at this
   point. Read the overlap line (see *Overlap, duplicates, and the timezone*
   below) before going on: if it warns that the values disagree, run the dry
   run again with the other timezone.
5. If that looks right, run it again with **dry run** unticked. The readings go
   into the database, the export is rebuilt and both are committed.

The two remaining options are there for awkward files: `sump` names the device
for a file that does not identify itself, and `month first` reads ambiguous
dates as MM/DD.

### Loading it from a command line

If you do have Python to hand:

```bash
python tools/import_history.py --dry-run --timezone Europe/Gibraltar exports/
python tools/import_history.py --timezone Europe/Gibraltar exports/
python -m harvester.harvest --export-only
```

Always run `--dry-run` first. It prints the columns it matched, how many rows it
could read, the period they span, two example rows and the reason for anything
it would skip — and writes nothing. If the columns it matched are not the ones
you expected, that is visible before anything reaches the database.

### Overlap, duplicates, and the timezone

An export nearly always overlaps readings the harvester has already collected.
That costs nothing: readings are keyed on `(device_id, reading_time)` and
nothing is overwritten, so a row that is already there is skipped and the
harvester's copy stands.

The dry run reports the overlap, and reports two numbers rather than one:

```
    48 of these are already in the database and would be skipped;
    48 of those match the stored reading
```

The second number is the timezone check, and it is the one to read. Counting
collisions alone proves nothing — readings sit on a regular half-hourly grid,
so an export an hour or two out still lands on timestamps that exist; it just
lands on the wrong ones. Comparing the values tells the two apart:

| What the dry run says | What it means |
|---|---|
| Overlap, and the values match | The timezone is right. Import it. |
| Overlap, but the values disagree | The export is being shifted onto its neighbours. A warning is printed. Try the other timezone before importing. |
| No overlap at all | Either the export stops before the harvester started, or the device could not be matched. Check which. |

If the values disagree, run the dry run again with the other `--timezone` and
take whichever gives agreement. Nothing has been written at this point, so
there is no harm in trying both.

| Option | What it does |
|---|---|
| `--timezone` | IANA name the export's timestamps are in. Defaults to `UTC`. |
| `--sump` | Sump code for a file that does not name its device, e.g. `SA12`. |
| `--month-first` | Read ambiguous dates as MM/DD. The default is DD/MM. |
| `--tag` | Written to `slide_serial`. Default `IMPORTED`. |
| `--dry-run` | Report only; write nothing. |
| `--database-url` | Import somewhere other than the default database. |

Files can be given as paths, glob patterns or a folder of CSVs.

### How it decides things

**Columns** are matched by heading rather than position, so the export's layout
does not matter as long as the headings are recognisable (`Date/Time`,
`Timestamp`, `Temp (°C)`, `pH`, `NH3 (mg/L)`, and so on). Preamble lines above
the header row are skipped: the first row in the first fifteen that has both a
date column and a measurement column is taken as the header. Separate `Date` and
`Time` columns work, in either order, as do unix seconds and milliseconds.

**Devices** are resolved in order: a device id that matches `config.json`, then
a name or sump column, then `--sump`, then the sump code in the filename. A row
that matches none of those is counted and reported rather than attributed to a
guess.

**Re-running is safe.** Rows are keyed on `(device_id, reading_time)` and
nothing is ever overwritten, so importing the same file twice adds nothing the
second time, and a historical file cannot clobber a reading the harvester
collected. Imported rows carry `IMPORTED` in `slide_serial`, so they stay
identifiable:

```sql
SELECT COUNT(*) FROM readings WHERE slide_serial = 'IMPORTED';
DELETE FROM readings WHERE slide_serial = 'IMPORTED';   -- to start the import over
```

If the history is longer than a year, raise `export.window_days` in
`config.json` before rebuilding, or the dashboard will only summarise the most
recent 365 days of it.

## Limitations worth knowing

- The public Seneye API serves the **last** reading only; there is no historical
  endpoint. The record therefore starts the day the harvester starts, and its
  resolution is the polling interval, not the device's own. Anything from before
  that has to be asked of Seneye as a file and loaded with
  `tools/import_history.py` (see *Importing history from Seneye* above).
- Credentials are the Seneye account e-mail and password sent as query
  parameters. That is what the API offers. Keep them in Actions secrets or an
  environment file; never in `config.json`.
- Seneye publish this interface for hobbyist use with no support and no stated
  rate limit. The harvester makes one request per poll and backs off on errors.
- Plug state is only ever as fresh as the last harvest, so the dashboard is not
  a way to catch a chiller failing in the next five minutes. It is a way to
  notice one that failed this morning, which is the failure that actually
  happens. Anything faster would need a receiver running at the nursery.
- Meross plugs have no official public API. Only the Tuya and Smart Life ones
  are readable here.
- If per-reading resolution becomes worth having, the alternative is the Seneye
  Local Data Exchange (`github.com/seneye/LDE`): the SWS or Connect app POSTs a
  JWT-signed payload for every reading to a URL you host. That needs an
  always-on receiver at the nursery end, which is why this version polls the
  cloud instead.

## Development

```bash
python -m unittest discover -s tests -v   # unit tests
python tools/mock_data.py --days 120      # fill a database with plausible readings
python -m harvester.harvest --export-only # rebuild the JSON from the database
```

Mock rows carry `slide_serial` values beginning `MOCK-`; clear them with
`DELETE FROM readings WHERE slide_serial LIKE 'MOCK-%';`.

## Layout

```
harvester/    seneye.py (API client) · nutrients.py (sheet + workbook reader)
              plugs.py (Tuya chiller plugs + air sensor, read only)
              schedule.py (checks the nursery against the chiller timetable)
              maintenance.py (issues + planned jobs) · store.py (database)
              export.py (JSON) · harvest.py (CLI)
dashboard/    index.html + data/   (public)
board/        index.html + data/   (internal maintenance board, not published)
templates/    maintenance sheet template
sql/          schema for PostgreSQL and MySQL
data/history/ historical Seneye exports waiting to be imported
tools/        mock_data.py · import_history.py · build_artifact.py
tests/        unit tests
```

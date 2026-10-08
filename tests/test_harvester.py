"""Unit tests. Run with: python -m unittest discover -s tests -v"""

import collections
import datetime as dt
import json
import math
import os
import tempfile
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harvester.export import _daily_stats, _slides, build_payload
from harvester import plugs
from harvester import schedule as sched
from tools import sensor_cadence, swap_device
from harvester.seneye import parse_reading
from harvester.store import Store

# Shape of one device from GET /v1/devices?IncludeState=1
SAMPLE = {
    "id": "12345",
    "description": "Sump SA12",
    "type": 1,
    "status": {
        "disconnected": "0",
        "slide_serial": "SLD-987",
        "slide_expires": 1790000000,
        "out_of_water": "0",
        "wrong_slide": 0,
        "last_experiment": 1789990000,
    },
    "exps": {
        "temperature": {"trend": 1, "critical_in": -1, "avg": "21.1", "status": 0, "curr": "21.35", "advises": []},
        "ph": {"trend": 0, "critical_in": -1, "avg": "8.10", "status": 0, "curr": "8.12", "advises": []},
        "nh3": {"trend": -1, "critical_in": -1, "avg": "0.006", "status": 0, "curr": "0.004", "advises": []},
        "light": {"curr": "142", "max_value": "180", "status": 0, "advises": []},
    },
}


class TestParsing(unittest.TestCase):
    def test_parses_values_and_state(self):
        r = parse_reading("12345", SAMPLE["exps"], SAMPLE["status"], 1789990000, 1789990600)
        self.assertEqual(r.device_id, "12345")
        self.assertAlmostEqual(r.values["temperature"], 21.35)
        self.assertAlmostEqual(r.values["ph"], 8.12)
        self.assertAlmostEqual(r.values["nh3"], 0.004)
        self.assertNotIn("par", r.values)  # light metrics are no longer kept
        self.assertEqual(r.slide_serial, "SLD-987")
        self.assertEqual(r.out_of_water, 0)
        self.assertEqual(r.trends["temperature"], 1)

    def test_missing_parameters_are_absent_not_zero(self):
        r = parse_reading("1", {"ph": {"curr": "8.0"}}, {}, 1, 2)
        self.assertNotIn("temperature", r.values)
        self.assertIsNone(r.as_row()["temperature"])

    def test_garbage_values_are_dropped(self):
        r = parse_reading("1", {"ph": {"curr": "n/a"}, "nh3": {"curr": ""}}, {}, 1, 2)
        self.assertEqual(r.values, {})


class TestStore(unittest.TestCase):
    def setUp(self):
        self.store = Store("sqlite://:memory:")
        self.store.migrate()

    def tearDown(self):
        self.store.close()

    def test_readings_deduplicate(self):
        r = parse_reading("1", SAMPLE["exps"], SAMPLE["status"], 1789990000, 1789990600)
        self.assertEqual(self.store.insert_readings([r.as_row()]), 1)
        self.assertEqual(self.store.insert_readings([r.as_row()]), 0)
        rows = self.store.query("SELECT COUNT(*) AS n FROM readings")
        self.assertEqual(rows[0]["n"], 1)

    def test_device_upsert_updates_rather_than_duplicates(self):
        self.store.upsert_device("1", "SA12", 1, "SA12", "A", "SA12", 100)
        self.store.upsert_device("1", "SA12 renamed", 1, "SA12", "A", "SA12 renamed", 200)
        rows = self.store.query("SELECT * FROM devices")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "SA12 renamed")
        self.assertEqual(rows[0]["first_seen"], 100)
        self.assertEqual(rows[0]["last_seen"], 200)


class TestStats(unittest.TestCase):
    def test_daily_stats_use_sample_sd(self):
        day = 1789948800
        rows = [
            {"device_id": "1", "reading_time": day + 3600, "temperature": 20.0},
            {"device_id": "1", "reading_time": day + 7200, "temperature": 22.0},
            {"device_id": "1", "reading_time": day + 10800, "temperature": 24.0},
        ]
        stats = _daily_stats(rows, ["temperature"])
        self.assertEqual(len(stats), 1)
        t = stats[0]["temperature"]
        self.assertEqual(t["n"], 3)
        self.assertEqual(t["min"], 20.0)
        self.assertEqual(t["max"], 24.0)
        self.assertEqual(t["mean"], 22.0)
        self.assertEqual(t["sd"], 2.0)  # sample sd, not population


class TestPayload(unittest.TestCase):
    def test_payload_shape(self):
        store = Store("sqlite://:memory:")
        store.migrate()
        now = int(time.time())
        store.upsert_device("1", "SA12", 1, "SA12", "A", "SA12", now)
        row = parse_reading("1", SAMPLE["exps"], SAMPLE["status"], now - 600, now).as_row()
        store.insert_readings([row])

        config = {
            "site": {"title": "t"},
            "systems": {"A": {"label": "System A"}},
            "sumps": {"SA12": {"system": "A", "tanks": ["A1", "A2"]}},
            "parameters": {"temperature": {"label": "Temperature", "unit": "C", "precision": 2}},
        }
        payload = build_payload(store, config, window_days=30, raw_days=7)
        json.dumps(payload)  # must be serialisable

        self.assertEqual(payload["devices"][0]["tanks"], ["A1", "A2"])
        self.assertEqual(payload["devices"][0]["system_label"], "System A")
        self.assertIn("temperature", payload["columns"])
        self.assertEqual(len(payload["readings"]), 1)
        self.assertIn("1", payload["latest"])
        store.close()



class TestNutrients(unittest.TestCase):
    """Parsing rules the TNP workbook actually exercises."""

    def test_excel_serial_and_typed_dates_both_parse(self):
        from harvester.nutrients import _excel_date
        self.assertEqual(_excel_date("46247").isoformat(), "2026-08-13")
        self.assertEqual(_excel_date("21/9/26").isoformat(), "2026-09-21")
        self.assertIsNone(_excel_date(""))

    def test_na_and_blanks_become_none(self):
        from harvester.nutrients import _number
        self.assertIsNone(_number("N/A"))
        self.assertIsNone(_number(""))
        self.assertIsNone(_number(None))
        self.assertEqual(_number("0.0"), 0.0)
        self.assertEqual(_number("<0.02"), 0.02)  # detection limit keeps its number

    def test_tank_id_maps_to_sump(self):
        from harvester.nutrients import sump_code
        sumps = {"SA12": {}, "SD345": {}}
        self.assertEqual(sump_code("A12", sumps), "SA12")
        self.assertEqual(sump_code("SD345", sumps), "SD345")
        self.assertIsNone(sump_code("Tank ID", sumps))
        self.assertIsNone(sump_code("Z99", sumps))

    def test_nutrients_upsert_replaces_a_corrected_value(self):
        store = Store("sqlite://:memory:")
        store.migrate()
        base = {"sample_date": "2026-09-21", "sump_code": "SA12", "no3": 0.0}
        store.upsert_nutrients([base])
        store.upsert_nutrients([dict(base, no3=1.5)])
        rows = store.query("SELECT sample_date, sump_code, no3 FROM nutrients")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["no3"], 1.5)
        store.close()



class TestSheetSource(unittest.TestCase):
    """The CSV route from Google must agree with the .xlsx route exactly."""

    HEADERS = ",Date,Time,Tank ID,Temp (°C),Salinity (ppt),pH,dKH (°dKH),NO₃ (mg/L),NO₂ (mg/L),NH₃,PO₄³⁻,Ca2+ (mg/L),Mg2+ (mg/L)"

    def test_headings_normalise_past_units_and_subscripts(self):
        from harvester.nutrients import normalise_heading
        self.assertEqual(normalise_heading("NO₃ (mg/L)"), "no3")
        self.assertEqual(normalise_heading("Mg2+ (mg/L)"), "mg2")
        self.assertEqual(normalise_heading("dKH (°dKH)"), "dkh")
        self.assertEqual(normalise_heading("Temp (°C)"), "temp")

    def test_phosphate_does_not_steal_the_ph_column(self):
        from harvester.nutrients import map_headings
        headings = {"6": "pH", "11": "PO₄³⁻"}
        mapping = map_headings(headings)
        self.assertEqual(mapping["ph"], "6")
        self.assertEqual(mapping["po4"], "11")

    def test_csv_export_parses_with_carried_dates(self):
        from harvester.nutrients import parse_csv
        text = "\n".join([
            self.HEADERS,
            ",13/08/2026,12:38:00,A12,14.1,36.18,7.4,15,0,0,,,400,",
            ",,,A345,15.6,36.93,7.4,10,0,0,,,400,",
            ",,,,,,,,,,,,,",
            ",21/9/26,,E12,16.9,33.8,8.3,,,,,,573,1560",
        ])
        records = parse_csv(text, {"SA12": {}, "SA345": {}, "SE12": {}})
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["sample_date"], "2026-08-13")
        self.assertEqual(records[0]["sample_time"], "12:38")
        self.assertEqual(records[1]["sample_date"], "2026-08-13")  # carried down
        self.assertEqual(records[1]["sump_code"], "SA345")
        self.assertEqual(records[2]["sample_date"], "2026-09-21")
        self.assertEqual(records[2]["mg"], 1560.0)

    def test_a_reordered_sheet_still_maps_correctly(self):
        from harvester.nutrients import parse_csv
        text = "\n".join([
            "Tank ID,Date,pH,Temp (°C),NO₃ (mg/L),Salinity (ppt)",
            "A12,13/08/2026,7.4,14.1,0.5,36.18",
        ])
        record = parse_csv(text, {"SA12": {}})[0]
        self.assertEqual(record["ph"], 7.4)
        self.assertEqual(record["temp_c"], 14.1)
        self.assertEqual(record["no3"], 0.5)
        self.assertEqual(record["salinity_ppt"], 36.18)

    def test_sheet_urls_normalise_to_a_csv_endpoint(self):
        from harvester.nutrients import sheet_csv_url
        self.assertIn(
            "format=csv",
            sheet_csv_url("https://docs.google.com/spreadsheets/d/ABC123/edit?usp=sharing"),
        )
        published = "https://docs.google.com/spreadsheets/d/e/2PACX-1vABC/pubhtml"
        self.assertIn("output=csv", sheet_csv_url(published))
        already = "https://docs.google.com/spreadsheets/d/e/2PACX/pub?gid=7&single=true&output=csv"
        self.assertEqual(sheet_csv_url(already), already)

    def test_an_unparseable_sheet_raises_rather_than_returning_nothing(self):
        from harvester.nutrients import NutrientError, parse_csv
        with self.assertRaises(NutrientError):
            parse_csv("some,unrelated,csv\n1,2,3\n", {"SA12": {}})



class TestMaintenance(unittest.TestCase):
    """The issue log is the nursery's record, so parsing must not invent state."""

    ISSUE_HEADERS = ",Date,Time,Equipment,Tank ID,Fault/Maintenance,Action Taken,Resp. Person,Status,Severity"

    def _issues(self, *lines):
        from harvester.maintenance import parse_issues
        from harvester.nutrients import parse_csv_rows
        return parse_issues(parse_csv_rows("\n".join((self.ISSUE_HEADERS,) + lines)))

    def test_status_words_people_actually_type(self):
        rows = self._issues(
            ",13/08/2026,,Pump,SA12,Noise,,Jules,Closed,",
            ",13/08/2026,,Pump,SB12,Leak,,Jules,WIP,",
            ",13/08/2026,,Pump,SC12,Drip,,Jules,Outstanding,",
        )
        self.assertEqual([i["status"] for i in rows], ["resolved", "in_progress", "open"])

    def test_an_unknown_status_is_kept_not_discarded(self):
        row = self._issues(",13/08/2026,,Pump,SA12,Noise,,Jules,Waiting for Pedro,")[0]
        self.assertEqual(row["status"], "Waiting for Pedro")

    def test_a_blank_status_with_no_resolution_date_is_open(self):
        row = self._issues(",13/08/2026,,Pump,SA12,Noise,,,,")[0]
        self.assertEqual(row["status"], "open")

    def test_rows_without_a_description_are_not_issues(self):
        rows = self._issues(
            ",13/08/2026,,Pump,SA12,,,,,",
            ",,,,,,,,,",
            ",13/08/2026,,Pump,SB12,Real fault,,,,",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["summary"], "Real fault")

    def test_issue_ids_are_stable_and_unique(self):
        rows = self._issues(
            ",13/08/2026,,Pump,SA12,One,,,,",
            ",13/08/2026,,Pump,SB12,Two,,,,",
        )
        self.assertEqual(len({r["issue_id"] for r in rows}), 2)

    def test_next_due_comes_from_frequency_when_not_given(self):
        from harvester.maintenance import parse_schedule
        from harvester.nutrients import parse_csv_rows
        import datetime as dt
        text = "\n".join([
            "Task ID,Task,Tank ID,Equipment,Frequency (days),Last done,Done by",
            "T1,Replace slide,SA12,Seneye,30,01/09/2026,Jules",
            "T2,Calibrate,,Refractometer,180,,",
        ])
        jobs = parse_schedule(parse_csv_rows(text), today=dt.date(2026, 10, 5))
        self.assertEqual(jobs[0]["next_due"], "2026-10-01")
        self.assertEqual(jobs[0]["days_until_due"], -4)
        self.assertIsNone(jobs[1]["next_due"])       # never done, no guess
        self.assertIsNone(jobs[1]["days_until_due"])

    def test_the_sheet_is_the_record_so_deleted_rows_disappear(self):
        store = Store("sqlite://:memory:")
        store.migrate()
        cols = ("issue_id", "summary", "status")
        store.replace_table("issues", cols, [
            {"issue_id": "a", "summary": "one", "status": "open"},
            {"issue_id": "b", "summary": "two", "status": "open"},
        ])
        store.replace_table("issues", cols, [
            {"issue_id": "a", "summary": "one", "status": "resolved"},
        ])
        rows = store.query("SELECT issue_id, status FROM issues")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "resolved")
        store.close()

    def test_anonymise_strips_names_from_the_export(self):
        from harvester.maintenance import build_payload
        store = Store("sqlite://:memory:")
        store.migrate()
        store.replace_table(
            "issues",
            ("issue_id", "summary", "status", "reported_by", "assigned_to"),
            [{"issue_id": "a", "summary": "one", "status": "open",
              "reported_by": "Jules", "assigned_to": "Alice"}],
        )
        payload = build_payload(store, {"maintenance": {"anonymise": True}})
        self.assertIsNone(payload["issues"][0]["reported_by"])
        self.assertIsNone(payload["issues"][0]["assigned_to"])
        self.assertEqual(payload["issues"][0]["summary"], "one")
        self.assertTrue(payload["anonymised"])
        store.close()

class TestAnalyteGrouping(unittest.TestCase):
    """Physical and chemical measurements stay in their own families."""

    def test_every_analyte_has_a_known_group(self):
        from harvester.nutrients import ANALYTES, ANALYTE_GROUPS
        known = {key for key, _ in ANALYTE_GROUPS}
        for entry in ANALYTES:
            self.assertEqual(len(entry), 5, entry)
            self.assertIn(entry[4], known, entry[0])

    def test_physical_and_chemical_families(self):
        """pH and carbonate hardness sit with the physical measurements, as
        the nursery reads them: they come off the same handheld kit as
        temperature and salinity rather than a nutrient assay."""
        from harvester.nutrients import ANALYTES
        groups = {a[0]: a[4] for a in ANALYTES}
        for key in ("temp_c", "salinity_ppt", "ph", "dkh"):
            self.assertEqual(groups[key], "physical", key)
        for key in ("no3", "no2", "nh3", "po4", "ca", "mg"):
            self.assertEqual(groups[key], "chemical", key)

    def test_export_only_offers_groups_that_were_measured(self):
        store = Store("sqlite://:memory:")
        store.migrate()
        store.upsert_nutrients([
            {"sample_date": "2026-09-21", "sump_code": "SA12", "no3": 0.4},
        ])
        payload = build_payload(store, {})["nutrients"]
        self.assertEqual([g["key"] for g in payload["groups"]], ["chemical"])
        self.assertEqual([a["key"] for a in payload["analytes"]], ["no3"])
        store.close()

class TestReferenceRanges(unittest.TestCase):
    """The seawater reference ranges must stay internally consistent."""

    def setUp(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "config.json"), encoding="utf-8") as fh:
            self.config = json.load(fh)
        self.reference = {
            k: v for k, v in self.config["insitu_reference"].items()
            if not k.startswith("_")
        }

    def test_every_analyte_has_a_reference(self):
        from harvester.nutrients import ANALYTES
        for entry in ANALYTES:
            self.assertIn(entry[0], self.reference, entry[0])

    def test_typical_sits_inside_outer(self):
        for key, ref in self.reference.items():
            t, o = ref["typical"], ref["outer"]
            self.assertLess(t[0], t[1], key)
            self.assertLess(o[0], o[1], key)
            self.assertLessEqual(o[0], t[0], key)
            self.assertGreaterEqual(o[1], t[1], key)

    def test_every_reference_records_its_basis(self):
        for key, ref in self.reference.items():
            self.assertTrue(ref.get("basis"), key)

    def test_known_seawater_values_land_in_the_typical_band(self):
        """Textbook seawater should read as typical, not flagged."""
        seawater = {
            "salinity_ppt": 36.5,   # Strait of Gibraltar surface
            "ph": 8.1,              # surface ocean
            "dkh": 7.4,             # ~2570 umol/kg alkalinity
            "ca": 430.0,            # 412 mg/L at S=35, scaled
            "mg": 1345.0,
            "no3": 0.06,            # ~1 umol/L
            "no2": 0.005,
            "po4": 0.015,
        }
        for key, value in seawater.items():
            t = self.reference[key]["typical"]
            self.assertTrue(t[0] <= value <= t[1],
                            f"{key}={value} outside typical {t}")

    def test_reference_reaches_the_dashboard_payload(self):
        store = Store("sqlite://:memory:")
        store.migrate()
        store.upsert_nutrients([
            {"sample_date": "2026-09-21", "sump_code": "SA12", "ca": 578.0},
        ])
        analyte = build_payload(store, self.config)["nutrients"]["analytes"][0]
        self.assertEqual(analyte["key"], "ca")
        self.assertEqual(analyte["typical"], [400.0, 455.0])
        self.assertEqual(analyte["outer"], [360.0, 520.0])
        store.close()

class TestDerivedValues(unittest.TestCase):
    """Modelled values are checked against published tables, not just against
    themselves, and refuse to answer outside the fits' stated range."""

    def test_oxygen_saturation_matches_published_tables(self):
        from harvester.derived import oxygen_at_saturation
        # Benson and Krause values as tabulated by the USGS, mg/L at 1 atm
        for temp, salinity, expected in (
            (10.0, 0.0, 11.29),
            (20.0, 0.0, 9.08),
            (10.0, 35.0, 9.03),
            (20.0, 35.0, 7.38),
        ):
            got = oxygen_at_saturation(temp, salinity)
            self.assertAlmostEqual(got, expected, delta=0.03,
                                   msg=f"{temp}C S={salinity}: got {got}")

    def test_free_ammonia_fraction_is_a_few_percent_in_seawater(self):
        from harvester.derived import free_ammonia_fraction
        fraction = free_ammonia_fraction(17.0, 8.1, 36.5)
        self.assertTrue(0.02 < fraction < 0.05, fraction)

    def test_raising_ph_raises_the_free_ammonia_share(self):
        from harvester.derived import free_ammonia_fraction
        low = free_ammonia_fraction(17.0, 7.8, 36.5)
        high = free_ammonia_fraction(17.0, 8.4, 36.5)
        self.assertGreater(high, low * 2)

    def test_ammonium_needs_all_three_inputs(self):
        from harvester.derived import ammonium_from_free_ammonia
        self.assertIsNone(ammonium_from_free_ammonia(0.005, 17.0, None, 36.5))
        self.assertIsNone(ammonium_from_free_ammonia(0.005, None, 8.1, 36.5))
        self.assertIsNone(ammonium_from_free_ammonia(0.005, 17.0, 8.1, None))
        self.assertIsNone(ammonium_from_free_ammonia(None, 17.0, 8.1, 36.5))

    def test_zero_free_ammonia_gives_zero_ammonium(self):
        from harvester.derived import ammonium_from_free_ammonia
        self.assertEqual(ammonium_from_free_ammonia(0.0, 17.0, 8.1, 36.5), 0.0)

    def test_models_refuse_to_extrapolate(self):
        from harvester.derived import ammonium_from_free_ammonia, oxygen_at_saturation
        self.assertIsNone(oxygen_at_saturation(60.0, 36.0))
        self.assertIsNone(oxygen_at_saturation(20.0, 90.0))
        self.assertIsNone(ammonium_from_free_ammonia(0.005, 17.0, 12.0, 36.5))

    def test_ammonium_exceeds_free_ammonia_at_seawater_ph(self):
        """At pH ~8 most of the pool is ammonium, so NH4 should dwarf NH3."""
        from harvester.derived import ammonium_from_free_ammonia
        nh4 = ammonium_from_free_ammonia(0.005, 17.1, 7.98, 36.6)
        self.assertGreater(nh4, 0.005 * 10)

    def test_light_metrics_are_gone(self):
        from harvester.seneye import PARAMETERS
        for key in ("par", "lux", "kelvin"):
            self.assertNotIn(key, PARAMETERS)

    def test_derived_fields_reach_the_payload_and_are_flagged(self):
        store = Store("sqlite://:memory:")
        store.migrate()
        now = int(time.time())
        store.upsert_device("1", "SA12", 1, "SA12", "A", "SA12", now)
        store.insert_readings([{
            "device_id": "1", "reading_time": now - 60, "fetched_at": now,
            "temperature": 17.1, "ph": 7.98, "nh3": 0.005,
        }])
        store.upsert_nutrients([
            {"sample_date": "2026-09-21", "sump_code": "SA12", "salinity_ppt": 36.6},
        ])
        config = {
            "sumps": {"SA12": {"system": "A", "tanks": ["A1"]}},
            "derived": {"enabled": True, "default_salinity": 36.5},
            "parameters": {"nh4": {"label": "Ammonium", "model_note": "note"}},
        }
        payload = build_payload(store, config)
        by_key = {p["key"]: p for p in payload["parameters"]}
        self.assertIn("nh4", by_key)
        self.assertIn("o2_sat", by_key)
        self.assertTrue(by_key["nh4"]["modelled"])
        self.assertFalse(by_key["temperature"]["modelled"])
        self.assertEqual(by_key["nh4"]["model_note"], "note")
        store.close()

    def test_derived_can_be_switched_off(self):
        store = Store("sqlite://:memory:")
        store.migrate()
        now = int(time.time())
        store.upsert_device("1", "SA12", 1, "SA12", "A", "SA12", now)
        store.insert_readings([{
            "device_id": "1", "reading_time": now - 60, "fetched_at": now,
            "temperature": 17.1, "ph": 7.98, "nh3": 0.005,
        }])
        payload = build_payload(store, {"derived": {"enabled": False}})
        keys = [p["key"] for p in payload["parameters"]]
        self.assertNotIn("nh4", keys)
        self.assertNotIn("o2_sat", keys)
        store.close()


class TestSlideCountdown(unittest.TestCase):
    """When each sump's slide is due to be replaced."""

    DEVICES = [{"device_id": "1", "sump_code": "SA12"},
               {"device_id": "2", "sump_code": "SB34"}]
    NOW = int(dt.datetime(2026, 9, 21, 12, 0, tzinfo=dt.timezone.utc).timestamp())
    DUE_14_OCT = int(dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc).timestamp())

    def slides(self, cfg, latest=None):
        return _slides(self.DEVICES, latest or {}, {"slides": cfg}, self.NOW)

    def test_logged_change_date_plus_the_interval(self):
        out = self.slides({"interval_days": 30, "default_changed": "2026-09-14"})
        self.assertEqual(out["1"]["due"], self.DUE_14_OCT)
        self.assertEqual(out["1"]["source"], "logged")
        # The 14th of September plus 30 days, read on the 21st, is 23 days off.
        self.assertEqual(math.ceil((out["1"]["due"] - self.NOW) / 86400), 23)

    def test_a_per_sump_date_overrides_the_default(self):
        out = self.slides({"interval_days": 30, "default_changed": "2026-09-14",
                           "changed": {"SB34": "2026-09-20", "_comment": "ignored"}})
        self.assertEqual(out["1"]["due"], self.DUE_14_OCT)
        self.assertGreater(out["2"]["due"], out["1"]["due"])

    def test_the_sensor_expiry_wins_when_it_reports_one(self):
        reported = self.NOW + 5 * 86400
        out = self.slides({"interval_days": 30, "default_changed": "2026-09-14"},
                          {"1": {"slide_expires": reported, "slide_serial": "SLD-1"}})
        self.assertEqual(out["1"]["due"], reported)
        self.assertEqual(out["1"]["source"], "sensor")
        self.assertEqual(out["1"]["serial"], "SLD-1")
        self.assertEqual(out["2"]["source"], "logged")

    def test_an_absurd_sensor_expiry_is_ignored(self):
        out = self.slides({"interval_days": 30, "default_changed": "2026-09-14"},
                          {"1": {"slide_expires": 0}, "2": {"slide_expires": 4102444800}})
        self.assertEqual(out["1"]["source"], "logged")
        self.assertEqual(out["2"]["source"], "logged")

    def test_trust_sensor_false_goes_by_the_logged_dates_only(self):
        out = self.slides({"interval_days": 30, "default_changed": "2026-09-14",
                           "trust_sensor": False},
                          {"1": {"slide_expires": self.NOW + 5 * 86400}})
        self.assertEqual(out["1"]["due"], self.DUE_14_OCT)

    def test_no_date_anywhere_means_no_countdown_rather_than_a_guess(self):
        self.assertEqual(self.slides({"interval_days": 30}), {})
        self.assertEqual(self.slides({"default_changed": "not a date"}), {})

    def test_can_be_switched_off(self):
        self.assertEqual(
            self.slides({"enabled": False, "default_changed": "2026-09-14"}), {})

    def test_the_payload_carries_the_interval_and_warning_threshold(self):
        store = Store("sqlite://:memory:")
        store.migrate()
        now = int(time.time())
        store.upsert_device("1", "SA12", 1, "SA12", "A", "SA12", now)
        store.insert_readings([{
            "device_id": "1", "reading_time": now - 60, "fetched_at": now,
            "temperature": 17.1, "ph": 7.98, "nh3": 0.005,
        }])
        payload = build_payload(store, {"slides": {
            "interval_days": 30, "warn_days": 7, "default_changed": "2026-09-14"}})
        self.assertEqual(payload["slides"]["interval_days"], 30)
        self.assertEqual(payload["slides"]["warn_days"], 7)
        self.assertIn("1", payload["slides"]["by_device"])
        store.close()



# --------------------------------------------------------------------------
# Historical CSV import (tools/import_history.py)
# --------------------------------------------------------------------------

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import import_history as ih  # noqa: E402


class Args:
    """Stand-in for the argparse namespace import_file expects."""

    def __init__(self, **kw):
        self.timezone = "UTC"
        self.sump = None
        self.month_first = False
        self.tag = "IMPORTED"
        self.dry_run = False
        self.__dict__.update(kw)


CONFIG = {"devices": {
    "_comment": "ignored",
    "168779": {"sump": "SA12"},
    "166115": {"sump": "SA345"},
    "165185": {"sump": "SB12"},
}}


class TestImportColumns(unittest.TestCase):
    def test_matches_headings_with_units_and_punctuation(self):
        cols = ih.match_columns(
            ["Date/Time", "Device ID", "Temperature (\u00b0C)", "pH", "NH3 (mg/L)"])
        self.assertEqual(cols["timestamp"], 0)
        self.assertEqual(cols["device_id"], 1)
        self.assertEqual(cols["temperature"], 2)
        self.assertEqual(cols["ph"], 3)
        self.assertEqual(cols["nh3"], 4)

    def test_ammonium_is_not_mistaken_for_ammonia(self):
        cols = ih.match_columns(["Timestamp", "Free Ammonia", "Ammonium"])
        self.assertEqual(cols["nh3"], 1)
        self.assertEqual(cols["nh4"], 2)

    def test_unrecognised_columns_are_ignored(self):
        cols = ih.match_columns(["Timestamp", "Slide batch", "Kelvin", "PAR"])
        self.assertEqual(set(cols), {"timestamp"})


class TestImportDates(unittest.TestCase):
    def when(self, headings, row, **kw):
        return ih.parse_when(row, ih.match_columns(headings), **kw)

    def test_iso_timestamp(self):
        got = self.when(["Timestamp"], ["2025-03-04 09:15:00"])
        self.assertEqual(got, dt.datetime(2025, 3, 4, 9, 15))

    def test_unix_seconds_and_milliseconds(self):
        secs = self.when(["Timestamp"], ["1700000000"])
        millis = self.when(["Timestamp"], ["1700000000000"])
        self.assertEqual(secs, millis)

    def test_separate_date_and_time_columns(self):
        got = self.when(["Date", "Time"], ["04/03/2025", "09:15"])
        self.assertEqual(got, dt.datetime(2025, 3, 4, 9, 15))

    def test_date_and_time_columns_the_other_way_round(self):
        got = self.when(["Time", "Date"], ["09:15", "04/03/2025"])
        self.assertEqual(got, dt.datetime(2025, 3, 4, 9, 15))

    def test_day_first_is_the_default_and_month_first_is_opt_in(self):
        headings, row = ["Timestamp"], ["04/03/2025 09:15"]
        self.assertEqual(self.when(headings, row).month, 3)
        self.assertEqual(self.when(headings, row, dayfirst=False).month, 4)

    def test_unreadable_date_is_none_rather_than_a_guess(self):
        self.assertIsNone(self.when(["Timestamp"], ["last Tuesday"]))

    def test_timezone_is_applied_when_given(self):
        naive = dt.datetime(2025, 7, 1, 12, 0)
        utc = ih.to_unix(naive, "UTC")
        gib = ih.to_unix(naive, "Europe/Gibraltar")
        self.assertEqual(utc - gib, 7200)  # BST-equivalent summer offset


class TestImportDeviceMatching(unittest.TestCase):
    def resolve(self, headings, row, sump=None, filename="export.csv"):
        return ih.device_for(row, ih.match_columns(headings), CONFIG, sump, filename)

    def test_device_id_column(self):
        self.assertEqual(self.resolve(["Device ID"], ["166115"])[0], "166115")

    def test_sump_named_in_a_column(self):
        self.assertEqual(self.resolve(["Sump"], ["Sump SA12"])[0], "168779")

    def test_longer_sump_code_wins(self):
        self.assertEqual(self.resolve(["Sump"], ["SA345"])[0], "166115")

    def test_falls_back_to_the_sump_argument_then_the_filename(self):
        self.assertEqual(self.resolve(["Note"], ["x"], sump="SB12")[0], "165185")
        self.assertEqual(
            self.resolve(["Note"], ["x"], filename="/tmp/seneye_SA345_2025.csv")[0],
            "166115")

    def test_unknown_device_is_reported_not_guessed(self):
        device, problem = self.resolve(["Device ID"], ["999999"])
        self.assertIsNone(device)
        self.assertIn("999999", problem)


class TestImportEndToEnd(unittest.TestCase):
    def write(self, name, text):
        path = os.path.join(self.dir.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store("sqlite://:memory:")
        self.store.migrate()

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_imports_and_is_safe_to_run_twice(self):
        path = self.write("history.csv", (
            "Seneye Home export\n"
            "Date/Time,Device ID,Temperature (\u00b0C),pH,NH3 (mg/L)\n"
            "2025-03-04 09:15:00,168779,17.2,8.01,0.004\n"
            "2025-03-04 10:15:00,168779,17.4,8.02,0.005\n"
            "2025-03-04 09:15:00,166115,17.9,7.95,0.006\n"
        ))
        read, written, skipped = ih.import_file(path, self.store, CONFIG, Args())
        self.assertEqual((read, written), (3, 3))
        self.assertEqual(skipped, {})
        again = ih.import_file(path, self.store, CONFIG, Args())
        self.assertEqual(again[1], 0)
        rows = self.store.query("SELECT * FROM readings ORDER BY reading_time")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["slide_serial"], "IMPORTED")
        self.assertAlmostEqual(rows[0]["temperature"], 17.2)

    def test_dry_run_writes_nothing(self):
        path = self.write("history.csv", (
            "Timestamp,Device ID,Temp,pH,NH3\n"
            "2025-03-04 09:15,168779,17.2,8.01,0.004\n"
        ))
        read, written, _ = ih.import_file(path, self.store, CONFIG, Args(dry_run=True))
        self.assertEqual((read, written), (1, 0))
        self.assertEqual(self.store.query("SELECT * FROM readings"), [])

    def test_one_file_per_device_with_the_sump_in_the_filename(self):
        path = self.write("SA345.csv", (
            "Date,Time,Temperature,pH,Free Ammonia\n"
            "04/03/2025,09:15,17.9,7.95,0.006\n"
        ))
        read, written, _ = ih.import_file(path, self.store, CONFIG, Args())
        self.assertEqual((read, written), (1, 1))
        rows = self.store.query("SELECT device_id FROM readings")
        self.assertEqual(rows[0]["device_id"], "166115")

    def test_bad_rows_are_counted_rather_than_aborting_the_file(self):
        path = self.write("history.csv", (
            "Timestamp,Device ID,Temp,pH,NH3\n"
            "2025-03-04 09:15,168779,17.2,8.01,0.004\n"
            "not a date,168779,17.3,8.01,0.004\n"
            "2025-03-04 11:15,999999,17.4,8.01,0.004\n"
            "2025-03-04 12:15,168779,,,\n"
        ))
        read, written, skipped = ih.import_file(path, self.store, CONFIG, Args())
        self.assertEqual((read, written), (1, 1))
        self.assertEqual(skipped["unreadable date"], 1)
        self.assertEqual(skipped["no values"], 1)
        self.assertEqual(sum(skipped.values()), 3)

    def test_registers_a_device_the_harvester_has_never_polled(self):
        path = self.write("history.csv", (
            "Timestamp,Device ID,Temp,pH,NH3\n"
            "2025-03-04 09:15,166115,17.2,8.01,0.004\n"
        ))
        ih.import_file(path, self.store, dict(CONFIG, sumps={
            "SA345": {"system": "A", "tanks": ["A3", "A4", "A5"]}}), Args())
        rows = self.store.query("SELECT * FROM devices")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sump_code"], "SA345")
        self.assertEqual(rows[0]["system_code"], "A")
        self.assertIn("A3", rows[0]["label"])

    def test_an_import_does_not_wind_back_last_seen_on_a_known_device(self):
        now = int(time.time())
        self.store.upsert_device("168779", "SA12", 1, "SA12", "A", "SA12", now)
        path = self.write("history.csv", (
            "Timestamp,Device ID,Temp,pH,NH3\n"
            "2025-03-04 09:15,168779,17.2,8.01,0.004\n"
        ))
        ih.import_file(path, self.store, CONFIG, Args())
        rows = self.store.query("SELECT * FROM devices")
        self.assertEqual(rows[0]["last_seen"], now)

    def test_dry_run_reports_the_overlap_with_what_is_already_stored(self):
        now = int(time.time() // 1800 * 1800)
        self.store.upsert_device("168779", "SA12", 1, "SA12", "A", "SA12", now)
        self.store.insert_readings([{
            "device_id": "168779", "reading_time": now - 1800, "fetched_at": now,
            "temperature": 17.2, "ph": 8.01, "nh3": 0.004,
        }])
        when = dt.datetime.fromtimestamp(now - 1800, dt.timezone.utc)
        path = self.write("history.csv", (
            "Timestamp,Device ID,Temp,pH,NH3\n"
            + when.strftime("%Y-%m-%d %H:%M") + ",168779,17.2,8.01,0.004\n"
            + (when - dt.timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")
            + ",168779,17.3,8.02,0.004\n"
        ))
        read, written, skipped = ih.import_file(path, self.store, CONFIG, Args(dry_run=True))
        self.assertEqual((read, written), (2, 0))
        self.assertEqual(skipped["_already"], 1)

    def test_a_shifted_export_collides_but_does_not_agree(self):
        """The timezone check: same timestamps, different numbers."""
        now = int(time.time() // 1800 * 1800)
        self.store.upsert_device("168779", "SA12", 1, "SA12", "A", "SA12", now)
        rows = []
        for i in range(8):
            rows.append({"device_id": "168779", "reading_time": now - i * 1800,
                         "fetched_at": now, "temperature": 17.0 + i * 0.5,
                         "ph": 8.0, "nh3": 0.004})
        self.store.insert_readings(rows)
        batch_same = [{"device_id": "168779", "reading_time": r["reading_time"],
                       "temperature": r["temperature"], "ph": 8.0, "nh3": 0.004}
                      for r in rows]
        already, agreeing = ih.compare_existing(self.store, batch_same)
        self.assertEqual((already, agreeing), (8, 8))

        # The same readings shifted by an hour: they still land on stored
        # timestamps, but on the wrong ones, so the values no longer agree.
        batch_shifted = [{"device_id": "168779",
                          "reading_time": r["reading_time"] - 3600,
                          "temperature": r["temperature"], "ph": 8.0,
                          "nh3": 0.004} for r in rows]
        already, agreeing = ih.compare_existing(self.store, batch_shifted)
        self.assertGreater(already, 0)
        self.assertLess(agreeing, already)

    def test_repeated_timestamps_collapse_to_their_median(self):
        """Seneye's own exports log the same minute more than once."""
        path = self.write("history.csv", (
            "Declared,Temperature,NH3,pH\n"
            "21/09/2026 02:11,15.375,0.001,7.94\n"
            "21/09/2026 02:11,15.25,0.001,7.94\n"
            "21/09/2026 02:11,15.375,0.001,8.62\n"
            "21/09/2026 03:11,15.5,0.001,7.95\n"
        ))
        read, written, _ = ih.import_file(path, self.store, CONFIG, Args(sump="SA12"))
        self.assertEqual((read, written), (2, 2))
        rows = self.store.query("SELECT * FROM readings ORDER BY reading_time")
        # median of 7.94, 7.94, 8.62 is 7.94, not whichever row came first
        self.assertAlmostEqual(rows[0]["ph"], 7.94)
        self.assertAlmostEqual(rows[0]["temperature"], 15.375)

    def test_seneye_declared_column_is_recognised(self):
        path = self.write("SB12.csv", (
            "Declared,Temperature,NH3,pH\n"
            "22/09/2026 14:58,14.5,0.001,7.93\n"
        ))
        read, written, skipped = ih.import_file(path, self.store, CONFIG, Args())
        self.assertEqual((read, written), (1, 1))
        self.assertEqual(skipped, {})

    def test_a_file_with_no_usable_header_is_skipped_quietly(self):
        path = self.write("notes.csv", "some,random,notes\na,b,c\n")
        self.assertEqual(ih.import_file(path, self.store, CONFIG, Args()),
                         (0, 0, {}))



# ---------------------------------------------------------------------------
# Smart plugs and the nursery air sensor
# ---------------------------------------------------------------------------

PLUG_CONFIG = {
    "plugs": {
        "enabled": True,
        "region": "eu",
        "stale_minutes": 90,
        "reference": {"air_temperature": {"band": [12, 28]}},
        "devices": {
            "_comment": "ignored",
            "plugD": {
                "label": "Row D chillers",
                "kind": "switch",
                "role": "chiller",
                "sockets": {
                    "_comment": "ignored",
                    "switch_1": {"sump": "SD12", "role": "chiller"},
                    "switch_2": {"sump": "SD345", "role": "chiller"},
                },
            },
            "plugE": {
                "label": "Row E chillers",
                "kind": "switch",
                "role": "chiller",
                "sockets": {"switch_1": {"sump": "SE12", "role": "chiller"}},
            },
            "airSensor": {
                "label": "Nursery air",
                "kind": "ambient",
                "location": "inside the nursery",
                "scales": {"air_temperature": 10, "humidity": 1},
            },
        },
    }
}


class FakeTuya:
    """Stands in for the cloud so the tests never touch the network."""

    def __init__(self, codes, online=True, update_time=None):
        self.codes = codes
        self.online = online
        self.update_time = update_time

    def all_devices(self, limit=200):
        return [{"id": i, "name": i, "product_name": "thing",
                 "online": self.online, "update_time": self.update_time,
                 "active_time": None}
                for i in self.codes]

    def status(self, ids):
        return {i: dict(self.codes.get(i, {})) for i in ids if i in self.codes}

    def info(self, ids):
        return {
            i: {"name": i, "online": 1 if self.online else 0,
                "product_name": "plug", "update_time": self.update_time}
            for i in ids
        }


def plug_codes(d1=True, d2=False, e1=True, temp=213, hum=57):
    return {
        "plugD": {"switch_1": d1, "switch_2": d2, "cur_power": 1124},
        "plugE": {"switch_1": e1},
        "airSensor": {"va_temperature": temp, "va_humidity": hum,
                      "battery_percentage": 88},
    }


class TestPlugPolling(unittest.TestCase):
    def test_every_configured_socket_is_reported_with_its_sump(self):
        result = plugs.poll(FakeTuya(plug_codes()), PLUG_CONFIG, now=1000)
        by_sump = {s.sump_code: s for s in result.sockets}
        self.assertEqual(set(by_sump), {"SD12", "SD345", "SE12"})
        self.assertEqual(by_sump["SD12"].on_state, 1)
        self.assertEqual(by_sump["SD345"].on_state, 0)
        self.assertEqual(by_sump["SD12"].role, "chiller")

    def test_the_comment_keys_in_config_are_not_treated_as_devices(self):
        result = plugs.poll(FakeTuya(plug_codes()), PLUG_CONFIG, now=1000)
        self.assertNotIn("_comment", {s.device_id for s in result.sockets})
        self.assertNotIn("_comment", {s.socket for s in result.sockets})

    def test_ambient_values_are_divided_by_the_configured_scale(self):
        result = plugs.poll(FakeTuya(plug_codes(temp=213, hum=57)), PLUG_CONFIG, now=1000)
        self.assertEqual(len(result.ambient), 1)
        reading = result.ambient[0]
        self.assertAlmostEqual(reading.air_temperature, 21.3)
        self.assertAlmostEqual(reading.humidity, 57.0)

    def test_a_shared_meter_goes_to_the_only_socket_drawing(self):
        # plug_codes has switch_1 on and switch_2 off, so the plug's 112.4 W is
        # unambiguously the first chiller's.
        result = plugs.poll(FakeTuya(plug_codes(d1=True, d2=False)), PLUG_CONFIG, now=1000)
        by_socket = {s.socket: s for s in result.sockets if s.device_id == "plugD"}
        self.assertAlmostEqual(by_socket["switch_1"].power_w, 112.4)
        self.assertIsNone(by_socket["switch_2"].power_w)

    def test_a_shared_meter_is_not_split_when_both_sockets_are_on(self):
        # One figure cannot be divided between two chillers, so claiming it for
        # both would double the nursery's apparent draw and halving it would be
        # an invention. The plug total is kept; neither socket claims it.
        result = plugs.poll(FakeTuya(plug_codes(d1=True, d2=True)), PLUG_CONFIG, now=1000)
        doubles = [s for s in result.sockets if s.device_id == "plugD"]
        self.assertTrue(all(s.power_w is None for s in doubles))
        self.assertTrue(all(s.plug_power_w == 112.4 for s in doubles))

    def test_an_offline_plug_carries_the_time_contact_was_lost(self):
        # A plug that dropped off the network days ago is still reporting the
        # state it held then. Timestamping that with the moment we polled would
        # present a four-day-old reading as current.
        lost = 1000 - 4 * 86400
        result = plugs.poll(FakeTuya(plug_codes(), online=False, update_time=lost),
                            PLUG_CONFIG, now=1000)
        switches = [s for s in result.sockets]
        self.assertTrue(all(s.reading_time == 1000 for s in switches))
        self.assertTrue(all(s.last_contact == lost for s in switches))
        self.assertTrue(all(s.online == 0 for s in switches))

    def test_an_online_plug_is_in_contact_now_whatever_its_record_says(self):
        result = plugs.poll(FakeTuya(plug_codes(), update_time=1000 - 6 * 3600),
                            PLUG_CONFIG, now=1000)
        self.assertTrue(all(s.last_contact == 1000 for s in result.sockets))

    def test_a_device_that_answers_nothing_is_listed_as_offline(self):
        codes = plug_codes()
        codes.pop("plugE")
        result = plugs.poll(FakeTuya(codes, online=False), PLUG_CONFIG, now=1000)
        self.assertIn("plugE", result.offline)

    def test_a_switch_is_timestamped_when_it_answered_not_when_it_last_changed(self):
        # Tuya's update_time on a switch is the last time the device record
        # changed, so a chiller nobody has touched for a fortnight reports a
        # fortnight-old timestamp while working perfectly. Reading that as a
        # heartbeat marked every plug in the nursery as dead.
        stale = 1000 - 14 * 86400
        result = plugs.poll(FakeTuya(plug_codes(), update_time=stale),
                            PLUG_CONFIG, now=1000)
        self.assertTrue(all(s.reading_time == 1000 for s in result.sockets))

    def test_a_poll_is_a_reading_and_the_report_time_is_kept_beside_it(self):
        # Keying the stored reading on the sensor's update_time meant every
        # poll that found the same value carried the same timestamp, the
        # insert deduplicated it away, and a fortnight of monitoring produced
        # two stored readings. The air was measured every half hour and almost
        # all of it was thrown out.
        result = plugs.poll(FakeTuya(plug_codes(), update_time=940),
                            PLUG_CONFIG, now=1000)
        self.assertEqual(result.ambient[0].reading_time, 1000)
        self.assertEqual(result.ambient[0].reported_at, 940)

    def test_a_long_silent_sensor_still_gets_a_current_reading_time(self):
        old = 1000 - 9 * 86400
        result = plugs.poll(FakeTuya(plug_codes(), update_time=old),
                            PLUG_CONFIG, now=1000)
        self.assertEqual(result.ambient[0].reading_time, 1000)
        self.assertEqual(result.ambient[0].reported_at, old)

    def test_a_device_that_returns_no_data_points_is_marked_offline(self):
        codes = plug_codes()
        codes.pop("plugE")
        result = plugs.poll(FakeTuya(codes), PLUG_CONFIG, now=1000)
        quiet = [s for s in result.sockets if s.device_id == "plugE"]
        self.assertTrue(quiet)
        self.assertTrue(all(s.online == 0 for s in quiet))

    def test_an_absurd_device_timestamp_falls_back_to_the_clock(self):
        # A sensor reporting a date in 2038 should not make the dashboard say
        # its reading is fresh for the next twelve years.
        result = plugs.poll(FakeTuya(plug_codes(), update_time=2 ** 31),
                            PLUG_CONFIG, now=1000)
        self.assertEqual(result.ambient[0].reading_time, 1000)
        self.assertEqual(result.ambient[0].reported_at, 1000)

    def test_nothing_is_polled_when_no_devices_are_configured(self):
        result = plugs.poll(FakeTuya({}), {"plugs": {"enabled": True}}, now=1000)
        self.assertEqual(result.sockets, [])
        self.assertEqual(result.ambient, [])

    def test_the_inventory_marks_an_id_config_does_not_know(self):
        codes = plug_codes()
        codes["bfNEWsensor"] = {"va_temperature": 210}
        text = plugs.inventory(FakeTuya(codes), PLUG_CONFIG)
        self.assertIn("NEW bfNEWsensor", text)
        self.assertIn("not in config.json", text)

    def test_the_inventory_names_a_configured_id_that_has_vanished(self):
        codes = plug_codes()
        codes.pop("airSensor")
        text = plugs.inventory(FakeTuya(codes), PLUG_CONFIG)
        self.assertIn("NOT on the account any more", text)
        self.assertIn("airSensor", text)

    def test_a_previous_id_is_recognised_rather_than_flagged_as_new(self):
        config = json.loads(json.dumps(PLUG_CONFIG))
        config["plugs"]["devices"]["airSensor"]["previous_ids"] = ["bfOLDsensor"]
        codes = plug_codes()
        codes["bfOLDsensor"] = {"va_temperature": 210}
        text = plugs.inventory(FakeTuya(codes), config)
        self.assertIn("previous ID", text)
        self.assertNotIn("NEW bfOLDsensor", text)

    def test_describe_lists_the_switch_codes_to_map(self):
        text = plugs.describe(FakeTuya(plug_codes()), ["plugD"])
        self.assertIn("switch_1", text)
        self.assertIn("switch_2", text)
        self.assertIn("sockets", text)


class TestPlugReconcile(unittest.TestCase):
    """Telling "off the network" apart from "no longer exists"."""

    def reconcile(self, codes, account_ids, raises=False):
        class Client(FakeTuya):
            def all_devices(inner, limit=200):
                if raises:
                    raise plugs.TuyaError("no")
                return [{"id": i, "name": i, "product_name": "thing",
                         "online": True, "update_time": None, "active_time": None}
                        for i in account_ids]
        client = Client(codes)
        return plugs.reconcile(client, PLUG_CONFIG,
                               plugs.poll(client, PLUG_CONFIG, now=1000))

    def test_nothing_is_fetched_when_every_device_answered(self):
        # The account listing costs a request, so it is only worth making when
        # something has actually gone quiet.
        out = self.reconcile(plug_codes(), [], raises=True)
        self.assertEqual(out, {"gone": [], "candidates": []})

    def test_a_plug_off_the_network_is_not_reported_as_gone(self):
        # Rows C and E behave exactly like this: unreachable, but still
        # registered and still answering with their last known state.
        codes = plug_codes()
        codes.pop("plugE")
        out = self.reconcile(codes, ["plugD", "plugE", "airSensor"])
        self.assertEqual(out["gone"], [])

    def test_a_reset_device_is_reported_as_gone_with_candidates(self):
        codes = plug_codes()
        codes.pop("airSensor")
        out = self.reconcile(codes, ["plugD", "plugE", "bfNEWsensor"])
        self.assertEqual(out["gone"], ["airSensor"])
        self.assertEqual([c["id"] for c in out["candidates"]], ["bfNEWsensor"])

    def test_a_previous_id_is_not_offered_as_a_candidate(self):
        config = json.loads(json.dumps(PLUG_CONFIG))
        config["plugs"]["devices"]["airSensor"]["previous_ids"] = ["bfOLD"]
        codes = plug_codes()
        codes.pop("plugE")

        class Client(FakeTuya):
            def all_devices(inner, limit=200):
                return [{"id": i, "name": i, "product_name": "thing",
                         "online": True, "update_time": None, "active_time": None}
                        for i in ("plugD", "plugE", "airSensor", "bfOLD")]
        client = Client(codes)
        out = plugs.reconcile(client, config, plugs.poll(client, config, now=1000))
        self.assertNotIn("bfOLD", [c["id"] for c in out["candidates"]])

    def test_a_listing_that_cannot_be_read_says_so_rather_than_guessing(self):
        codes = plug_codes()
        codes.pop("airSensor")
        out = self.reconcile(codes, [], raises=True)
        self.assertEqual(out["gone"], [])
        self.assertEqual(out["unchecked"], ["airSensor"])


class TestSwapDevice(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(json.dumps(PLUG_CONFIG))

    def test_the_entry_moves_and_the_old_id_is_remembered(self):
        out = swap_device.swap(self.config, "airSensor", "bfNEW")
        devices = out["plugs"]["devices"]
        self.assertNotIn("airSensor", devices)
        self.assertIn("bfNEW", devices)
        self.assertEqual(devices["bfNEW"]["previous_ids"], ["airSensor"])
        self.assertEqual(devices["bfNEW"]["label"], "Nursery air")

    def test_the_order_of_the_other_devices_is_untouched(self):
        before = [k for k in self.config["plugs"]["devices"]]
        after = [k for k in swap_device.swap(self.config, "plugD", "bfNEW")
                 ["plugs"]["devices"]]
        self.assertEqual(len(before), len(after))
        self.assertEqual(
            [k for k in after if k != "bfNEW"],
            [k for k in before if k != "plugD"])

    def test_a_device_reset_twice_keeps_the_whole_chain(self):
        once = swap_device.swap(self.config, "airSensor", "bfSECOND")
        twice = swap_device.swap(once, "bfSECOND", "bfTHIRD")
        self.assertEqual(twice["plugs"]["devices"]["bfTHIRD"]["previous_ids"],
                         ["airSensor", "bfSECOND"])

    def test_an_unknown_old_id_is_refused_rather_than_silently_added(self):
        with self.assertRaises(SystemExit):
            swap_device.swap(self.config, "neverConfigured", "bfNEW")

    def test_swapping_onto_an_id_already_in_use_is_refused(self):
        with self.assertRaises(SystemExit):
            swap_device.swap(self.config, "airSensor", "plugD")

    def test_a_malformed_id_never_reaches_the_config(self):
        # A typo here would point the harvester at nothing, which is the exact
        # failure this tool exists to repair.
        for bad in ("nope", "bad id", "BF79A8D7FBE41CC23CDPXQ", ""):
            with self.assertRaises(SystemExit):
                swap_device.main(["--old", "airSensor", "--new", bad])

    def test_an_end_to_end_swap_rewrites_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"plugs": {"devices": {
                    "bf0dee5c3a68c22ad66baz": {"label": "Nursery air",
                                               "kind": "ambient"}}}}, fh)
            swap_device.main(["--old", "bf0dee5c3a68c22ad66baz",
                              "--new", "bf79a8d7fbe41cc23cdpxq",
                              "--config", path])
            with open(path, encoding="utf-8") as fh:
                after = json.load(fh)
            entry = after["plugs"]["devices"]["bf79a8d7fbe41cc23cdpxq"]
            self.assertEqual(entry["previous_ids"], ["bf0dee5c3a68c22ad66baz"])

    def test_a_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            payload = {"plugs": {"devices": {
                "bf0dee5c3a68c22ad66baz": {"label": "Nursery air"}}}}
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            swap_device.main(["--old", "bf0dee5c3a68c22ad66baz",
                              "--new", "bf79a8d7fbe41cc23cdpxq",
                              "--config", path, "--dry-run"])
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), payload)


class TestSensorCadence(unittest.TestCase):
    """Reading the sensor's own reporting rate out of the stored readings."""

    def report(self, times, harvest=30, recent=0):
        rows = [{"reading_time": t, "reported_at": t} for t in times]
        return "\n".join(
            sensor_cadence.report(rows, "sensor", "UTC", harvest, recent))

    def test_one_reading_says_so_rather_than_inventing_a_rate(self):
        self.assertIn("not enough yet", self.report([1000]))

    def test_a_steady_half_hourly_sensor_reads_as_half_hourly(self):
        text = self.report([1000 + i * 1800 for i in range(20)])
        self.assertIn("median gap 30 min", text)

    def test_gaps_at_the_harvest_interval_are_flagged_as_unmeasurable(self):
        # The harvester cannot see a sensor reporting faster than it polls, so
        # the figures are a floor on the true rate, not a measurement of it.
        text = self.report([1000 + i * 1800 for i in range(10)], harvest=30)
        self.assertIn("9 of 9 gaps are at or below the 30-minute", text)

    def test_a_long_silence_is_surfaced_separately(self):
        times = [1000 + i * 1800 for i in range(10)]
        times.append(times[-1] + 30 * 3600)
        text = self.report(times)
        self.assertIn("longest silences", text)
        self.assertIn("30.0 h", text)

    def test_duplicate_timestamps_do_not_become_zero_length_gaps(self):
        text = self.report([1000, 1000, 2800, 4600])
        self.assertIn("median gap 30 min", text)

    def test_stored_readings_and_sensor_reports_are_counted_separately(self):
        # 48 polls of a sensor that moved three times is 48 readings and 3
        # reports, and saying "48" would answer the wrong question: it would
        # only describe how often the harvester runs.
        rows = [{"reading_time": 1000 + i * 1800,
                 "reported_at": 1000 + (i // 16) * 28800} for i in range(48)]
        text = "\n".join(sensor_cadence.report(rows, "sensor", "UTC", 30, 0))
        self.assertIn("48 reading(s) stored, 3 distinct report(s)", text)

    def test_a_sensor_that_never_moved_says_so_rather_than_inventing_a_rate(self):
        rows = [{"reading_time": 1000 + i * 1800, "reported_at": 900}
                for i in range(20)]
        text = "\n".join(sensor_cadence.report(rows, "sensor", "UTC", 30, 0))
        self.assertIn("not enough yet", text)

    def test_human_reads_in_the_unit_that_suits_the_size(self):
        self.assertEqual(sensor_cadence.human(45), "45 s")
        self.assertEqual(sensor_cadence.human(1800), "30 min")
        self.assertEqual(sensor_cadence.human(9000), "2.5 h")
        self.assertEqual(sensor_cadence.human(180000), "2.1 days")


class TestPlugSigning(unittest.TestCase):
    def setUp(self):
        self.client = plugs.TuyaClient("id123", "secret456", region="eu")

    def test_missing_credentials_are_refused_before_any_request(self):
        with self.assertRaises(plugs.TuyaError):
            plugs.TuyaClient("", "")

    def test_query_parameters_are_sorted_for_the_signature(self):
        self.assertEqual(
            self.client._canonical("GET", "/v1.0/x", {"b": "2", "a": "1"}),
            "/v1.0/x?a=1&b=2",
        )

    def test_the_signature_is_uppercase_hex_and_depends_on_the_token(self):
        without = self.client._sign("GET", "/v1.0/token?grant_type=1", "", None)
        self.assertRegex(without["sign"], r"^[0-9A-F]{64}$")
        self.assertNotIn("access_token", without)
        with_token = self.client._sign("GET", "/v1.0/devices", "", "tok")
        self.assertEqual(with_token["access_token"], "tok")
        self.assertNotEqual(without["sign"], with_token["sign"])

    def test_the_region_name_picks_the_data_centre(self):
        self.assertEqual(plugs.TuyaClient("a", "b", "eu").base,
                         "https://openapi.tuyaeu.com")
        self.assertEqual(plugs.TuyaClient("a", "b", "us").base,
                         "https://openapi.tuyaus.com")
        # An unknown region falls back to Central Europe rather than crashing a
        # scheduled harvest over a typo in config.json.
        self.assertEqual(plugs.TuyaClient("a", "b", "nowhere").base,
                         "https://openapi.tuyaeu.com")


class TestPlugStorage(unittest.TestCase):
    def setUp(self):
        self.store = Store("sqlite:///:memory:")
        self.store.migrate()

    def poll_into(self, codes, when):
        result = plugs.poll(FakeTuya(codes, update_time=when), PLUG_CONFIG, now=when)
        return (self.store.insert_plug_states(s.as_row() for s in result.sockets),
                self.store.insert_ambient(a.as_row() for a in result.ambient))

    def test_only_changes_are_recorded(self):
        first, _ = self.poll_into(plug_codes(), 1000)
        self.assertEqual(first, 3)
        again, _ = self.poll_into(plug_codes(), 2800)
        self.assertEqual(again, 0)
        rows = self.store.query("SELECT * FROM plug_states")
        self.assertEqual(len(rows), 3)

    def test_a_switch_writes_one_new_row_and_keeps_the_old_one(self):
        self.poll_into(plug_codes(d2=False), 1000)
        changed, _ = self.poll_into(plug_codes(d2=True), 2800)
        self.assertEqual(changed, 1)
        rows = self.store.query(
            "SELECT * FROM plug_states WHERE socket = 'switch_2' ORDER BY changed_at"
        )
        self.assertEqual([r["on_state"] for r in rows], [0, 1])
        self.assertEqual(rows[1]["changed_at"], 2800)

    def test_an_offline_plug_goes_stale_even_though_its_state_is_unchanged(self):
        # The bug this guards: last_seen used to be floored at the state's own
        # timestamp, so a plug that lost the network days ago kept reporting a
        # fresh last_seen and the dashboard trusted a stale state.
        self.poll_into(plug_codes(), 100000)
        result = plugs.poll(FakeTuya(plug_codes(), online=False, update_time=90000),
                            PLUG_CONFIG, now=100000)
        self.store.insert_plug_states(s.as_row() for s in result.sockets)
        row = self.store.query(
            "SELECT * FROM plug_states WHERE device_id = 'plugE' "
            "ORDER BY changed_at DESC LIMIT 1")[0]
        self.assertEqual(row["last_seen"], 90000)
        self.assertEqual(row["online"], 0)

    def test_a_column_added_after_release_is_migrated_in(self):
        # A database written before plug_power_w existed must gain the column
        # rather than failing every insert from then on.
        store = Store("sqlite:///:memory:")
        with store.cursor() as cur:
            cur.execute("CREATE TABLE plug_states (device_id TEXT, socket TEXT, "
                        "changed_at INTEGER, last_seen INTEGER, sump_code TEXT, "
                        "role TEXT, on_state INTEGER, online INTEGER, power_w REAL, "
                        "PRIMARY KEY (device_id, socket, changed_at))")
        store.migrate()
        result = plugs.poll(FakeTuya(plug_codes()), PLUG_CONFIG, now=1000)
        self.assertEqual(store.insert_plug_states(s.as_row() for s in result.sockets), 3)
        cols = {r["name"] for r in store.query("PRAGMA table_info(plug_states)")}
        self.assertIn("plug_power_w", cols)

    def test_migrating_twice_is_harmless(self):
        store = Store("sqlite:///:memory:")
        store.migrate()
        store.migrate()
        cols = {r["name"] for r in store.query("PRAGMA table_info(plug_states)")}
        self.assertIn("plug_power_w", cols)

    def test_last_seen_advances_while_the_state_holds(self):
        self.poll_into(plug_codes(), 1000)
        self.poll_into(plug_codes(), 5000)
        row = self.store.query(
            "SELECT * FROM plug_states WHERE socket = 'switch_1' AND device_id = 'plugE'"
        )[0]
        self.assertEqual(row["changed_at"], 1000)
        self.assertEqual(row["last_seen"], 5000)

    def test_a_change_reported_with_a_stale_timestamp_still_sorts_forward(self):
        self.poll_into(plug_codes(d2=False), 5000)
        self.poll_into(plug_codes(d2=True), 4000)
        rows = self.store.query(
            "SELECT * FROM plug_states WHERE socket = 'switch_2' ORDER BY changed_at"
        )
        self.assertEqual(len(rows), 2)
        self.assertGreater(rows[1]["changed_at"], rows[0]["changed_at"])

    def test_ambient_readings_are_not_duplicated(self):
        self.poll_into(plug_codes(), 1000)
        _, second = self.poll_into(plug_codes(), 1000)
        self.assertEqual(second, 0)

    def test_an_ambient_row_with_no_values_is_not_stored(self):
        codes = plug_codes()
        codes["airSensor"] = {"battery_percentage": 90}
        _, written = self.poll_into(codes, 1000)
        self.assertEqual(written, 0)


class TestPlugExport(unittest.TestCase):
    def setUp(self):
        self.store = Store("sqlite:///:memory:")
        self.store.migrate()
        self.now = int(time.time())
        config = dict(PLUG_CONFIG)
        for offset, codes in ((-7200, plug_codes(d2=False)), (-3600, plug_codes(d2=True))):
            when = self.now + offset
            result = plugs.poll(FakeTuya(codes, update_time=when), config, now=when)
            self.store.insert_plug_states(s.as_row() for s in result.sockets)
            self.store.insert_ambient(a.as_row() for a in result.ambient)

    def payload(self):
        return build_payload(self.store, PLUG_CONFIG, window_days=30, raw_days=30)

    def test_each_sump_gets_its_current_socket_state(self):
        by_sump = self.payload()["plugs"]["by_sump"]
        self.assertEqual(by_sump["SD12"][0]["on"], 1)
        self.assertEqual(by_sump["SD345"][0]["on"], 1)

    def test_since_is_the_moment_the_state_changed_not_the_last_poll(self):
        entry = self.payload()["plugs"]["by_sump"]["SD345"][0]
        self.assertEqual(entry["since"], self.now - 3600)

    def test_a_plug_not_heard_from_recently_is_marked_stale(self):
        store = Store("sqlite:///:memory:")
        store.migrate()
        old = self.now - 6 * 3600
        result = plugs.poll(FakeTuya(plug_codes(), update_time=old), PLUG_CONFIG, now=old)
        store.insert_plug_states(s.as_row() for s in result.sockets)
        entry = build_payload(store, PLUG_CONFIG, 30, 30)["plugs"]["by_sump"]["SD12"][0]
        self.assertTrue(entry["stale"])

    def test_the_transition_log_is_exported_for_the_recent_window(self):
        history = self.payload()["plugs"]["history"]
        switches = [h for h in history if h["socket"] == "switch_2"]
        self.assertEqual([h["on"] for h in switches], [0, 1])

    def test_ambient_is_exported_with_its_reference_band(self):
        ambient = self.payload()["ambient"]
        self.assertTrue(ambient["enabled"])
        keys = [p["key"] for p in ambient["parameters"]]
        self.assertIn("air_temperature", keys)
        band = [p["band"] for p in ambient["parameters"] if p["key"] == "air_temperature"][0]
        self.assertEqual(band, [12, 28])

    def test_air_readings_never_join_the_water_parameters(self):
        # The air sensor measures the room, not a sump. Letting it into the
        # parameter list would put a tenth row in the overview table that no
        # amount of chiller would ever bring into band.
        payload = self.payload()
        self.assertNotIn("air_temperature", [p["key"] for p in payload["parameters"]])
        self.assertNotIn("airSensor", [d["device_id"] for d in payload["devices"]])

    def test_the_subscription_expiry_travels_with_the_payload(self):
        config = json.loads(json.dumps(PLUG_CONFIG))
        config["plugs"]["subscription_expires"] = "2026-10-31"
        payload = build_payload(self.store, config, 30, 30)
        self.assertIsNotNone(payload["plugs"]["subscription_expires"])
        self.assertEqual(payload["plugs"]["subscription_warn_days"], 14)

    def test_subscription_days_counts_down_and_goes_negative(self):
        import datetime
        at = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc).timestamp()
        self.assertEqual(
            plugs.subscription_days({"subscription_expires": "2026-10-31"}, at), 30)
        self.assertLess(
            plugs.subscription_days({"subscription_expires": "2026-09-01"}, at), 0)
        self.assertIsNone(plugs.subscription_days({}, at))
        self.assertIsNone(
            plugs.subscription_days({"subscription_expires": "not a date"}, at))

    def test_a_reset_sensors_old_readings_stay_on_the_same_chart(self):
        # Rejoining the sensor to a different Wi-Fi network factory resets it,
        # and it comes back with a new device ID. Without previous_ids its
        # months of readings would drop off the chart, as though the nursery
        # had no air temperature before the day the router changed.
        store = Store("sqlite:///:memory:")
        store.migrate()
        old_rows = [
            {"device_id": "oldSensor", "reading_time": self.now - 7200,
             "air_temperature": 21.1, "humidity": 60, "battery": None, "online": 1},
            {"device_id": "newSensor", "reading_time": self.now - 600,
             "air_temperature": 22.4, "humidity": 62, "battery": None, "online": 1},
        ]
        store.insert_ambient(old_rows)

        config = json.loads(json.dumps(PLUG_CONFIG))
        devices = config["plugs"]["devices"]
        devices["newSensor"] = devices.pop("airSensor")
        devices["newSensor"]["previous_ids"] = ["oldSensor"]

        ambient = build_payload(store, config, 30, 30)["ambient"]
        self.assertEqual(list(ambient["latest"]), ["newSensor"])
        self.assertEqual(len(ambient["readings"]), 2)
        self.assertEqual({r[0] for r in ambient["readings"]}, {"newSensor"})

    def test_readings_from_an_unlisted_device_are_left_out(self):
        store = Store("sqlite:///:memory:")
        store.migrate()
        store.insert_ambient([
            {"device_id": "airSensor", "reading_time": self.now - 600,
             "air_temperature": 22.0, "humidity": 60, "battery": None, "online": 1},
            {"device_id": "someoneElses", "reading_time": self.now - 600,
             "air_temperature": 30.0, "humidity": 10, "battery": None, "online": 1},
        ])
        ambient = build_payload(store, PLUG_CONFIG, 30, 30)["ambient"]
        self.assertEqual(list(ambient["latest"]), ["airSensor"])

    def test_the_air_sensor_gets_its_own_staleness_threshold(self):
        # A plug answers every poll, so ninety minutes of silence means
        # trouble. The sensor reports on change, so a steady room sends
        # nothing for hours with nothing whatever the matter. Sharing the
        # plugs' threshold made a healthy sensor look dead.
        payload = self.payload()
        self.assertEqual(payload["plugs"]["stale_after"], 90 * 60)
        self.assertEqual(payload["ambient"]["stale_after"], 6 * 3600)

    def test_the_ambient_threshold_is_configurable(self):
        config = json.loads(json.dumps(PLUG_CONFIG))
        config["plugs"]["ambient_stale_hours"] = 2
        self.assertEqual(
            build_payload(self.store, config, 30, 30)["ambient"]["stale_after"],
            7200)

    def test_plugs_disabled_exports_nothing_but_the_flag(self):
        payload = build_payload(self.store, {"plugs": {"enabled": False}}, 30, 30)
        self.assertEqual(payload["plugs"], {"enabled": False})
        self.assertFalse(payload["ambient"]["enabled"])

    def test_an_export_against_a_database_without_the_tables_still_builds(self):
        bare = Store("sqlite:///:memory:")
        with bare.cursor() as cur:
            cur.execute("CREATE TABLE devices (device_id TEXT PRIMARY KEY, "
                        "description TEXT, device_type INTEGER, sump_code TEXT, "
                        "system_code TEXT, label TEXT, first_seen INTEGER, "
                        "last_seen INTEGER)")
            cur.execute("CREATE TABLE readings (device_id TEXT, reading_time INTEGER)")
            cur.execute("CREATE TABLE harvest_runs (run_id INTEGER PRIMARY KEY, "
                        "started_at INTEGER, finished_at INTEGER, status TEXT, "
                        "devices_polled INTEGER, readings_inserted INTEGER, message TEXT)")
        payload = build_payload(bare, PLUG_CONFIG, 30, 30)
        self.assertEqual(payload["plugs"]["by_sump"], {})
        self.assertFalse(payload["ambient"]["enabled"])


# ---------------------------------------------------------------------------
# Checking the nursery against the chiller schedule
# ---------------------------------------------------------------------------

try:
    from zoneinfo import ZoneInfo
    GIB = ZoneInfo("Europe/Gibraltar")
except Exception:  # pragma: no cover
    GIB = dt.timezone.utc

SCHEDULE = {
    "enabled": True,
    "timezone": "Europe/Gibraltar",
    "limits": [{"from": "12:00", "to": "21:00", "max_running": 1},
               {"from": "21:00", "to": "12:00", "max_running": 3}],
    "blocks": [
        {"from": "21:00", "to": "23:30", "sumps": ["SB12", "SB34", "SC34"]},
        {"from": "23:30", "to": "02:00", "sumps": ["SA345", "SD12", "SD345"]},
        {"from": "02:00", "to": "04:30", "sumps": ["SA12", "SC12", "SE12"]},
        {"from": "04:30", "to": "07:00", "sumps": ["SB12", "SB34", "SC34"]},
        {"from": "07:00", "to": "09:30", "sumps": ["SA345", "SD12", "SD345"]},
        {"from": "09:30", "to": "12:00", "sumps": ["SA12", "SC12", "SE12"]},
        {"from": "12:00", "to": "14:15", "sumps": ["SA345"]},
        {"from": "14:15", "to": "16:30", "sumps": ["SA12"]},
        {"from": "16:30", "to": "18:45", "sumps": ["SC12"]},
        {"from": "18:45", "to": "21:00", "sumps": ["SE12"]},
    ],
    "daily_target_hours": {"SA12": 7.25, "SA345": 7.25, "SC12": 7.25, "SE12": 7.25,
                           "SB12": 5.0, "SB34": 5.0, "SC34": 5.0,
                           "SD12": 5.0, "SD345": 5.0},
}

SOCKETS = {"SA12": ("plugA", "switch_1"), "SA345": ("plugA", "switch_2"),
           "SB12": ("plugB", "switch_1"), "SB34": ("plugB", "switch_2"),
           "SC12": ("plugC", "switch_1"), "SC34": ("plugC", "switch_2"),
           "SD12": ("plugD", "switch_1"), "SD345": ("plugD", "switch_2"),
           "SE12": ("plugE", "switch_1")}


def at(y, m, d, hh, mm=0):
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=GIB).timestamp())


def play(store, cfg, start, end, skip=()):
    """Write the transitions a set of plugs would produce running the schedule.

    `skip` drops named (sump, local hour) block starts, which is how a missed
    block is simulated: the plug was off the network when the event fired.
    """
    events = []
    for o in sched.occurrences(cfg, start, end, GIB):
        for sump in o["sumps"]:
            if (sump, o["label"]) in skip:
                continue
            events.append((o["from"], sump, 1))
            events.append((o["to"], sump, 0))
    events.sort()
    state = {}
    with store.cursor() as cur:
        for t, sump, on in events:
            if state.get(sump) == on:
                continue
            state[sump] = on
            dev, sock = SOCKETS[sump]
            cur.execute(
                "INSERT OR REPLACE INTO plug_states (device_id, socket, changed_at, "
                "last_seen, sump_code, role, on_state, online) VALUES (?,?,?,?,?,?,?,?)",
                (dev, sock, t, t, sump, "chiller", on, 1))


class TestScheduleModel(unittest.TestCase):
    def test_a_block_that_crosses_midnight_is_one_block_not_two(self):
        runs = sched.occurrences(SCHEDULE, at(2026, 10, 7, 20), at(2026, 10, 8, 3), GIB)
        night = [o for o in runs if o["label"] == "23:30\u201302:00"]
        self.assertTrue(night)
        self.assertEqual(night[0]["to"] - night[0]["from"], int(2.5 * 3600))

    def test_the_cabinet_limit_follows_the_time_of_day(self):
        self.assertEqual(sched.limit_at(SCHEDULE, at(2026, 10, 8, 13), GIB), 1)
        self.assertEqual(sched.limit_at(SCHEDULE, at(2026, 10, 8, 3), GIB), 3)
        # the boundaries themselves belong to the block they open
        self.assertEqual(sched.limit_at(SCHEDULE, at(2026, 10, 8, 12), GIB), 1)
        self.assertEqual(sched.limit_at(SCHEDULE, at(2026, 10, 8, 21), GIB), 3)

    # occurrences() returns every block OVERLAPPING the window, which is what
    # compliance needs: the 23:30 block belongs to the night it starts and has
    # to be judged whole. Totalling a day therefore means clipping to the day,
    # or the block that straddles midnight is counted at both ends.
    @staticmethod
    def _clipped(a, z):
        total = collections.Counter()
        for o in sched.occurrences(SCHEDULE, a, z, GIB):
            inside = max(0, min(o["to"], z) - max(o["from"], a))
            for sump in o["sumps"]:
                total[sump] += inside
        return total

    def test_every_sump_runs_the_hours_the_schedule_promises(self):
        total = self._clipped(at(2026, 10, 8, 0), at(2026, 10, 9, 0))
        for sump, target in SCHEDULE["daily_target_hours"].items():
            self.assertAlmostEqual(total[sump] / 3600, target, places=2, msg=sump)

    def test_the_whole_timetable_is_fifty_four_chiller_hours(self):
        total = self._clipped(at(2026, 10, 8, 0), at(2026, 10, 9, 0))
        self.assertAlmostEqual(sum(total.values()) / 3600, 54.0, places=2)

    def test_a_block_is_returned_whole_even_when_it_straddles_the_window(self):
        runs = sched.occurrences(SCHEDULE, at(2026, 10, 8, 0), at(2026, 10, 9, 0), GIB)
        night = [o for o in runs if o["label"] == "23:30\u201302:00"]
        self.assertEqual(len(night), 2)
        self.assertTrue(all(o["to"] - o["from"] == int(2.5 * 3600) for o in night))

    def test_a_malformed_time_is_refused_rather_than_guessed(self):
        for bad in ("", "9", "25:00", "12:60", "noon"):
            with self.assertRaises(ValueError):
                sched._hhmm(bad)


class TestScheduleCompliance(unittest.TestCase):
    def setUp(self):
        self.store = Store("sqlite:///:memory:")
        self.store.migrate()
        self.now = at(2026, 10, 8, 13)
        self.config = {"schedule": SCHEDULE}

    def run_perfect(self, skip=()):
        play(self.store, SCHEDULE, self.now - 3 * 86400, self.now, skip=skip)
        return sched.build(self.store, self.config, self.now, days=3)

    def test_a_schedule_run_perfectly_reports_nothing_wrong(self):
        out = self.run_perfect()
        self.assertEqual(out["misses"], [])
        self.assertEqual(out["breaches"], [])
        self.assertFalse(out["now"]["over_limit"])

    def test_it_knows_what_should_be_running_this_minute(self):
        out = self.run_perfect()["now"]
        self.assertEqual(out["expected"], ["SA345"])
        self.assertEqual(out["actual"], ["SA345"])
        self.assertEqual(out["missing"], [])
        self.assertEqual(out["unexpected"], [])

    def test_what_is_on_now_comes_from_the_last_transition(self):
        # The open span ends at `now`, so bracketing it with start <= t < end
        # reported an idle nursery at exactly the moment anyone would ask.
        play(self.store, SCHEDULE, self.now - 86400, self.now)
        spans = sched.spans(self.store.query(
            "SELECT device_id, socket, changed_at, sump_code, on_state, online "
            "FROM plug_states ORDER BY changed_at"), self.now)
        self.assertIn("SA345", sched.running_at(spans, self.now))

    def test_a_block_the_plug_slept_through_is_reported_as_missed(self):
        out = self.run_perfect(skip=(("SC12", "09:30\u201312:00"),
                                     ("SE12", "09:30\u201312:00")))
        missed = {(m["sump"], m["label"]) for m in out["misses"]}
        self.assertIn(("SC12", "09:30\u201312:00"), missed)
        self.assertIn(("SE12", "09:30\u201312:00"), missed)
        self.assertTrue(all(m["fraction"] < 0.2 for m in out["misses"]))

    def test_a_missed_block_lengthens_that_sumps_worst_gap(self):
        clean = self.run_perfect()["gaps"]["SE12"]["hours"]
        self.setUp()
        after = self.run_perfect(skip=(("SE12", "09:30\u201312:00"),))["gaps"]["SE12"]["hours"]
        self.assertGreater(after, clean)

    def test_a_fourth_chiller_overnight_is_a_cabinet_breach(self):
        play(self.store, SCHEDULE, self.now - 3 * 86400, self.now)
        a, z = at(2026, 10, 8, 2), at(2026, 10, 8, 4, 30)
        with self.store.cursor() as cur:
            for t, on in ((a, 1), (z, 0)):
                cur.execute(
                    "INSERT OR REPLACE INTO plug_states (device_id, socket, changed_at, "
                    "last_seen, sump_code, role, on_state, online) VALUES (?,?,?,?,?,?,?,?)",
                    ("plugD", "switch_1", t, t, "SD12", "chiller", on, 1))
        out = sched.build(self.store, self.config, self.now, days=3)
        self.assertTrue(out["breaches"])
        worst = max(b["peak"] for b in out["breaches"])
        self.assertEqual(worst, 4)
        self.assertEqual(out["breaches"][0]["limit"], 3)

    def test_one_extra_chiller_in_the_day_lockout_is_a_breach(self):
        play(self.store, SCHEDULE, self.now - 3 * 86400, self.now)
        t = at(2026, 10, 8, 12, 30)
        with self.store.cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO plug_states (device_id, socket, changed_at, "
                "last_seen, sump_code, role, on_state, online) VALUES (?,?,?,?,?,?,?,?)",
                ("plugB", "switch_1", t, t, "SB12", "chiller", 1, 1))
        out = sched.build(self.store, self.config, self.now, days=3)
        self.assertEqual(out["now"]["unexpected"], ["SB12"])
        self.assertTrue(out["now"]["over_limit"])
        self.assertEqual(out["now"]["running"], 2)
        self.assertEqual(out["now"]["limit"], 1)

    def test_a_full_day_matches_the_schedules_own_targets(self):
        out = self.run_perfect()
        full = [d for d in out["daily"] if len(d["by_sump"]) == 9][1]
        for sump, got in full["by_sump"].items():
            self.assertAlmostEqual(got["hours"], got["target"], places=2, msg=sump)

    def test_time_a_plug_was_offline_is_counted_but_flagged(self):
        play(self.store, SCHEDULE, self.now - 86400, self.now)
        with self.store.cursor() as cur:
            cur.execute("UPDATE plug_states SET online = 0 WHERE sump_code = 'SB12'")
        out = sched.build(self.store, self.config, self.now, days=1)
        day = out["daily"][-1]["by_sump"]["SB12"]
        self.assertGreater(day["unconfirmed_hours"], 0)

    def test_the_schedule_switched_off_exports_nothing(self):
        out = sched.build(self.store, {"schedule": {"enabled": False}}, self.now)
        self.assertEqual(out, {"enabled": False})

    def test_no_plug_history_says_so_rather_than_claiming_compliance(self):
        out = sched.build(self.store, self.config, self.now)
        self.assertFalse(out.get("ready"))


if __name__ == "__main__":
    unittest.main()

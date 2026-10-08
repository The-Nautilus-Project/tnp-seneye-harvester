"""Database-agnostic storage for Seneye readings.

Default backend is SQLite (standard library, no install). PostgreSQL and MySQL
are supported when their driver happens to be present, so the same harvester can
write straight into whatever the TNP website sits on without changing the code.

Pick the backend with DATABASE_URL:

    sqlite:///data/nursery.db                 (default)
    postgresql://user:pwd@host:5432/dbname    needs psycopg or psycopg2
    mysql://user:pwd@host:3306/dbname         needs mysql-connector-python or PyMySQL

Every write is an upsert on (device_id, reading_time), so polling the same last
reading several times never duplicates a row.
"""

from __future__ import annotations

import os
import sqlite3
import urllib.parse
from contextlib import contextmanager
from typing import Any, Iterable, Sequence

from .nutrients import NUMERIC_FIELDS as NUTRIENT_FIELDS
from .seneye import PARAMETERS

READING_COLUMNS: tuple[str, ...] = (
    ("device_id", "reading_time", "fetched_at")
    + PARAMETERS
    + tuple(f"{p}_status" for p in PARAMETERS)
    + ("slide_serial", "slide_expires", "out_of_water", "disconnected")
)


class Store:
    """Thin wrapper over a DB-API connection with one paramstyle smoothed out."""

    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get("DATABASE_URL") or "sqlite:///data/nursery.db"
        self.scheme = self.url.split("://", 1)[0].split("+", 1)[0].lower()
        self._conn = self._connect()

    # -- connection --------------------------------------------------------

    def _connect(self):
        if self.scheme == "sqlite":
            path = self.url.split("://", 1)[1]
            path = path.lstrip("/") if not path.startswith("//") else path[1:]
            if path and path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            conn = sqlite3.connect(path or ":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            return conn

        parts = urllib.parse.urlparse(self.url)
        kwargs = {
            "host": parts.hostname,
            "port": parts.port,
            "user": urllib.parse.unquote(parts.username or ""),
            "password": urllib.parse.unquote(parts.password or ""),
            "database": parts.path.lstrip("/"),
        }

        if self.scheme in ("postgresql", "postgres"):
            try:
                import psycopg  # type: ignore

                return psycopg.connect(
                    host=kwargs["host"],
                    port=kwargs["port"] or 5432,
                    user=kwargs["user"],
                    password=kwargs["password"],
                    dbname=kwargs["database"],
                )
            except ImportError:
                import psycopg2  # type: ignore

                return psycopg2.connect(
                    host=kwargs["host"],
                    port=kwargs["port"] or 5432,
                    user=kwargs["user"],
                    password=kwargs["password"],
                    dbname=kwargs["database"],
                )

        if self.scheme in ("mysql", "mariadb"):
            try:
                import mysql.connector  # type: ignore

                return mysql.connector.connect(
                    host=kwargs["host"],
                    port=kwargs["port"] or 3306,
                    user=kwargs["user"],
                    password=kwargs["password"],
                    database=kwargs["database"],
                )
            except ImportError:
                import pymysql  # type: ignore

                return pymysql.connect(
                    host=kwargs["host"],
                    port=kwargs["port"] or 3306,
                    user=kwargs["user"],
                    password=kwargs["password"],
                    database=kwargs["database"],
                )

        raise ValueError(f"Unsupported DATABASE_URL scheme: {self.scheme}")

    @property
    def placeholder(self) -> str:
        return "?" if self.scheme == "sqlite" else "%s"

    def sql(self, statement: str) -> str:
        """Rewrite '?' placeholders for drivers that want '%s'."""
        return statement if self.placeholder == "?" else statement.replace("?", "%s")

    @contextmanager
    def cursor(self):
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def close(self) -> None:
        self._conn.close()

    # -- schema ------------------------------------------------------------

    def migrate(self) -> None:
        numeric = "REAL" if self.scheme == "sqlite" else "DOUBLE PRECISION"
        if self.scheme in ("mysql", "mariadb"):
            numeric = "DOUBLE"
        text = "TEXT" if self.scheme != "mysql" else "VARCHAR(255)"

        param_cols = ",\n            ".join(
            f"{p} {numeric}" for p in PARAMETERS
        )
        status_cols = ",\n            ".join(
            f"{p}_status INTEGER" for p in PARAMETERS
        )

        with self.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS devices (
                    device_id {text} PRIMARY KEY,
                    description {text},
                    device_type INTEGER,
                    sump_code {text},
                    system_code {text},
                    label {text},
                    first_seen INTEGER,
                    last_seen INTEGER
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS readings (
                    device_id {text} NOT NULL,
                    reading_time INTEGER NOT NULL,
                    fetched_at INTEGER NOT NULL,
                    {param_cols},
                    {status_cols},
                    slide_serial {text},
                    slide_expires INTEGER,
                    out_of_water INTEGER,
                    disconnected INTEGER,
                    PRIMARY KEY (device_id, reading_time)
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS harvest_runs (
                    run_id INTEGER PRIMARY KEY {"AUTOINCREMENT" if self.scheme == "sqlite" else "AUTO_INCREMENT" if self.scheme in ("mysql", "mariadb") else ""},
                    started_at INTEGER,
                    finished_at INTEGER,
                    status {text},
                    devices_polled INTEGER,
                    readings_inserted INTEGER,
                    message {text}
                )
                """
                if self.scheme != "postgresql"
                else """
                CREATE TABLE IF NOT EXISTS harvest_runs (
                    run_id SERIAL PRIMARY KEY,
                    started_at INTEGER,
                    finished_at INTEGER,
                    status TEXT,
                    devices_polled INTEGER,
                    readings_inserted INTEGER,
                    message TEXT
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_readings_time ON readings (reading_time)"
            )
            nutrient_cols = ",\n            ".join(
                f"{f} {numeric}" for f in NUTRIENT_FIELDS
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS nutrients (
                    sample_date {text} NOT NULL,
                    sump_code {text} NOT NULL,
                    sample_time {text},
                    {nutrient_cols},
                    observer {text},
                    notes {text},
                    PRIMARY KEY (sample_date, sump_code)
                )
                """
            )

            # Smart plugs are stored as a transition log, not a sample every
            # half hour: a row is written when a socket changes state and its
            # last_seen is bumped otherwise. Eleven sockets polled every
            # thirty minutes would be two hundred thousand rows a year to say
            # "still on"; this way the table holds the switching history, which
            # is the thing anyone would actually want to read back.
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS plug_states (
                    device_id {text} NOT NULL,
                    socket {text} NOT NULL,
                    changed_at INTEGER NOT NULL,
                    last_seen INTEGER,
                    sump_code {text},
                    role {text},
                    on_state INTEGER,
                    online INTEGER,
                    power_w {numeric},
                    plug_power_w {numeric},
                    PRIMARY KEY (device_id, socket, changed_at)
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_plug_states_seen "
                "ON plug_states (device_id, socket, changed_at)"
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS ambient (
                    device_id {text} NOT NULL,
                    reading_time INTEGER NOT NULL,
                    reported_at INTEGER,
                    air_temperature {numeric},
                    humidity {numeric},
                    battery {numeric},
                    online INTEGER,
                    PRIMARY KEY (device_id, reading_time)
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_ambient_time ON ambient (reading_time)"
            )

            # The meter readings need a row per poll, which is why they cannot
            # live in plug_states: that table is a transition log and each
            # update overwrites the last power figure. Energy is the integral
            # of power over time, so it needs the series, not the latest value.
            #
            # add_ele is the plugs' own accumulated-energy counter. It is
            # stored and never displayed: it resets when a plug loses power and
            # two of the five have been stuck on the same figure for over a
            # week, so it is the number that would look most authoritative on a
            # dashboard while being the least true. Kept only so we can tell
            # later whether it ever starts behaving.
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS plug_power (
                    device_id {text} NOT NULL,
                    reading_time INTEGER NOT NULL,
                    power_w {numeric},
                    voltage_v {numeric},
                    current_ma {numeric},
                    add_ele {numeric},
                    online INTEGER,
                    PRIMARY KEY (device_id, reading_time)
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_plug_power_time "
                "ON plug_power (reading_time)"
            )

            # CREATE TABLE IF NOT EXISTS is silent about a table that already
            # exists with fewer columns, so a database written by an earlier
            # version keeps its old shape and every later insert fails. Columns
            # added after a table has shipped have to be added explicitly.
            self._add_column(cur, "plug_states", "plug_power_w", numeric)
            self._add_column(cur, "ambient", "reported_at", "INTEGER")

            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS issues (
                    issue_id {text} PRIMARY KEY,
                    raised_on {text},
                    raised_at {text},
                    location {text},
                    equipment {text},
                    summary {text},
                    severity {text},
                    status {text},
                    assigned_to {text},
                    action_taken {text},
                    resolved_on {text},
                    reported_by {text},
                    notes {text}
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS schedule (
                    task_id {text} PRIMARY KEY,
                    task {text},
                    location {text},
                    equipment {text},
                    frequency_days INTEGER,
                    last_done {text},
                    done_by {text},
                    next_due {text},
                    notes {text}
                )
                """
            )

    def _add_column(self, cur, table: str, column: str, coltype: str) -> bool:
        """Add a column to an existing table, doing nothing if it is there.

        Written by hand because SQLite has no IF NOT EXISTS for ALTER TABLE.
        The check reads the catalogue rather than trying a SELECT and catching
        the error: on PostgreSQL a failed statement aborts the whole
        transaction, so probing by failure would take the rest of the
        migration down with it.
        """
        if self.scheme == "sqlite":
            cur.execute(f"PRAGMA table_info({table})")
            existing = {row[1] for row in cur.fetchall()}
        else:
            cur.execute(
                self.sql(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = ?"
                ),
                (table,),
            )
            existing = {str(row[0]).lower() for row in cur.fetchall()}
        if not existing or column.lower() in {c.lower() for c in existing}:
            return False
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        return True

    # -- writes ------------------------------------------------------------

    def upsert_device(
        self,
        device_id: str,
        description: str,
        device_type: int | None,
        sump_code: str | None,
        system_code: str | None,
        label: str | None,
        seen_at: int,
    ) -> None:
        with self.cursor() as cur:
            cur.execute(
                self.sql("SELECT device_id FROM devices WHERE device_id = ?"),
                (device_id,),
            )
            exists = cur.fetchone() is not None
            if exists:
                cur.execute(
                    self.sql(
                        "UPDATE devices SET description = ?, device_type = ?, "
                        "sump_code = ?, system_code = ?, label = ?, last_seen = ? "
                        "WHERE device_id = ?"
                    ),
                    (
                        description,
                        device_type,
                        sump_code,
                        system_code,
                        label,
                        seen_at,
                        device_id,
                    ),
                )
            else:
                cur.execute(
                    self.sql(
                        "INSERT INTO devices (device_id, description, device_type, "
                        "sump_code, system_code, label, first_seen, last_seen) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    ),
                    (
                        device_id,
                        description,
                        device_type,
                        sump_code,
                        system_code,
                        label,
                        seen_at,
                        seen_at,
                    ),
                )

    def insert_readings(self, rows: Iterable[dict[str, Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        cols = ", ".join(READING_COLUMNS)
        marks = ", ".join("?" for _ in READING_COLUMNS)
        inserted = 0
        with self.cursor() as cur:
            for row in rows:
                values = tuple(row.get(c) for c in READING_COLUMNS)
                cur.execute(
                    self.sql(
                        "SELECT 1 FROM readings WHERE device_id = ? AND reading_time = ?"
                    ),
                    (row["device_id"], row["reading_time"]),
                )
                if cur.fetchone() is not None:
                    continue
                cur.execute(
                    self.sql(f"INSERT INTO readings ({cols}) VALUES ({marks})"), values
                )
                inserted += 1
        return inserted

    def upsert_nutrients(self, records: Iterable[dict[str, Any]]) -> int:
        """Insert or replace hand-sampled nutrient rows, keyed on date + sump.

        Replacing rather than skipping means a corrected value in the workbook
        overwrites what was loaded before, which is what you want for data that
        gets checked and revised after the fact.
        """
        records = list(records)
        if not records:
            return 0
        columns = (
            ("sample_date", "sump_code", "sample_time")
            + tuple(NUTRIENT_FIELDS)
            + ("observer", "notes")
        )
        cols = ", ".join(columns)
        marks = ", ".join("?" for _ in columns)
        written = 0
        with self.cursor() as cur:
            for record in records:
                cur.execute(
                    self.sql(
                        "DELETE FROM nutrients WHERE sample_date = ? AND sump_code = ?"
                    ),
                    (record["sample_date"], record["sump_code"]),
                )
                cur.execute(
                    self.sql(f"INSERT INTO nutrients ({cols}) VALUES ({marks})"),
                    tuple(record.get(c) for c in columns),
                )
                written += 1
        return written


    def insert_plug_states(self, rows: Iterable[dict[str, Any]]) -> int:
        """Append a row per socket only when its state has actually changed.

        Returns the number of transitions recorded, so a run that found
        everything as it left it reports zero rather than eleven.
        """
        rows = list(rows)
        if not rows:
            return 0
        changes = 0
        with self.cursor() as cur:
            for row in rows:
                cur.execute(
                    self.sql(
                        "SELECT changed_at, on_state, online FROM plug_states "
                        "WHERE device_id = ? AND socket = ? "
                        "ORDER BY changed_at DESC LIMIT 1"
                    ),
                    (row["device_id"], row["socket"]),
                )
                prev = cur.fetchone()
                seen = int(row["reading_time"])
                contact = row.get("last_contact")
                contact = int(contact) if contact is not None else seen
                if prev is not None:
                    prev_changed = int(prev[0])
                    same = (_same(prev[1], row.get("on_state"))
                            and _same(prev[2], row.get("online")))
                    if same:
                        # last_seen is taken as given, not maxed against
                        # changed_at. A plug that dropped off the network days
                        # ago has a last contact older than the state it is
                        # still reporting, and flooring it at the state's own
                        # timestamp would quietly make a dead plug look fresh.
                        cur.execute(
                            self.sql(
                                "UPDATE plug_states SET last_seen = ?, power_w = ?, "
                                "plug_power_w = ?, online = ?, sump_code = ?, "
                                "role = ? WHERE device_id = ? AND socket = ? "
                                "AND changed_at = ?"
                            ),
                            (
                                contact,
                                row.get("power_w"),
                                row.get("plug_power_w"),
                                row.get("online"),
                                row.get("sump_code"),
                                row.get("role"),
                                row["device_id"],
                                row["socket"],
                                prev_changed,
                            ),
                        )
                        continue
                    # A change that reports an older timestamp than the row it
                    # supersedes would sort behind it and read as history
                    # running backwards, so it is clamped forward.
                    if seen <= prev_changed:
                        seen = prev_changed + 1

                cur.execute(
                    self.sql(
                        "INSERT INTO plug_states (device_id, socket, changed_at, "
                        "last_seen, sump_code, role, on_state, online, power_w, "
                        "plug_power_w) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    ),
                    (
                        row["device_id"],
                        row["socket"],
                        seen,
                        contact,
                        row.get("sump_code"),
                        row.get("role"),
                        row.get("on_state"),
                        row.get("online"),
                        row.get("power_w"),
                        row.get("plug_power_w"),
                    ),
                )
                changes += 1
        return changes

    def insert_plug_power(self, rows: Iterable[dict[str, Any]]) -> int:
        """One meter reading per plug per poll.

        A plug the cloud cannot reach is reporting whatever it last said, so
        storing it would invent a flat line of consumption through an outage.
        Its absence is what later tells the export that those hours are not
        covered.
        """
        rows = list(rows)
        if not rows:
            return 0
        columns = ("device_id", "reading_time", "power_w", "voltage_v",
                   "current_ma", "add_ele", "online")
        cols = ", ".join(columns)
        marks = ", ".join("?" for _ in columns)
        inserted = 0
        with self.cursor() as cur:
            for row in rows:
                if row.get("online") == 0 or row.get("power_w") is None:
                    continue
                cur.execute(
                    self.sql("SELECT 1 FROM plug_power WHERE device_id = ? "
                             "AND reading_time = ?"),
                    (row["device_id"], int(row["reading_time"])),
                )
                if cur.fetchone() is not None:
                    continue
                cur.execute(
                    self.sql(f"INSERT INTO plug_power ({cols}) VALUES ({marks})"),
                    tuple(row.get(c) for c in columns),
                )
                inserted += 1
        return inserted

    def insert_ambient(self, rows: Iterable[dict[str, Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        columns = ("device_id", "reading_time", "reported_at", "air_temperature",
                   "humidity", "battery", "online")
        cols = ", ".join(columns)
        marks = ", ".join("?" for _ in columns)
        inserted = 0
        with self.cursor() as cur:
            for row in rows:
                if row.get("air_temperature") is None and row.get("humidity") is None:
                    continue
                # A sensor the cloud cannot reach is still reporting its last
                # value, and writing that every half hour would manufacture a
                # flat line out of nothing. Its absence is the honest record.
                if row.get("online") == 0:
                    continue
                cur.execute(
                    self.sql(
                        "SELECT 1 FROM ambient WHERE device_id = ? AND reading_time = ?"
                    ),
                    (row["device_id"], int(row["reading_time"])),
                )
                if cur.fetchone() is not None:
                    continue
                cur.execute(
                    self.sql(f"INSERT INTO ambient ({cols}) VALUES ({marks})"),
                    tuple(row.get(c) for c in columns),
                )
                inserted += 1
        return inserted

    def replace_table(self, table: str, columns: tuple, records) -> int:
        """Replace a sheet-backed table wholesale.

        The sheet is the record of truth for issues and planned jobs: a row
        deleted there should disappear here too, which an upsert would not do.
        The replace runs inside one transaction, so a failure part way through
        leaves the previous contents intact rather than an empty table.
        """
        records = list(records)
        if table not in {"issues", "schedule"}:
            raise ValueError(f"replace_table refuses to touch {table}")
        cols = ", ".join(columns)
        marks = ", ".join("?" for _ in columns)
        with self.cursor() as cur:
            cur.execute(f"DELETE FROM {table}")
            for record in records:
                cur.execute(
                    self.sql(f"INSERT INTO {table} ({cols}) VALUES ({marks})"),
                    tuple(record.get(c) for c in columns),
                )
        return len(records)

    def start_run(self, started_at: int) -> None:
        self._run_started = started_at

    def finish_run(
        self,
        started_at: int,
        finished_at: int,
        status: str,
        devices_polled: int,
        readings_inserted: int,
        message: str = "",
    ) -> None:
        with self.cursor() as cur:
            cur.execute(
                self.sql(
                    "INSERT INTO harvest_runs (started_at, finished_at, status, "
                    "devices_polled, readings_inserted, message) VALUES (?, ?, ?, ?, ?, ?)"
                ),
                (
                    started_at,
                    finished_at,
                    status,
                    devices_polled,
                    readings_inserted,
                    message[:900],
                ),
            )

    # -- reads -------------------------------------------------------------

    def query(self, statement: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute(self.sql(statement), tuple(params))
            columns = [d[0] for d in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]


def _same(a: Any, b: Any) -> bool:
    """Compare two nullable flags without NULL swallowing the comparison."""
    if a is None or b is None:
        return a is None and b is None
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return a == b

"""Every check, written down in SQLite, and the reports read back out of it.

The database is deliberately the only state this tool has. Nothing is kept in
memory between runs, which means `netmon --once` from cron every five minutes
and `netmon --watch` as a long-running loop behave identically: both decide
what is wrong by reading the last few rows back. A restart loses nothing, and
there is no in-memory counter that can disagree with the history.

SQLite is enough here on purpose. One writer, a few hundred rows an hour, and
a file you can copy off the machine and open anywhere.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id         INTEGER PRIMARY KEY,
    device     TEXT    NOT NULL,
    checked_at TEXT    NOT NULL,          -- ISO-8601 UTC, so it sorts as text
    reachable  INTEGER NOT NULL,          -- 0 or 1
    rtt_ms     REAL,                      -- NULL when nothing came back
    sent       INTEGER NOT NULL,
    received   INTEGER NOT NULL,
    if_status  TEXT,                      -- NULL when the device has no SNMP
    note       TEXT
);

-- Every report and every alert decision reads one device's recent rows newest
-- first, so that is the index.
CREATE INDEX IF NOT EXISTS checks_by_device_time ON checks (device, checked_at DESC);

CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY,
    device     TEXT NOT NULL,
    kind       TEXT NOT NULL,             -- 'unreachable' or 'slow'
    opened_at  TEXT NOT NULL,
    cleared_at TEXT,                      -- NULL while the alert is open
    note       TEXT
);

-- At most one open alert of a kind per device. The partial index enforces it
-- in the database rather than trusting every caller to check first.
CREATE UNIQUE INDEX IF NOT EXISTS one_open_alert_per_kind
    ON alerts (device, kind) WHERE cleared_at IS NULL;
"""


def _stamp(moment: datetime) -> str:
    """An ISO-8601 UTC timestamp of fixed width.

    timespec="microseconds" is not decoration. Plain .isoformat() leaves the
    microseconds out altogether when they happen to be zero, so one row in a
    million is written as ...:02+00:00 while its neighbours are written as
    ...:02.500000+00:00. Every ordering and every window in this file compares
    these as text, and "+" sorts before "." - so that one row would quietly
    sort before rows that came earlier than it, and fall outside windows it
    belongs in. Fixed width makes text order and time order the same thing.
    """
    return moment.isoformat(timespec="microseconds")


def _now() -> str:
    return _stamp(datetime.now(timezone.utc))


@dataclass(frozen=True)
class Check:
    device: str
    checked_at: str
    reachable: bool
    rtt_ms: float | None
    sent: int
    received: int
    if_status: str | None


@dataclass(frozen=True)
class Uptime:
    device: str
    checks: int
    reachable_checks: int
    packets_sent: int
    packets_received: int
    avg_rtt_ms: float | None

    @property
    def uptime_pct(self) -> float:
        return 100.0 * self.reachable_checks / self.checks if self.checks else 0.0

    @property
    def loss_pct(self) -> float:
        if not self.packets_sent:
            return 0.0
        lost = self.packets_sent - self.packets_received
        return 100.0 * lost / self.packets_sent


class Store:
    def __init__(self, path: str = "netmon.db") -> None:
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- writing

    def record(
        self,
        device: str,
        reachable: bool,
        rtt_ms: float | None,
        sent: int,
        received: int,
        if_status: str | None = None,
        note: str | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO checks (device, checked_at, reachable, rtt_ms, sent, received,"
            " if_status, note) VALUES (?,?,?,?,?,?,?,?)",
            (device, _now(), int(reachable), rtt_ms, sent, received, if_status, note),
        )
        self.db.commit()

    # ------------------------------------------------------------- reading

    def recent(self, device: str, limit: int = 30) -> list[Check]:
        """The last `limit` checks for one device, newest first."""
        rows = self.db.execute(
            "SELECT device, checked_at, reachable, rtt_ms, sent, received, if_status"
            " FROM checks WHERE device = ? ORDER BY checked_at DESC, id DESC LIMIT ?",
            (device, limit),
        ).fetchall()
        return [
            Check(r["device"], r["checked_at"], bool(r["reachable"]), r["rtt_ms"],
                  r["sent"], r["received"], r["if_status"])
            for r in rows
        ]

    def devices(self) -> list[str]:
        rows = self.db.execute("SELECT DISTINCT device FROM checks ORDER BY device").fetchall()
        return [r["device"] for r in rows]

    def uptime(self, hours: int | None = 24) -> list[Uptime]:
        """Uptime per device over the last `hours` (all of history if None)."""
        where, params = "", []
        if hours is not None:
            since = _stamp(datetime.now(timezone.utc) - timedelta(hours=hours))
            where, params = "WHERE checked_at >= ?", [since]

        rows = self.db.execute(
            "SELECT device, COUNT(*) AS checks, SUM(reachable) AS up,"
            " SUM(sent) AS sent, SUM(received) AS received, AVG(rtt_ms) AS avg_rtt"
            " FROM checks %s GROUP BY device ORDER BY device" % where,
            params,
        ).fetchall()
        return [
            Uptime(r["device"], r["checks"], r["up"] or 0, r["sent"] or 0,
                   r["received"] or 0, r["avg_rtt"])
            for r in rows
        ]

    # -------------------------------------------------------------- alerts

    def open_alert(self, device: str, kind: str, note: str | None = None) -> bool:
        """Open an alert. Returns False if one of that kind was already open.

        The unique index does the deciding, so two processes racing cannot
        both open the same alert - one of them gets the IntegrityError.
        """
        try:
            self.db.execute(
                "INSERT INTO alerts (device, kind, opened_at, note) VALUES (?,?,?,?)",
                (device, kind, _now(), note),
            )
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def clear_alert(self, device: str, kind: str) -> bool:
        """Close an open alert. Returns False if there was nothing open."""
        cursor = self.db.execute(
            "UPDATE alerts SET cleared_at = ? WHERE device = ? AND kind = ?"
            " AND cleared_at IS NULL",
            (_now(), device, kind),
        )
        self.db.commit()
        return cursor.rowcount > 0

    def open_alerts(self, device: str | None = None) -> list[sqlite3.Row]:
        if device is None:
            return self.db.execute(
                "SELECT * FROM alerts WHERE cleared_at IS NULL ORDER BY opened_at"
            ).fetchall()
        return self.db.execute(
            "SELECT * FROM alerts WHERE cleared_at IS NULL AND device = ? ORDER BY opened_at",
            (device,),
        ).fetchall()

    def alert_history(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM alerts ORDER BY opened_at DESC LIMIT ?", (limit,)
        ).fetchall()

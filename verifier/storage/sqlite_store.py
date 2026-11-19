"""SQLite implementation of the Storage interface (default, local, zero setup)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from .base import Storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    source_file     TEXT,
    source_sha256   TEXT,
    email_column    TEXT,
    columns_json    TEXT,
    status          TEXT NOT NULL,
    stage_label     TEXT,
    message         TEXT,
    error           TEXT,
    total_rows      INTEGER DEFAULT 0,
    created_at      REAL,
    started_at      REAL,
    finished_at     REAL,
    updated_at      REAL
);

CREATE TABLE IF NOT EXISTS emails (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    row_index       INTEGER,
    original_email  TEXT,
    email           TEXT,
    domain          TEXT,
    status          TEXT NOT NULL,
    stage           TEXT NOT NULL,
    reason          TEXT,
    mx_host         TEXT,
    is_role         INTEGER DEFAULT 0,
    role_prefix     TEXT,
    is_disposable   INTEGER DEFAULT 0,
    typo_fixed      TEXT,
    smtp_code       INTEGER,
    smtp_message    TEXT,
    attempts        INTEGER DEFAULT 0,
    next_attempt_at REAL,
    extra_json      TEXT,
    updated_at      REAL
);
CREATE INDEX IF NOT EXISTS ix_emails_job_status ON emails(job_id, status);
CREATE INDEX IF NOT EXISTS ix_emails_job_stage_domain ON emails(job_id, stage, domain);
CREATE INDEX IF NOT EXISTS ix_emails_job_updated ON emails(job_id, updated_at);
CREATE INDEX IF NOT EXISTS ix_emails_job_domain ON emails(job_id, domain);

CREATE TABLE IF NOT EXISTS domains (
    domain               TEXT PRIMARY KEY,
    mx_status            TEXT,
    mx_hosts_json        TEXT,
    mx_detail            TEXT,
    mx_checked_at        REAL,
    catch_all            INTEGER,
    catch_all_checked_at REAL
);
"""

EMAIL_COLUMNS = [
    "row_index", "original_email", "email", "domain", "status", "stage", "reason",
    "mx_host", "is_role", "role_prefix", "is_disposable", "typo_fixed", "smtp_code",
    "smtp_message", "attempts", "next_attempt_at", "extra_json",
]


class SQLiteStorage(Storage):
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---------------------------------------------------------------- utils
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def _all(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def _one(self, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    # ---------------------------------------------------------------- jobs
    def create_job(self, **fields: Any) -> int:
        now = time.time()
        fields.setdefault("created_at", now)
        fields["updated_at"] = now
        keys = list(fields)
        sql = "INSERT INTO jobs (%s) VALUES (%s)" % (",".join(keys), ",".join("?" * len(keys)))
        return int(self._exec(sql, [fields[k] for k in keys]).lastrowid)

    def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        sets = ",".join("%s=?" % k for k in fields)
        self._exec("UPDATE jobs SET %s WHERE id=?" % sets, list(fields.values()) + [job_id])

    def get_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        return self._one("SELECT * FROM jobs WHERE id=?", [job_id])

    def list_jobs(self) -> List[Dict[str, Any]]:
        return self._all("SELECT * FROM jobs ORDER BY id DESC")

    def delete_job(self, job_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM emails WHERE job_id=?", (job_id,))
            self._conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            self._conn.commit()

    def find_job_by_hash(self, sha256: str) -> Optional[Dict[str, Any]]:
        return self._one(
            "SELECT * FROM jobs WHERE source_sha256=? ORDER BY id DESC LIMIT 1", [sha256]
        )

    def mark_interrupted_jobs(self) -> int:
        cur = self._exec(
            "UPDATE jobs SET status='paused', message='Interrupted - resume to continue' "
            "WHERE status='running'"
        )
        return cur.rowcount

    # ---------------------------------------------------------------- emails
    def insert_emails(self, job_id: int, rows: Iterable[Dict[str, Any]]) -> None:
        now = time.time()
        sql = "INSERT INTO emails (job_id,%s,updated_at) VALUES (?,%s,?)" % (
            ",".join(EMAIL_COLUMNS), ",".join("?" * len(EMAIL_COLUMNS))
        )
        batch: List[Tuple[Any, ...]] = []
        with self._lock:
            for r in rows:
                batch.append((job_id,) + tuple(r.get(c) for c in EMAIL_COLUMNS) + (now,))
                if len(batch) >= 2000:
                    self._conn.executemany(sql, batch)
                    batch = []
            if batch:
                self._conn.executemany(sql, batch)
            self._conn.commit()

    def update_emails(self, updates: List[Tuple[int, Dict[str, Any]]]) -> None:
        if not updates:
            return
        now = time.time()
        with self._lock:
            for email_id, fields in updates:
                fields = dict(fields)
                fields["updated_at"] = now
                sets = ",".join("%s=?" % k for k in fields)
                self._conn.execute(
                    "UPDATE emails SET %s WHERE id=?" % sets, list(fields.values()) + [email_id]
                )
            self._conn.commit()

    def domains_at_stage(self, job_id: int, stage: str) -> List[str]:
        rows = self._all(
            "SELECT domain, COUNT(*) n FROM emails WHERE job_id=? AND stage=? "
            "GROUP BY domain ORDER BY n DESC",
            [job_id, stage],
        )
        return [r["domain"] for r in rows]

    def emails_for_domain(self, job_id: int, domain: str, stage: str) -> List[Dict[str, Any]]:
        return self._all(
            "SELECT * FROM emails WHERE job_id=? AND domain=? AND stage=? ORDER BY id",
            [job_id, domain, stage],
        )

    def release_due_retries(self, job_id: int, now: float) -> int:
        cur = self._exec(
            "UPDATE emails SET stage='smtp' WHERE job_id=? AND stage='retry' AND next_attempt_at<=?",
            [job_id, now],
        )
        return cur.rowcount

    def next_retry_at(self, job_id: int) -> Optional[float]:
        row = self._one(
            "SELECT MIN(next_attempt_at) t FROM emails WHERE job_id=? AND stage='retry'", [job_id]
        )
        return row["t"] if row and row["t"] is not None else None

    def iter_emails(self, job_id: int, statuses: Optional[List[str]] = None) -> Iterator[Dict[str, Any]]:
        last_id = 0
        while True:
            if statuses:
                q = ",".join("?" * len(statuses))
                rows = self._all(
                    "SELECT * FROM emails WHERE job_id=? AND id>? AND status IN (%s) "
                    "ORDER BY id LIMIT 5000" % q,
                    [job_id, last_id] + list(statuses),
                )
            else:
                rows = self._all(
                    "SELECT * FROM emails WHERE job_id=? AND id>? ORDER BY id LIMIT 5000",
                    [job_id, last_id],
                )
            if not rows:
                return
            for r in rows:
                yield r
            last_id = rows[-1]["id"]

    # ---------------------------------------------------------------- stats
    def counts(self, job_id: int) -> Dict[str, int]:
        out: Dict[str, int] = {"total": 0}
        for r in self._all(
            "SELECT status, COUNT(*) n FROM emails WHERE job_id=? GROUP BY status", [job_id]
        ):
            out[r["status"]] = r["n"]
            out["total"] += r["n"]
        for r in self._all(
            "SELECT stage, COUNT(*) n FROM emails WHERE job_id=? AND status='pending' GROUP BY stage",
            [job_id],
        ):
            out["stage_" + r["stage"]] = r["n"]
        out["role"] = (self._one(
            "SELECT COUNT(*) n FROM emails WHERE job_id=? AND is_role=1", [job_id]) or {"n": 0})["n"]
        out["typo_fixed"] = (self._one(
            "SELECT COUNT(*) n FROM emails WHERE job_id=? AND typo_fixed<>''", [job_id]) or {"n": 0})["n"]
        return out

    def reason_breakdown(self, job_id: int) -> List[Dict[str, Any]]:
        return self._all(
            "SELECT status, reason, COUNT(*) n FROM emails WHERE job_id=? AND status NOT IN ('pending') "
            "GROUP BY status, reason ORDER BY n DESC",
            [job_id],
        )

    def top_domains(self, job_id: int, limit: int = 25) -> List[Dict[str, Any]]:
        return self._all(
            "SELECT domain, COUNT(*) total, "
            "SUM(status='valid') valid, SUM(status='risky') risky, "
            "SUM(status='invalid') invalid, SUM(status='pending') pending "
            "FROM emails WHERE job_id=? AND status<>'duplicate' AND domain<>'' "
            "AND reason NOT LIKE 'Invalid syntax%' "
            "GROUP BY domain ORDER BY total DESC LIMIT ?",
            [job_id, limit],
        )

    def recent(self, job_id: int, limit: int = 25) -> List[Dict[str, Any]]:
        return self._all(
            "SELECT email, status, reason, domain, mx_host, is_role, updated_at FROM emails "
            "WHERE job_id=? AND status NOT IN ('pending','duplicate') "
            "ORDER BY updated_at DESC, id DESC LIMIT ?",
            [job_id, limit],
        )

    # ---------------------------------------------------------------- domain cache
    def get_domain(self, domain: str) -> Optional[Dict[str, Any]]:
        row = self._one("SELECT * FROM domains WHERE domain=?", [domain])
        if row:
            row["mx_hosts"] = json.loads(row.get("mx_hosts_json") or "[]")
        return row

    def upsert_domain(self, domain: str, **fields: Any) -> None:
        if "mx_hosts" in fields:
            fields["mx_hosts_json"] = json.dumps(fields.pop("mx_hosts"))
        with self._lock:
            exists = self._conn.execute("SELECT 1 FROM domains WHERE domain=?", (domain,)).fetchone()
            if exists:
                sets = ",".join("%s=?" % k for k in fields)
                self._conn.execute(
                    "UPDATE domains SET %s WHERE domain=?" % sets, list(fields.values()) + [domain]
                )
            else:
                keys = ["domain"] + list(fields)
                self._conn.execute(
                    "INSERT INTO domains (%s) VALUES (%s)" % (",".join(keys), ",".join("?" * len(keys))),
                    [domain] + list(fields.values()),
                )
            self._conn.commit()

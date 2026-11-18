from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .domain import BudgetExceeded, BusyRun, Phase


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "runs.sqlite3"
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, data TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
                lease_owner TEXT, lease_until REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                timestamp REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS costs (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, amount REAL NOT NULL,
                status TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER
            );
            """)

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def create(self, backend: dict, mode: str, *, run_id: str | None = None) -> str:
        run_id = run_id or uuid.uuid4().hex
        run = {
            "id": run_id,
            "phase": Phase.DIAGNOSING,
            "backend": backend,
            "mode": mode,
            "created_at": time.time(),
            "evidence": {},
            "diagnosis": None,
            "review": None,
            "approval": None,
            "proposal_digest": None,
            "review_round": 0,
        }
        with self.connect() as db:
            db.execute("INSERT INTO runs(id,data) VALUES (?,?)", (run_id, canonical(run)))
        self.event(run_id, "created", {"mode": mode, "backend_type": backend["kind"]})
        return run_id

    def get(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown run: {run_id}")
        return json.loads(row["data"])

    def update(self, run_id: str, *, expected: set[str] | None = None, **changes) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data,version FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            run = json.loads(row["data"])
            if expected is not None and run["phase"] not in expected:
                raise BusyRun(f"Expected phase {sorted(expected)}, got {run['phase']}")
            run.update(changes)
            run["updated_at"] = time.time()
            db.execute("UPDATE runs SET data=?,version=version+1 WHERE id=?", (canonical(run), run_id))
        return run

    def event(self, run_id: str, kind: str, payload: dict):
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(run_id,timestamp,kind,payload) VALUES (?,?,?,?)",
                (run_id, time.time(), kind, canonical(payload)),
            )

    def events(self, run_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    @contextmanager
    def lease(self, run_id: str, ttl: float = 420):
        owner = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute(
                "UPDATE runs SET lease_owner=?,lease_until=? WHERE id=? AND lease_until<?",
                (owner, time.time() + ttl, run_id, time.time()),
            )
            if updated.rowcount != 1:
                raise BusyRun("Run is owned by another worker; after a crash wait for its lease to expire")
        try:
            yield
        finally:
            with self.connect() as db:
                db.execute(
                    "UPDATE runs SET lease_owner=NULL,lease_until=0 WHERE id=? AND lease_owner=?",
                    (run_id, owner),
                )

    def reserve(self, run_id: str, amount: float, limit: float) -> str:
        if amount <= 0:
            raise ValueError("Reservation must be positive")
        request_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM costs WHERE status='overrun' LIMIT 1").fetchone():
                raise BudgetExceeded("A provider reservation was exceeded; further requests are blocked")
            spent = db.execute("SELECT COALESCE(SUM(amount),0) FROM costs").fetchone()[0]
            if spent + amount > limit:
                raise BudgetExceeded(
                    f"CNY budget would be exceeded: {spent:.4f} + {amount:.4f} > {limit:.2f}"
                )
            db.execute(
                "INSERT INTO costs(id,run_id,amount,status) VALUES (?,?,?,'reserved')",
                (request_id, run_id, amount),
            )
        return request_id

    def settle(self, request_id: str, actual: float, input_tokens: int, output_tokens: int):
        with self.connect() as db:
            row = db.execute("SELECT amount,status FROM costs WHERE id=?", (request_id,)).fetchone()
            if row is None or row["status"] != "reserved":
                raise ValueError("Unknown or already settled request")
            # A conservative reservation must be an upper bound, not merely an estimate.
            overrun = actual > row["amount"] + 1e-9
            db.execute(
                "UPDATE costs SET amount=?,status=?,input_tokens=?,output_tokens=? WHERE id=?",
                (actual, "overrun" if overrun else "settled", input_tokens, output_tokens, request_id),
            )
        if overrun:
            raise BudgetExceeded("Provider usage exceeded its reservation; remaining calls are blocked")

    def cost(self, run_ids: list[str] | None = None) -> dict:
        if run_ids == []:
            return {}
        where = " WHERE run_id IN (" + ",".join("?" for _ in run_ids) + ")" if run_ids is not None else ""
        with self.connect() as db:
            rows = db.execute(
                "SELECT status,SUM(amount) amount,COUNT(*) n FROM costs" + where + " GROUP BY status",
                run_ids or [],
            ).fetchall()
        return {r["status"]: {"cny": r["amount"], "requests": r["n"]} for r in rows}

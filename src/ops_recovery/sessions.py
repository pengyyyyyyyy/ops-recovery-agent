from __future__ import annotations

import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from .backends import backend_from_spec
from .coordinator import Coordinator
from .domain import Phase
from .locks import try_lock
from .storage import Store, canonical, digest

RECOVERABLE = {Phase.EXECUTING, Phase.UNKNOWN, Phase.VERIFYING}


class Sessions:
    def __init__(self, store: Store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS incidents (
                id TEXT PRIMARY KEY, backend TEXT NOT NULL, mode TEXT NOT NULL,
                constraints TEXT NOT NULL, created REAL NOT NULL, alert_key TEXT
            );
            CREATE TABLE IF NOT EXISTS incident_jobs (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES incidents(id), request_key TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES runs(id), kind TEXT NOT NULL,
                message TEXT NOT NULL, status TEXT NOT NULL, epoch TEXT, created REAL NOT NULL,
                started REAL, finished REAL, error TEXT, UNIQUE(session_id,request_key)
            );
            CREATE INDEX IF NOT EXISTS incident_queue ON incident_jobs(status, seq);
            CREATE TABLE IF NOT EXISTS incident_daemon (id INTEGER PRIMARY KEY CHECK(id=1), epoch TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS probe_watches (
                key TEXT PRIMARY KEY, failures INTEGER NOT NULL, successes INTEGER NOT NULL,
                session_id TEXT REFERENCES incidents(id)
            );
            CREATE TABLE IF NOT EXISTS probe_history (
                key TEXT NOT NULL, sample TEXT NOT NULL, observed REAL NOT NULL, healthy INTEGER NOT NULL,
                session_id TEXT REFERENCES incidents(id), event TEXT, PRIMARY KEY(key, sample)
            );
            """)

    def create(self, backend: dict, mode: str, message: str, constraints: list[str] | None = None):
        constraints = constraints or []
        if len(constraints) > 12 or any(len(value.encode()) > 1000 for value in constraints):
            raise ValueError("Use at most 12 constraints of 1000 bytes each")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._create(db, backend, mode, message, constraints)

    def _create(self, db, backend, mode, message, constraints, alert_key=None):
        session_id = uuid.uuid4().hex
        db.execute(
            "INSERT INTO incidents VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, canonical(backend), mode, canonical(constraints), time.time(), alert_key),
        )
        self._submit(db, session_id, backend, mode, message, "initial")
        return session_id

    def tell(self, session_id: str, message: str, request_key: str):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT backend,mode FROM incidents WHERE id=?", (session_id,)).fetchone()
            if not row:
                raise KeyError("Unknown incident session")
            if db.execute(
                "SELECT 1 FROM incident_jobs WHERE session_id=? AND kind='repair' AND status IN ('queued','running')",
                (session_id,),
            ).fetchone():
                raise ValueError("A repair or reconciliation is active for this incident")
            return self._submit(db, session_id, json.loads(row["backend"]), row["mode"], message, request_key)

    def _submit(self, db, session_id, backend, mode, message, request_key):
        if not 0 < len(message.strip().encode()) <= 6000 or not 0 < len(request_key) <= 128:
            raise ValueError("Use a nonempty message up to 6000 bytes and a request key up to 128 characters")
        existing = db.execute(
            "SELECT run_id,message,kind FROM incident_jobs WHERE session_id=? AND request_key=?",
            (session_id, request_key),
        ).fetchone()
        if existing:
            if existing["message"] != message or existing["kind"] != "diagnose":
                raise ValueError("Request key already belongs to a different request")
            return existing["run_id"]
        self._ensure_capacity(db)
        run_id = uuid.uuid4().hex
        self.store.insert_run(db, backend, mode, run_id, session_id=session_id)
        db.execute(
            """INSERT INTO incident_jobs(session_id,request_key,run_id,kind,message,status,created)
               VALUES (?, ?, ?, 'diagnose', ?, 'queued', ?)""",
            (session_id, request_key, run_id, message, time.time()),
        )
        return run_id

    @staticmethod
    def _ensure_capacity(db):
        pending = db.execute(
            "SELECT count(*) FROM incident_jobs WHERE status IN ('queued','running')"
        ).fetchone()[0]
        if pending >= 100:
            raise ValueError("Incident queue is full (100 pending jobs)")

    def watch(self, backend: dict, mode: str, *, key: str, healthy: bool, sample: str, threshold: int = 3):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM probe_watches WHERE key=?", (key,)).fetchone()
            if db.execute("SELECT 1 FROM probe_history WHERE key=? AND sample=?", (key, sample)).fetchone():
                return {"watch": key, "event": None, "duplicate": True}
            fails, successes, session_id = (
                (row["failures"], row["successes"], row["session_id"]) if row else (0, 0, None)
            )
            fails = 0 if healthy else min(fails + 1, threshold)
            successes = min(successes + 1, 2) if healthy else 0
            event = None
            if fails == threshold and session_id is None:
                session_id = self._create(
                    db,
                    backend,
                    mode,
                    "Business probe failed consecutively. Investigate current evidence.",
                    [],
                    key,
                )
                event = "alert_opened"
            if successes == 2 and session_id is not None:
                event = "alert_closed"
            incident = session_id
            if event:
                run_id = db.execute(
                    "SELECT run_id FROM incident_jobs WHERE session_id=? ORDER BY seq LIMIT 1", (session_id,)
                ).fetchone()[0]
                self.store.insert_event(db, run_id, event, {"watch": key})
                if event == "alert_closed":
                    session_id = None
            db.execute(
                "INSERT OR REPLACE INTO probe_watches VALUES (?, ?, ?, ?)",
                (key, fails, successes, session_id),
            )
            db.execute(
                "INSERT INTO probe_history VALUES (?,?,?,?,?,?)",
                (key, sample, time.time(), healthy, incident, event),
            )
            return {"watch": key, "active_session": session_id, "consecutive_failures": fails, "event": event}

    def list(self):
        with self.store.connect() as db:
            return [
                dict(row)
                for row in db.execute("""SELECT i.id,i.mode,i.created,i.alert_key,j.status,j.run_id
                FROM incidents i JOIN incident_jobs j ON j.seq=(SELECT max(seq) FROM incident_jobs WHERE session_id=i.id)
                ORDER BY i.created DESC LIMIT 100""")
            ]

    def history(self, session_id: str, *, before: int | None = None, limit: int = 100):
        with self.store.connect() as db:
            session = db.execute("SELECT * FROM incidents WHERE id=?", (session_id,)).fetchone()
            if not session:
                raise KeyError("Unknown incident session")
            rows = db.execute(
                """SELECT j.*, r.data FROM incident_jobs j JOIN runs r ON j.run_id=r.id
                WHERE j.session_id=? AND (? IS NULL OR j.seq<?) ORDER BY j.seq DESC LIMIT ?""",
                (session_id, before, before, limit),
            ).fetchall()
        return {
            **dict(session),
            "constraints": json.loads(session["constraints"]),
            "backend": json.loads(session["backend"]),
            "turns": [
                {
                    **{key: row[key] for key in row.keys() if key != "data"},
                    "run": self._public_run(json.loads(row["data"])),
                }
                for row in reversed(rows)
            ],
        }

    @staticmethod
    def _public_run(run):
        return {
            key: run.get(key)
            for key in ("phase", "diagnosis", "review", "proposal_digest", "proposal", "verification")
        }

    def context(self, job: dict):
        history = self.history(job["session_id"], before=job["seq"], limit=80)
        past = [turn for turn in history["turns"] if turn["kind"] == "diagnose"]
        query = set(re.findall(r"\w+", job["message"].casefold()))
        older = past[:-2]
        relevant = sorted(
            older,
            key=lambda t: (len(query.intersection(re.findall(r"\w+", t["message"].casefold()))), t["seq"]),
            reverse=True,
        )[:3]

        def extract(turn):
            result = turn["run"]["diagnosis"] or {}
            return {
                "turn": turn["seq"],
                "run_id": turn["run_id"],
                "message_excerpt": turn["message"][:380],
                "diagnosed_service": result.get("service"),
                "prior_summary": (result.get("summary") or "")[:330],
                "prior_evidence_ids": (result.get("evidence_ids") or [])[:6],
            }

        recent = past[-2:]
        return canonical(
            {
                "operator_constraints": history["constraints"],
                "current_message": job["message"],
                "recent_turns": [extract(t) for t in recent],
                "retrieved_prior_turns": [extract(t) for t in relevant],
                "archived_turns": len(older),
                "prior_evidence_note": "Use get_prior_turn to retrieve prior sources. Cite only current observations.",
            }
        )

    def request_repair(self, session_id, run_id, proposal_digest, *, reconcile=False):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
            run = json.loads(row[0]) if row else {}
            if run.get("session_id") != session_id:
                raise PermissionError("The run does not belong to this incident")
            if reconcile:
                if run["phase"] not in RECOVERABLE:
                    raise ValueError("The run has no pending operation to reconcile")
            else:
                if (
                    run["phase"] != Phase.WAITING_APPROVAL
                    or proposal_digest != run["proposal_digest"]
                    or digest(run["proposal"]) != proposal_digest
                ):
                    raise ValueError("The current proposal and full digest must match")
                if db.execute(
                    "SELECT 1 FROM incident_jobs WHERE session_id=? AND kind='diagnose' AND run_id<>? AND seq>(SELECT seq FROM incident_jobs WHERE run_id=? AND kind='diagnose')",
                    (session_id, run_id, run_id),
                ).fetchone():
                    raise ValueError("A more recent investigation exists; approve its proposal")
                epoch = db.execute("SELECT epoch FROM incident_daemon WHERE id=1").fetchone()
                if not epoch:
                    raise ValueError("Start the incident daemon before approving")
                approval = {
                    "actor": "human-cli",
                    "digest": proposal_digest,
                    "epoch": epoch[0],
                    "expires_at": time.time() + 600,
                }
                run.update(phase=Phase.APPROVED, approval=approval)
                db.execute("UPDATE runs SET data=?,version=version+1 WHERE id=?", (canonical(run), run_id))
                self.store.insert_event(db, run_id, "approval", approval)
            existing = db.execute(
                "SELECT seq FROM incident_jobs WHERE run_id=? AND kind='repair' AND status IN ('queued','running')",
                (run_id,),
            ).fetchone()
            if existing:
                return existing[0]
            self._ensure_capacity(db)
            return db.execute(
                """INSERT INTO incident_jobs(session_id,request_key,run_id,kind,message,status,created)
                VALUES (?, ?, ?, 'repair', '', 'queued', ?)""",
                (session_id, uuid.uuid4().hex, run_id, time.time()),
            ).lastrowid

    def recover(self, epoch: str):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR REPLACE INTO incident_daemon VALUES (1, ?)", (epoch,))
            rows = db.execute("""SELECT DISTINCT r.id, r.data FROM runs r JOIN incident_jobs j ON j.run_id=r.id
                WHERE j.status IN ('queued','running','paused')""").fetchall()
            for row in rows:
                run = json.loads(row["data"])
                phase, run_id = run["phase"], row["id"]
                db.execute("UPDATE runs SET lease_owner=NULL,lease_until=0 WHERE id=?", (run_id,))
                if phase == Phase.APPROVED:
                    run.update(phase=Phase.WAITING_APPROVAL, approval=None)
                    db.execute(
                        "UPDATE runs SET data=?,version=version+1 WHERE id=?", (canonical(run), run_id)
                    )
                    db.execute(
                        "UPDATE incident_jobs SET status='paused',finished=? WHERE run_id=? AND kind='repair' AND status IN ('queued','running')",
                        (time.time(), run_id),
                    )
                    self.store.insert_event(
                        db, run_id, "approval_invalidated", {"reason": "daemon restarted"}
                    )
                elif phase in RECOVERABLE:
                    db.execute(
                        """UPDATE incident_jobs SET status='queued',epoch=NULL WHERE seq=(SELECT max(seq)
                        FROM incident_jobs WHERE run_id=? AND kind='repair')""",
                        (run_id,),
                    )
            db.execute("UPDATE incident_jobs SET status='queued',epoch=NULL WHERE status='running'")

    def claim(self, epoch):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT * FROM incident_jobs j WHERE status='queued' AND NOT EXISTS
                (SELECT 1 FROM incident_jobs other WHERE other.session_id=j.session_id AND other.status='running')
                ORDER BY seq LIMIT 1""").fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE incident_jobs SET status='running',epoch=?,started=coalesce(started,?) WHERE seq=?",
                (epoch, time.time(), row["seq"]),
            )
            return dict(row)

    def finish(self, job, epoch, status, error=None):
        with self.store.connect() as db:
            db.execute(
                """UPDATE incident_jobs SET status=?,error=?,finished=?
                WHERE seq=? AND status='running' AND epoch=?""",
                (status, error, time.time(), job["seq"], epoch),
            )


class Daemon:
    def __init__(self, settings, *, dual=True, factory=backend_from_spec):
        self.settings, self.dual, self.factory = settings, dual, factory
        self.store = Store(settings.state_dir)
        self.sessions = Sessions(self.store)
        self.epoch = uuid.uuid4().hex

    def run(self, *, drain=False):
        with try_lock(self.store.path.with_name("incident-daemon.lock")) as acquired:
            if not acquired:
                raise RuntimeError("An incident daemon already owns this state directory")
            self.sessions.recover(self.epoch)
            try:
                with ThreadPoolExecutor(max_workers=10) as pool:
                    pending = set()
                    while True:
                        for future in list(pending):
                            if future.done():
                                future.result()
                                pending.remove(future)
                        while len(pending) < 10 and (job := self.sessions.claim(self.epoch)) is not None:
                            pending.add(pool.submit(self.process, job))
                        if drain and not pending:
                            return
                        time.sleep(0.1)
            finally:
                with self.store.connect() as db:
                    db.execute("DELETE FROM incident_daemon WHERE epoch=?", (self.epoch,))

    def process(self, job):
        run_id = job["run_id"]
        try:
            run = self.store.get(run_id)
            settings = replace(self.settings, mode=run["mode"])
            if run["mode"] == "online" and self.settings.mode != "online":
                raise ValueError("An online incident requires the daemon's online provider configuration")
            backend = self.factory(run["backend"])
            coordinator = Coordinator(settings, self.store)
            if job["kind"] == "diagnose":
                coordinator.diagnose(
                    backend, run_id=run_id, dual=self.dual, context=self.sessions.context(job)
                )
            else:
                scope_key = digest(
                    {"kind": run["backend"]["kind"], "directory": run["backend"].get("directory")}
                )
                lock_path = Path(self.settings.state_dir) / f"repair-{scope_key}.lock"
                with try_lock(lock_path) as acquired:
                    if not acquired:
                        # A conflicting target is left for the operator to review and enqueue again.
                        raise PermissionError("Another repair is active for this resource")
                    coordinator.resume(run_id, backend, epoch=self.epoch)
                if self.store.get(run_id)["phase"] == Phase.RESOLVED:
                    from .skills import Skills

                    try:
                        Skills(self.store).from_run(run_id, settings)
                    except Exception as error:
                        self.store.event(run_id, "skill_generation_failed", {"type": type(error).__name__})
            status = "paused" if self.store.get(run_id)["phase"] == Phase.UNKNOWN else "done"
            self.sessions.finish(job, self.epoch, status)
        except Exception as error:
            run = self.store.get(run_id)
            if job["kind"] == "repair" and run["phase"] == Phase.APPROVED:
                self.store.update(
                    run_id, expected={Phase.APPROVED}, phase=Phase.WAITING_APPROVAL, approval=None
                )
            self.sessions.finish(
                job,
                self.epoch,
                "paused" if job["kind"] == "repair" else "failed",
                f"{type(error).__name__}: {str(error)[:500]}",
            )

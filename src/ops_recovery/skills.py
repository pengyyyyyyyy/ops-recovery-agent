from __future__ import annotations

import json
import re
import time
import uuid

from pydantic import BaseModel, ConfigDict, Field

from .agent_loop import AgentLoop
from .domain import Phase
from .models import make_model
from .storage import Store, canonical, digest


class Guide(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=100)
    service: str = Field(min_length=1, max_length=120)
    required_signals: list[str] = Field(min_length=1, max_length=6)
    steps: list[str] = Field(min_length=1, max_length=6)
    stop_conditions: list[str] = Field(min_length=1, max_length=4)
    verify: str = Field(min_length=1, max_length=360)


class ReplayCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    group: str = Field(min_length=1, max_length=120)
    backend: str
    observations: dict[str, list[str]]
    expected_match: bool


def matches(guide, observations):
    return set(guide["required_signals"]).issubset(observations.get(guide["service"], []))


class Skills:
    def __init__(self, store: Store):
        self.store = store
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS incident_skills (
                id TEXT PRIMARY KEY, family TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL,
                backend TEXT NOT NULL, source_run TEXT NOT NULL REFERENCES runs(id), source_group TEXT NOT NULL,
                guide TEXT NOT NULL, hash TEXT NOT NULL, source TEXT NOT NULL,
                replay TEXT, approval TEXT, created REAL NOT NULL, UNIQUE(family,version), UNIQUE(source_run)
            )""")

    def from_run(self, run_id, settings, *, previous=None):
        with self.store.connect() as db:
            existing = db.execute("SELECT id FROM incident_skills WHERE source_run=?", (run_id,)).fetchone()
            if existing:
                return existing[0]
        run = self.store.get(run_id)
        if run["backend"]["kind"] not in {"lab", "umami"} or run["phase"] != Phase.RESOLVED:
            raise ValueError("Skill generation requires a verified repair from a lab or pilot run")
        diagnosis = run["diagnosis"]
        rows = [run["evidence"][key] for key in diagnosis["evidence_ids"]]
        signals = sorted(
            {
                x["signal"]
                for x in rows
                if x.get("service") == diagnosis["service"]
                and isinstance(x.get("signal"), str)
                and re.fullmatch(r"[a-z][a-z0-9_]{1,63}", x["signal"])
            }
        )
        if not signals:
            raise ValueError("No observed, structured root-cause signals available for a skill")
        source = {
            "signals": signals,
            "evidence_ids": diagnosis["evidence_ids"],
            "verified": True,
            "service": diagnosis["service"],
            "action": diagnosis["action"],
        }
        group = f"{run['backend']['kind']}:{diagnosis['root_cause']}"

        def validate(value):
            guide = Guide.model_validate(value)
            if guide.service != source["service"] or not set(guide.required_signals).issubset(signals):
                raise ValueError("Candidate conditions must be source observations on the diagnosed service")
            if any(not 0 < len(step) <= 360 for step in guide.steps + guide.stop_conditions):
                raise ValueError("Keep each instruction within 360 characters")
            return guide.model_dump()

        if settings.mode == "online":
            loop = AgentLoop(
                model=make_model(settings, self.store, run_id, "skill-author"),
                tools=[],
                max_steps=3,
                instructions="Draft a read-only incident playbook. Submit final_answer using the provided schema. The verify field is a single concise string. Use only the provided service and exact observed signal names. No commands, secrets, URLs, or authority to execute repairs.",
                answer_schema=Guide.model_json_schema(),
                check=validate,
                trace=lambda kind, data: self.store.event(run_id, kind, {**data, "role": "skill-author"}),
            )
            guide = validate(loop.run(canonical(source)))
        else:
            guide = validate(
                {
                    "title": f"Investigate {source['service']}",
                    "service": source["service"],
                    "required_signals": signals[:1],
                    "steps": [
                        "Check service state and dependency health.",
                        "Retrieve the matching observations and check for contradictory telemetry.",
                    ],
                    "stop_conditions": ["Escalate if the observed component or signals differ."],
                    "verify": "Confirm the approved remediation via the business probe.",
                }
            )
        skill_id = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id FROM incident_skills WHERE source_run=?", (run_id,)).fetchone()
            if existing:
                return existing[0]
            family, version = skill_id, 1
            if previous:
                parent = db.execute(
                    "SELECT family,backend FROM incident_skills WHERE id=?", (previous,)
                ).fetchone()
                if not parent or parent["backend"] != run["backend"]["kind"]:
                    raise ValueError("Prior version must belong to the same backend")
                family = parent["family"]
                version = db.execute(
                    "SELECT max(version)+1 FROM incident_skills WHERE family=?", (family,)
                ).fetchone()[0]
            db.execute(
                "INSERT INTO incident_skills VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL,?)",
                (
                    skill_id,
                    family,
                    version,
                    "candidate",
                    run["backend"]["kind"],
                    run_id,
                    group,
                    canonical(guide),
                    digest(guide),
                    canonical({**source, "mode": settings.mode}),
                    time.time(),
                ),
            )
            self.store.insert_event(
                db, run_id, "skill_candidate_created", {"skill_id": skill_id, "version": version}
            )
        return skill_id

    def show(self, skill_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM incident_skills WHERE id=?", (skill_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown skill version")
        return {
            **dict(row),
            **{k: json.loads(row[k]) if row[k] else None for k in ("guide", "source", "replay", "approval")},
        }

    def list(self):
        with self.store.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,family,version,backend,status,hash FROM incident_skills ORDER BY created DESC LIMIT 100"
                )
            ]

    def replay(self, skill_id, values):
        skill = self.show(skill_id)
        cases = [ReplayCase.model_validate(case) for case in values]
        if not 2 <= len(cases) <= 100 or len({c.id for c in cases}) != len(cases):
            raise ValueError("Replay requires 2 to 100 unique cases")
        if {c.expected_match for c in cases} != {True, False} or any(
            c.group == skill["source_group"] or c.backend != skill["backend"] for c in cases
        ):
            raise ValueError(
                "Provide separate replay groups with both matches and nonmatches for this backend"
            )
        outcomes = [
            {
                "id": c.id,
                "group": c.group,
                "expected": c.expected_match,
                "matched": matches(skill["guide"], c.observations),
            }
            for c in cases
        ]
        report = {
            "guide_hash": skill["hash"],
            "replay_hash": digest(values),
            "type": "static applicability",
            "passed": all(x["expected"] == x["matched"] for x in outcomes),
            "cases": outcomes,
        }
        with self.store.connect() as db:
            result = db.execute(
                "UPDATE incident_skills SET status=?,replay=? WHERE id=? AND status IN ('candidate','replay_passed')",
                ("replay_passed" if report["passed"] else "candidate", canonical(report), skill_id),
            )
            if result.rowcount != 1:
                raise ValueError("Replay a new candidate version to change an active or disabled guide")
        return report

    def enable(self, skill_id, guide_hash, replay_hash):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM incident_skills WHERE id=?", (skill_id,)).fetchone()
            if (
                not row
                or row["status"] != "replay_passed"
                or row["hash"] != guide_hash
                or json.loads(row["replay"])["replay_hash"] != replay_hash
            ):
                raise ValueError("Approval requires both the exact guide hash and passing replay hash")
            db.execute(
                "UPDATE incident_skills SET status='disabled' WHERE family=? AND status='active'",
                (row["family"],),
            )
            db.execute(
                "UPDATE incident_skills SET status='active',approval=? WHERE id=?",
                (
                    canonical(
                        {
                            "actor": "human-cli",
                            "at": time.time(),
                            "guide_hash": guide_hash,
                            "replay_hash": replay_hash,
                        }
                    ),
                    skill_id,
                ),
            )

    def disable(self, skill_id):
        with self.store.connect() as db:
            if (
                db.execute(
                    "UPDATE incident_skills SET status='disabled' WHERE id=? AND status='active'", (skill_id,)
                ).rowcount
                != 1
            ):
                raise ValueError("Only an active guide can be disabled")

    def match(self, backend, evidence):
        observations: dict[str, list[str]] = {}
        for row in evidence:
            if not row["id"].startswith("rb-") and row.get("service") and row.get("signal"):
                observations.setdefault(row["service"], []).append(row["signal"])
        if not observations:
            return []
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT id, version, hash, guide FROM incident_skills WHERE backend=? AND status='active' ORDER BY created DESC LIMIT 100",
                (backend,),
            ).fetchall()
        result = []
        for row in rows:
            guide = json.loads(row["guide"])
            if matches(guide, observations):
                result.append(
                    {
                        "id": "rb-skill-" + row["id"],
                        "kind": "documentation",
                        "service": guide["service"],
                        "version": row["version"],
                        "hash": row["hash"],
                        "guide": guide,
                    }
                )
        return result[:2]

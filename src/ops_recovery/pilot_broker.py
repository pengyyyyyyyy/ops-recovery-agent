from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .locks import try_lock
from .storage import canonical, digest
from .umami import PILOT_ACTIONS

SERVICES = {"umami": "umami", "postgres": "db"}
OPERATION = re.compile(r"repair-[0-9a-f]{32}\Z")


class Pilot:
    """Root-owned local broker. No user argument is interpolated into a command."""

    def __init__(self, directory: Path, state: Path):
        self.directory = directory.resolve()
        self.state = state.resolve()
        self.ledger = self.directory / "actions.sqlite3"
        self.active = self.directory / "active.env"
        self.good = self.directory / "known-good.env"
        with sqlite3.connect(self.ledger) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS actions (
                id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, result TEXT NOT NULL
            )""")

    def command(self, args, timeout=12):
        return subprocess.run(
            args,
            cwd=self.directory,
            capture_output=True,
            text=True,
            errors="replace",
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C.UTF-8"},
            timeout=timeout,
            check=False,
        )

    def compose(self, *args, timeout=12):
        return self.command(
            [
                "docker",
                "compose",
                "--env-file",
                ".env",
                "--env-file",
                "images.env",
                "--env-file",
                "active.env",
                *args,
            ],
            timeout=timeout,
        )

    def containers(self):
        result = self.compose("ps", "-a", "--format", "json")
        if result.returncode:
            raise RuntimeError("Compose status unavailable")
        contents = result.stdout.strip()
        rows = (
            json.loads(contents)
            if contents.startswith("[")
            else [json.loads(line) for line in contents.splitlines()]
        )
        result = {name: {"id": "", "state": "missing", "health": ""} for name in SERVICES}
        for row in rows:
            for name, target in SERVICES.items():
                if row.get("Service") == target and re.fullmatch(r"[0-9a-f]{12,64}", row.get("ID", "")):
                    result[name] = {
                        "id": row["ID"],
                        "state": row.get("State", "unknown"),
                        "health": row.get("Health", ""),
                    }
        return result

    def scope(self):
        containers = self.containers()
        app = containers["umami"]["id"]
        started = (
            self.command(
                ["docker", "inspect", "--format", "{{.State.StartedAt}} {{.State.FinishedAt}}", app]
            ).stdout.strip()
            if app
            else ""
        )
        return {
            "target": "ops-recovery-umami/umami",
            "container": app,
            "start_finish": started,
            "database": containers["postgres"]["id"],
            "configuration": hashlib.sha256(self.active.read_bytes()).hexdigest(),
            "known_good": hashlib.sha256(self.good.read_bytes()).hexdigest(),
        }

    def last_probe(self):
        result = self.command(
            [
                "systemctl",
                "show",
                "ops-pilot-probe.service",
                "--property=Result,ExecMainExitTimestampMonotonic,ExecMainStartTimestampMonotonic,ActiveState",
            ]
        )
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if result.returncode or fields.get("ActiveState") in {"activating", "active"}:
            return {"sample": None, "healthy": None}
        ended = fields.get("ExecMainExitTimestampMonotonic")
        if not ended or ended == "0":
            return {"sample": None, "healthy": None}
        return {
            "sample": f"{Path('/proc/sys/kernel/random/boot_id').read_text().strip()}:{ended}",
            "healthy": fields.get("Result") == "success",
        }

    def health(self):
        return self.last_probe()

    def read(self, tool, params):
        service = params.get("service", "all")
        if service not in {*SERVICES, "all"}:
            raise ValueError("Unknown service")
        if tool == "list_services":
            return {"services": list(SERVICES), "evidence": []}
        if tool == "search_runbooks":
            return {
                "evidence": [
                    {
                        "id": "rb-pilot",
                        "kind": "documentation",
                        "message": "Check Umami process, the business probe, and PostgreSQL. Escalate database faults. Approved application restart and known-good config rollback are the only possible repairs.",
                    }
                ]
            }
        if tool not in {"query_metrics", "query_logs"}:
            raise ValueError("Unknown read tool")
        status = self.containers()
        if tool == "query_metrics":
            rows = [
                {
                    "service": name,
                    "kind": "metric",
                    "container_state": row["state"],
                    "health": row["health"],
                    "signal": ("database_unreachable" if name == "postgres" else "umami_exited")
                    if row["state"] != "running"
                    else "container_running",
                }
                for name, row in status.items()
            ]
            probe = self.last_probe()
            if probe["healthy"] is not None:
                rows.append(
                    {
                        "service": "umami",
                        "kind": "metric",
                        "signal": "business_probe_succeeded" if probe["healthy"] else "business_probe_failed",
                    }
                )
            if self.active.read_bytes() != self.good.read_bytes():
                rows.append({"service": "umami", "kind": "metric", "signal": "managed_config_drift"})
            if status["postgres"]["health"] == "unhealthy":
                rows.append({"service": "postgres", "kind": "metric", "signal": "database_unreachable"})
        else:
            rows = []
            for name, target in SERVICES.items():
                if service not in {name, "all"}:
                    continue
                result = self.compose("logs", "--no-color", "--since", "5m", "--tail", "80", target)
                if result.returncode:
                    raise RuntimeError("Pilot logs unavailable")
                log = (result.stdout + result.stderr)[-80_000:].lower()
                patterns = {
                    "umami": {
                        "upstream_db_error": ("can't reach database", "econnrefused", "p1001"),
                        "dependency_timeout_observed": (
                            "connection timeout",
                            "connection timed out",
                            "etimedout",
                        ),
                    },
                    "postgres": {
                        "db_shutdown_observed": ("database system is shut down",),
                        "db_auth_error_observed": ("password authentication failed",),
                    },
                }[name]
                for signal, markers in patterns.items():
                    if any(marker in log for marker in markers):
                        rows.append({"service": name, "kind": "log", "signal": signal, "window_seconds": 300})
        return {
            "evidence": [
                {"id": "pilot-" + digest(row)[:20], **row}
                for row in rows
                if service in {row["service"], "all"}
            ]
        }

    def operation(self, operation_id):
        if not OPERATION.fullmatch(operation_id):
            raise ValueError("Invalid operation ID")
        with sqlite3.connect(self.ledger) as db:
            row = db.execute("SELECT result FROM actions WHERE id=?", (operation_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def check_approval(self, operation_id, action, scope):
        with sqlite3.connect(self.state.as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute(
                "SELECT data FROM runs WHERE id=?", (operation_id.removeprefix("repair-"),)
            ).fetchone()
            epoch = db.execute("SELECT epoch FROM incident_daemon WHERE id=1").fetchone()
            newest = db.execute(
                """SELECT j.run_id FROM incident_jobs j WHERE j.kind='diagnose' AND j.session_id=(
                SELECT session_id FROM incident_jobs WHERE run_id=? AND kind='diagnose') ORDER BY seq DESC LIMIT 1""",
                (operation_id.removeprefix("repair-"),),
            ).fetchone()
        run = json.loads(row[0]) if row else {}
        approval = run.get("approval") or {}
        proposal = run.get("proposal") or {}
        if (
            run.get("phase") != "executing"
            or run.get("operation_id") != operation_id
            or run.get("backend", {}).get("kind") != "umami"
            or not (epoch and newest and newest[0] == run["id"] and approval.get("epoch") == epoch[0])
            or approval.get("actor") != "human-cli"
            or approval.get("expires_at", 0) <= time.time()
            or approval.get("digest") != digest(proposal)
            or run.get("proposal_digest") != digest(proposal)
            or proposal.get("action") != action
            or proposal.get("service") != "umami"
            or scope != proposal.get("scope")
            or scope != self.scope()
        ):
            raise PermissionError("The current incident and resource version require a valid human approval")

    def repair(self, payload):
        operation_id, action, scope = payload["operation_id"], payload["action"], payload["scope"]
        if (
            set(payload) != {"operation_id", "action", "scope"}
            or action not in PILOT_ACTIONS
            or not OPERATION.fullmatch(operation_id)
        ):
            raise ValueError("Invalid pilot repair request")
        with try_lock(self.directory / "umami-repair.lock") as acquired:
            if not acquired:
                raise PermissionError("A repair is already active for Umami")
            request_hash = digest(payload)
            with sqlite3.connect(self.ledger) as db:
                previous = db.execute(
                    "SELECT request_hash,result FROM actions WHERE id=?", (operation_id,)
                ).fetchone()
                if previous:
                    if previous[0] != request_hash:
                        raise PermissionError("Operation ID belongs to a different request")
                    return json.loads(previous[1])
            self.check_approval(operation_id, action, scope)
            if action == "rollback_umami_config":
                config = self.good.read_bytes()
                if config != b"UMAMI_PORT=3000\n":
                    raise PermissionError("Known-good configuration is outside the managed schema")
                if self.active.read_bytes() == config:
                    raise PermissionError("No managed configuration change to roll back")
            marker = {"operation_id": operation_id, "action": action, "completed": False}
            with sqlite3.connect(self.ledger) as db:
                db.execute(
                    "INSERT INTO actions VALUES (?, ?, ?)", (operation_id, request_hash, canonical(marker))
                )
            if action == "restart_umami":
                result = self.compose("restart", "--timeout", "10", "umami", timeout=40)
            else:
                staging = self.directory / "active.env.new"
                staging.write_bytes(config)
                os.chmod(staging, 0o600)
                os.replace(staging, self.active)
                result = self.compose("up", "-d", "--no-deps", "umami", timeout=100)
            if result.returncode:
                raise RuntimeError("Action did not confirm success; check the broker ledger")
            complete = {**marker, "completed": True, "completed_at": time.time()}
            with sqlite3.connect(self.ledger) as db:
                db.execute("UPDATE actions SET result=? WHERE id=?", (canonical(complete), operation_id))
            return complete

    def probe(self):
        results = []
        # Give a restarted application time to become ready without rerunning the action.
        for attempt in range(12):
            started = time.monotonic()
            result = self.command(["bash", "pilot-probe.sh"], timeout=20)
            results.append(
                {
                    "status": 200 if result.returncode == 0 else 503,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                }
            )
            if len(results) >= 3 and all(row["status"] == 200 for row in results[-3:]):
                break
            if result.returncode and attempt < 11:
                time.sleep(2)
        return {"probes": results[-3:]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Do not log browser, operator, or request bodies here.
        pass

    def respond(self, method):
        pilot = self.server.pilot
        parsed = urlsplit(self.path)
        status = 200
        try:
            if method == "GET" and parsed.path == "/scope":
                data = pilot.scope()
            elif method == "GET" and parsed.path == "/health":
                data = pilot.health()
            elif method == "GET" and parsed.path.startswith("/read/"):
                params = {key: values[0] for key, values in parse_qs(parsed.query, max_num_fields=4).items()}
                data = pilot.read(parsed.path.removeprefix("/read/"), params)
            elif method == "GET" and parsed.path.startswith("/operations/"):
                data = {"operation": pilot.operation(parsed.path.removeprefix("/operations/"))}
            elif method == "POST" and parsed.path in {"/repair", "/probe"}:
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= 8192:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(size))
                data = pilot.repair(body) if parsed.path == "/repair" else pilot.probe()
            else:
                status, data = 404, {"error": "Unknown endpoint"}
        except PermissionError:
            status, data = 403, {"error": "Action policy rejected the request"}
        except (KeyError, ValueError, TypeError):
            status, data = 400, {"error": "Invalid parameters"}
        except (OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError):
            status, data = 503, {"error": "Pilot dependency unavailable"}
        content = canonical(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        self.respond("GET")

    def do_POST(self):
        self.respond("POST")


def main():
    from socketserver import ThreadingMixIn, UnixStreamServer

    class Server(ThreadingMixIn, UnixStreamServer):
        daemon_threads = True

    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o007)
    if args.socket.is_socket():
        args.socket.unlink()
    with Server(str(args.socket), Handler) as server:
        server.pilot = Pilot(args.directory, args.state)
        os.chmod(args.socket, 0o660)
        server.serve_forever()


if __name__ == "__main__":
    main()

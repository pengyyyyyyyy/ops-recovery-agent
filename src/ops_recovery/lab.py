"""A loopback-only, token-protected supervisor and two real child HTTP services."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from .domain import ACTIONS
from .storage import canonical, digest

FAULTS = {
    "api_crash": {"service": "gateway", "root_cause": "process_crash", "action": "restart_api"},
    "dependency_timeout": {
        "service": "inventory",
        "root_cause": "dependency_timeout",
        "action": "reset_dependency_delay",
    },
    "database_lock": {
        "service": "database",
        "root_cause": "database_lock",
        "action": "release_database_lock",
    },
}
RUNBOOKS = [
    {
        "id": "rb-process",
        "service": "gateway",
        "text": "A missing gateway process with an unreachable health endpoint indicates process failure. restart_api starts only the owned gateway if it is down.",
    },
    {
        "id": "rb-dependency",
        "service": "inventory",
        "text": "Gateway upstream timeout with a healthy inventory process can indicate slow inventory responses. Check request latency. reset_dependency_delay resets the lab inventory delay configuration.",
    },
    {
        "id": "rb-database",
        "service": "database",
        "text": "SQLite 'database is locked' during checkout indicates lock contention. release_database_lock stops only the lab-owned lock holder.",
    },
]


def child_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not any(part in key.upper() for part in ("API_KEY", "PASSWORD", "SECRET", "ACCESS_TOKEN"))
    }


def atomic_json(path: Path, value: dict):
    temporary = path.with_suffix(".tmp-" + uuid.uuid4().hex)
    temporary.write_text(canonical(value), encoding="utf-8")
    temporary.replace(path)


def free_port() -> int:
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def emit(directory: Path, service: str, signal: str, **fields):
    configuration = read_json(directory / "configuration.json")
    entry = {
        "id": uuid.uuid4().hex,
        "timestamp": time.time(),
        "service": service,
        "signal": signal,
        "incident": configuration["generation"],
        **fields,
    }
    with (directory / f"{service}.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(canonical(entry) + "\n")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status: int, value: dict):
        body = canonical(value).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 65536:
            raise ValueError("Request body must be between 1 and 65536 bytes")
        return json.loads(self.rfile.read(length))


def serve_child(role: str, directory: Path, port: int):
    class ServiceHandler(Handler):
        def do_GET(self):
            if self.path == "/health":
                return self.reply(200, {"healthy": True})
            configuration = read_json(directory / "configuration.json")
            start = time.monotonic()
            if role == "inventory" and self.path == "/inventory":
                time.sleep(configuration["inventory_delay"])
                emit(
                    directory,
                    "inventory",
                    "request",
                    incident=configuration["generation"],
                    status=200,
                    latency_ms=round((time.monotonic() - start) * 1000, 2),
                )
                return self.reply(200, {"available": 10})
            if role == "gateway" and self.path == "/checkout":
                try:
                    with sqlite3.connect(directory / "application.sqlite3", timeout=0.12) as database:
                        database.execute("UPDATE stock SET requests=requests+1 WHERE id=1")
                    with urlopen(
                        f"http://127.0.0.1:{configuration['inventory_port']}/inventory", timeout=0.35
                    ) as response:
                        response.read()
                    emit(
                        directory,
                        "gateway",
                        "request",
                        incident=configuration["generation"],
                        status=200,
                        latency_ms=round((time.monotonic() - start) * 1000, 2),
                    )
                    return self.reply(200, {"checkout": "ok"})
                except sqlite3.OperationalError as error:
                    emit(
                        directory,
                        "database",
                        "database_locked",
                        incident=configuration["generation"],
                        message=str(error),
                    )
                    emit(
                        directory,
                        "gateway",
                        "request",
                        incident=configuration["generation"],
                        status=503,
                        latency_ms=round((time.monotonic() - start) * 1000, 2),
                    )
                    return self.reply(503, {"error": "database unavailable"})
                except (URLError, TimeoutError, OSError) as error:
                    emit(
                        directory,
                        "gateway",
                        "upstream_timeout",
                        incident=configuration["generation"],
                        dependency="inventory",
                        message=type(error).__name__,
                    )
                    emit(
                        directory,
                        "gateway",
                        "request",
                        incident=configuration["generation"],
                        status=504,
                        latency_ms=round((time.monotonic() - start) * 1000, 2),
                    )
                    return self.reply(504, {"error": "upstream request failed"})
            return self.reply(404, {"error": "unknown route"})

    ThreadingHTTPServer(("127.0.0.1", port), ServiceHandler).serve_forever()


class Supervisor:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.children: dict[str, subprocess.Popen] = {}
        self.operation_lock = threading.Lock()
        self.files = []
        self.token = secrets.token_urlsafe(32)
        self.lab_id = uuid.uuid4().hex
        self.configuration = {
            "lab_id": self.lab_id,
            "generation": uuid.uuid4().hex,
            "inventory_port": free_port(),
            "gateway_port": free_port(),
            "inventory_delay": 0,
        }
        atomic_json(directory / "configuration.json", self.configuration)
        with sqlite3.connect(directory / "application.sqlite3") as database:
            database.execute("CREATE TABLE IF NOT EXISTS stock(id INTEGER PRIMARY KEY, requests INTEGER)")
            database.execute("INSERT OR IGNORE INTO stock VALUES (1,0)")
        with sqlite3.connect(directory / "operations.sqlite3") as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, request_hash TEXT, result TEXT)"
            )
        self.spawn("inventory", self.configuration["inventory_port"])
        self.spawn("gateway", self.configuration["gateway_port"])

    def spawn(self, role: str, port: int = 0):
        previous = self.children.get(role)
        if previous and previous.poll() is None:
            return
        log = (self.directory / f"{role}-process.log").open("ab")
        self.files.append(log)
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        )
        self.children[role] = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ops_recovery.lab",
                "--role",
                role,
                "--directory",
                str(self.directory),
                "--port",
                str(port),
            ],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            env=child_environment(),
            **options,
        )
        if role != "locker":
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                try:
                    with urlopen(f"http://127.0.0.1:{port}/health", timeout=0.2):
                        return
                except (OSError, URLError):
                    time.sleep(0.05)
            raise RuntimeError(f"{role} did not become ready")

    def stop(self, role: str):
        process = self.children.get(role)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def close(self):
        for role in list(self.children):
            self.stop(role)
        for stream in self.files:
            stream.close()

    def scope(self) -> dict:
        return {key: self.configuration[key] for key in ("lab_id", "generation")}

    def probe(self) -> list[dict]:
        results = []
        for _ in range(3):
            start = time.monotonic()
            try:
                with urlopen(
                    f"http://127.0.0.1:{self.configuration['gateway_port']}/checkout", timeout=1
                ) as response:
                    results.append(
                        {"status": response.status, "latency_ms": round((time.monotonic() - start) * 1000, 2)}
                    )
            except Exception as error:
                results.append({"status": getattr(error, "code", 0), "error": type(error).__name__})
        return results

    def logs(self) -> list[dict]:
        entries = []
        for service in ("gateway", "inventory", "database"):
            path = self.directory / f"{service}.jsonl"
            if path.exists():
                # Per-case logs are small in the lab. Public corpora have a separate bounded adapter.
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        row = json.loads(line)
                        if row["incident"] == self.configuration["generation"]:
                            entries.append(row)
                    except json.JSONDecodeError:
                        continue  # A writer may currently be appending its last line.
        return sorted(entries, key=lambda row: row["timestamp"])[-80:]

    def read(self, name: str, arguments: dict) -> dict:
        service = arguments.get("service", "all")
        if service not in {"all", "gateway", "inventory", "database"}:
            raise ValueError("Unknown service")
        if name == "list_services":
            rows = [
                {
                    "id": "status-" + role + "-" + str(process.poll() is None),
                    "service": role,
                    "signal": "process_alive",
                    "value": process.poll() is None,
                }
                for role, process in self.children.items()
                if role != "locker"
            ]
            return {"scope": self.scope(), "evidence": rows, "services": ["gateway", "inventory", "database"]}
        if name == "query_logs":
            return {"evidence": [row for row in self.logs() if service == "all" or row["service"] == service]}
        if name == "query_metrics":
            rows = self.logs()
            metrics = []
            for target in ("gateway", "inventory"):
                requests = [r for r in rows if r["service"] == target and r["signal"] == "request"]
                if requests and service in {"all", target}:
                    metric = {
                        "service": target,
                        "signal": "request_summary",
                        "count": len(requests),
                        "errors": sum(r["status"] >= 400 for r in requests),
                        "max_latency_ms": max(r["latency_ms"] for r in requests),
                    }
                    metrics.append({"id": "metric-" + digest(metric)[:16], **metric})
            return {"evidence": metrics}
        if name == "search_runbooks":
            if len(arguments.get("query", "")) > 200:
                raise ValueError("Runbook query too long")
            return {"evidence": RUNBOOKS}
        raise ValueError("Tool not in read allowlist")

    def inject(self, fault: str) -> dict:
        if fault not in FAULTS:
            raise ValueError("Unknown fault")
        self.stop("locker")
        self.configuration.update(generation=uuid.uuid4().hex, inventory_delay=0)
        atomic_json(self.directory / "configuration.json", self.configuration)
        self.spawn("gateway", self.configuration["gateway_port"])
        if fault == "api_crash":
            self.stop("gateway")
            emit(self.directory, "gateway", "process_exited", message="owned gateway process is not running")
        elif fault == "dependency_timeout":
            self.configuration["inventory_delay"] = 1.2
            atomic_json(self.directory / "configuration.json", self.configuration)
        else:
            ready = self.directory / "lock-ready"
            ready.unlink(missing_ok=True)
            self.spawn("locker")
            deadline = time.monotonic() + 5
            while not ready.exists():
                if time.monotonic() > deadline:
                    raise RuntimeError("Database lock holder did not start")
                time.sleep(0.02)
        return {"scope": self.scope(), "probes": self.probe()}

    def operation(self, operation_id: str) -> dict | None:
        with sqlite3.connect(self.directory / "operations.sqlite3") as database:
            row = database.execute("SELECT result FROM operations WHERE id=?", (operation_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def repair(self, payload: dict) -> dict:
        if set(payload) - {"operation_id", "action", "scope", "drop_response"}:
            raise ValueError("Unknown repair parameters")
        action, operation_id = payload["action"], payload["operation_id"]
        if action not in ACTIONS or not isinstance(operation_id, str) or len(operation_id) > 100:
            raise ValueError("Invalid action or idempotency key")
        request_hash = digest({key: payload[key] for key in ("action", "scope")})
        with self.operation_lock:
            with sqlite3.connect(self.directory / "operations.sqlite3") as database:
                previous = database.execute(
                    "SELECT request_hash,result FROM operations WHERE id=?", (operation_id,)
                ).fetchone()
                if previous:
                    if previous[0] != request_hash:
                        raise ValueError("Idempotency key cannot be reused with a different request")
                    return {**json.loads(previous[1]), "replayed": True}
                if payload["scope"] != self.scope():
                    raise ValueError("Approval scope is stale; a different incident is active")
                if action == "restart_api":
                    # Desired-state semantics: starting an already healthy process is a no-op.
                    self.spawn("gateway", self.configuration["gateway_port"])
                elif action == "reset_dependency_delay":
                    self.configuration["inventory_delay"] = 0
                    atomic_json(self.directory / "configuration.json", self.configuration)
                else:
                    self.stop("locker")
                result = {
                    "operation_id": operation_id,
                    "action": action,
                    "scope": self.scope(),
                    "completed": True,
                    "completed_at": time.time(),
                    "replayed": False,
                }
                database.execute(
                    "INSERT INTO operations VALUES (?,?,?)", (operation_id, request_hash, canonical(result))
                )
            emit(
                self.directory, ACTIONS[action], "repair_completed", action=action, operation_id=operation_id
            )
            return result


def serve_supervisor(directory: Path):
    supervisor = Supervisor(directory)

    class ControlHandler(Handler):
        def authorized(self):
            return secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + supervisor.token)

        def do_GET(self):
            if not self.authorized():
                return self.reply(403, {"error": "permission denied"})
            path = urlsplit(self.path)
            if path.path == "/scope":
                return self.reply(200, supervisor.scope())
            if path.path.startswith("/operations/"):
                result = supervisor.operation(path.path.rsplit("/", 1)[1])
                return self.reply(200, {"operation": result})
            if path.path.startswith("/read/"):
                arguments = {key: values[0] for key, values in parse_qs(path.query).items()}
                try:
                    return self.reply(200, supervisor.read(path.path.rsplit("/", 1)[1], arguments))
                except ValueError as error:
                    return self.reply(400, {"error": str(error)})
            return self.reply(404, {"error": "unknown route"})

        def do_POST(self):
            if not self.authorized():
                return self.reply(403, {"error": "permission denied"})
            try:
                payload = self.body()
                with supervisor.operation_lock if self.path == "/inject" else threading.Lock():
                    if self.path == "/inject":
                        return self.reply(200, supervisor.inject(payload["fault"]))
                    if self.path == "/probe":
                        return self.reply(200, {"probes": supervisor.probe()})
                    if self.path == "/repair":
                        result = supervisor.repair(payload)
                        if payload.get("drop_response") and not result["replayed"]:
                            self.close_connection = (
                                True  # Commit succeeds; the client sees a transport failure.
                            )
                            return
                        return self.reply(200, result)
                    if self.path == "/shutdown":
                        self.reply(200, {"stopping": True})
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
                        return
                    return self.reply(404, {"error": "unknown route"})
            except (ValueError, KeyError) as error:
                return self.reply(400, {"error": str(error)})
            except Exception as error:
                return self.reply(500, {"error": type(error).__name__})

    server = ThreadingHTTPServer(("127.0.0.1", 0), ControlHandler)
    atomic_json(
        directory / "manifest.json",
        {
            "url": f"http://127.0.0.1:{server.server_port}",
            "token": supervisor.token,
            "lab_id": supervisor.lab_id,
            "pid": os.getpid(),
        },
    )
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        supervisor.close()
        server.server_close()


def start_lab(directory: Path) -> dict:
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.json"
    if manifest.exists():
        from .backends import LabBackend

        try:
            LabBackend(directory).scope()
            return read_json(manifest)
        except Exception:
            manifest.unlink()
    options = (
        {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    )
    with (directory / "supervisor-process.log").open("ab") as stream:
        process = subprocess.Popen(
            [sys.executable, "-m", "ops_recovery.lab", "--role", "supervisor", "--directory", str(directory)],
            stdout=stream,
            stderr=stream,
            stdin=subprocess.DEVNULL,
            env=child_environment(),
            **options,
        )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if manifest.exists():
            return read_json(manifest)
        if process.poll() is not None:
            break
        time.sleep(0.05)
    process.terminate()
    raise RuntimeError(f"Lab startup failed; inspect {directory / 'supervisor-process.log'}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=["supervisor", "gateway", "inventory", "locker"], required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    arguments = parser.parse_args()
    if arguments.role == "locker":
        database = sqlite3.connect(arguments.directory / "application.sqlite3")
        database.execute("BEGIN EXCLUSIVE")
        (arguments.directory / "lock-ready").write_text("ready")
        threading.Event().wait()
    elif arguments.role == "supervisor":
        serve_supervisor(arguments.directory)
    else:
        serve_child(arguments.role, arguments.directory, arguments.port)


if __name__ == "__main__":
    main()

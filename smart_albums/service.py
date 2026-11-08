"""One-shot and cron execution, locking and secret-free status files."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

from .api import APIError, ImmichClient
from .config import ConfigError, load_api_key
from .engine import run_job


class AlreadyRunning(RuntimeError):
    pass


def log(event, *, tz=timezone.utc, **fields):
    """Display event times in the configured zone; use UTC before config is valid."""
    print(json.dumps({"time": datetime.now(tz).isoformat(), "event": event, **fields},
                     ensure_ascii=False), flush=True)


def state_dir():
    path = Path(os.getenv("SMART_ALBUMS_STATE_DIR", "/data"))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def write_state(path, data):
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextlib.contextmanager
def run_lock(directory):
    with open(directory / "run.lock", "a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning("A sync is already running; skipped to prevent overlap") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def run_cycle(cfg, directory, force_dry_run=False, selected_job=None):
    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    with run_lock(directory):
        key = load_api_key(cfg)
        client = ImmichClient(cfg["server_url"], key, timeout=cfg["request_timeout"], max_pages=cfg["max_pages"])
        summary = {"started_at": datetime.now(timezone.utc).isoformat(), "status": "ok", "jobs": []}
        try:
            for job in cfg["jobs"]:
                if not job["enabled"] or (selected_job and job["name"] != selected_job):
                    continue
                dry_run = force_dry_run or cfg["dry_run"] or job.get("dry_run", False)
                log("job_started", tz=tz, job=job["name"], dry_run=dry_run)
                try:
                    result = run_job(client, job, dry_run=dry_run)
                except APIError as exc:
                    result = {"status": "failed", "dry_run": dry_run, "error": str(exc)}
                except Exception:
                    # Never expose arbitrary HTTP/SDK exception strings or tracebacks with secrets.
                    result = {"status": "failed", "dry_run": dry_run,
                              "error": "Unexpected job error; no automatic write retry"}
                result["job"] = job["name"]
                summary["jobs"].append(result)
                log("job_finished", tz=tz, **result)
                if result["status"] != "ok":
                    summary["status"] = "failed"
        finally:
            client.close()
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_state(directory / "last-run.json", summary)
        return summary


def next_run(schedule, now):
    return croniter(schedule, now, max_years_between_matches=5).get_next(datetime)


def healthcheck(directory):
    try:
        heart = json.loads((directory / "heartbeat.json").read_text(encoding="utf-8"))
        if not isinstance(heart, dict):
            return False
        age = time.time() - heart["timestamp"]
        if not 0 <= age < 90:
            return False
        last = directory / "last-run.json"
        if last.exists():
            result = json.loads(last.read_text(encoding="utf-8"))
            if not isinstance(result, dict) or result.get("status") != "ok":
                return False
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def serve(cfg, directory):
    tz = ZoneInfo(cfg["timezone"])
    # Missing secrets should fail immediately rather than wait for the next cron tick.
    load_api_key(cfg)
    stop = threading.Event()
    old_handlers = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        old_handlers[sig] = signal.signal(sig, lambda *_: stop.set())

    def heartbeat():
        while not stop.is_set():
            try:
                write_state(directory / "heartbeat.json", {"timestamp": time.time(), "pid": os.getpid()})
            except OSError:
                log("heartbeat_failed", tz=tz, error="State directory is not writable")
                stop.set()
                return
            stop.wait(10)

    pulse = threading.Thread(target=heartbeat, daemon=True)
    pulse.start()

    def cycle():
        try:
            run_cycle(cfg, directory)
        except AlreadyRunning:
            log("run_skipped", tz=tz, reason="Another process holds run.lock")
        except (OSError, ConfigError):
            log("run_failed", tz=tz, error="Cannot read secret or write state; stopping scheduler")
            stop.set()

    log("scheduler_started", tz=tz, timezone=cfg["timezone"], schedule=cfg["schedule"], dry_run=cfg["dry_run"])
    try:
        if cfg["run_on_startup"]:
            cycle()
        while not stop.is_set():
            due = next_run(cfg["schedule"], datetime.now(tz))
            log("next_run", tz=tz, at=due.isoformat())
            while not stop.is_set():
                delay = due.timestamp() - time.time()
                if delay <= 0:
                    break
                stop.wait(min(delay, 30))
            if not stop.is_set():
                cycle()
            # Missed ticks are coalesced: no catch-up storm and no concurrent jobs.
    finally:
        stop.set()
        pulse.join(timeout=2)
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        try:
            (directory / "heartbeat.json").unlink(missing_ok=True)
        except OSError:
            pass
        log("scheduler_stopped", tz=tz)
    return 0

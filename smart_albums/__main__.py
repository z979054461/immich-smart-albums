"""Container entry point. No command evaluates shell strings or user-supplied code."""
import argparse
import json
import os
from pathlib import Path

from . import __version__
from .api import APIError, ImmichClient
from .config import ConfigError, load_api_key, load_config
from .service import AlreadyRunning, healthcheck, log, run_cycle, serve, state_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description="Immich Smart Albums: add-only rules + cron scheduler")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", default=os.getenv("SMART_ALBUMS_CONFIG", "/config/config.yaml"))
    parser.add_argument("command", nargs="?", default="serve",
                        choices=("serve", "check", "once", "list-people", "list-albums", "healthcheck"))
    parser.add_argument("--dry-run", action="store_true", help="Force read-only preview for once")
    parser.add_argument("--job", help="Only run the exact job name (once only)")
    args = parser.parse_args(argv)
    if args.command == "healthcheck":
        return 0 if healthcheck(Path(os.getenv("SMART_ALBUMS_STATE_DIR", "/data"))) else 1
    try:
        cfg = load_config(args.config)
        if args.command == "check":
            log("config_valid", jobs=len(cfg["jobs"]), schedule=cfg["schedule"], timezone=cfg["timezone"],
                dry_run=cfg["dry_run"])
            return 0
        if (args.job or args.dry_run) and args.command != "once":
            raise ConfigError("--job and --dry-run apply to once only")
        if args.job and not any(j["name"] == args.job and j["enabled"] for j in cfg["jobs"]):
            raise ConfigError("Selected job is missing or disabled")
        if args.command in ("list-people", "list-albums"):
            client = ImmichClient(cfg["server_url"], load_api_key(cfg), timeout=cfg["request_timeout"], max_pages=cfg["max_pages"])
            try:
                rows = client.people() if args.command == "list-people" else client.albums()
                field = "name" if args.command == "list-people" else "albumName"
                print(json.dumps([{"id": r["id"], "name": r.get(field, "")} for r in rows], ensure_ascii=False, indent=2))
            finally:
                client.close()
            return 0
        directory = state_dir()
        if args.command == "once":
            result = run_cycle(cfg, directory, force_dry_run=args.dry_run, selected_job=args.job)
            return 0 if result["status"] == "ok" else 1
        return serve(cfg, directory)
    except (ConfigError, APIError, AlreadyRunning) as exc:
        log("error", error=str(exc))
        return 2
    except OSError:
        log("error", error="Cannot read configuration/secret or write state; check mounted paths and permissions")
        return 2
    except Exception:
        log("error", error="Unexpected service error; verify config and supported Immich version")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from cueflow.config import APP_VERSION, TrialConfig
from cueflow.trial_migrations import initialize_trial_database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cueflow-trial", description=f"CueFlow v{APP_VERSION} Trial"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="initialize an empty TrialStore")
    init.add_argument("database", type=Path)
    commands.add_parser("check", help="validate configuration, disk, and object storage")
    serve = commands.add_parser("serve", help="run the single-process Trial Web service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8000, type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            path = args.database.resolve()
            initialize_trial_database(path)
            return _write({"status": "initialized", "database": str(path)})
        if args.command == "serve":
            _require_trial_extra()
        config = TrialConfig.from_environment()
        from cueflow.trial_service import TrialService

        service = TrialService(config)
        if args.command == "check":
            readiness = service.refresh_storage_readiness()
            disk = service.disk_capacity()
            summary = service.summary()
            service.close()
            return _write({
                "status": "ready" if readiness.ready else "not_ready",
                "storage": {"ready": readiness.ready, "reasons": readiness.reasons},
                "disk": disk,
                "control": {
                    "paused": summary["paused"],
                    "daily_budget_micros": summary["daily_budget_micros"],
                },
            }, error=not readiness.ready or not bool(disk["ready"]))
        uvicorn: Any = importlib.import_module("uvicorn")

        from cueflow.trial_http import create_trial_app

        uvicorn.run(
            create_trial_app(service=service, config=config),
            host=args.host,
            port=args.port,
            workers=1,
        )
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        return _write(
            {"status": "failed", "error": {"code": type(exc).__name__, "message": str(exc)}},
            error=True,
        )


def _require_trial_extra() -> None:
    missing = [
        name
        for name in ("openai", "tos", "uvicorn", "multipart")
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        raise RuntimeError(
            "CueFlow Trial Web dependencies are missing; install them with "
            'pip install "cueflow[trial]"'
        )


def _write(value: dict[str, Any], *, error: bool = False) -> int:
    stream = sys.stderr if error else sys.stdout
    stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return 2 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())

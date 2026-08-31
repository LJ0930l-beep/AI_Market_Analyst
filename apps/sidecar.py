"""Packaged FastAPI sidecar entry point used by the Tauri desktop shell."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from apps.api.main import app
from core.config import database_path_from_env
from core.desktop_runtime import ensure_app_data_layout, ownership_fingerprint, write_runtime_manifest
from core.storage import SQLiteStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Market Analyst owned local API sidecar")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("the packaged sidecar binds only to loopback")
    if not 1024 <= args.port <= 65_535:
        parser.error("port must be between 1024 and 65535")
    os.environ.setdefault("AIMA_PACKAGED_SIDECAR", "1")
    paths = ensure_app_data_layout()
    started_at = datetime.now(timezone.utc)
    command_line = " ".join(sys.argv)
    fingerprint = ownership_fingerprint(
        pid=os.getpid(),
        executable=sys.executable,
        started_at=started_at,
        command_line=command_line,
    )
    write_runtime_manifest(paths, sidecar=fingerprint, port=args.port)
    log_path = paths.logs / "sidecar.log"
    handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[handler],
        force=True,
    )
    logging.getLogger(__name__).info("owned sidecar starting on loopback port %s", args.port)
    SQLiteStore(database_path_from_env()).initialize()
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False, log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

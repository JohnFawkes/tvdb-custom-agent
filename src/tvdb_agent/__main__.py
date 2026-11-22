"""Entry point: ``python -m tvdb_agent`` or ``tvdb-agent``."""

from __future__ import annotations

import argparse
import logging

import uvicorn

from .app import create_app
from .config import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="TVDB custom metadata provider for Plex")
    parser.add_argument("--config", help="path to config.yaml (default: $CONFIG_FILE, /config/config.yaml, ./config.yaml)")
    parser.add_argument("--host", help="bind address (default 0.0.0.0)")
    parser.add_argument("--port", type=int, help="port (default 8000)")
    args = parser.parse_args()

    settings = load_settings(args.config)
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning", proxy_headers=True)


if __name__ == "__main__":
    main()

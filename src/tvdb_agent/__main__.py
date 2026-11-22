"""Entry point.

    tvdb-agent                    run the provider (same as `tvdb-agent serve`)
    tvdb-agent register-plex ...  add this agent's orderings to Plex as agents
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import load_settings


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

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
    return 0


def _register_plex(args: argparse.Namespace) -> int:
    from .plex import PlexClient, PlexError, register, resolve_profiles

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    settings = load_settings(args.config)
    plex_url = args.plex_url or settings.plex_url
    token = args.token or settings.plex_token
    agent_url = args.agent_url or settings.public_url
    if not plex_url:
        print("error: --plex-url (or PLEX_URL) is required", file=sys.stderr)
        return 2
    if not agent_url:
        print("error: --agent-url (or PUBLIC_URL) is required: the address Plex uses to reach this agent", file=sys.stderr)
        return 2
    if args.all:
        names = [p.name or "" for p in settings.all_profiles()]
    else:
        names = args.profile  # None -> defaults
    try:
        profiles = resolve_profiles(settings, names)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    async def run():
        client = PlexClient(plex_url, token)
        try:
            return await register(client, settings, profiles, agent_url)
        finally:
            await client.aclose()

    try:
        results = asyncio.run(run())
    except (PlexError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    failed = False
    for r in results:
        if r.error:
            failed = True
            print(f"FAILED  {r.agent}\n        {r.uri}\n        {r.error}")
        else:
            print(f"ok      {r.agent}  (provider {r.provider}, agent {r.group})\n        {r.uri}")
    if not failed:
        print("\nIn Plex, open a TV library's settings > Advanced > Agent and pick the ordering it should use,"
              "\nthen run Refresh All Metadata on that library.")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tvdb-agent", description="TVDB custom metadata provider for Plex")
    parser.add_argument("--config", help="path to config.yaml (default: $CONFIG_FILE, /config/config.yaml, ./config.yaml)")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the metadata provider (default)")
    parser.add_argument("--host", help="bind address (default 0.0.0.0)")
    parser.add_argument("--port", type=int, help="port (default 8000)")
    # SUPPRESS keeps "tvdb-agent --port 9000 serve" from being reset by the subcommand.
    serve.add_argument("--host", default=argparse.SUPPRESS, help="bind address (default 0.0.0.0)")
    serve.add_argument("--port", type=int, default=argparse.SUPPRESS, help="port (default 8000)")

    reg = sub.add_parser(
        "register-plex",
        help="add one Plex agent per episode ordering, so it can be picked in library settings",
    )
    reg.add_argument("--plex-url", help="Plex Media Server URL, e.g. http://192.168.1.10:32400 (default: $PLEX_URL)")
    reg.add_argument("--token", help="X-Plex-Token of the server owner (default: $PLEX_TOKEN)")
    reg.add_argument("--agent-url", help="URL Plex uses to reach this agent, e.g. http://tvdb-agent:8000 (default: $PUBLIC_URL)")
    reg.add_argument("--profile", action="append", metavar="NAME",
                     help="profile to register; repeatable; 'root' for the default provider "
                          "(default: root, dvd, absolute and every profile in config.yaml)")
    reg.add_argument("--all", action="store_true", help="register every profile, including all built-in orderings")

    args = parser.parse_args(argv)
    if args.command == "register-plex":
        return _register_plex(args)
    return _serve(args)


if __name__ == "__main__":
    sys.exit(main())

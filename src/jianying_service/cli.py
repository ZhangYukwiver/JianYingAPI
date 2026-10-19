from __future__ import annotations

import argparse
import os
from typing import Sequence

from .app import create_app
from .config import Settings
from .mcp_server import create_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jianying-ai",
        description="Local Jianying draft project service",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    http_parser = subcommands.add_parser("http", help="Run the HTTP/JSON API")
    http_parser.add_argument(
        "--host",
        choices=("127.0.0.1", "localhost", "::1"),
        default=None,
        help="Loopback address (default: JY_HOST or 127.0.0.1)",
    )
    http_parser.add_argument(
        "--port",
        type=_port,
        default=None,
        help="TCP port (default: JY_PORT or 8765)",
    )

    mcp_parser = subcommands.add_parser("mcp", help="Run the MCP server over stdio")
    mcp_parser.set_defaults(
        host=None,
        port=None,
    )
    return parser


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be in 1..65535")
    return port


def configure_settings(args: argparse.Namespace) -> Settings:
    if args.host is not None:
        os.environ["JY_HOST"] = args.host
    if args.port is not None:
        os.environ["JY_PORT"] = str(args.port)
    try:
        return Settings.from_env()
    except (OSError, ValueError) as exc:
        raise SystemExit(f"jianying-ai: invalid configuration: {exc}") from None


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings = configure_settings(args)
    if args.command == "http":
        import uvicorn

        uvicorn.run(
            create_app(settings=settings),
            host=settings.host,
            port=settings.port,
        )
        return
    if args.command == "mcp":
        create_server(settings=settings).run("stdio")
        return
    raise SystemExit(f"jianying-ai: unknown command: {args.command}")


if __name__ == "__main__":
    main()

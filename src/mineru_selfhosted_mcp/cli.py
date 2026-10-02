"""CLI entry: argparse, optional .env loading, UTF-8 stdio forcing, run()."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import Settings
from .server import run


def _force_utf8_stdio() -> None:
    """Windows cp936 etc. would break the MCP handshake pipe; force UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="MinerU 4.0 self-hosted MCP server (async task model)")
    parser.add_argument("--transport", "-t",
                        choices=["stdio", "streamable-http"],
                        default="stdio")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", "-p", type=int, default=7000)
    parser.add_argument("--output-dir", "-o", default=None,
                        help="Artifact directory (default: ~/mineru-downloads)")
    parser.add_argument("--env-file", default=None,
                        help="Optional .env file loaded before env vars are read")
    args = parser.parse_args(argv)

    if args.env_file:
        _load_env_file(Path(args.env_file).expanduser())

    settings = Settings.from_env()
    if args.output_dir:
        settings = settings.with_overrides(
            output_dir=Path(args.output_dir).expanduser())

    run(settings, args.transport, args.host, args.port)
    return 0

"""The small standard-library CLI for the local TraceMotive experience."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from typing import Any

from tracemotive.collector import DEFAULT_BIND_HOST, create_app
from tracemotive.demo import DEFAULT_DEMO_ENDPOINT, DemoError, format_demo_result, seed_demo
from tracemotive.storage import (
    DatabasePathError,
    MigrationError,
    resolve_database_path,
)
from tracemotive.ui.server import PackagedUIError, add_ui_routes


DEFAULT_PORT = 8765
_SHUTDOWN_TIMEOUT_SECONDS = 5


class ServeStartupError(RuntimeError):
    """Raised when the local serve application is not safe to start."""


def _port_value(value: str) -> int:
    try:
        port = int(value, 10)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "port must be an integer from 1 through 65535"
        ) from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be from 1 through 65535")
    return port


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tracemotive")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="serve the local Collector and UI")
    serve.add_argument("--db", metavar="PATH", help="SQLite path or explicit :memory:")
    serve.add_argument(
        "--port",
        default=DEFAULT_PORT,
        type=_port_value,
        metavar="PORT",
        help=f"loopback port (default: {DEFAULT_PORT})",
    )
    serve.set_defaults(handler=_run_serve)
    demo = commands.add_parser("demo", help="seed the deterministic local v0.3 demo")
    demo.add_argument(
        "--scenario",
        choices=("identified", "uncertain"),
        default="identified",
        help="deterministic local scenario (default: identified)",
    )
    demo.add_argument(
        "--endpoint",
        default=DEFAULT_DEMO_ENDPOINT,
        metavar="URL",
        help=f"existing loopback TraceMotive server (default: {DEFAULT_DEMO_ENDPOINT})",
    )
    demo.set_defaults(handler=_run_demo)
    return parser


def create_serve_app(database_path: str) -> tuple[Any, Any]:
    """Create the database-backed app and package-owned UI routes."""

    app = create_app(database_path=database_path)
    collector = app.state.tracemotive_collector
    try:
        if not collector.repository.health_check():
            raise ServeStartupError("configured database is not queryable")
        add_ui_routes(app)
    except Exception:
        collector.close()
        raise
    return app, collector


def _run_serve(arguments: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            "tracemotive serve requires the server extra; "
            'install "tracemotive[server]"',
            file=sys.stderr,
        )
        return 1

    collector = None
    exit_code = 1
    try:
        database_path = resolve_database_path(arguments.db, environ=os.environ)
        app, collector = create_serve_app(database_path)
        config = uvicorn.Config(
            app,
            host=DEFAULT_BIND_HOST,
            port=arguments.port,
            timeout_graceful_shutdown=_SHUTDOWN_TIMEOUT_SECONDS,
        )
        server = uvicorn.Server(config)
        server.run()
        if server.started:
            exit_code = 0
        else:
            print(
                f"tracemotive serve: could not bind {DEFAULT_BIND_HOST}:{arguments.port}",
                file=sys.stderr,
            )
    except KeyboardInterrupt:
        exit_code = 0
    except SystemExit as exc:
        print(
            f"tracemotive serve: could not bind {DEFAULT_BIND_HOST}:{arguments.port}",
            file=sys.stderr,
        )
        exit_code = exc.code if type(exc.code) is int and exc.code != 0 else 1
    except (DatabasePathError, MigrationError, PackagedUIError, ServeStartupError) as exc:
        print(f"tracemotive serve: {exc}", file=sys.stderr)
    except Exception:
        print("tracemotive serve: startup or server failure", file=sys.stderr)
    finally:
        if collector is not None:
            try:
                collector.close()
            except Exception:
                print("tracemotive serve: database shutdown failure", file=sys.stderr)
                exit_code = 1
    return exit_code


def _run_demo(arguments: argparse.Namespace) -> int:
    try:
        result = seed_demo(arguments.endpoint, scenario=arguments.scenario)
    except DemoError as exc:
        print(f"tracemotive demo: {exc}", file=sys.stderr)
        return 1
    print(format_demo_result(result))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``tracemotive`` command and return its process exit code."""

    arguments = _parser().parse_args(argv)
    return arguments.handler(arguments)


__all__ = ["DEFAULT_PORT", "ServeStartupError", "create_serve_app", "main"]

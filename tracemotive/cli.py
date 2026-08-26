"""The small standard-library CLI for the local TraceMotive experience."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from typing import Any

from tracemotive.collector import DEFAULT_BIND_HOST, create_app
from tracemotive.demo import DEFAULT_DEMO_ENDPOINT, DemoError, format_demo_result, seed_demo
from tracemotive.local_client import (
    ApiContractError,
    ApiResponseStatusError,
    BrowserOpenError,
    InvalidEndpointError,
    LocalClientFailures,
    TransportFailureError,
    compare_traces as compare_traces_http,
    format_comparison_result,
    last_comparison,
    open_comparison,
)
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
    demo = commands.add_parser("demo", help="seed the deterministic local v0.4.0 demo")
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
    compare = commands.add_parser(
        "compare",
        help="compare two traces with the local v3 investigation API",
    )
    compare.add_argument("left", metavar="LEFT_TRACE_ID")
    compare.add_argument("right", metavar="RIGHT_TRACE_ID")
    compare.add_argument(
        "--endpoint",
        default=DEFAULT_DEMO_ENDPOINT,
        metavar="URL",
        help=f"existing loopback TraceMotive server (default: {DEFAULT_DEMO_ENDPOINT})",
    )
    compare.add_argument("--json", action="store_true", help="write the v3 JSON response to stdout")
    compare.add_argument("--open", action="store_true", help="open the local comparison URL")
    compare.set_defaults(handler=_run_compare)
    last = commands.add_parser(
        "last",
        help="compare the two newest exact-name trace summaries",
    )
    last.add_argument("trace_name", metavar="TRACE_NAME")
    last.add_argument(
        "--endpoint",
        default=DEFAULT_DEMO_ENDPOINT,
        metavar="URL",
        help=f"existing loopback TraceMotive server (default: {DEFAULT_DEMO_ENDPOINT})",
    )
    last.add_argument("--json", action="store_true", help="write the v3 JSON response to stdout")
    last.add_argument("--open", action="store_true", help="open the selected comparison URL")
    last.set_defaults(handler=_run_last)
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


def _local_client_exit_code(exc: BaseException) -> int:
    if isinstance(exc, InvalidEndpointError):
        return 3
    if isinstance(exc, TransportFailureError):
        return 4
    if isinstance(exc, (ApiContractError, ApiResponseStatusError)):
        return 5
    return 1


def _write_local_json(raw: bytes) -> None:
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()


def _open_if_requested(
    arguments: argparse.Namespace,
    command: str,
    left: str,
    right: str,
) -> int:
    if not arguments.open:
        return 0
    try:
        open_comparison(left, right, endpoint=arguments.endpoint)
    except BrowserOpenError as exc:
        print(f"tracemotive {command}: {exc}", file=sys.stderr)
        return 6
    return 0


def _run_compare(arguments: argparse.Namespace) -> int:
    try:
        comparison = compare_traces_http(
            arguments.left,
            arguments.right,
            endpoint=arguments.endpoint,
        )
    except LocalClientFailures as exc:
        print(f"tracemotive compare: {exc}", file=sys.stderr)
        return _local_client_exit_code(exc)
    except Exception:
        print("tracemotive compare: unexpected internal failure", file=sys.stderr)
        return 1

    if arguments.json:
        _write_local_json(comparison.raw)
    else:
        print(format_comparison_result(comparison, endpoint=arguments.endpoint))
    return _open_if_requested(arguments, "compare", arguments.left, arguments.right)


def _run_last(arguments: argparse.Namespace) -> int:
    try:
        selection, comparison = last_comparison(
            arguments.trace_name,
            endpoint=arguments.endpoint,
        )
    except LocalClientFailures as exc:
        print(f"tracemotive last: {exc}", file=sys.stderr)
        return _local_client_exit_code(exc)
    except Exception:
        print("tracemotive last: unexpected internal failure", file=sys.stderr)
        return 1

    if arguments.json:
        _write_local_json(comparison.raw)
    else:
        print(
            format_comparison_result(
                comparison,
                endpoint=arguments.endpoint,
                selection=selection,
            )
        )
    return _open_if_requested(arguments, "last", selection.left.trace_id, selection.right.trace_id)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``tracemotive`` command and return its process exit code."""

    arguments = _parser().parse_args(argv)
    return arguments.handler(arguments)


__all__ = ["DEFAULT_PORT", "ServeStartupError", "create_serve_app", "main"]

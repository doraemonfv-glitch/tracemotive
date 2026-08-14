"""Opt-in V02-22 validation for the installed-user P0 path.

The test is release-only so normal development CI remains fast.  It validates
the boundary that unit tests cannot cover together: a freshly built wheel,
fresh isolated installation, installed ``tracemotive serve``, real public SDK
traces, persistence across restart, comparison, packaged UI, and privacy.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener
from urllib.parse import quote
import zipfile


_RUN_RELEASE_VALIDATION = os.environ.get("TRACEMOTIVE_RUN_V02_22") == "1"
_OPENER = build_opener(ProxyHandler({}))
_SECRET = "V02-22-INTEGRATION-SECRET"


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise AssertionError(
            "command failed with exit code "
            f"{result.returncode}: {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def _http(
    endpoint: str,
    path: str,
    *,
    method: str = "GET",
) -> tuple[int, bytes]:
    request = Request(
        f"{endpoint}{path}",
        method=method,
        headers={"Accept": "application/json"},
    )
    try:
        with _OPENER.open(request, timeout=5) as response:
            return response.status, response.read()
    except HTTPError as error:
        body = error.read()
        error.close()
        return error.code, body


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(endpoint: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 20
    last_error: object = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                "serve exited before health became available: "
                f"{process.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"
            )
        try:
            status, body = _http(endpoint, "/api/v1/health")
            if status == 200 and json.loads(body)["status"] == "ok":
                return
            last_error = (status, body)
        except (OSError, URLError, json.JSONDecodeError, KeyError) as error:
            last_error = error
        time.sleep(0.1)
    raise AssertionError(f"serve health did not become available: {last_error!r}")


def _stop_server(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    stdout, stderr = process.communicate()
    if process.returncode not in (0, 1, -15):
        raise AssertionError(
            f"serve exited unexpectedly with {process.returncode}\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )


def _sdk_trace_script() -> str:
    return r'''
import json
import sys
import tracemotive
from tracemotive.canonical import AgentDetails, ToolDetails

endpoint = sys.argv[1]
tracemotive.configure(
    enabled=True,
    endpoint=endpoint,
    capture_content=True,
)


def produce(repeated_count, side_name):
    with tracemotive.trace(f"V02-22 {side_name}") as run:
        with tracemotive.span(
            "Comparison agent",
            type="agent",
            operation="agent.run",
            details=AgentDetails("agent", "Comparison agent", "0.1"),
            input={"secret": "V02-22-INTEGRATION-SECRET", "api_key": "sk-v02-22"},
        ):
            for index in range(repeated_count):
                with tracemotive.span(
                    "Repeated lookup",
                    type="tool",
                    operation="tool.call",
                    details=ToolDetails("tool", "lookup", "call-v02-22"),
                    input={"index": index, "password": "V02-22-INTEGRATION-SECRET"},
                ) as repeated_tool:
                    repeated_tool.set_output("<script>alert(1)</script>")
                    pass
            with tracemotive.span(
                side_name,
                type="tool",
                operation="tool.call",
                details=ToolDetails("tool", "lookup", "call-v02-22"),
                input={"side": side_name},
            ) as side_tool:
                side_tool.set_output({"ok": True})
                pass
    return run.trace_id


left = produce(2, "Baseline only")
right = produce(3, "Candidate only")
tracemotive.flush(timeout_seconds=10)
print(json.dumps([left, right]))
'''


@unittest.skipUnless(
    _RUN_RELEASE_VALIDATION,
    "set TRACEMOTIVE_RUN_V02_22=1 for release-only validation",
)
class V02P0FullStackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[1]
        configured_wheel = os.environ.get("TRACEMOTIVE_V02_22_WHEEL")
        if configured_wheel:
            cls.wheel_path = Path(configured_wheel).resolve()
        else:
            wheels = sorted((cls.repo_root / "dist").glob("tracemotive-*.whl"))
            if not wheels:
                raise AssertionError(
                    "build a wheel first or set TRACEMOTIVE_V02_22_WHEEL"
                )
            cls.wheel_path = wheels[-1]
        if not cls.wheel_path.is_file():
            raise AssertionError(f"wheel does not exist: {cls.wheel_path}")

        cls.temp_dir = tempfile.TemporaryDirectory(prefix="tracemotive-v02-22-")
        cls.external_root = Path(cls.temp_dir.name)
        cls.venv_path = cls.external_root / "isolated-venv"
        cls.install_env = os.environ.copy()
        cls.install_env.pop("PYTHONPATH", None)
        cls.install_env["PYTHONNOUSERSITE"] = "1"
        _run(
            [sys.executable, "-m", "venv", str(cls.venv_path)],
            cwd=cls.external_root,
            env=cls.install_env,
            timeout=120,
        )
        if os.name == "nt":
            cls.venv_python = cls.venv_path / "Scripts" / "python.exe"
            cls.serve_command = cls.venv_path / "Scripts" / "tracemotive.exe"
        else:
            cls.venv_python = cls.venv_path / "bin" / "python"
            cls.serve_command = cls.venv_path / "bin" / "tracemotive"
        _run(
            [
                str(cls.venv_python),
                "-m",
                "pip",
                "install",
                f"{cls.wheel_path}[server]",
            ],
            cwd=cls.external_root,
            env=cls.install_env,
            timeout=240,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "temp_dir"):
            cls.temp_dir.cleanup()

    def _serve(self, database_path: Path, port: int) -> tuple[subprocess.Popen[str], str]:
        env = self.install_env.copy()
        env["TRACEMOTIVE_DB"] = str(database_path)
        endpoint = f"http://127.0.0.1:{port}"
        process = subprocess.Popen(
            [str(self.serve_command), "serve", "--port", str(port)],
            cwd=self.external_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _wait_for_health(endpoint, process)
        return process, endpoint

    def test_wheel_and_installed_runtime_are_checkout_independent(self) -> None:
        with zipfile.ZipFile(self.wheel_path) as wheel:
            names = wheel.namelist()
            self.assertIn("tracemotive/ui/index.html", names)
            self.assertTrue(any(name.startswith("tracemotive/ui/assets/") for name in names))
            self.assertFalse(any("node_modules/" in name for name in names))
            self.assertFalse(any(name.startswith("frontend/") for name in names))
            self.assertFalse(any("agentlens" in name.lower() for name in names))

        result = _run(
            [str(self.venv_python), "-c", "import tracemotive; print(tracemotive.__file__)"],
            cwd=self.external_root,
            env=self.install_env,
        )
        installed_path = Path(result.stdout.strip())
        self.assertTrue(installed_path.is_file())
        self.assertNotEqual(installed_path.parent.parent.resolve(), self.repo_root.resolve())
        self.assertTrue(self.serve_command.is_file())

    def test_installed_serve_persists_and_compares_real_sdk_traces_after_restart(self) -> None:
        database_path = self.external_root / "data" / "tracemotive.sqlite3"
        port = _free_loopback_port()
        process, endpoint = self._serve(database_path, port)
        try:
            status, index = _http(endpoint, "/")
            self.assertEqual(status, 200)
            index_text = index.decode("utf-8")
            self.assertIn('<div id="root">', index_text)
            self.assertNotIn("http://", index_text)
            asset_match = re.search(r"/assets/([^\"']+)", index_text)
            self.assertIsNotNone(asset_match)
            asset_path = f"/assets/{quote(asset_match.group(1))}"
            asset_status, asset_body = _http(endpoint, asset_path)
            self.assertEqual(asset_status, 200)
            self.assertIn(b"Changed only", asset_body)
            self.assertIn(b"/api/v2/compare", asset_body)

            sdk_env = self.install_env.copy()
            sdk_result = _run(
                [
                    str(self.venv_python),
                    "-c",
                    _sdk_trace_script(),
                    endpoint,
                ],
                cwd=self.external_root,
                env=sdk_env,
                timeout=120,
            )
            trace_ids = json.loads(sdk_result.stdout.strip().splitlines()[-1])
            self.assertEqual(len(trace_ids), 2)
            self.assertEqual(len(set(trace_ids)), 2)
            self.assertTrue(database_path.is_file())

            list_status, list_body = _http(endpoint, "/api/v1/traces?limit=100&offset=0")
            self.assertEqual(list_status, 200)
            listed_ids = {item["trace_id"] for item in json.loads(list_body)["items"]}
            self.assertTrue(set(trace_ids).issubset(listed_ids))

            compare_path = f"/api/v2/compare/{trace_ids[0]}/{trace_ids[1]}"
            compare_status, compare_body = _http(endpoint, compare_path)
            self.assertEqual(compare_status, 200)
            comparison = json.loads(compare_body)
            self.assertEqual(comparison["comparison_version"], "0.2")
            self.assertEqual(comparison["summary"]["alignment"]["ambiguous_groups"], 1)
            group = comparison["ambiguous_groups"][0]
            self.assertEqual(group["alignment"], "ambiguous_group")
            self.assertEqual((group["left_count"], group["right_count"]), (2, 3))
            self.assertTrue(
                group["group_signature"]["name"] == "Repeated lookup"
            )
            self.assertTrue(any(item["alignment"] == "left_only" for item in comparison["spans"]))
            self.assertTrue(any(item["alignment"] == "right_only" for item in comparison["spans"]))
            left_ambiguous_ids = {
                item["span_id"] for item in group["ambiguous_members"]["left"]
            }
            right_ambiguous_ids = {
                item["span_id"] for item in group["ambiguous_members"]["right"]
            }
            self.assertTrue(
                all(
                    item["left"]["span_id"] not in left_ambiguous_ids
                    and item["right"]["span_id"] not in right_ambiguous_ids
                    for item in comparison["spans"]
                    if item["alignment"] == "exact_match"
                )
            )
            self.assertNotIn(_SECRET.encode("utf-8"), compare_body)

            invalid_status, _ = _http(endpoint, "/api/v2/compare/not-a-trace/not-a-trace")
            self.assertEqual(invalid_status, 400)
            missing_status, _ = _http(
                endpoint,
                "/api/v2/compare/4bf92f3577b34da6a3ce929d0e0e4736/"
                "5bf92f3577b34da6a3ce929d0e0e4736",
            )
            self.assertEqual(missing_status, 404)
        finally:
            _stop_server(process)

        persisted_files = [database_path]
        persisted_files.extend(database_path.parent.glob(f"{database_path.name}-*"))
        for persisted_file in persisted_files:
            if persisted_file.is_file():
                self.assertNotIn(_SECRET.encode("utf-8"), persisted_file.read_bytes())

        restart_process, restart_endpoint = self._serve(database_path, _free_loopback_port())
        try:
            restart_status, restart_body = _http(
                restart_endpoint,
                "/api/v1/traces?limit=100&offset=0",
            )
            self.assertEqual(restart_status, 200)
            restart_ids = {
                item["trace_id"] for item in json.loads(restart_body)["items"]
            }
            self.assertTrue(set(trace_ids).issubset(restart_ids))
            restarted_compare_status, restarted_compare_body = _http(
                restart_endpoint,
                f"/api/v2/compare/{trace_ids[0]}/{trace_ids[1]}",
            )
            self.assertEqual(restarted_compare_status, 200)
            self.assertEqual(restarted_compare_body, compare_body)
        finally:
            _stop_server(restart_process)

    def test_installed_cli_rejects_remote_host_and_invalid_port(self) -> None:
        remote_host = subprocess.run(
            [str(self.serve_command), "serve", "--host", "127.0.0.1"],
            cwd=self.external_root,
            env=self.install_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(remote_host.returncode, 0)
        self.assertIn("unrecognized arguments", remote_host.stderr)

        invalid_port = subprocess.run(
            [str(self.serve_command), "serve", "--port", "0"],
            cwd=self.external_root,
            env=self.install_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(invalid_port.returncode, 0)
        self.assertIn("port must be from 1 through 65535", invalid_port.stderr)


if __name__ == "__main__":
    unittest.main()

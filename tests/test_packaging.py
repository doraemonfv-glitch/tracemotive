import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
import unittest
import zipfile
from email.parser import Parser


ROOT = Path(__file__).resolve().parents[1]


class PackagingOnboardingTests(unittest.TestCase):
    def test_pyproject_declares_single_runtime_and_optional_boundaries(self) -> None:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('name = "tracemotive"', text)
        self.assertIn('version = "0.1.1"', text)
        self.assertIn('requires-python = ">=3.10"', text)
        self.assertRegex(text, r'"fastapi>=0\.110,<1"')
        self.assertRegex(text, r'"uvicorn>=0\.30,<1"')
        self.assertRegex(text, r'"openai-agents>=0\.17,<0\.18"')

    def test_license_and_runtime_package_selection_are_present(self) -> None:
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertTrue(license_text.startswith("MIT License\n"))
        self.assertIn("Copyright (c) 2026 TraceMotive contributors", license_text)
        self.assertNotIn("AgentLens contributors", license_text)
        self.assertIn(
            'include = ["tracemotive*"]',
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        )
        self.assertIn(
            '"tracemotive.ui" = ["index.html", "assets/*"]',
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        )

    def test_core_import_does_not_require_openai_agents(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import tracemotive; assert 'agents' not in sys.modules",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_onboarding_uses_tracemotive_identity_and_no_legacy_install(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotRegex(readme, r"pip install agentlens(?:\s|$)")
        self.assertIn("import tracemotive", readme)
        self.assertIn('pip install -e ".[server]"', readme)
        self.assertIn("127.0.0.1", readme)
        self.assertIn("capture_content=False", readme)
        self.assertIn("local_only=True", readme)

    def test_pre_release_package_namespace_is_migrated(self) -> None:
        self.assertTrue((ROOT / "tracemotive").is_dir())
        self.assertFalse((ROOT / "agentlens").exists())
        self.assertTrue((ROOT / "tracemotive" / "storage" / "migrations.py").is_file())

    def test_public_exception_is_renamed_without_compatibility_alias(self) -> None:
        import tracemotive

        self.assertTrue(issubclass(tracemotive.TraceMotiveConfigurationError, ValueError))
        self.assertTrue(hasattr(tracemotive, "TraceMotiveConfigurationError"))
        self.assertFalse(hasattr(tracemotive, "AgentLensConfigurationError"))

    def test_all_active_onboarding_docs_reject_legacy_public_names(self) -> None:
        documents = (
            ROOT / "README.md",
            ROOT / "docs" / "openai-agents.md",
            ROOT / "docs" / "release-readiness.md",
            ROOT / "examples" / "README.md",
        )
        forbidden = (
            "pip install agentlens",
            "import agentlens",
            "from agentlens",
            "agentlens.collector",
            "AgentLensConfigurationError",
        )
        for document in documents:
            text = document.read_text(encoding="utf-8")
            for value in forbidden:
                self.assertNotIn(value, text, document.name)
        self.assertIn(
            "tracemotive.TraceMotiveConfigurationError",
            (ROOT / "docs" / "openai-agents.md").read_text(encoding="utf-8"),
        )

    def test_frontend_app_uses_neutral_component_name_and_branding(self) -> None:
        frontend_source = ROOT / "frontend" / "src"
        active_sources = tuple(
            path
            for path in frontend_source.rglob("*")
            if path.is_file() and path.suffix in {".ts", ".tsx"}
        )
        legacy_app_name = "AgentLens" + "App"
        source_text = "\n".join(path.read_text(encoding="utf-8") for path in active_sources)
        self.assertNotIn(legacy_app_name, source_text)
        self.assertIn("export function TraceMotiveApp", (frontend_source / "app.tsx").read_text(encoding="utf-8"))
        self.assertIn("TraceMotive / Local trace observer", (frontend_source / "trace-list.tsx").read_text(encoding="utf-8"))
        self.assertIn("TraceMotive / Trace detail", (frontend_source / "trace-detail.tsx").read_text(encoding="utf-8"))


def _clean_subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


class BuiltArtifactPackagingTests(unittest.TestCase):
    """Build and exercise the actual local artifacts without source leakage."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory(prefix="tracemotive-issue16-packaging-")
        temporary_root = Path(cls._temporary.name)
        cls._source_copy = temporary_root / "source"
        shutil.copytree(
            ROOT,
            cls._source_copy,
            ignore=shutil.ignore_patterns(
                ".git",
                ".pytest_cache",
                "__pycache__",
                "*.egg-info",
                "node_modules",
                "build",
                "dist",
            ),
        )
        cls._artifact_dir = temporary_root / "artifacts"
        cls._artifact_dir.mkdir()
        build = subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--sdist",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(cls._artifact_dir),
            ],
            cwd=cls._source_copy,
            env=_clean_subprocess_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        if build.returncode != 0:
            raise RuntimeError(f"local artifact build failed:\n{build.stdout}\n{build.stderr}")
        wheels = list(cls._artifact_dir.glob("tracemotive-0.1.1-*.whl"))
        sdists = list(cls._artifact_dir.glob("tracemotive-0.1.1.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise RuntimeError(f"unexpected artifacts: {list(cls._artifact_dir.iterdir())}")
        cls._wheel = wheels[0]
        cls._sdist = sdists[0]

        cls._run_root = temporary_root / "run"
        cls._run_root.mkdir()
        cls._venv = temporary_root / "venv"
        venv = subprocess.run(
            [
                sys.executable,
                "-m",
                "venv",
                str(cls._venv),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if venv.returncode != 0:
            raise RuntimeError(f"validation venv creation failed:\n{venv.stdout}\n{venv.stderr}")
        cls._installed_python = cls._venv / (
            Path("Scripts") / "python.exe" if os.name == "nt" else Path("bin") / "python"
        )
        install = subprocess.run(
            [str(cls._installed_python), "-m", "pip", "install", "--no-deps", str(cls._wheel)],
            cwd=cls._run_root,
            env=_clean_subprocess_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        if install.returncode != 0:
            raise RuntimeError(f"wheel installation failed:\n{install.stdout}\n{install.stderr}")
        server_install = subprocess.run(
            [str(cls._installed_python), "-m", "pip", "install", f"{cls._wheel}[server]"],
            cwd=cls._run_root,
            env=_clean_subprocess_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        if server_install.returncode != 0:
            raise RuntimeError(
                f"wheel server-extra installation failed:\n"
                f"{server_install.stdout}\n{server_install.stderr}"
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def _run_installed(self, script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self._installed_python), "-c", textwrap.dedent(script)],
            cwd=self._run_root,
            env=_clean_subprocess_environment(),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_wheel_and_sdist_contents_and_metadata_are_final(self) -> None:
        legacy_processor_name = "AgentLens" + "OpenAI" + "Processor"
        legacy_processor_bytes = legacy_processor_name.encode("ascii")
        new_processor_bytes = b"OpenAITracingProcessor"
        with zipfile.ZipFile(self._wheel) as archive:
            names = archive.namelist()
            self.assertIn("tracemotive/__init__.py", names)
            self.assertIn("tracemotive/storage/migrations.py", names)
            self.assertIn("tracemotive/ui/__init__.py", names)
            self.assertIn("tracemotive/ui/server.py", names)
            self.assertIn("tracemotive/ui/index.html", names)
            self.assertTrue(any(name.startswith("tracemotive/ui/assets/") for name in names))
            self.assertFalse(any("node_modules" in name for name in names))
            self.assertFalse(any(name.startswith("frontend/") for name in names))
            license_name = "tracemotive-0.1.1.dist-info/licenses/LICENSE"
            self.assertIn(license_name, names)
            self.assertFalse(any(name.startswith("agentlens/") for name in names))
            self.assertFalse(any(b"AgentLensConfigurationError" in archive.read(name) for name in names))
            self.assertFalse(any(legacy_processor_bytes in archive.read(name) for name in names))
            processor_source = archive.read("tracemotive/integrations/openai_agents.py")
            self.assertIn(new_processor_bytes, processor_source)
            metadata = Parser().parsestr(
                archive.read("tracemotive-0.1.1.dist-info/METADATA").decode("utf-8")
            )
            self.assertEqual(metadata["Name"], "tracemotive")
            self.assertEqual(metadata["Version"], "0.1.1")
            self.assertEqual(metadata["Requires-Python"], ">=3.10")
            requires = metadata.get_all("Requires-Dist")
            self.assertIn("fastapi<1,>=0.110", requires)
            self.assertIn('uvicorn<1,>=0.30; extra == "server"', requires)
            self.assertIn('openai-agents<0.18,>=0.17; extra == "openai-agents"', requires)
            self.assertEqual(
                archive.read(license_name).decode("utf-8"),
                (ROOT / "LICENSE").read_text(encoding="utf-8"),
            )

        with tarfile.open(self._sdist) as archive:
            names = archive.getnames()
            roots = {name.split("/", 1)[0] for name in names}
            sdist_roots = [root for root in roots if root.startswith("tracemotive-")]
            self.assertEqual(len(sdist_roots), 1)
            sdist_root = sdist_roots[0]
            pkg_info_name = f"{sdist_root}/PKG-INFO"
            pkg_info_member = archive.extractfile(pkg_info_name)
            self.assertIsNotNone(pkg_info_member)
            sdist_metadata = Parser().parsestr(pkg_info_member.read().decode("utf-8"))
            self.assertEqual(sdist_metadata["Name"], "tracemotive")
            self.assertEqual(sdist_metadata["Version"], "0.1.1")
            self.assertEqual(sdist_metadata["Requires-Python"], ">=3.10")
            sdist_requires = sdist_metadata.get_all("Requires-Dist")
            self.assertIn("fastapi<1,>=0.110", sdist_requires)
            self.assertIn('uvicorn<1,>=0.30; extra == "server"', sdist_requires)
            self.assertIn('openai-agents<0.18,>=0.17; extra == "openai-agents"', sdist_requires)
            self.assertIn(f"{sdist_root}/LICENSE", names)
            self.assertIn(f"{sdist_root}/tracemotive/__init__.py", names)
            self.assertIn(f"{sdist_root}/tracemotive/storage/migrations.py", names)
            self.assertIn(f"{sdist_root}/tracemotive/ui/__init__.py", names)
            self.assertIn(f"{sdist_root}/tracemotive/ui/server.py", names)
            self.assertIn(f"{sdist_root}/tracemotive/ui/index.html", names)
            self.assertTrue(any(name.startswith(f"{sdist_root}/tracemotive/ui/assets/") for name in names))
            self.assertFalse(any(name.startswith(f"{sdist_root}/agentlens/") for name in names))
            self.assertFalse(
                any(
                    b"AgentLensConfigurationError" in archive.extractfile(name).read()
                    for name in names
                    if archive.getmember(name).isreg()
                )
            )
            self.assertFalse(
                any(
                    legacy_processor_bytes in archive.extractfile(name).read()
                    for name in names
                    if archive.getmember(name).isreg()
                )
            )
            processor_member = archive.extractfile(f"{sdist_root}/tracemotive/integrations/openai_agents.py")
            self.assertIsNotNone(processor_member)
            self.assertIn(new_processor_bytes, processor_member.read())
            license_member = archive.extractfile(f"{sdist_root}/LICENSE")
            self.assertIsNotNone(license_member)
            self.assertEqual(
                license_member.read().decode("utf-8"),
                (ROOT / "LICENSE").read_text(encoding="utf-8"),
            )

    def test_installed_wheel_isolated_namespace_and_public_api(self) -> None:
        legacy_processor_name = "AgentLens" + "OpenAI" + "Processor"
        result = self._run_installed(
            f"""
            import importlib.util
            import pathlib
            import site
            import sys
            import tracemotive
            from tracemotive.integrations import openai_agents

            location = pathlib.Path(tracemotive.__file__).resolve()
            assert any(location.is_relative_to(pathlib.Path(root).resolve()) for root in site.getsitepackages())
            assert importlib.util.find_spec("agentlens") is None
            assert callable(tracemotive.configure)
            assert callable(tracemotive.trace)
            assert callable(tracemotive.span)
            assert callable(tracemotive.flush)
            from tracemotive.ui import get_ui_root
            ui_root = get_ui_root()
            assert ui_root.joinpath("index.html").is_file()
            assert any(
                asset.name.endswith((".js", ".css"))
                for asset in ui_root.joinpath("assets").iterdir()
            )
            assert hasattr(tracemotive, "TraceMotiveConfigurationError")
            assert not hasattr(tracemotive, "AgentLensConfigurationError")
            assert hasattr(openai_agents, "OpenAITracingProcessor")
            assert not hasattr(openai_agents, {legacy_processor_name!r})
            assert "OpenAITracingProcessor" in openai_agents.__all__
            assert {legacy_processor_name!r} not in openai_agents.__all__
            try:
                exec("from tracemotive.integrations.openai_agents import " + {legacy_processor_name!r})
            except ImportError:
                pass
            else:
                raise AssertionError("legacy processor import unexpectedly succeeded")
            assert "agentlens" not in sys.modules
            print(location)
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installed_cli_serves_packaged_ui_without_checkout_or_node(self) -> None:
        executable = self._venv / (
            Path("Scripts") / "tracemotive.exe" if os.name == "nt" else Path("bin") / "tracemotive"
        )
        self.assertTrue(executable.is_file(), executable)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        database_path = self._run_root / "v02-11-serve.sqlite3"
        environment = _clean_subprocess_environment()
        environment["PATH"] = str(executable.parent)
        server = subprocess.Popen(
            [str(executable), "serve", "--db", str(database_path), "--port", str(port)],
            cwd=self._run_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        endpoint = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 10
            while True:
                if server.poll() is not None:
                    self.fail(server.stderr.read())
                try:
                    with urllib.request.urlopen(endpoint + "/api/v1/health", timeout=1) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.read(), b'{"status":"ok"}')
                    break
                except Exception:
                    if time.monotonic() >= deadline:
                        self.fail("installed tracemotive serve did not become ready")
                    time.sleep(0.1)

            with urllib.request.urlopen(endpoint + "/", timeout=2) as response:
                index = response.read()
                self.assertEqual(response.status, 200)
                self.assertIn(b'<div id="root">', index)
            asset_match = re.search(rb'/(assets/[^"\']+)', index)
            self.assertIsNotNone(asset_match)
            asset_path = "/" + asset_match.group(1).decode("ascii")
            with urllib.request.urlopen(endpoint + asset_path, timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(response.read())
            with self.assertRaises(urllib.error.HTTPError) as unknown_api:
                urllib.request.urlopen(endpoint + "/api/v1/not-a-route", timeout=2)
            self.assertEqual(unknown_api.exception.code, 404)
        finally:
            if server.poll() is None:
                server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            if server.stdout is not None:
                server.stdout.close()
            if server.stderr is not None:
                server.stderr.close()
        self.assertIsNotNone(server.returncode)

    def test_installed_server_extra_runs_documented_uvicorn_factory(self) -> None:
        factory_target = "tracemotive.collector:create_app"
        documented_command = (
            "python -m uvicorn tracemotive.collector:create_app --factory "
            "--host 127.0.0.1 --port 8765"
        )
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(factory_target, readme)
        self.assertIn("--factory", readme)
        self.assertIn("--host 127.0.0.1", readme)
        self.assertIn("--port 8765", readme)
        self.assertIn(documented_command, readme)

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        result = self._run_installed(
            f"""
            import importlib.util
            import pathlib
            import subprocess
            import sys
            import time
            import urllib.request

            uvicorn_spec = importlib.util.find_spec("uvicorn")
            assert uvicorn_spec is not None
            assert uvicorn_spec.origin is not None
            assert "site-packages" in str(pathlib.Path(uvicorn_spec.origin).resolve())
            assert importlib.util.find_spec("tracemotive.collector") is not None
            assert pathlib.Path.cwd().resolve() != pathlib.Path(r"{ROOT}").resolve()

            port = {port}
            endpoint = f"http://127.0.0.1:{{port}}"
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "{factory_target}",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=pathlib.Path.cwd(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 10
                while True:
                    if server.poll() is not None:
                        raise AssertionError(server.stderr.read())
                    try:
                        health = urllib.request.urlopen(endpoint + "/api/v1/health", timeout=1)
                        assert health.status == 200
                        assert health.read() == b'{{"status":"ok"}}'
                        break
                    except Exception:
                        if time.monotonic() >= deadline:
                            raise AssertionError("installed Uvicorn health endpoint did not become ready")
                        time.sleep(0.1)
            finally:
                if server.poll() is None:
                    server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
            assert server.poll() is not None
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installed_optional_boundary_uses_new_exception(self) -> None:
        result = self._run_installed(
            """
            import sys
            sys.modules["agents"] = None
            import tracemotive
            from tracemotive.integrations import openai_agents

            try:
                openai_agents.install()
            except tracemotive.TraceMotiveConfigurationError as error:
                assert str(error) == "openai-agents is required for tracemotive.integrations.openai_agents.install"
            else:
                raise AssertionError("missing optional dependency did not fail")
            assert not hasattr(tracemotive, "AgentLensConfigurationError")
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installed_collector_and_sdk_query_smoke(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        result = self._run_installed(
            f"""
            import asyncio
            import json
            import socketserver
            import threading
            import time
            import urllib.request
            from urllib.parse import unquote, urlsplit

            port = {port}
            endpoint = f"http://127.0.0.1:{{port}}"
            from tracemotive.collector import create_app

            app = create_app()
            assert any(route.path == "/api/v1/health" for route in app.routes)
            assert any(route.path == "/api/v1/traces" for route in app.routes)

            class Server(socketserver.ThreadingTCPServer):
                allow_reuse_address = True
                daemon_threads = True

            class Handler(socketserver.BaseRequestHandler):
                def handle(self):
                    request = self.request
                    data = b""
                    while b"\\r\\n\\r\\n" not in data:
                        chunk = request.recv(65536)
                        if not chunk:
                            return
                        data += chunk
                    head, body = data.split(b"\\r\\n\\r\\n", 1)
                    lines = head.split(b"\\r\\n")
                    method, target, _ = lines[0].decode("ascii").split(" ", 2)
                    headers = {{}}
                    for line in lines[1:]:
                        name, value = line.split(b":", 1)
                        headers[name.lower()] = value.strip()
                    length = int(headers.get(b"content-length", b"0"))
                    while len(body) < length:
                        body += request.recv(65536)
                    body = body[:length]
                    parsed = urlsplit(target)
                    sent = []
                    received = False

                    async def receive():
                        nonlocal received
                        if not received:
                            received = True
                            return {{"type": "http.request", "body": body, "more_body": False}}
                        return {{"type": "http.disconnect"}}

                    async def send(message):
                        sent.append(message)

                    scope = {{
                        "type": "http",
                        "asgi": {{"version": "3.0"}},
                        "http_version": "1.1",
                        "method": method,
                        "scheme": "http",
                        "path": unquote(parsed.path),
                        "raw_path": parsed.path.encode("ascii"),
                        "query_string": parsed.query.encode("ascii"),
                        "headers": [(name, value) for name, value in headers.items()],
                        "server": ("127.0.0.1", port),
                        "client": self.client_address,
                    }}
                    asyncio.run(app(scope, receive, send))
                    response_start = next(message for message in sent if message["type"] == "http.response.start")
                    response_body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
                    reason = {{200: "OK", 404: "Not Found", 405: "Method Not Allowed"}}.get(response_start["status"], "")
                    response = f"HTTP/1.1 {{response_start['status']}} {{reason}}\\r\\n".encode("ascii")
                    response += b"".join(name + b": " + value + b"\\r\\n" for name, value in response_start["headers"])
                    response += b"content-length: " + str(len(response_body)).encode("ascii") + b"\\r\\nconnection: close\\r\\n\\r\\n" + response_body
                    request.sendall(response)

            server = Server(("127.0.0.1", port), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 8
                while True:
                    try:
                        health = urllib.request.urlopen(endpoint + "/api/v1/health", timeout=1)
                        assert health.status == 200
                        break
                    except Exception:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.05)

                import tracemotive
                tracemotive.configure(enabled=True, endpoint=endpoint, capture_content=False)
                with tracemotive.trace("installed-wheel-smoke"):
                    with tracemotive.span("installed-span"):
                        pass
                assert tracemotive.flush(5)
                payload = json.loads(urllib.request.urlopen(endpoint + "/api/v1/traces", timeout=2).read())
                assert payload["total"] == 1
                assert payload["items"][0]["name"] == "installed-wheel-smoke"
                assert payload["items"][0]["span_count"] == 1
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                assert not thread.is_alive()
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)

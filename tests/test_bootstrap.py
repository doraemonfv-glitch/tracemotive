from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
from unittest import mock
import unittest

from scripts import bootstrap


class BootstrapTests(unittest.TestCase):
    def test_windows_resolves_npm_cmd(self) -> None:
        with (
            mock.patch.object(bootstrap.os, "name", "nt"),
            mock.patch.object(bootstrap.shutil, "which", return_value=r"C:\Node\npm.cmd"),
        ):
            self.assertEqual(bootstrap._npm_executable(), r"C:\Node\npm.cmd")

    def test_posix_resolves_npm(self) -> None:
        with (
            mock.patch.object(bootstrap.os, "name", "posix"),
            mock.patch.object(bootstrap.shutil, "which", return_value="/usr/bin/npm"),
        ):
            self.assertEqual(bootstrap._npm_executable(), "/usr/bin/npm")

    def test_missing_npm_is_actionable(self) -> None:
        with mock.patch.object(bootstrap.shutil, "which", return_value=None):
            with self.assertRaises(bootstrap.BootstrapError) as context:
                bootstrap._npm_executable()
        self.assertIn("was not found on PATH", str(context.exception))

    def test_main_runs_locked_install_then_package_build(self) -> None:
        with (
            mock.patch.object(bootstrap, "_npm_executable", return_value="npm"),
            mock.patch.object(bootstrap, "_run_step") as run_step,
            mock.patch.object(bootstrap, "_validate_output") as validate_output,
        ):
            self.assertEqual(bootstrap.main(), 0)

        self.assertEqual(
            run_step.call_args_list,
            [
                mock.call("npm", "npm ci", "ci"),
                mock.call("npm", "npm run build:package", "run", "build:package"),
            ],
        )
        validate_output.assert_called_once_with()

    def test_failed_step_returns_nonzero_without_running_output_validation(self) -> None:
        with (
            mock.patch.object(bootstrap, "_npm_executable", return_value="npm"),
            mock.patch.object(
                bootstrap,
                "_run_step",
                side_effect=bootstrap.BootstrapError("npm ci failed"),
            ),
            mock.patch.object(bootstrap, "_validate_output") as validate_output,
        ):
            self.assertEqual(bootstrap.main(), 1)
        validate_output.assert_not_called()

    def test_failed_step_message_names_the_prerequisite(self) -> None:
        failure = subprocess.CalledProcessError(1, ["npm", "run", "build:package"])
        with mock.patch.object(bootstrap.subprocess, "run", side_effect=failure):
            with self.assertRaises(bootstrap.BootstrapError) as context:
                bootstrap._run_step("npm", "npm run build:package", "run", "build:package")
        self.assertIn("npm run build:package failed with exit code 1", str(context.exception))

    def test_run_step_preserves_frontend_paths_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tracemotive bootstrap ") as temp_root:
            frontend = Path(temp_root)
            with (
                mock.patch.object(bootstrap, "FRONTEND", frontend),
                mock.patch.object(bootstrap.subprocess, "run") as run,
            ):
                bootstrap._run_step("npm.cmd", "npm ci", "ci")
        run.assert_called_once_with(["npm.cmd", "ci"], cwd=frontend, check=True)

    def test_validate_output_requires_packaged_index_and_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            package_ui = Path(temp_root)
            (package_ui / "index.html").write_text("<html></html>", encoding="utf-8")
            assets = package_ui / "assets"
            assets.mkdir()
            (assets / "index.js").write_text("console.log(1);", encoding="utf-8")
            (assets / "index.css").write_text("body {}", encoding="utf-8")
            with mock.patch.object(bootstrap, "PACKAGE_UI", package_ui):
                bootstrap._validate_output()

    def test_docs_and_ci_use_the_canonical_entry_point(self) -> None:
        for relative_path in ("README.md", "CONTRIBUTING.md", ".github/workflows/ci.yml"):
            document = (bootstrap.ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn("python scripts/bootstrap.py", document, relative_path)


if __name__ == "__main__":
    unittest.main()

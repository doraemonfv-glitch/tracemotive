from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
COMPATIBILITY = ROOT / "docs" / "compatibility.md"


class CIWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.compatibility = COMPATIBILITY.read_text(encoding="utf-8")

    def _job(self, name: str) -> str:
        match = re.search(
            rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
            self.workflow,
        )
        self.assertIsNotNone(match, name)
        return match.group(0)

    def test_blocking_linux_matrix_is_unchanged(self) -> None:
        job = self._job("python-tests")
        self.assertIn("runs-on: ubuntu-latest", job)
        self.assertIn('- "3.10"', job)
        self.assertIn('- "3.12"', job)
        self.assertNotIn("continue-on-error", job)
        self.assertIn("python -m unittest discover -s tests -v", job)

    def test_cross_platform_job_runs_the_same_suite_on_windows_and_macos(self) -> None:
        job = self._job("cross-platform-python-tests")
        self.assertIn("runs-on: ${{ matrix.os }}", job)
        self.assertIn("- windows-latest", job)
        self.assertIn("- macos-latest", job)
        self.assertIn("fail-fast: false", job)
        self.assertIn("python scripts/bootstrap.py", job)
        self.assertIn("python -m pip install -r requirements.txt", job)
        self.assertIn("python -m unittest discover -s tests -v", job)

    def test_windows_also_runs_the_suite_from_a_contributor_venv(self) -> None:
        job = self._job("cross-platform-python-tests")
        match = re.search(
            r"(?ms)^      - name: Run Python tests from a Windows contributor venv\n(.*?)(?=^      - |\Z)",
            job,
        )
        self.assertIsNotNone(match)
        step = match.group(0)
        self.assertIn("if: runner.os == 'Windows' && !cancelled()", step)
        self.assertIn("$PSNativeCommandUseErrorActionPreference = $true", step)
        commands = [
            "python -m venv",
            "Scripts\\Activate.ps1",
            "assert sys.prefix != sys.base_prefix",
            'python -m pip install -e ".[server]"',
            "python -m unittest discover -s tests -v",
        ]
        positions = [step.find(command) for command in commands]
        self.assertNotIn(-1, positions, dict(zip(commands, positions)))
        self.assertEqual(positions, sorted(positions))

    def test_non_blocking_platform_evidence_is_not_a_support_claim(self) -> None:
        job = self._job("cross-platform-python-tests")
        if "continue-on-error: true" not in job:
            self.skipTest("cross-platform job is blocking; platform wording is a release decision")
        self.assertIn("non-blocking evidence job", self.compatibility)
        self.assertIn("macOS or Windows as formally validated platforms", self.compatibility)
        for claim in (
            "Windows is supported",
            "macOS is supported",
            "Windows is CI-validated",
            "macOS is CI-validated",
            "Windows and macOS are supported",
            "Windows and macOS are CI-validated",
        ):
            self.assertNotIn(claim, self.compatibility)

    def test_workflow_stays_read_only_and_secret_free(self) -> None:
        self.assertIn("permissions:\n  contents: read\n", self.workflow)
        self.assertNotIn("contents: write", self.workflow)
        self.assertNotIn("id-token: write", self.workflow)
        self.assertNotIn("secrets.", self.workflow)


if __name__ == "__main__":
    unittest.main()

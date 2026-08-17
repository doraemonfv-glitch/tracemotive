from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


class ReleaseWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def _job(self, name: str) -> str:
        match = re.search(
            rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
            self.workflow,
        )
        self.assertIsNotNone(match, name)
        return match.group(0)

    def test_trigger_is_manual_only_and_requires_explicit_confirmation(self) -> None:
        self.assertIn("\n  workflow_dispatch:\n", self.workflow)
        self.assertNotRegex(self.workflow, r"(?m)^  (push|pull_request|schedule):")
        self.assertIn("inputs:\n      confirm:", self.workflow)
        self.assertIn("type: string", self.workflow)
        self.assertIn("inputs.confirm == 'PUBLISH'", self.workflow)
        self.assertIn("startsWith(github.ref, 'refs/tags/v')", self.workflow)

    def test_build_job_runs_validation_and_uploads_exact_artifact_set(self) -> None:
        build = self._job("build")
        self.assertIn("python scripts/bootstrap.py", build)
        self.assertIn("python -m unittest discover -s tests -v", build)
        self.assertIn("npm test", build)
        self.assertIn("npm run build", build)
        self.assertIn("python -m build --sdist --wheel --no-isolation", build)
        self.assertIn("tracemotive/ui/index.html", build)
        self.assertIn("tracemotive/ui/assets/", build)
        self.assertIn("actions/upload-artifact@v4", build)
        self.assertIn("if-no-files-found: error", build)

    def test_publish_job_is_separate_and_uses_the_pypa_oidc_action(self) -> None:
        publish = self._job("publish")
        self.assertIn("needs: build", publish)
        self.assertIn("actions/download-artifact@v4", publish)
        self.assertIn("environment:\n      name: pypi", publish)
        self.assertIn("uses: pypa/gh-action-pypi-publish@release/v1", publish)
        self.assertIn("packages-dir: dist/", publish)

    def test_oidc_is_job_scoped_and_no_long_lived_credentials_are_configured(self) -> None:
        build = self._job("build")
        publish = self._job("publish")
        self.assertEqual(self.workflow.count("id-token: write"), 1)
        self.assertNotIn("id-token: write", build)
        self.assertIn("id-token: write", publish)
        self.assertNotRegex(self.workflow, r"(?i)PYPI_TOKEN|TWINE_PASSWORD|password:|username:")
        self.assertNotIn("secrets.", self.workflow)
        self.assertNotIn("contents: write", publish)
        self.assertIn("actions: read", publish)

    def test_workflow_keeps_canonical_provenance_values(self) -> None:
        for value in (
            "tracemotive",
            "doraemonfv-glitch",
            "https://github.com/doraemonfv-glitch/tracemotive",
            "https://pypi.org/p/tracemotive",
            "name: pypi",
        ):
            self.assertIn(value, self.workflow)


if __name__ == "__main__":
    unittest.main()

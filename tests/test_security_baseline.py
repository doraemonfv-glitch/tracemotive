from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.prepare_audit_runtime import AuditRuntimeError, prepare_audit_runtime


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "security.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
SECURITY_MD = ROOT / "SECURITY.md"
SECURITY_MODEL = ROOT / "docs" / "security-model.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"


class SecurityBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.dependabot = DEPENDABOT.read_text(encoding="utf-8")
        cls.security = SECURITY_MD.read_text(encoding="utf-8")
        cls.model = SECURITY_MODEL.read_text(encoding="utf-8")
        cls.contributing = CONTRIBUTING.read_text(encoding="utf-8")

    def test_security_workflow_is_least_privilege_and_uses_pinned_commands(self) -> None:
        self.assertIn("\n  pull_request:\n", self.workflow)
        self.assertIn("\n  push:\n", self.workflow)
        self.assertIn("\n  schedule:\n", self.workflow)
        self.assertIn("\n  workflow_dispatch:\n", self.workflow)
        self.assertIn('cron: "17 4 * * 1"', self.workflow)
        self.assertIn("permissions:\n  contents: read\n", self.workflow)
        self.assertNotIn("contents: write", self.workflow)
        self.assertNotIn("id-token: write", self.workflow)
        self.assertNotIn("pull-requests: write", self.workflow)
        self.assertNotIn("packages: write", self.workflow)
        self.assertIn('python -m pip install --target .audit-runtime ".[server,openai-agents]"', self.workflow)
        self.assertIn("python scripts/prepare_audit_runtime.py .audit-runtime", self.workflow)
        self.assertIn('python -m pip install "pip-audit==2.10.1"', self.workflow)
        self.assertIn("python -m pip_audit --progress-spinner off --strict --path .audit-runtime", self.workflow)
        self.assertNotIn("--ignore-vuln", self.workflow)
        self.assertNotIn("python -m venv .audit-runtime", self.workflow)
        self.assertNotIn("npm audit fix", self.workflow)
        self.assertNotIn("github/codeql-action", self.workflow)
        self.assertNotIn("dependency-review-action", self.workflow)
        self.assertLess(
            self.workflow.index('python -m pip install --target .audit-runtime ".[server,openai-agents]"'),
            self.workflow.index("python scripts/prepare_audit_runtime.py .audit-runtime"),
        )
        self.assertLess(
            self.workflow.index("python scripts/prepare_audit_runtime.py .audit-runtime"),
            self.workflow.index("python -m pip_audit --progress-spinner off --strict --path .audit-runtime"),
        )

    def test_dependabot_covers_used_ecosystems_weekly_without_automerge(self) -> None:
        self.assertIn("package-ecosystem: pip", self.dependabot)
        self.assertIn("package-ecosystem: npm", self.dependabot)
        self.assertIn("package-ecosystem: github-actions", self.dependabot)
        self.assertIn("directory: /frontend", self.dependabot)
        self.assertEqual(self.dependabot.count("interval: weekly"), 3)
        self.assertNotIn("auto-merge", self.dependabot)
        self.assertNotIn("groups:", self.dependabot)

    def test_security_docs_keep_conservative_local_first_claims(self) -> None:
        for text in (self.security, self.model):
            self.assertIn("loopback is not authentication", text.casefold())
            self.assertIn("No formal security audit has been completed.", text)
            self.assertNotIn("localhost makes TraceMotive secure", text)
            self.assertNotIn("secrets can never be captured", text.casefold())
            self.assertNotIn("cannot store credentials", text.casefold())
        self.assertIn("GitHub Private Vulnerability Reporting", self.security)
        self.assertIn("cannot enable that setting by itself", self.security)
        self.assertIn("There is no promised response SLA", self.security)
        self.assertIn("%LOCALAPPDATA%\\TraceMotive\\tracemotive.sqlite3", self.model)
        self.assertIn("DELETE /api/v1/traces/{trace_id}", self.model)
        self.assertIn("There is no automatic retention", self.model)
        self.assertIn("Redaction is defense in depth", self.model)
        self.assertIn("does not intentionally persist provider credentials", self.model)
        self.assertIn("CodeQL, GitHub dependency-review, and SBOM generation remain deferred P1 work.", self.model)
        collapsed_security = " ".join(self.security.split())
        collapsed_model = " ".join(self.model.split())
        self.assertIn(
            "isolated third-party shipped runtime dependency surface",
            collapsed_security,
        )
        self.assertIn(
            "isolated third-party shipped runtime dependency surface",
            collapsed_model,
        )
        self.assertIn("First-party TraceMotive is excluded from that PyPI lookup surface", collapsed_security)
        self.assertIn("First-party TraceMotive is excluded from that PyPI lookup surface", collapsed_model)

    def test_documented_commands_match_the_security_workflow(self) -> None:
        for text in (self.security, self.contributing):
            self.assertIn('python -m pip install --target .audit-runtime ".[server,openai-agents]"', text)
            self.assertIn("python scripts/prepare_audit_runtime.py .audit-runtime", text)
            self.assertIn('python -m pip install "pip-audit==2.10.1"', text)
            self.assertIn("python -m pip_audit --progress-spinner off --strict --path .audit-runtime", text)
            self.assertIn("npm audit --audit-level=high", text)
            self.assertNotIn("--ignore-vuln", text)
            self.assertNotIn("python -m venv .audit-runtime", text)

    def test_prepare_audit_runtime_excludes_only_first_party_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for name, dist_name in (
                ("tracemotive-0.5.0.dist-info", "tracemotive"),
                ("fastapi-0.141.1.dist-info", "fastapi"),
                ("uvicorn-0.52.3.dist-info", "uvicorn"),
                ("openai_agents-0.17.8.dist-info", "openai-agents"),
            ):
                info = target / name
                info.mkdir()
                (info / "METADATA").write_text(f"Name: {dist_name}\nVersion: 0.0\n", encoding="utf-8")
            names = prepare_audit_runtime(target)
            self.assertEqual(names, ("fastapi", "openai-agents", "uvicorn"))
            self.assertFalse((target / "tracemotive-0.5.0.dist-info").exists())
            self.assertTrue((target / "fastapi-0.141.1.dist-info").exists())

    def test_prepare_audit_runtime_rejects_missing_or_duplicate_first_party(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            info = target / "fastapi-0.141.1.dist-info"
            info.mkdir()
            (info / "METADATA").write_text("Name: fastapi\n", encoding="utf-8")
            with self.assertRaises(AuditRuntimeError):
                prepare_audit_runtime(target)

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for name in ("tracemotive-0.5.0.dist-info", "tracemotive-0.4.1.dist-info"):
                info = target / name
                info.mkdir()
                (info / "METADATA").write_text("Name: tracemotive\n", encoding="utf-8")
            with self.assertRaises(AuditRuntimeError):
                prepare_audit_runtime(target)

    def test_prepare_audit_runtime_rejects_installer_or_scanner_tooling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for name, dist_name in (
                ("tracemotive-0.5.0.dist-info", "tracemotive"),
                ("fastapi-0.141.1.dist-info", "fastapi"),
                ("pip-26.2.1.dist-info", "pip"),
            ):
                info = target / name
                info.mkdir()
                (info / "METADATA").write_text(f"Name: {dist_name}\n", encoding="utf-8")
            with self.assertRaises(AuditRuntimeError):
                prepare_audit_runtime(target)




if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from pathlib import Path
import re
import unittest

from tests.divergence_evaluation import build_evaluation_corpus
from tests.release_consistency import (
    DEVELOPMENT_DOCUMENT_PATHS,
    HISTORICAL_DOCUMENT_PATHS,
    find_checkout_commands,
    find_current_langgraph_support_claims,
    find_current_version_claims,
    find_stale_publication_claims,
    frontend_lockfile_root_version,
    frontend_package_version,
    installed_user_readme_section,
    live_document_texts,
    package_version,
    pypi_long_description_source,
)
from tracemotive.canonical.models import AGENTLENS_SCHEMA_VERSION
from tracemotive.cli import _parser
from tracemotive.collector import PROTOCOL_VERSION


ROOT = Path(__file__).resolve().parents[1]


class DocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        cls.contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        cls.agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    def test_readme_documents_the_installed_two_terminal_path(self) -> None:
        for command in (
            'python -m pip install "tracemotive[server]"',
            "tracemotive serve",
            "tracemotive demo",
            "tracemotive demo --scenario uncertain",
            "python scripts/bootstrap.py",
        ):
            self.assertIn(command, self.readme)
        self.assertIn("In a second terminal", self.readme)
        self.assertIn("Normal installed users do not install Node.js", self.readme)
        self.assertEqual(_parser().parse_args(["serve"]).command, "serve")
        self.assertEqual(_parser().parse_args(["demo"]).scenario, "identified")
        self.assertEqual(
            _parser().parse_args(["demo", "--scenario", "uncertain"]).scenario,
            "uncertain",
        )
        self.assertIn('python -m pip install "tracemotive[openai-agents]"', self.readme)
        self.assertIn("from tracemotive.integrations.openai_agents import install", self.readme)
        self.assertIn("install(local_only=True)", self.readme)
        self.assertIn("Generic Python support is manual instrumentation", self.readme)
        self.assertIn("LangGraph is not currently supported.", self.readme)
        self.assertNotIn("python -m examples.openai_agents_example", self.readme)

    def test_readme_evaluation_facts_match_the_current_oracle(self) -> None:
        scenarios = build_evaluation_corpus()
        expected_meaningful = sum(
            scenario.meaningful_divergence == "supported" for scenario in scenarios
        )
        expected_starting_points = sum(
            scenario.investigation_starting_point == "supported" for scenario in scenarios
        )
        self.assertEqual(len(scenarios), 30)
        self.assertEqual(expected_meaningful, 15)
        self.assertEqual(expected_starting_points, 14)
        self.assertIn("current **V03-10 adversarial", self.readme)
        self.assertIn(f"- {len(scenarios)} scenarios are mandatory;", self.readme)
        self.assertIn(
            f"- {expected_meaningful} have an expected confident meaningful-divergence answer;",
            self.readme,
        )
        self.assertIn(
            f"- {expected_starting_points} have an expected supported investigation starting point;",
            self.readme,
        )
        self.assertIn("false-confident meaningful-divergence target/result", self.readme)
        self.assertIn("false-confident investigation-starting-point target/result", self.readme)

    def test_public_docs_describe_the_current_specification_layers(self) -> None:
        for path in (
            "spec/v0.1-frozen-spec.md",
            "spec/v0.2-proposed-spec.md",
            "spec/v0.3-proposed-spec.md",
            "docs/v0.4/",
        ):
            self.assertIn(path, self.readme)
        self.assertIn("current v0.4 Core", self.contributing)
        self.assertIn("highest-authority compatibility contract", self.contributing)
        self.assertIn("TraceMotive v0.4 Core implementation", self.agents)
        self.assertIn("highest-authority compatibility contract", self.agents)
        self.assertNotIn("causal debugger", self.readme.casefold())
        self.assertNotIn("never wrong", self.readme.casefold())

    def test_repository_relative_readme_links_exist(self) -> None:
        links = re.findall(r"\]\(([^)]+)\)", self.readme)
        for target in links:
            if target.startswith(("http://", "https://", "#")):
                continue
            relative = target.split("#", 1)[0]
            self.assertTrue((ROOT / relative).exists(), target)


class ReleaseConsistencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.version = package_version()
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        cls.installed = installed_user_readme_section(cls.readme)
        cls.live = live_document_texts()

    def test_pyproject_version_is_authoritative_and_matches_frontend_root(self) -> None:
        frontend_version = frontend_package_version()
        lock_top, lock_nested = frontend_lockfile_root_version()
        self.assertRegex(self.version, r"^\d+\.\d+\.\d+$")
        self.assertEqual(
            frontend_version,
            self.version,
            f"frontend/package.json version {frontend_version} does not match pyproject.toml version {self.version}",
        )
        self.assertEqual(
            lock_top,
            self.version,
            f"frontend/package-lock.json version {lock_top} does not match pyproject.toml version {self.version}",
        )
        self.assertEqual(
            lock_nested,
            self.version,
            f"frontend/package-lock.json packages[''].version {lock_nested} does not match pyproject.toml version {self.version}",
        )

    def test_live_current_version_claims_match_pyproject(self) -> None:
        for path, text in self.live.items():
            for claimed in find_current_version_claims(text):
                self.assertEqual(
                    claimed,
                    self.version,
                    f"{path.relative_to(ROOT)} current-version claim {claimed} does not match pyproject.toml version {self.version}",
                )
        self.assertIn(
            f"The current package version is `{self.version}`.",
            self.readme,
            "README current-package version sentence is missing or no longer matches pyproject.toml",
        )
        self.assertIn(
            f"| Package metadata | `{self.version}` distribution version",
            self.readme,
            "README compatibility table current-package version does not match pyproject.toml",
        )

    def test_pypi_long_description_source_is_readme(self) -> None:
        self.assertEqual(pypi_long_description_source(), "README.md")

    def test_pypi_long_description_source_uses_project_readme_table(self) -> None:
        from tempfile import TemporaryDirectory
        from tests.release_consistency import pypi_long_description_source as read_readme

        decoy = (
            "[tool.decoy]\n"
            'readme = "WRONG.md"\n\n'
            "[project]\n"
            'name = "tracemotive"\n'
            'version = "0.4.1"\n'
            'readme = "README.md"\n\n'
            "[tool.other]\n"
            'readme = "ALSO-WRONG.md"\n'
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(decoy, encoding="utf-8")
            self.assertEqual(read_readme(root), "README.md")

    def test_live_docs_have_no_stale_current_publication_wording(self) -> None:
        for path, text in self.live.items():
            stale = find_stale_publication_claims(text)
            self.assertEqual(
                stale,
                [],
                f"{path.relative_to(ROOT)} contains stale current-package publication wording: {stale[0] if stale else ''}",
            )

    def test_readme_installed_user_path_uses_packaged_commands(self) -> None:
        self.assertIn('python -m pip install "tracemotive[server]"', self.installed)
        self.assertIn('python -m pip install "tracemotive[openai-agents]"', self.installed)
        self.assertIn("from tracemotive.integrations.openai_agents import install", self.installed)
        checkout = find_checkout_commands(self.installed)
        self.assertEqual(
            checkout,
            [],
            "README installed-user onboarding contains checkout-only command:\n" + "\n".join(checkout),
        )
        self.assertNotIn("python -m examples", self.installed)
        self.assertIn("Normal installed users do not install Node.js", self.installed)

    def test_live_support_claims_are_consistent(self) -> None:
        self.assertIn("validated framework integration is the public OpenAI Agents SDK adapter", self.readme)
        self.assertIn("Generic Python support is manual instrumentation", self.readme)
        self.assertIn("LangGraph is not currently supported.", self.readme)
        for path, text in self.live.items():
            claims = find_current_langgraph_support_claims(text)
            self.assertEqual(
                claims,
                [],
                f"{path.relative_to(ROOT)} advertises current LangGraph support: {claims[0] if claims else ''}",
            )

    def test_compatibility_claims_match_source_and_do_not_invent_v5(self) -> None:
        self.assertIn(f"Canonical schema `{AGENTLENS_SCHEMA_VERSION}`", self.readme)
        self.assertIn(f"ingest protocol `{PROTOCOL_VERSION}`", self.readme)
        self.assertIn("`/api/v1`", self.readme)
        self.assertIn("`/api/v2`", self.readme)
        self.assertIn("`/api/v3`", self.readme)
        self.assertIn("`/api/v4/compare/{left}/{right}`", self.readme)
        self.assertIn("investigation comparison surface", self.readme)
        self.assertIn("structured-diff projection", self.readme)
        self.assertNotIn("remain compatibility surfaces", self.readme)
        for path, text in self.live.items():
            self.assertNotIn(
                "/api/v5",
                text,
                f"{path.relative_to(ROOT)} introduces /api/v5",
            )

    def test_development_docs_may_keep_checkout_commands(self) -> None:
        contributing = DEVELOPMENT_DOCUMENT_PATHS[0].read_text(encoding="utf-8")
        examples = DEVELOPMENT_DOCUMENT_PATHS[1].read_text(encoding="utf-8")
        self.assertIn("pip install -e", contributing)
        self.assertIn("scripts/bootstrap.py", contributing)
        self.assertIn("python -m examples.openai_agents_example", examples)

    def test_historical_design_docs_are_not_live_product_claims(self) -> None:
        historical = "\n".join(path.read_text(encoding="utf-8") for path in HISTORICAL_DOCUMENT_PATHS)
        self.assertTrue(historical)
        live = "\n".join(self.live.values())
        self.assertNotIn("conditional v0.4 design work", live)
        self.assertIn("LangGraph", historical)


if __name__ == "__main__":
    unittest.main()

class CompatibilityLimitsStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        cls.compatibility = (ROOT / "docs" / "compatibility.md").read_text(encoding="utf-8")
        cls.limits = (ROOT / "docs" / "limits.md").read_text(encoding="utf-8")
        cls.storage = (ROOT / "docs" / "storage.md").read_text(encoding="utf-8")
        cls.ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        from tests.release_consistency import _project_table

        cls.project = _project_table()

    def test_python_and_agents_claims_match_metadata_and_ci(self) -> None:
        self.assertEqual(self.project.get("requires-python"), ">=3.10")
        extras = self.project.get("optional-dependencies")
        self.assertIsInstance(extras, dict)
        self.assertIn("openai-agents>=0.17,<0.18", extras["openai-agents"])
        self.assertIn('- "3.10"', self.ci)
        self.assertIn('- "3.12"', self.ci)
        for text in (self.readme, self.compatibility):
            self.assertIn("`>=3.10`", text)
            self.assertIn("`3.10`", text)
            self.assertIn("`3.12`", text)
            self.assertIn("openai-agents>=0.17,<0.18", text)
            self.assertIn("LangGraph is not currently supported.", text)
            self.assertNotIn("/api/v5", text)
        self.assertIn("Installed users do not need Node.js", self.compatibility)
        self.assertIn("Normal installed users do not install Node.js", self.readme)
        self.assertIn("accepted by metadata", self.compatibility)
        self.assertIn("not a current CI matrix version", self.compatibility)

    def test_documented_hard_limits_match_source_constants(self) -> None:
        from tracemotive.comparison import (
            MAX_COMPARISON_RESPONSE_BYTES,
            MAX_COMPARISON_SPANS,
            MAX_DIFFERENCE_RECORDS,
        )
        from tracemotive.structured_diff import (
            MAX_STRUCTURED_DIFF_DEPTH,
            MAX_STRUCTURED_DIFF_NODES,
            MAX_STRUCTURED_DIFF_RECORDS,
            MAX_STRUCTURED_DIFF_VALUE_BYTES,
        )
        from tracemotive.privacy import MAX_CONTENT_BYTES

        self.assertEqual(MAX_COMPARISON_SPANS, 10_000)
        self.assertEqual(MAX_DIFFERENCE_RECORDS, 4_096)
        self.assertEqual(MAX_COMPARISON_RESPONSE_BYTES, 4 * 1024 * 1024)
        self.assertEqual(MAX_STRUCTURED_DIFF_DEPTH, 32)
        self.assertEqual(MAX_STRUCTURED_DIFF_NODES, 4_096)
        self.assertEqual(MAX_STRUCTURED_DIFF_RECORDS, 256)
        self.assertEqual(MAX_STRUCTURED_DIFF_VALUE_BYTES, 64 * 1024)
        self.assertEqual(MAX_CONTENT_BYTES, 262144)
        self.assertIn("`10,000`", self.limits)
        self.assertIn("`4,096`", self.limits)
        self.assertIn("`4 MiB`", self.limits)
        self.assertIn("`32`", self.limits)
        self.assertIn("`256`", self.limits)
        self.assertIn("`64 KiB`", self.limits)
        self.assertIn("comparison fails", self.limits)
        self.assertIn("bounded", self.limits)
        self.assertIn("projection", self.limits)
        self.assertIn("per subtree", self.limits)
        self.assertIn("global visited-node budget", self.limits)
        self.assertIn("`262,144`", self.limits)
        self.assertIn("`10,000`", self.readme)
        self.assertIn("`4,096`", self.readme)
        self.assertIn("`4 MiB`", self.readme)

    def test_storage_docs_match_current_deletion_and_retention_reality(self) -> None:
        self.assertIn("`--db PATH`", self.storage)
        self.assertIn("`TRACEMOTIVE_DB`", self.storage)
        self.assertIn("`:memory:`", self.storage)
        self.assertIn("DELETE /api/v1/traces/{trace_id}", self.storage)
        self.assertIn("automatic retention", self.storage)
        self.assertIn("SQLite is not encrypted at rest.", self.storage)
        self.assertNotIn("encrypted at rest", self.storage.replace("SQLite is not encrypted at rest.", ""))
        self.assertNotIn("automatic cleanup", self.storage.casefold())
        self.assertIn("File-backed databases have no automatic retention", self.storage)
        self.assertIn("persist across process restarts", self.storage)
        self.assertIn("default `:memory:` database", self.storage)
        self.assertIn("does not persist across process termination", self.storage)
        self.assertNotIn("Data remains until that trace is deleted or the database file is removed.", self.storage)
        self.assertIn("no automatic retention", self.readme)
        self.assertIn("does not persist across process termination", self.readme)
        self.assertIn("DELETE /api/v1/traces/{trace_id}", self.readme)
        self.assertIn("Loopback is not authentication.", self.readme)
        self.assertIn("investigation comparison surface", self.compatibility)
        self.assertIn("structured-diff projection", self.compatibility)
        self.assertNotIn("remain compatibility surfaces", self.compatibility)

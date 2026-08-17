from __future__ import annotations

from pathlib import Path
import re
import unittest

from tests.divergence_evaluation import build_evaluation_corpus
from tracemotive.cli import _parser


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


if __name__ == "__main__":
    unittest.main()

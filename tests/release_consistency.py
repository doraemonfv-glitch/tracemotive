"""Release-consistency helpers for live public TraceMotive surfaces."""

from __future__ import annotations

from pathlib import Path
import json
import re

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
LIVE_DOCUMENT_PATHS = (
    ROOT / "README.md",
    ROOT / "docs" / "openai-agents.md",
    ROOT / "docs" / "compatibility.md",
    ROOT / "docs" / "limits.md",
    ROOT / "docs" / "storage.md",
    ROOT / "SECURITY.md",
    ROOT / "frontend" / "src" / "onboarding.tsx",
)
HISTORICAL_DOCUMENT_PATHS = (
    ROOT / "spec" / "v0.1-frozen-spec.md",
    ROOT / "spec" / "v0.2-proposed-spec.md",
    ROOT / "spec" / "v0.3-proposed-spec.md",
    ROOT / "docs" / "v0.4" / "v0.4-frozen-spec.md",
    ROOT / "docs" / "v0.4" / "v0.4-scope.md",
)
DEVELOPMENT_DOCUMENT_PATHS = (
    ROOT / "CONTRIBUTING.md",
    ROOT / "examples" / "README.md",
)


_README_HEADING = re.compile(r"(?m)^(?P<heading>## .+)$")
_STALE_PUBLICATION = re.compile(
    r"has not been published|not yet published|is not yet published|"
    r"publication is outside this (?:local onboarding )?procedure|"
    r"available only from source|unreleased package",
    re.IGNORECASE,
)
_SUPPORTED_LANGGRAPH = re.compile(
    r"LangGraph[^\n.]{0,80}(?:currently supported|experimental support|is supported)",
    re.IGNORECASE,
)
_CONDITIONAL_LANGGRAPH = re.compile(
    r"LangGraph[^\n.]{0,120}(?:conditional v0\.4|deferred to v0\.4\.1|currently supported)",
    re.IGNORECASE,
)
_CHECKOUT_COMMANDS = (
    "pip install -e",
    "python -m examples",
    "scripts/bootstrap.py",
    "npm ci",
    "npm run build:package",
)
_CURRENT_VERSION_CLAIMS = (
    re.compile(r"The current package version is `([^`]+)`"),
    re.compile(r"Package metadata \| `([^`]+)` distribution version"),
    re.compile(r"(?m)^TraceMotive v(\d+\.\d+\.\d+) supports"),
)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _project_table(root: Path = ROOT) -> dict[str, object]:
    data = tomllib.loads(read_text(root / "pyproject.toml"))
    project = data.get("project")
    if not isinstance(project, dict):
        raise AssertionError("pyproject.toml [project] table is missing")
    return project


def package_version(root: Path = ROOT) -> str:
    version = _project_table(root).get("version")
    if not isinstance(version, str) or not version:
        raise AssertionError("pyproject.toml project.version could not be read deterministically")
    return version


def frontend_package_version(root: Path = ROOT) -> str:
    payload = json.loads(read_text(root / "frontend" / "package.json"))
    version = payload.get("version")
    if not isinstance(version, str) or not version:
        raise AssertionError("frontend/package.json version is missing")
    return version


def frontend_lockfile_root_version(root: Path = ROOT) -> tuple[str, str]:
    payload = json.loads(read_text(root / "frontend" / "package-lock.json"))
    top = payload.get("version")
    packages = payload.get("packages")
    if not isinstance(top, str) or not top:
        raise AssertionError("frontend/package-lock.json root version is missing")
    if not isinstance(packages, dict) or "" not in packages:
        raise AssertionError("frontend/package-lock.json packages[''] is missing")
    nested = packages[""].get("version")
    if not isinstance(nested, str) or not nested:
        raise AssertionError("frontend/package-lock.json packages[''].version is missing")
    return top, nested


def pypi_long_description_source(root: Path = ROOT) -> str:
    readme = _project_table(root).get("readme")
    if not isinstance(readme, str) or not readme:
        raise AssertionError("pyproject.toml project.readme is missing")
    return readme


def installed_user_readme_section(readme: str) -> str:
    headings = list(_README_HEADING.finditer(readme))
    start = 0
    end = len(readme)
    for index, heading in enumerate(headings):
        title = heading.group("heading")
        if title.startswith("## Contributor setup"):
            end = heading.start()
            break
        if title.startswith("## Try it locally"):
            start = heading.start()
    section = readme[start:end]
    if "python -m pip install \"tracemotive[server]\"" not in section:
        raise AssertionError("README installed-user onboarding is missing the packaged server install command")
    return section


def live_document_texts(root: Path = ROOT) -> dict[Path, str]:
    return {path: read_text(path) for path in LIVE_DOCUMENT_PATHS}


def find_stale_publication_claims(text: str) -> list[str]:
    return [match.group(0) for match in _STALE_PUBLICATION.finditer(text)]


def find_checkout_commands(text: str) -> list[str]:
    return [command for command in _CHECKOUT_COMMANDS if command in text]


def find_current_langgraph_support_claims(text: str) -> list[str]:
    claims = [match.group(0) for match in _SUPPORTED_LANGGRAPH.finditer(text)]
    claims.extend(match.group(0) for match in _CONDITIONAL_LANGGRAPH.finditer(text))
    return [claim for claim in claims if "not currently supported" not in claim.casefold()]


def find_current_version_claims(text: str) -> list[str]:
    versions: list[str] = []
    for pattern in _CURRENT_VERSION_CLAIMS:
        versions.extend(match.group(1) for match in pattern.finditer(text))
    return versions

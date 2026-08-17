"""Bootstrap the frontend package data for a repository checkout.

This is the single repository entry point for the disposable frontend build:
install the locked npm dependencies, build the Vite application, and copy the
result into the Python package data directory.  The generated files remain
ignored and are not part of the source checkout contract.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
PACKAGE_UI = ROOT / "tracemotive" / "ui"


class BootstrapError(RuntimeError):
    """Raised when the checkout cannot be prepared safely."""


def _npm_executable() -> str:
    command = "npm.cmd" if os.name == "nt" else "npm"
    executable = shutil.which(command)
    if executable is None:
        raise BootstrapError(
            f"{command} was not found on PATH; install a supported Node.js/npm "
            "runtime before running the repository bootstrap"
        )
    return executable


def _run_step(npm: str, label: str, *arguments: str) -> None:
    command = [npm, *arguments]
    print(f"tracemotive bootstrap: {label}")
    try:
        subprocess.run(command, cwd=FRONTEND, check=True)
    except FileNotFoundError as exc:
        raise BootstrapError(
            f"could not start {label}; verify that the Node.js/npm runtime is installed"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise BootstrapError(
            f"{label} failed with exit code {exc.returncode}; see the command output above"
        ) from exc


def _validate_inputs() -> None:
    if sys.version_info < (3, 10):
        raise BootstrapError("Python 3.10 or newer is required for the repository bootstrap")
    for relative_path in ("package.json", "package-lock.json"):
        if not (FRONTEND / relative_path).is_file():
            raise BootstrapError(f"frontend/{relative_path} is missing from the checkout")
    if not (ROOT / "scripts" / "package_frontend.py").is_file():
        raise BootstrapError("scripts/package_frontend.py is missing from the checkout")


def _validate_output() -> None:
    index = PACKAGE_UI / "index.html"
    assets = PACKAGE_UI / "assets"
    if not index.is_file():
        raise BootstrapError(
            "packaged frontend index.html is missing after the build; "
            "check the npm build output"
        )
    try:
        asset_files = [path for path in assets.iterdir() if path.is_file()]
    except OSError as exc:
        raise BootstrapError("packaged frontend assets are missing after the build") from exc
    if not asset_files:
        raise BootstrapError("packaged frontend assets are empty after the build")
    if not any(path.suffix.lower() in {".js", ".css"} for path in asset_files):
        raise BootstrapError("packaged frontend has no JavaScript or CSS asset after the build")
    print(
        "tracemotive bootstrap: packaged frontend ready "
        f"({len(asset_files)} asset files under {PACKAGE_UI / 'assets'})"
    )


def main() -> int:
    try:
        _validate_inputs()
        npm = _npm_executable()
        _run_step(npm, "npm ci", "ci")
        _run_step(npm, "npm run build:package", "run", "build:package")
        _validate_output()
    except BootstrapError as exc:
        print(f"tracemotive bootstrap: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

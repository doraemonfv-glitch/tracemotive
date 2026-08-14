"""Copy deterministic Vite output into Python package data.

The release boundary is npm ci -> npm run build -> this script -> Python
wheel/sdist construction. frontend/dist remains disposable build output; only
the copied files under tracemotive/ui are package data.
"""

from __future__ import annotations

from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "frontend" / "dist"
DESTINATION = ROOT / "tracemotive" / "ui"


def main() -> int:
    if not (SOURCE / "index.html").is_file():
        raise SystemExit("frontend/dist/index.html is missing; run npm run build first")

    DESTINATION.mkdir(parents=True, exist_ok=True)
    for child in DESTINATION.iterdir():
        # Keep Python package code in the resource package.  The generated
        # Vite output is the only content this step owns.
        if child.suffix == ".py":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()

    for child in SOURCE.iterdir():
        target = DESTINATION / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

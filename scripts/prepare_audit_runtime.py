"""Prepare a disposable pip-audit target for third-party shipped runtime deps.

The Security workflow installs ``tracemotive[server,openai-agents]`` into a
disposable ``--target`` directory so dependency resolution stays realistic.
pip-audit must then inspect that third-party runtime surface without requiring
the first-party TraceMotive distribution to exist on PyPI.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import sys


FIRST_PARTY_NAME = "tracemotive"
FORBIDDEN_AUDIT_NAMES = frozenset(
    {
        "pip",
        "setuptools",
        "wheel",
        "pip-audit",
        "pip_audit",
    }
)


class AuditRuntimeError(RuntimeError):
    """Raised when the disposable audit target is not in the expected state."""


def _distribution_name(dist_info: Path) -> str:
    metadata = dist_info / "METADATA"
    if not metadata.is_file():
        raise AuditRuntimeError(f"{dist_info} is missing METADATA")
    for line in metadata.read_text(encoding="utf-8").splitlines():
        if line.startswith("Name:"):
            name = line.split(":", 1)[1].strip()
            if name:
                return name
    raise AuditRuntimeError(f"{dist_info} METADATA has no Name field")


def _dist_info_directories(target: Path) -> list[Path]:
    return sorted(
        path
        for path in target.iterdir()
        if path.is_dir() and path.name.endswith(".dist-info")
    )


def prepared_distribution_names(target: Path) -> tuple[str, ...]:
    names = [_distribution_name(path) for path in _dist_info_directories(target)]
    return tuple(sorted(names, key=str.casefold))


def prepare_audit_runtime(target: Path) -> tuple[str, ...]:
    if not target.is_dir():
        raise AuditRuntimeError(f"audit target does not exist: {target}")

    first_party: list[Path] = []
    remaining: list[str] = []
    forbidden: list[str] = []
    for dist_info in _dist_info_directories(target):
        name = _distribution_name(dist_info)
        folded = name.casefold()
        if folded == FIRST_PARTY_NAME:
            first_party.append(dist_info)
            continue
        if folded in {item.casefold() for item in FORBIDDEN_AUDIT_NAMES}:
            forbidden.append(name)
            continue
        remaining.append(name)

    if len(first_party) != 1:
        raise AuditRuntimeError(
            "expected exactly one first-party TraceMotive dist-info directory; "
            f"found {len(first_party)}"
        )
    if forbidden:
        raise AuditRuntimeError(
            "excluded installer or scanner tooling appeared in the audit "
            f"target: {', '.join(sorted(forbidden, key=str.casefold))}"
        )

    shutil.rmtree(first_party[0])
    names = prepared_distribution_names(target)
    if any(name.casefold() == FIRST_PARTY_NAME for name in names):
        raise AuditRuntimeError("TraceMotive dist-info remained after exclusion")
    leftover_forbidden = [
        name for name in names if name.casefold() in {item.casefold() for item in FORBIDDEN_AUDIT_NAMES}
    ]
    if leftover_forbidden:
        raise AuditRuntimeError(
            "excluded installer or scanner tooling remained in the audit "
            f"target: {', '.join(leftover_forbidden)}"
        )
    if not names:
        raise AuditRuntimeError("audit target has no third-party distributions after exclusion")
    return names


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        raise SystemExit("usage: python scripts/prepare_audit_runtime.py <target>")
    target = Path(arguments[0])
    try:
        names = prepare_audit_runtime(target)
    except AuditRuntimeError as exc:
        raise SystemExit(f"tracemotive audit-runtime: {exc}") from exc
    print("tracemotive audit-runtime: excluded first-party TraceMotive metadata")
    print(f"tracemotive audit-runtime: {len(names)} third-party distributions remain")
    for name in names:
        print(f"tracemotive audit-runtime: {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

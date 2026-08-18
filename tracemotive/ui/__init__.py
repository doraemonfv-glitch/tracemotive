"""Access to the packaged TraceMotive production UI resources."""

from importlib.resources import files

try:
    from importlib.resources.abc import Traversable
except ModuleNotFoundError:  # Python 3.10
    from importlib.abc import Traversable


def get_ui_root() -> Traversable:
    """Return the installed production UI resource root."""

    return files(__package__)


__all__ = ["get_ui_root"]

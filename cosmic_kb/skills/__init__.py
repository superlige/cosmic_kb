"""Packaged Agent Skills shipped with :mod:`cosmic_kb`."""

from __future__ import annotations

from importlib import resources
try:  # Python 3.11+
    from importlib.resources.abc import Traversable
except ImportError:  # Python 3.10
    from importlib.abc import Traversable


SKILL_NAMES = ("cosmic-kb-understand", "cosmic-kb-setup")


def skill_file(name: str) -> Traversable:
    """Return the packaged ``SKILL.md`` resource for *name*."""
    if name not in SKILL_NAMES:
        raise ValueError(f"unknown bundled skill: {name}")
    return resources.files(__package__).joinpath(name, "SKILL.md")


def read_skill(name: str) -> bytes:
    """Read one bundled skill as bytes, failing clearly on a broken package."""
    resource = skill_file(name)
    if not resource.is_file():
        raise FileNotFoundError(f"packaged skill resource is missing: {name}/SKILL.md")
    return resource.read_bytes()


__all__ = ["SKILL_NAMES", "read_skill", "skill_file"]

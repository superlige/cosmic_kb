"""Cross-platform installer for Cosmic KB Agent Skills.

CodeBuddy and Qoder expose documented filesystem locations. TRAE currently
documents UI import, so its adapter only prepares a stable import bundle.
"""

from __future__ import annotations

import hashlib
import os
import stat
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .. import __version__
from . import SKILL_NAMES, read_skill


AGENTS = ("codebuddy", "qoder", "trae")
_COMMANDS = {
    "codebuddy": ("codebuddy",),
    "qoder": ("qoder", "qodercli"),
    "trae": ("trae",),
}


class SkillResourceError(RuntimeError):
    """Raised when the installed wheel does not contain its Skill resources."""


def _candidate_config_paths(agent: str, home: Path, environ: Mapping[str, str]) -> tuple[Path, ...]:
    if agent == "codebuddy":
        return (home / ".codebuddy",)
    if agent == "qoder":
        return (home / ".qoder",)
    paths = [home / ".trae"]
    appdata = environ.get("APPDATA")
    local = environ.get("LOCALAPPDATA")
    if appdata:
        paths.extend((Path(appdata) / "Trae", Path(appdata) / "TRAE"))
    if local:
        paths.extend(
            (
                Path(local) / "Trae",
                Path(local) / "TRAE",
                Path(local) / "Programs" / "Trae",
                Path(local) / "Programs" / "TRAE",
            )
        )
    return tuple(paths)


def detect_agents(
    *,
    home: Path | None = None,
    project: Path | None = None,
    scope: str = "user",
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Detect supported hosts without writing to their configuration."""
    home = (home or Path.home()).expanduser()
    project = (project or Path.cwd()).expanduser()
    environ = os.environ if environ is None else environ
    found: list[str] = []
    for agent in AGENTS:
        command_found = any(which(command) for command in _COMMANDS[agent])
        config_found = any(path.exists() for path in _candidate_config_paths(agent, home, environ))
        if scope == "project" and agent in ("codebuddy", "qoder"):
            config_found = config_found or (project / f".{agent}").exists()
        if command_found or config_found:
            found.append(agent)
    return found


def resolve_agents(
    requested: Iterable[str] | None,
    *,
    detected: Iterable[str] | None = None,
) -> list[str]:
    """Resolve ``auto``/``all`` and de-duplicate explicit agent names."""
    values = list(requested or ["auto"])
    invalid = [value for value in values if value not in (*AGENTS, "auto", "all")]
    if invalid:
        raise ValueError(f"unsupported agent: {', '.join(invalid)}")
    if "all" in values:
        return list(AGENTS)
    if "auto" in values:
        if len(values) != 1:
            raise ValueError("--agent auto cannot be combined with explicit agents")
        return list(detected or [])
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def target_root(
    agent: str,
    *,
    scope: str,
    home: Path,
    project: Path,
    version: str = __version__,
) -> Path:
    """Return the documented install root or TRAE import staging root."""
    if scope not in ("user", "project"):
        raise ValueError(f"unsupported scope: {scope}")
    if agent == "codebuddy":
        return (home / ".codebuddy" if scope == "user" else project / ".codebuddy") / "skills"
    if agent == "qoder":
        return (home / ".qoder" if scope == "user" else project / ".qoder") / "skills"
    if agent == "trae":
        return home / ".cosmic_kb" / "trae-import" / version
    raise ValueError(f"unsupported agent: {agent}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _load_resources() -> dict[str, bytes]:
    try:
        return {name: read_skill(name) for name in SKILL_NAMES}
    except (FileNotFoundError, OSError) as exc:
        raise SkillResourceError(str(exc)) from exc


def _trae_steps(root: Path) -> list[str]:
    return [
        "打开 TRAE 设置（Settings）",
        "进入 Rule & Skills → Skills → Create",
        f"依次导入 {root / SKILL_NAMES[0] / 'SKILL.md'} 和 {root / SKILL_NAMES[1] / 'SKILL.md'}",
    ]


def _trae_uninstall_steps() -> list[str]:
    return [
        "打开 TRAE 设置（Settings）",
        "进入 Rule & Skills → Skills",
        f"删除 {SKILL_NAMES[0]} 和 {SKILL_NAMES[1]}",
    ]


def _is_directory_link(path: Path) -> bool:
    """Return whether *path* is a directory symlink, junction, or reparse point."""
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        if os.name == "nt":
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
            return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return False
    return False


def _remove_directory_link(path: Path) -> None:
    """Remove a directory link itself without traversing into its target."""
    if path.is_symlink():
        path.unlink()
    else:
        # Windows directory junctions are removed with rmdir; this leaves the
        # junction target and all of its contents untouched.
        path.rmdir()


def _remove_managed_skill(path: Path) -> None:
    """Remove a managed host entry without following a linked Skill directory."""
    if _is_directory_link(path.parent):
        _remove_directory_link(path.parent)
        return
    path.unlink()
    try:
        path.parent.rmdir()
    except OSError:
        # Preserve unexpected user files in the same-name directory.
        pass


def install(
    agents: Iterable[str],
    *,
    scope: str = "user",
    project: Path | None = None,
    home: Path | None = None,
    dry_run: bool = False,
) -> tuple[dict, int]:
    """Install both bundled Skills, always replacing same-name files."""
    project = (project or Path.cwd()).expanduser().resolve()
    home = (home or Path.home()).expanduser().resolve()
    resources_by_name = _load_resources()
    payload: dict = {
        "command": "install",
        "scope": scope,
        "project": str(project),
        "dry_run": dry_run,
        "agents": [],
    }
    failures = 0
    for agent in agents:
        root = target_root(agent, scope=scope, home=home, project=project)
        initial_status = (
            "would_stage" if dry_run and agent == "trae" else
            "would_install" if dry_run else
            "manual_action_required" if agent == "trae" else
            "installed"
        )
        agent_result = {
            "agent": agent,
            "status": initial_status,
            "root": str(root),
            "skills": [],
        }
        for name, data in resources_by_name.items():
            target = root / name / "SKILL.md"
            item = {"name": name, "path": str(target), "sha256": _sha256(data)}
            try:
                if not dry_run:
                    if _is_directory_link(root):
                        raise OSError(f"refusing to install through linked Skills root: {root}")
                    if _is_directory_link(target.parent):
                        _remove_directory_link(target.parent)
                    _atomic_write(target, data)
                item["status"] = "would_stage" if dry_run and agent == "trae" else (
                    "would_install" if dry_run else "staged" if agent == "trae" else "installed"
                )
            except OSError as exc:
                failures += 1
                item.update(status="failed", error=f"{type(exc).__name__}: {exc}")
                agent_result["status"] = "failed"
            agent_result["skills"].append(item)
        if agent == "trae":
            agent_result["manual_steps"] = _trae_steps(root)
        payload["agents"].append(agent_result)
    payload["summary"] = {
        "requested_agents": len(payload["agents"]),
        "skill_files": len(payload["agents"]) * len(SKILL_NAMES),
        "failures": failures,
    }
    return payload, 1 if failures else 0


def status(
    agents: Iterable[str],
    *,
    scope: str = "user",
    project: Path | None = None,
    home: Path | None = None,
) -> tuple[dict, int]:
    """Compare installed Skill files with the resources in this package."""
    project = (project or Path.cwd()).expanduser().resolve()
    home = (home or Path.home()).expanduser().resolve()
    resources_by_name = _load_resources()
    payload: dict = {
        "command": "status",
        "scope": scope,
        "project": str(project),
        "agents": [],
    }
    for agent in agents:
        root = target_root(agent, scope=scope, home=home, project=project)
        items = []
        for name, data in resources_by_name.items():
            target = root / name / "SKILL.md"
            expected = _sha256(data)
            if not target.is_file():
                state = "missing"
                actual = None
            else:
                try:
                    actual = _sha256(target.read_bytes())
                    state = "installed" if actual == expected else "outdated"
                except OSError:
                    actual = None
                    state = "unreadable"
            items.append(
                {"name": name, "path": str(target), "status": state,
                 "sha256": actual, "expected_sha256": expected}
            )
        states = {item["status"] for item in items}
        if states == {"installed"}:
            agent_state = "manual_action_required" if agent == "trae" else "installed"
        elif "unreadable" in states:
            agent_state = "unreadable"
        elif "outdated" in states:
            agent_state = "outdated"
        else:
            agent_state = "missing"
        result = {"agent": agent, "status": agent_state, "root": str(root), "skills": items}
        if agent == "trae":
            result["manual_steps"] = _trae_steps(root)
        payload["agents"].append(result)
    return payload, 0


def uninstall(
    agents: Iterable[str],
    *,
    scope: str = "user",
    project: Path | None = None,
    home: Path | None = None,
    dry_run: bool = False,
) -> tuple[dict, int]:
    """Remove only the two managed SKILL.md files, preserving unrelated files."""
    project = (project or Path.cwd()).expanduser().resolve()
    home = (home or Path.home()).expanduser().resolve()
    payload: dict = {
        "command": "uninstall",
        "scope": scope,
        "project": str(project),
        "dry_run": dry_run,
        "agents": [],
    }
    failures = removed = missing = 0
    for agent in agents:
        root = target_root(agent, scope=scope, home=home, project=project)
        result = {"agent": agent, "status": "removed", "root": str(root), "skills": []}
        for name in SKILL_NAMES:
            target = root / name / "SKILL.md"
            item = {"name": name, "path": str(target)}
            skill_dir_is_link = _is_directory_link(target.parent)
            if not target.is_file() and not target.is_symlink() and not skill_dir_is_link:
                missing += 1
                item["status"] = "missing"
            elif dry_run:
                item["status"] = "would_remove"
            else:
                try:
                    if _is_directory_link(root):
                        raise OSError(f"refusing to uninstall through linked Skills root: {root}")
                    _remove_managed_skill(target)
                    removed += 1
                    item["status"] = "removed"
                except OSError as exc:
                    failures += 1
                    item.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            result["skills"].append(item)
        states = {item["status"] for item in result["skills"]}
        if "failed" in states:
            result["status"] = "failed"
        elif dry_run and "would_remove" in states:
            result["status"] = "would_remove"
        elif states == {"missing"}:
            result["status"] = "missing"
        if agent == "trae":
            result["status"] = "failed" if "failed" in states else "manual_action_required"
            result["manual_steps"] = _trae_uninstall_steps()
        payload["agents"].append(result)
    payload["summary"] = {
        "requested_agents": len(payload["agents"]),
        "removed": removed,
        "missing": missing,
        "failures": failures,
    }
    return payload, 1 if failures else 0


__all__ = [
    "AGENTS", "SkillResourceError", "detect_agents", "install", "resolve_agents",
    "status", "target_root", "uninstall",
]

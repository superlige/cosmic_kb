"""Agent Skill packaging, installation, detection, and CLI tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cosmic_kb import __version__
from cosmic_kb.cli import main as cli_main
from cosmic_kb.skills import SKILL_NAMES, read_skill
from cosmic_kb.skills import installer


def _make_directory_link(link: Path, target: Path) -> None:
    """Create a directory link, falling back to a Windows junction."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        import _winapi

        _winapi.CreateJunction(str(target), str(link))


def test_bundled_skills_have_portable_frontmatter():
    for name in SKILL_NAMES:
        text = read_skill(name).decode("utf-8")
        assert text.startswith("---\n")
        frontmatter = text.split("---", 2)[1].strip().splitlines()
        keys = {line.split(":", 1)[0] for line in frontmatter if ":" in line}
        assert keys == {"name", "description"}
        assert f"name: {name}" in frontmatter
        assert len(text) > 500


def test_detect_agents_by_command_and_config(tmp_path):
    home = tmp_path / "home"
    (home / ".qoder").mkdir(parents=True)
    commands = {"codebuddy": "C:/bin/codebuddy.exe"}

    assert installer.detect_agents(
        home=home, which=commands.get, environ={}
    ) == ["codebuddy", "qoder"]


def test_detects_project_skill_directories(tmp_path):
    project = tmp_path / "project"
    (project / ".codebuddy" / "skills").mkdir(parents=True)
    assert installer.detect_agents(
        home=tmp_path / "home", project=project, scope="project",
        which=lambda _name: None, environ={},
    ) == ["codebuddy"]


def test_resolve_agents_auto_all_and_multiple():
    assert installer.resolve_agents(["auto"], detected=["qoder"]) == ["qoder"]
    assert installer.resolve_agents(["all"]) == list(installer.AGENTS)
    assert installer.resolve_agents(["qoder", "codebuddy", "qoder"]) == ["qoder", "codebuddy"]
    with pytest.raises(ValueError):
        installer.resolve_agents(["auto", "qoder"], detected=[])


@pytest.mark.parametrize(
    ("agent", "scope", "parts"),
    [
        ("codebuddy", "user", (".codebuddy", "skills")),
        ("codebuddy", "project", (".codebuddy", "skills")),
        ("qoder", "user", (".qoder", "skills")),
        ("qoder", "project", (".qoder", "skills")),
        ("trae", "user", (".cosmic_kb", "trae-import", __version__)),
    ],
)
def test_target_roots(tmp_path, agent, scope, parts):
    home = tmp_path / "home"
    project = tmp_path / "project"
    root = installer.target_root(agent, scope=scope, home=home, project=project)
    base = home if scope == "user" or agent == "trae" else project
    assert root == base.joinpath(*parts)


def test_install_both_skills_and_overwrite(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    stale = home / ".codebuddy" / "skills" / SKILL_NAMES[0] / "SKILL.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale", encoding="utf-8")

    payload, rc = installer.install(["codebuddy", "qoder"], home=home, project=project)

    assert rc == 0
    assert payload["summary"]["skill_files"] == 4
    for agent in ("codebuddy", "qoder"):
        for name in SKILL_NAMES:
            target = home / f".{agent}" / "skills" / name / "SKILL.md"
            assert target.read_bytes() == read_skill(name)


def test_install_replaces_linked_skill_directory_without_touching_source(tmp_path):
    home = tmp_path / "home"
    source = tmp_path / "source-skill"
    source.mkdir()
    source_file = source / "SKILL.md"
    source_file.write_text("source must survive", encoding="utf-8")
    linked = home / ".qoder" / "skills" / SKILL_NAMES[0]
    linked.parent.mkdir(parents=True)
    _make_directory_link(linked, source)

    payload, rc = installer.install(["qoder"], home=home, project=tmp_path)

    assert rc == 0
    assert payload["summary"]["failures"] == 0
    assert source_file.read_text(encoding="utf-8") == "source must survive"
    assert not installer._is_directory_link(linked)
    assert (linked / "SKILL.md").read_bytes() == read_skill(SKILL_NAMES[0])


def test_project_scope_and_dry_run_do_not_write(tmp_path):
    project = tmp_path / "project"
    payload, rc = installer.install(
        ["codebuddy"], scope="project", home=tmp_path / "home",
        project=project, dry_run=True,
    )
    assert rc == 0
    assert payload["agents"][0]["skills"][0]["status"] == "would_install"
    assert not (project / ".codebuddy").exists()


def test_trae_stages_bundle_and_requires_manual_action(tmp_path):
    payload, rc = installer.install(["trae"], home=tmp_path / "home", project=tmp_path)
    result = payload["agents"][0]
    assert rc == 0
    assert result["status"] == "manual_action_required"
    assert len(result["manual_steps"]) == 3
    assert all(Path(item["path"]).is_file() for item in result["skills"])


def test_partial_write_failure_returns_one(tmp_path, monkeypatch):
    real_write = installer._atomic_write
    calls = 0

    def fail_once(path, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("denied")
        real_write(path, data)

    monkeypatch.setattr(installer, "_atomic_write", fail_once)
    payload, rc = installer.install(["qoder"], home=tmp_path / "home", project=tmp_path)
    assert rc == 1
    assert payload["summary"]["failures"] == 1
    assert {item["status"] for item in payload["agents"][0]["skills"]} == {"failed", "installed"}


def test_status_reports_missing_outdated_and_installed(tmp_path):
    home = tmp_path / "home"
    root = home / ".qoder" / "skills"
    old = root / SKILL_NAMES[0] / "SKILL.md"
    old.parent.mkdir(parents=True)
    old.write_text("old", encoding="utf-8")

    payload, _ = installer.status(["qoder"], home=home, project=tmp_path)
    assert [item["status"] for item in payload["agents"][0]["skills"]] == ["outdated", "missing"]

    installer.install(["qoder"], home=home, project=tmp_path)
    payload, _ = installer.status(["qoder"], home=home, project=tmp_path)
    assert {item["status"] for item in payload["agents"][0]["skills"]} == {"installed"}


def test_uninstall_removes_managed_files_and_preserves_unrelated_files(tmp_path):
    home = tmp_path / "home"
    installer.install(["codebuddy"], home=home, project=tmp_path)
    skill_dir = home / ".codebuddy" / "skills" / SKILL_NAMES[0]
    extra = skill_dir / "notes.txt"
    extra.write_text("keep", encoding="utf-8")

    payload, rc = installer.uninstall(["codebuddy"], home=home, project=tmp_path)

    assert rc == 0
    assert payload["summary"] == {
        "requested_agents": 1, "removed": 2, "missing": 0, "failures": 0,
    }
    assert extra.read_text(encoding="utf-8") == "keep"
    assert not (skill_dir / "SKILL.md").exists()
    assert not (home / ".codebuddy" / "skills" / SKILL_NAMES[1]).exists()


def test_uninstall_removes_linked_skill_directory_without_touching_source(tmp_path):
    home = tmp_path / "home"
    source = tmp_path / "source-skill"
    source.mkdir()
    source_file = source / "SKILL.md"
    source_file.write_text("source must survive", encoding="utf-8")
    linked = home / ".qoder" / "skills" / SKILL_NAMES[0]
    linked.parent.mkdir(parents=True)
    _make_directory_link(linked, source)

    payload, rc = installer.uninstall(["qoder"], home=home, project=tmp_path)

    assert rc == 0
    assert payload["summary"] == {
        "requested_agents": 1, "removed": 1, "missing": 1, "failures": 0,
    }
    assert source_file.read_text(encoding="utf-8") == "source must survive"
    assert not linked.exists()


def test_uninstall_is_idempotent_and_dry_run_does_not_delete(tmp_path):
    home = tmp_path / "home"
    installer.install(["qoder"], home=home, project=tmp_path)
    target = home / ".qoder" / "skills" / SKILL_NAMES[0] / "SKILL.md"

    payload, rc = installer.uninstall(
        ["qoder"], home=home, project=tmp_path, dry_run=True
    )
    assert rc == 0
    assert target.is_file()
    assert {item["status"] for item in payload["agents"][0]["skills"]} == {"would_remove"}

    installer.uninstall(["qoder"], home=home, project=tmp_path)
    payload, rc = installer.uninstall(["qoder"], home=home, project=tmp_path)
    assert rc == 0
    assert payload["agents"][0]["status"] == "missing"
    assert payload["summary"]["missing"] == 2


def test_trae_uninstall_removes_stage_and_requires_ui_cleanup(tmp_path):
    home = tmp_path / "home"
    installer.install(["trae"], home=home, project=tmp_path)
    payload, rc = installer.uninstall(["trae"], home=home, project=tmp_path)
    result = payload["agents"][0]
    assert rc == 0
    assert result["status"] == "manual_action_required"
    assert len(result["manual_steps"]) == 3
    assert all(item["status"] == "removed" for item in result["skills"])


def test_uninstall_partial_failure_returns_one(tmp_path, monkeypatch):
    home = tmp_path / "home"
    installer.install(["codebuddy"], home=home, project=tmp_path)
    real_remove = installer._remove_managed_skill
    calls = 0

    def fail_once(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("denied")
        real_remove(path)

    monkeypatch.setattr(installer, "_remove_managed_skill", fail_once)
    payload, rc = installer.uninstall(["codebuddy"], home=home, project=tmp_path)
    assert rc == 1
    assert payload["summary"]["failures"] == 1
    assert {item["status"] for item in payload["agents"][0]["skills"]} == {"failed", "removed"}


def test_cli_json_is_clean_and_auto_no_match_is_exit_two(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(installer, "detect_agents", lambda **_kwargs: [])
    assert cli_main.main(["skill", "install", "--json"]) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["error"] == "no_agent_detected"

    rc = cli_main.main([
        "skill", "install", "--agent", "qoder", "--scope", "project",
        "--project", str(tmp_path), "--dry-run", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["agents"][0]["agent"] == "qoder"
    assert payload["dry_run"] is True


def test_cli_uninstall_json_round_trip(tmp_path, capsys):
    assert cli_main.main([
        "skill", "install", "--agent", "qoder", "--scope", "project",
        "--project", str(tmp_path), "--json",
    ]) == 0
    capsys.readouterr()

    assert cli_main.main([
        "skill", "uninstall", "--agent", "qoder", "--scope", "project",
        "--project", str(tmp_path), "--json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "uninstall"
    assert payload["summary"]["removed"] == 2

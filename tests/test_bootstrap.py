"""对话式安装 Bootstrap（D2 MCP 注册/校验 + D3 编排器）验收。

覆盖计划 §9 验收口径里本仓可单测的部分：安装清单不含口令、plan 只读无副作用、apply 幂等 +
断点续跑、五工具 tools/list 校验、同名冲突先备份再替换、路径转义（绝对路径写入）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from cosmic_kb import __version__
from cosmic_kb.bootstrap import mcp_register, orchestrator


# ── D2 · server_spec / register ──────────────────────────────────────────────
def test_server_spec_uses_abs_python_and_db(tmp_path):
    spec = mcp_register.server_spec(tmp_path / "kb" / "cosmic_kb.db")
    assert spec["command"] == sys.executable
    assert spec["args"][:3] == ["-m", "cosmic_kb.cli.main", "mcp"]
    kb = spec["args"][spec["args"].index("--db") + 1]
    assert Path(kb).is_absolute()
    assert spec["env"]["COSMIC_KB_DB"] == kb


def test_register_writes_shared_mcp_json_and_dedupes(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    payload, rc = mcp_register.register(project, project / "cosmic_kb.db", ["codebuddy", "qoder"], home=tmp_path)
    assert rc == 0
    # codebuddy + qoder 合并成一次共享文件写
    assert len(payload["targets"]) == 1
    assert payload["targets"][0]["status"] == "registered"
    data = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["cosmic_kb"] == payload["spec"]


def test_register_idempotent_unchanged(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    mcp_register.register(project, project / "cosmic_kb.db", ["codebuddy"], home=tmp_path)
    before = (project / ".mcp.json").read_bytes()
    payload, rc = mcp_register.register(project, project / "cosmic_kb.db", ["codebuddy"], home=tmp_path)
    assert rc == 0
    assert payload["targets"][0]["status"] == "unchanged"
    assert (project / ".mcp.json").read_bytes() == before  # 字节级不动


def test_register_conflict_stops_without_force(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"cosmic_kb": {"command": "old"}}}), encoding="utf-8"
    )
    before = (project / ".mcp.json").read_bytes()
    payload, rc = mcp_register.register(project, project / "cosmic_kb.db", ["qoder"], home=tmp_path)
    assert rc == 1
    assert payload["targets"][0]["status"] == "conflict"
    assert (project / ".mcp.json").read_bytes() == before  # 冲突时绝不改文件


def test_register_force_backs_up_then_replaces(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"cosmic_kb": {"command": "old"}, "other": {"command": "keep"}}}),
        encoding="utf-8",
    )
    payload, rc = mcp_register.register(
        project, project / "cosmic_kb.db", ["qoder"], home=tmp_path, force=True
    )
    assert rc == 0
    target = payload["targets"][0]
    assert target["status"] == "replaced"
    backup = Path(target["backup"])
    assert backup.exists() and "old" in backup.read_text(encoding="utf-8")  # 旧配置进备份
    data = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["cosmic_kb"] == payload["spec"]  # 已替换成新配置
    assert data["mcpServers"]["other"] == {"command": "keep"}   # 别人的 server 不动


def test_register_rejects_corrupt_json(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".mcp.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(mcp_register.McpRegisterError):
        mcp_register.register(project, project / "cosmic_kb.db", ["codebuddy"], home=tmp_path)


def test_register_trae_generates_import_bundle(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    payload, rc = mcp_register.register(project, project / "cosmic_kb.db", ["trae"], home=tmp_path)
    assert rc == 0
    target = payload["targets"][0]
    assert target["status"] == "manual_action_required"
    bundle = Path(target["path"])
    assert bundle.exists() and __version__ in str(bundle)
    assert target["manual_steps"]  # 回传设置页导入步骤


# ── D2 · verify_mcp（注入假 probe，不真起子进程）─────────────────────────────
def test_verify_mcp_ok(tmp_path):
    result = mcp_register.verify_mcp(
        tmp_path / "cosmic_kb.db", probe=lambda spec, timeout: list(mcp_register.REQUIRED_TOOLS) + ["extra"]
    )
    assert result["ok"] is True
    assert result["missing"] == []


def test_verify_mcp_missing_tool(tmp_path):
    partial = list(mcp_register.REQUIRED_TOOLS)[:-1]
    result = mcp_register.verify_mcp(tmp_path / "cosmic_kb.db", probe=lambda spec, timeout: partial)
    assert result["ok"] is False
    assert mcp_register.REQUIRED_TOOLS[-1] in result["missing"]


def test_verify_mcp_probe_failure_is_not_ok(tmp_path):
    def _boom(spec, timeout):
        raise RuntimeError("server 未起来")

    result = mcp_register.verify_mcp(tmp_path / "cosmic_kb.db", probe=_boom)
    assert result["ok"] is False
    assert "server 未起来" in result["error"]


# ── D3 · install.json 清单 ───────────────────────────────────────────────────
def test_install_manifest_has_fields_and_no_secret(tmp_path):
    res = orchestrator._step_install_manifest(tmp_path, source="pypi", dry_run=False)
    assert res["status"] == "written"
    manifest = json.loads(Path(res["path"]).read_text(encoding="utf-8"))
    assert manifest["version"] == __version__
    assert manifest["python"] == sys.executable
    assert manifest["source"] == "pypi"
    assert "password" not in json.dumps(manifest).lower()


def test_read_install_manifest_missing_returns_none(tmp_path):
    assert orchestrator.read_install_manifest(tmp_path) is None


# ── D3 · plan 只读 ───────────────────────────────────────────────────────────
def test_plan_is_read_only(tmp_path):
    project = tmp_path / "proj"
    (project / "mod").mkdir(parents=True)
    (project / "mod" / "A.java").write_text("class A{}", encoding="utf-8")
    payload = orchestrator.plan(project, agents=["all"], home=tmp_path)
    # 无元数据来源 → 提问；且绝不落任何文件
    assert any(q["id"] == "metadata_source" for q in payload["questions"])
    assert not (project / ".mcp.json").exists()
    assert orchestrator.read_install_manifest(tmp_path) is None
    assert payload["planned_actions"]


# ── D3 · apply 编排（monkeypatch 重型步骤）───────────────────────────────────
@pytest.fixture
def _stub_steps(monkeypatch):
    """把有副作用/重型的步骤替换成记录调用的桩，专测编排顺序与幂等。"""
    calls: list[str] = []

    def _mk(name, status="done", extra=None):
        def _fn(*args, **kwargs):
            calls.append(name)
            out = {"step": name, "status": status}
            if extra:
                out.update(extra)
            return out
        return _fn

    monkeypatch.setattr(orchestrator, "_step_install_manifest", _mk("install_manifest", "written"))
    monkeypatch.setattr(orchestrator, "_step_skills", _mk("skills"))
    monkeypatch.setattr(orchestrator, "_step_build_kb", _mk("build_kb"))
    monkeypatch.setattr(orchestrator, "_step_doctor", _mk("doctor"))
    monkeypatch.setattr(orchestrator, "_step_register_mcp", _mk("register_mcp"))
    monkeypatch.setattr(orchestrator, "_step_verify_mcp", _mk("verify_mcp"))
    return calls


def test_apply_runs_steps_in_fixed_order(tmp_path, _stub_steps):
    payload, rc = orchestrator.apply(
        tmp_path / "proj", source_root=str(tmp_path / "proj"), agents=["all"], home=tmp_path
    )
    assert rc == 0
    assert _stub_steps == list(orchestrator.STEP_ORDER)
    assert payload["summary"]["ok"] is True


def test_build_step_forwards_symbol_options(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr("cosmic_kb.graph.store.kb_exists", lambda _p: False)

    def fake_build(ns, db):
        captured.update({"classpath_dir": ns.classpath_dir, "no_symbols": ns.no_symbols,
                         "db": db})
        return {"form": 1}, 0

    monkeypatch.setattr("cosmic_kb.cli.main._build_kb", fake_build)
    kb = tmp_path / "cosmic_kb.db"
    result = orchestrator._step_build_kb(
        tmp_path, kb,
        {"source_root": str(tmp_path), "classpath_dirs": ["a", "b"],
         "no_symbols": True},
        rebuild=True, dry_run=False,
    )
    assert result["status"] == "done"
    assert captured == {"classpath_dir": ["a", "b"], "no_symbols": True,
                        "db": str(kb)}


def test_apply_skips_verify_when_build_failed(tmp_path, monkeypatch, _stub_steps):
    monkeypatch.setattr(
        orchestrator, "_step_build_kb",
        lambda *a, **k: {"step": "build_kb", "status": "failed", "rc": 2},
    )
    payload, rc = orchestrator.apply(
        tmp_path / "proj", source_root=str(tmp_path / "proj"), agents=["all"], home=tmp_path
    )
    assert rc == 1
    verify = next(s for s in payload["steps"] if s["step"] == "verify_mcp")
    assert verify["status"] == "skipped"


def test_apply_prompt_db_password_not_persisted(tmp_path, monkeypatch):
    # 只桩掉 manifest 之外的重型步骤，保留真实清单写入以验证口令不落盘。
    for name in ("_step_skills", "_step_build_kb", "_step_doctor",
                 "_step_register_mcp", "_step_verify_mcp"):
        monkeypatch.setattr(orchestrator, name,
                            lambda *a, _n=name, **k: {"step": _n.replace("_step_", ""), "status": "done"})
    secret = "S3cr3t-should-never-persist"
    try:
        payload, rc = orchestrator.apply(
            tmp_path / "proj", source_root=str(tmp_path / "proj"),
            db_config=str(tmp_path / "cosmic_db.json"), prompt_db_password=True,
            agents=["all"], home=tmp_path, db_password_reader=lambda prompt: secret,
        )
        assert secret not in json.dumps(payload)  # 不进返回体
        manifest = Path(orchestrator.install_manifest_path(tmp_path))
        assert secret not in manifest.read_text(encoding="utf-8")  # 不进清单
        assert os.environ.get(orchestrator._DB_PASSWORD_ENV) == secret  # 只进环境变量
    finally:
        os.environ.pop(orchestrator._DB_PASSWORD_ENV, None)


def test_apply_prompt_db_password_requires_db_config(tmp_path, _stub_steps):
    payload, rc = orchestrator.apply(
        tmp_path / "proj", source_root=str(tmp_path / "proj"),
        prompt_db_password=True, agents=["all"], home=tmp_path,
        db_password_reader=lambda prompt: "x",
    )
    assert rc == 2
    assert payload["error"] == "db_config_required"


def test_build_kb_step_resumes_when_kb_exists(tmp_path, monkeypatch):
    monkeypatch.setattr("cosmic_kb.graph.store.kb_exists", lambda p: True)
    res = orchestrator._step_build_kb(
        tmp_path, tmp_path / "cosmic_kb.db",
        {"source_root": str(tmp_path)}, rebuild=False, dry_run=False,
    )
    assert res["status"] == "skipped_exists"  # 断点续跑：已建好则跳过，不重建

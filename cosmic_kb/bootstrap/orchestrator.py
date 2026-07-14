"""D3 · Bootstrap 编排器（`docs/设计方案/分发与多agent接入方案.md` §3）。

对外三段：

- :func:`plan`：**只读探测**——环境 / 候选（源码根、dym/cr/zip、db 配置）/ 已有产物（KB、Skill、
  MCP）/ 同名冲突，产出 `questions` / `planned_actions` / `manual_actions`。无任何副作用。
- :func:`apply`：**确定顺序、每步幂等、可断点续跑**——写 `install.json` → 装两份 Skill → 建 KB →
  `doctor`(+可选 coverage) → 注册 MCP → 四工具校验 → 输出重连步骤。
- :func:`status`：读 `install.json` + 各步产物，报告已完成 / 待做。

两条红线：`install.json` 及一切返回体 **绝不写数据库口令**（红线 #1）；数据库口令走单进程
`getpass` 隐藏输入（§3.5），用完只进 `os.environ`，不落命令行 / 配置 / JSON / 日志 / 清单。

各步做成模块级 `_step_*` 函数，测试可 monkeypatch 以验证编排逻辑（顺序 / 幂等 / 续跑 / 口令不落盘）
而不触发重型副作用。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .. import __version__
from ..skills.installer import _atomic_write, detect_agents, resolve_agents
from . import mcp_register

DEFAULT_DB = "cosmic_kb.db"  # 与 cli.main.DEFAULT_DB 一致（KB 随源码根落盘）
_DB_PASSWORD_ENV = "COSMIC_DB_PASSWORD"

# apply 步骤的固定顺序（status/续跑按此判断"下一步该做什么"）。
STEP_ORDER = (
    "install_manifest",
    "skills",
    "build_kb",
    "doctor",
    "register_mcp",
    "verify_mcp",
)


# ── install.json 清单（§3.3）─────────────────────────────────────────────────
def install_manifest_path(home: str | os.PathLike[str] | None = None) -> Path:
    home_path = (Path(home) if home else Path.home()).expanduser()
    return home_path / ".cosmic_kb" / "install.json"


def read_install_manifest(home: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
    """读安装清单；不存在或损坏返回 None（setup skill 据此判断"是否需要用口令启动 Bootstrap"）。"""
    path = install_manifest_path(home)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None


def _cli_invocation() -> list[str]:
    """setup skill 取 CLI 的调用形式：优先 PATH 上的 `cosmic_kb`，否则退回 `python -m ...`。"""
    import shutil

    found = shutil.which("cosmic_kb")
    if found:
        return [found]
    return [sys.executable, "-m", "cosmic_kb.cli.main"]


# ── 各步实现（可被测试 monkeypatch）──────────────────────────────────────────
def _step_install_manifest(home, *, source: str, dry_run: bool) -> dict[str, Any]:
    """写不含敏感信息的安装清单：runtime Python 绝对路径、CLI 调用形式、版本、来源。"""
    path = install_manifest_path(home)
    manifest = {
        "version": __version__,
        "python": sys.executable,
        "cli": _cli_invocation(),
        "source": source,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if dry_run:
        return {"step": "install_manifest", "status": "would_write", "path": str(path)}
    existed = path.exists()
    _atomic_write(path, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return {
        "step": "install_manifest",
        "status": "updated" if existed else "written",
        "path": str(path),
        "manifest": manifest,
    }


def _step_skills(project, agents, *, home, dry_run: bool) -> dict[str, Any]:
    from ..skills import installer

    if not agents:  # 没命中任何宿主：不是失败，只是无处可装
        return {"step": "skills", "status": "no_agent", "agents": []}
    payload, rc = installer.install(agents, scope="user", project=Path(project), home=home, dry_run=dry_run)
    return {"step": "skills", "status": "failed" if rc else "done", "detail": payload}


def _step_build_kb(project, kb_path, opts: dict[str, Any], *, rebuild: bool, dry_run: bool) -> dict[str, Any]:
    from ..graph import store

    if store.kb_exists(str(kb_path)) and not rebuild:  # 续跑：已建好就跳过（除非显式 rebuild）
        return {"step": "build_kb", "status": "skipped_exists", "kb": str(kb_path)}
    if dry_run:
        return {"step": "build_kb", "status": "would_build", "kb": str(kb_path)}

    from ..cli import main as cli_main

    ns = argparse.Namespace(
        source_root=str(opts["source_root"]),
        meta=list(opts.get("meta") or []),
        follow_symlinks=bool(opts.get("follow_symlinks", False)),
        template_dir=opts.get("template_dir"),
        db_config=opts.get("db_config"),
        isv=opts.get("isv"),
        vendor=opts.get("vendor"),
        creating=True,
        db=str(kb_path),
    )
    counts, rc = cli_main._build_kb(ns, str(kb_path))
    if rc:
        return {"step": "build_kb", "status": "failed", "rc": rc, "kb": str(kb_path)}
    return {"step": "build_kb", "status": "done", "kb": str(kb_path), "counts": counts}


def _step_doctor(*, run_coverage: bool, kb_path=None) -> dict[str, Any]:
    from .. import _assets

    statuses = _assets.check_assets()
    missing = [s.name for s in statuses if not s.present]
    result: dict[str, Any] = {
        "step": "doctor",
        "status": "failed" if missing else "done",
        "missing_assets": missing,
    }
    if run_coverage and not missing and kb_path is not None:
        try:
            from ..graph import store
            from ..report import coverage as coverage_mod

            if store.kb_exists(str(kb_path)):
                conn = store.open_kb(str(kb_path))
                try:
                    result["coverage"] = coverage_mod.coverage(conn)
                finally:
                    conn.close()
        except Exception as exc:  # noqa: BLE001 —— coverage 是可选增强，失败不阻断
            result["coverage_error"] = f"{type(exc).__name__}: {exc}"
    return result


def _step_register_mcp(project, kb_path, agents, *, home, force: bool, dry_run: bool) -> dict[str, Any]:
    if not agents:
        return {"step": "register_mcp", "status": "no_agent", "targets": []}
    payload, rc = mcp_register.register(
        project, kb_path, agents, force=force, dry_run=dry_run, home=home
    )
    status = "conflict" if rc else "done"
    return {"step": "register_mcp", "status": status, "detail": payload}


def _step_verify_mcp(kb_path, *, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return {"step": "verify_mcp", "status": "skipped_dry_run"}
    result = mcp_register.verify_mcp(kb_path)
    return {"step": "verify_mcp", "status": "done" if result["ok"] else "failed", "detail": result}


# ── 路径/候选探测助手 ────────────────────────────────────────────────────────
def _resolve_kb(project: Path, source_root: str | None, db: str | None) -> Path:
    if db:
        return Path(db).expanduser().resolve()
    if source_root:
        return (Path(source_root).expanduser().resolve() / DEFAULT_DB)
    from ..cli.main import _discover_db

    found = _discover_db(project)
    return found.resolve() if found else (project / DEFAULT_DB)


def _find_source_roots(project: Path, limit: int = 8) -> list[str]:
    """只读探测 Java 源码根候选（build 递归扫描，通常项目根即可；再列出像模块的子目录）。"""
    roots: list[str] = []
    if not project.is_dir():
        return roots
    has_java = (project / "src").is_dir() or next(project.glob("*.java"), None) is not None
    if has_java:
        roots.append(str(project))
    try:
        for child in sorted(project.iterdir()):
            if not child.is_dir():
                continue
            if (child / "pom.xml").exists() or (child / "build.gradle").exists() or (child / "src").is_dir():
                if str(child) not in roots:
                    roots.append(str(child))
            if len(roots) >= limit:
                break
    except OSError:
        pass
    return roots or [str(project)]


def _find_metadata_files(project: Path, limit: int = 50) -> list[str]:
    """浅扫（项目根 + 一层子目录）里的 dym/cr/zip 元数据候选。"""
    found: list[str] = []
    if not project.is_dir():
        return found
    patterns = ("*.dym", "*.cr", "*.zip")
    for pattern in patterns:
        for path in project.glob(pattern):
            found.append(str(path))
    for child in (p for p in project.iterdir() if p.is_dir()):
        for pattern in patterns:
            for path in child.glob(pattern):
                found.append(str(path))
                if len(found) >= limit:
                    return found
    return found


def _find_db_config(project: Path) -> str | None:
    from ..dbmeta.config import DEFAULT_CONFIG_NAMES

    for name in DEFAULT_CONFIG_NAMES:
        cand = project / name
        if cand.exists():
            return str(cand)
    return None


def _mcp_state(project: Path) -> dict[str, Any]:
    """读项目根共享 .mcp.json，看 cosmic_kb 是否已注册 / 是否会与新配置冲突。"""
    path = project / ".mcp.json"
    state: dict[str, Any] = {"shared_path": str(path), "registered": False}
    if not path.exists():
        return state
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        entry = servers.get(mcp_register.SERVER_NAME) if isinstance(servers, dict) else None
        state["registered"] = entry is not None
        if entry is not None:
            state["current"] = entry
    except (ValueError, OSError) as exc:
        state["error"] = f"{type(exc).__name__}: {exc}"
    return state


# ── plan ─────────────────────────────────────────────────────────────────────
def plan(
    project: str | os.PathLike[str],
    *,
    source_root: str | None = None,
    meta: Sequence[str] | None = None,
    db_config: str | None = None,
    agents: Iterable[str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """只读探测，产出待办与待确认问题。不写任何文件。"""
    project = Path(project).expanduser().resolve()
    home_path = (Path(home) if home else Path.home()).expanduser().resolve()

    detected = detect_agents(project=project)
    selected = resolve_agents(list(agents) if agents else ["auto"], detected=detected)

    kb_path = _resolve_kb(project, source_root, None)
    from ..graph import store

    src_candidates = [source_root] if source_root else _find_source_roots(project)
    meta_files = list(meta) if meta else _find_metadata_files(project)
    db_cfg = db_config or _find_db_config(project)
    mcp = _mcp_state(project)
    manifest = read_install_manifest(home_path)

    questions: list[dict[str, Any]] = []
    manual_actions: list[str] = []

    # 元数据来源：直连库 vs dym/cr/zip，二选一（都没探到就必须问）。
    if not db_cfg and not meta_files:
        questions.append({
            "id": "metadata_source",
            "ask": "元数据从哪来？①推荐直连底层库只读账号（给 --db-config，或先 db-meta --init-config 生成模板）"
                   "；②离线用导出的 dym/cr/zip 文件夹（给 --meta）。",
            "options": ["database", "files"],
        })
    if not source_root and len(src_candidates) != 1:
        questions.append({
            "id": "source_root",
            "ask": "有多个可能的 Java 源码根，请指定 --source-root。",
            "candidates": src_candidates,
        })
    if not detected:
        manual_actions.append(
            "未检测到 CodeBuddy/Qoder/TRAE；如需装 Skill/注册 MCP，apply 时显式 --agent all 或指定宿主。"
        )
    if mcp.get("registered"):
        manual_actions.append(
            "项目根 .mcp.json 已有 cosmic_kb 条目：apply 若配置不同会停在冲突，确认后加 --force-mcp 先备份再替换。"
        )
    if db_cfg:
        manual_actions.append(
            "走数据库路径：apply 加 --prompt-db-password，在终端隐藏输入口令（口令不写入任何文件）。"
        )
    manual_actions.append("apply 完成后必须重启 / 重连 Agent，MCP 常驻进程改配置后才生效。")

    planned_actions = [
        f"写安装清单 {install_manifest_path(home_path)}",
        f"安装 Skills 到宿主: {', '.join(selected) or '（无，需 --agent）'}",
        ("建 KB（直连库）" if db_cfg else "建 KB（dym/cr/zip）") + f" → {kb_path}"
        + ("（已存在，续跑会跳过）" if store.kb_exists(str(kb_path)) else ""),
        "跑 doctor 自检资产",
        f"注册 MCP server cosmic_kb 到 {mcp['shared_path']}（+ TRAE 导入包）",
        f"子进程校验四工具可用: {', '.join(mcp_register.REQUIRED_TOOLS)}",
    ]

    return {
        "command": "plan",
        "project": str(project),
        "environment": {
            "python": sys.executable,
            "python_version": sys.version.split()[0],
            "package_version": __version__,
            "detected_agents": detected,
            "selected_agents": selected,
        },
        "kb": {"path": str(kb_path), "exists": store.kb_exists(str(kb_path))},
        "manifest": {"path": str(install_manifest_path(home_path)), "exists": manifest is not None},
        "candidates": {
            "source_roots": src_candidates,
            "metadata_files": meta_files,
            "db_config": db_cfg,
        },
        "mcp": mcp,
        "questions": questions,
        "planned_actions": planned_actions,
        "manual_actions": manual_actions,
    }


# ── apply ────────────────────────────────────────────────────────────────────
def apply(
    project: str | os.PathLike[str],
    *,
    source_root: str,
    meta: Sequence[str] | None = None,
    db_config: str | None = None,
    isv: str | None = None,
    vendor: Sequence[str] | None = None,
    template_dir: str | None = None,
    follow_symlinks: bool = False,
    metadata: str = "auto",
    agents: Iterable[str] | None = None,
    home: str | os.PathLike[str] | None = None,
    force_mcp: bool = False,
    prompt_db_password: bool = False,
    run_coverage: bool = False,
    rebuild: bool = False,
    dry_run: bool = False,
    source: str = "source",
    db_password_reader: Callable[[str], str] = getpass.getpass,
) -> tuple[dict[str, Any], int]:
    """按 §3.2 顺序执行、每步幂等。返回 `(payload, rc)`；任一步 failed/conflict → rc=1。

    数据库口令（`prompt_db_password=True`）在本进程用 `db_password_reader` 取、只塞进 `os.environ`，
    绝不进入返回体 / 清单 / 日志。
    """
    project = Path(project).expanduser().resolve()
    home_path = (Path(home) if home else Path.home()).expanduser().resolve()
    kb_path = _resolve_kb(project, source_root, None)

    detected = detect_agents(project=project)
    selected = resolve_agents(list(agents) if agents else ["auto"], detected=detected)

    # 数据库口令：单进程隐藏输入，用完只进 env（不落盘、不进 payload）。
    if prompt_db_password:
        if not db_config:
            return (
                {"command": "apply", "error": "db_config_required",
                 "message": "--prompt-db-password 需配合 --db-config"},
                2,
            )
        secret = db_password_reader("苍穹底层库只读口令（隐藏输入，不会写入任何文件）: ")
        if secret:
            os.environ[_DB_PASSWORD_ENV] = secret
        del secret  # 尽快脱离局部变量引用

    build_opts = {
        "source_root": source_root,
        "meta": list(meta or []),
        "db_config": db_config,
        "isv": isv,
        "vendor": list(vendor) if vendor else None,
        "template_dir": template_dir,
        "follow_symlinks": follow_symlinks,
    }

    steps: list[dict[str, Any]] = []
    steps.append(_step_install_manifest(home_path, source=source, dry_run=dry_run))
    steps.append(_step_skills(project, selected, home=home_path, dry_run=dry_run))
    steps.append(_step_build_kb(project, kb_path, build_opts, rebuild=rebuild, dry_run=dry_run))

    build_ok = steps[-1]["status"] in ("done", "skipped_exists", "would_build")
    steps.append(_step_doctor(run_coverage=run_coverage, kb_path=kb_path))
    steps.append(_step_register_mcp(project, kb_path, selected, home=home_path, force=force_mcp, dry_run=dry_run))
    # 建库失败时四工具校验必然失败（KB 缺）；跳过以免噪声，标 skipped。
    if build_ok:
        steps.append(_step_verify_mcp(kb_path, dry_run=dry_run))
    else:
        steps.append({"step": "verify_mcp", "status": "skipped", "reason": "build_kb 未成功"})

    bad = {"failed", "conflict"}
    rc = 1 if any(s["status"] in bad for s in steps) else 0

    payload = {
        "command": "apply",
        "project": str(project),
        "kb": str(kb_path),
        "metadata_mode": "database" if db_config else "files",
        "selected_agents": selected,
        "dry_run": dry_run,
        "steps": steps,
        "reconnect": [
            "重启 / 重连你的 Agent（MCP 常驻进程，改配置后必须重连才生效）。",
            "重连后直接问「某字段是谁改的」验证 cosmic_kb 工具是否已可用。",
        ],
        "summary": {
            "ok": rc == 0,
            "failed_steps": [s["step"] for s in steps if s["status"] in bad],
        },
    }
    return payload, rc


# ── status ───────────────────────────────────────────────────────────────────
def status(
    project: str | os.PathLike[str],
    *,
    source_root: str | None = None,
    db: str | None = None,
    agents: Iterable[str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """读 install.json + 各步产物，报告已完成/待做，指出续跑该从哪步起。"""
    project = Path(project).expanduser().resolve()
    home_path = (Path(home) if home else Path.home()).expanduser().resolve()
    kb_path = _resolve_kb(project, source_root, db)

    from ..graph import store
    from ..skills import installer

    manifest = read_install_manifest(home_path)
    detected = detect_agents(project=project)
    selected = resolve_agents(list(agents) if agents else ["auto"], detected=detected)
    skills_state = None
    if selected:
        skills_payload, _ = installer.status(selected, scope="user", project=project, home=home_path)
        skills_state = {a["agent"]: a["status"] for a in skills_payload["agents"]}
    mcp = _mcp_state(project)

    done = {
        "install_manifest": manifest is not None,
        "skills": bool(skills_state) and all(v in ("installed", "manual_action_required") for v in skills_state.values()),
        "build_kb": store.kb_exists(str(kb_path)),
        "register_mcp": mcp.get("registered", False),
    }
    pending = [step for step in STEP_ORDER if step in done and not done[step]]
    next_step = pending[0] if pending else None

    return {
        "command": "status",
        "project": str(project),
        "kb": {"path": str(kb_path), "exists": done["build_kb"]},
        "manifest": {"path": str(install_manifest_path(home_path)), "exists": manifest is not None,
                     "version": (manifest or {}).get("version")},
        "skills": skills_state,
        "mcp": mcp,
        "completed": done,
        "next_step": next_step,
    }


__all__ = [
    "STEP_ORDER",
    "install_manifest_path",
    "read_install_manifest",
    "plan",
    "apply",
    "status",
]

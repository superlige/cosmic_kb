"""D2 · MCP 自动注册 + 四工具校验（`docs/设计方案/分发与多agent接入方案.md` §3.6 + §3.2 步骤 5–6）。

两件事：

1. **写配置**（`register`）：把 `cosmic_kb` server 幂等写进各宿主认的 MCP 配置。CodeBuddy / Qoder
   共享项目根 `.mcp.json`（与 Claude Code / Codex 同格式）；TRAE 生成导入包并回传设置页步骤。
   启动命令用**托管运行时 Python 绝对路径** `python -m cosmic_kb.cli.main mcp --db <KB绝对路径>`
   （不依赖 `cosmic_kb-mcp` 是否在 PATH），并显式传当前项目 KB 绝对路径。

2. **校验**（`verify_mcp`）：**仅写配置不算成功**。以子进程按写入的启动命令独立起服务器，完成
   `initialize` + `tools/list`，断言 `trace`/`bill`/`resolve_fields`/`cosmic_semantics` 四工具在列。

冲突保护：同名 `cosmic_kb` 但配置不同默认停止（`status="conflict"`）；`force=True` 时先备份
（`.mcp.json.bak-<时间戳>`）再替换。写配置走 installer 的原子写，避免半写坏宿主。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .. import __version__
from ..skills.installer import _atomic_write
from ..mcp.server import TOOLS as _MCP_TOOLS

# 固定 server 名（宿主里统一叫这个；同名冲突保护也认它）。
SERVER_NAME = "cosmic_kb"
# 段二四个核心取证工具——校验以「这四个都在 tools/list 里」为通过标准。从 server 真源派生，
# 避免这里与实际注册的工具集漂移。
REQUIRED_TOOLS: tuple[str, ...] = tuple(_MCP_TOOLS.keys())

# 认共享项目根 `.mcp.json` 的宿主（CodeBuddy / Qoder；Claude Code / Codex CLI 也认同一文件）。
_SHARED_AGENTS = ("codebuddy", "qoder")
_MCP_WRAP_KEY = "mcpServers"


class McpRegisterError(RuntimeError):
    """现有配置无法解析、或注册过程中的可预期错误。"""


# ── 启动命令 ────────────────────────────────────────────────────────────────
def server_spec(kb_path: str | os.PathLike[str], *, python: str | None = None) -> dict[str, Any]:
    """构造写进宿主配置的 `cosmic_kb` server 条目。

    命令用运行时 Python 绝对路径 + `-m cosmic_kb.cli.main mcp --db <KB绝对路径>`，KB 路径同时
    落进 `env.COSMIC_KB_DB` 双保险。返回结构即宿主 `mcpServers.cosmic_kb` 的值。
    """
    python = python or sys.executable
    kb = str(Path(kb_path).expanduser().resolve())
    return {
        "command": python,
        "args": ["-m", "cosmic_kb.cli.main", "mcp", "--db", kb],
        "env": {"COSMIC_KB_DB": kb},
    }


# ── 写配置（幂等 + 冲突保护 + 原子写）────────────────────────────────────────
def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:  # 权限/IO 等
        raise McpRegisterError(f"无法读取现有配置 {path}: {exc}") from exc
    raw = raw.strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise McpRegisterError(f"现有配置不是合法 JSON，拒绝覆盖 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise McpRegisterError(f"现有配置顶层不是对象，拒绝覆盖 {path}")
    return data


def _backup(path: Path) -> Path:
    """备份现有配置到 `<name>.bak-<时间戳>`，避开同秒重名。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    suffix = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak-{stamp}.{suffix}")
        suffix += 1
    _atomic_write(backup, path.read_bytes())
    return backup


def _register_file(
    path: Path, spec: Mapping[str, Any], *, force: bool, dry_run: bool
) -> dict[str, Any]:
    """把 `cosmic_kb` 条目并进 `path` 的 `mcpServers`，保留其它 server 与顶层键。"""
    existing = _load_json(path)
    servers = existing.get(_MCP_WRAP_KEY)
    servers = dict(servers) if isinstance(servers, dict) else {}
    current = servers.get(SERVER_NAME)

    if current == spec:  # 完全一致 → 幂等跳过（断点续跑重复调用走这里）
        return {"status": "unchanged", "path": str(path)}
    if current is not None and not force:  # 同名不同配置 → 默认停止
        return {
            "status": "conflict",
            "path": str(path),
            "detail": "已存在同名 cosmic_kb 且配置不同；确认后加 --force-mcp 先备份再替换",
        }

    will = "replaced" if current is not None else "registered"
    if dry_run:
        return {"status": f"would_{will}", "path": str(path)}

    backup = _backup(path) if (current is not None and force and path.exists()) else None
    servers[SERVER_NAME] = dict(spec)
    merged = dict(existing)
    merged[_MCP_WRAP_KEY] = servers
    _atomic_write(path, (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    result: dict[str, Any] = {"status": will, "path": str(path)}
    if backup is not None:
        result["backup"] = str(backup)
    return result


def _trae_steps(path: Path) -> list[str]:
    return [
        "打开 TRAE 设置（Settings）",
        "进入 MCP → MCP Servers 管理 → 创建 → 手动配置",
        f"粘贴 {path} 里的 mcpServers 片段并确认",
        "重启 / 重连 TRAE 使 MCP 生效",
    ]


def _register_trae(home: Path, spec: Mapping[str, Any], *, version: str, dry_run: bool) -> dict[str, Any]:
    """TRAE 无公开配置文件磁盘位置：生成稳定导入包 + 回传设置页手动步骤。"""
    path = home / ".cosmic_kb" / "trae-mcp" / version / "mcp.json"
    if dry_run:
        return {"target": "trae", "agents": ["trae"], "status": "would_stage", "path": str(path)}
    content = json.dumps({_MCP_WRAP_KEY: {SERVER_NAME: dict(spec)}}, ensure_ascii=False, indent=2) + "\n"
    _atomic_write(path, content.encode("utf-8"))
    return {
        "target": "trae",
        "agents": ["trae"],
        "status": "manual_action_required",
        "path": str(path),
        "manual_steps": _trae_steps(path),
    }


def register(
    project: str | os.PathLike[str],
    kb_path: str | os.PathLike[str],
    agents: Iterable[str],
    *,
    python: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    home: str | os.PathLike[str] | None = None,
    version: str = __version__,
) -> tuple[dict[str, Any], int]:
    """把 `cosmic_kb` MCP server 注册进 `agents` 各自认的配置。

    共享 `.mcp.json` 的宿主（CodeBuddy/Qoder）合并成**一次**文件写（去重，冲突只算一次）；TRAE
    单独出导入包。返回 `(payload, rc)`，`rc` 非零表示有冲突未处理（需 `force=True` 或人工介入）。
    """
    project = Path(project).expanduser().resolve()
    home = (Path(home) if home else Path.home()).expanduser().resolve()
    spec = server_spec(kb_path, python=python)

    requested = list(agents)
    shared = [a for a in requested if a in _SHARED_AGENTS]
    payload: dict[str, Any] = {
        "command": "register",
        "server": SERVER_NAME,
        "project": str(project),
        "kb": spec["env"]["COSMIC_KB_DB"],
        "spec": spec,
        "dry_run": dry_run,
        "targets": [],
    }
    conflicts = 0

    if shared:  # CodeBuddy + Qoder 共享 <project>/.mcp.json，只写一次
        res = _register_file(project / ".mcp.json", spec, force=force, dry_run=dry_run)
        res.update(target="shared", agents=shared)
        payload["targets"].append(res)
        if res["status"] == "conflict":
            conflicts += 1

    if "trae" in requested:
        payload["targets"].append(_register_trae(home, spec, version=version, dry_run=dry_run))

    unsupported = [a for a in requested if a not in (*_SHARED_AGENTS, "trae")]
    for agent in unsupported:
        payload["targets"].append({"target": agent, "agents": [agent], "status": "unsupported"})

    payload["summary"] = {"targets": len(payload["targets"]), "conflicts": conflicts}
    return payload, (1 if conflicts else 0)


# ── 校验：子进程真起服务器，握手确认四工具在列 ───────────────────────────────
def _probe_tools(spec: Mapping[str, Any], timeout: float) -> list[str]:
    """按 `spec` 起 MCP 子进程，`initialize` + `tools/list`，返回工具名清单（未装 mcp 抛错）。"""
    import asyncio

    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:  # noqa: BLE001
        raise McpRegisterError("未安装 MCP SDK，无法校验（pip install -e \".[mcp]\"）。") from exc

    env = {**os.environ, **{k: str(v) for k, v in spec.get("env", {}).items()}}
    params = StdioServerParameters(
        command=str(spec["command"]), args=[str(a) for a in spec.get("args", [])], env=env
    )

    async def _run() -> list[str]:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                return [tool.name for tool in listed.tools]

    return asyncio.run(asyncio.wait_for(_run(), timeout))


def verify_mcp(
    kb_path: str | os.PathLike[str] | None = None,
    *,
    spec: Mapping[str, Any] | None = None,
    python: str | None = None,
    timeout: float = 30.0,
    probe: Callable[[Mapping[str, Any], float], Sequence[str]] | None = None,
) -> dict[str, Any]:
    """起服务器校验四工具可用。给 `kb_path` 或直接给 `spec`（应与注册写入的一致）。

    返回 `{ok, tools, missing, [error]}`。`ok=True` 仅当四个核心工具全部出现在 `tools/list`。
    `probe` 可注入以便测试（默认真起子进程握手）。
    """
    if spec is None:
        if kb_path is None:
            raise ValueError("verify_mcp 需要 kb_path 或 spec 之一")
        spec = server_spec(kb_path, python=python)
    probe = probe or _probe_tools
    try:
        tools = list(probe(spec, timeout))
    except Exception as exc:  # noqa: BLE001 —— 起进程/握手失败都归一到 ok=False
        return {
            "ok": False,
            "tools": [],
            "missing": list(REQUIRED_TOOLS),
            "error": f"{type(exc).__name__}: {exc}",
        }
    missing = [name for name in REQUIRED_TOOLS if name not in tools]
    return {"ok": not missing, "tools": sorted(tools), "missing": missing}


__all__ = [
    "SERVER_NAME",
    "REQUIRED_TOOLS",
    "McpRegisterError",
    "server_spec",
    "register",
    "verify_mcp",
]

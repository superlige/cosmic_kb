"""阶段 12.1 · JVM 微工具 runner —— 喂路径收 JSONL，任何失败都不上抛。

职责：找 java（惰性探测 + 缓存，仿 java/parser.py 的 tree-sitter 装载套路）→ 组协议 v1
请求 → subprocess 起 ``vendor/symsolver.jar`` → 流式消费 stdout JSONL 进 SymbolTable →
静默看门狗兜底。**全 stdlib 实现**（subprocess/json/threading），零硬依赖红线不破。

RunOutcome 状态语义（软降级，`build` 永不因符号层崩）：
    ok           收到 summary，全量完整
    partial      收到部分 file 事件后进程死了/看门狗击杀 —— 已收部分照常可用
                 （JSONL 流式即恢复边界）
    unavailable  环境不具备（找不到 java / 随包 jar 缺失）—— 符号层整体降级
    failed       进程跑了但一个文件都没出来（协议不符 / 启动即崩）

看门狗两段阈值：start→solver_ready 600s（jar farm 首读"冷盘税"实测可达 140s+，
见设计方案 spike 结论），之后事件间隔 120s 无输出判静默击杀。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from queue import Empty, Queue
from typing import Callable

from .classpath import ClasspathResult, JarDir
from .table import SymbolTable

PROTOCOL_VERSION = 1

# start→solver_ready 阈值（冷盘税）；之后的事件间隔静默阈值
DEFAULT_SOLVER_TIMEOUT_S = 600.0
DEFAULT_IDLE_TIMEOUT_S = 120.0

# Python 编码探测口径（ingest/scanner.py 是单一真源）→ Java Charset 名映射。
# 只列 Java 认不出的别名；其余（gb18030/gbk/utf-8 等）Java Charset.forName 原样可解。
_JAVA_CHARSET_ALIASES = {
    "utf-8-sig": "UTF-8",      # Java 无 -sig 概念；BOM 由微工具解码后剥 U+FEFF
    "utf_8_sig": "UTF-8",
    "utf_8": "UTF-8",
    "ascii": "US-ASCII",
    "latin-1": "ISO-8859-1",
    "latin_1": "ISO-8859-1",
    "cp936": "GBK",
}


def to_java_charset(encoding: str | None) -> str:
    """Python codec 名 → Java Charset 名（编码由 Python 侧探测，Java 只管按名解码）。"""
    if not encoding:
        return "UTF-8"
    return _JAVA_CHARSET_ALIASES.get(encoding.lower(), encoding)


# ── java 可执行探测（惰性 + 缓存，仿 parser.get_parser）──────────────
_java_cache: str | None = None
_java_error: str | None = None


def find_java() -> str | None:
    """定位可用的 java 可执行：环境变量 COSMIC_KB_JAVA → JAVA_HOME/bin/java →
    PATH 上的 java。候选须通过 ``java -version``（10s 超时）验活；结果缓存。"""
    global _java_cache, _java_error
    if _java_cache is not None:
        return _java_cache
    if _java_error is not None:
        return None

    candidates: list[str] = []
    env_java = os.environ.get("COSMIC_KB_JAVA")
    if env_java:
        candidates.append(env_java)
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        exe = "java.exe" if os.name == "nt" else "java"
        candidates.append(str(Path(java_home) / "bin" / exe))
    which = shutil.which("java")
    if which:
        candidates.append(which)

    tried: list[str] = []
    for cand in candidates:
        try:
            proc = subprocess.run(
                [cand, "-version"], capture_output=True, timeout=10)
            if proc.returncode == 0:
                _java_cache = cand
                return cand
            tried.append(f"{cand}（退出码 {proc.returncode}）")
        except (OSError, subprocess.TimeoutExpired) as exc:
            tried.append(f"{cand}（{type(exc).__name__}）")
    if not candidates:
        _java_error = "找不到 java：环境变量 COSMIC_KB_JAVA / JAVA_HOME 未设置，PATH 上也没有 java"
    else:
        _java_error = "java 验活全部失败：" + "; ".join(tried)
    return None


def java_error() -> str | None:
    """返回 java 探测失败原因（供 doctor / 降级报告提示）。"""
    find_java()
    return _java_error


def _reset_java_cache() -> None:
    """仅测试用：清缓存以便 monkeypatch 环境重探。"""
    global _java_cache, _java_error
    _java_cache = None
    _java_error = None


def symsolver_jar() -> Path | None:
    """随包 vendor/symsolver.jar 的真实文件路径；缺失（安装损坏）返回 None。

    常规（目录式）安装下 Traversable 就是磁盘真实文件，直接转 Path；
    极端 zip 安装场景不支持（setuptools 现行安装均为目录式，doctor 会如实报缺失）。"""
    try:
        trav = files("cosmic_kb.java") / "vendor" / "symsolver.jar"
        if not trav.is_file():
            return None
        p = Path(str(trav))
        return p if p.is_file() else None
    except Exception:
        return None


# ── 请求组装 ────────────────────────────────────────────────────


def build_request(source_roots: list[str],
                  jar_dirs: list[JarDir] | list[dict],
                  file_entries: list[dict],
                  jar_files: list[str] | None = None,
                  options: dict | None = None) -> dict:
    """组协议 v1 请求。file_entries 每项 {path, relpath, encoding}（encoding 是
    ingest/scanner.py 探测口径，这里翻译成 Java Charset 名——编码判定的单一真源在 Python 侧）。"""
    dirs = []
    for jd in jar_dirs:
        dirs.append(jd.to_dict() if isinstance(jd, JarDir) else
                    {"path": str(jd["path"]), "recursive": bool(jd.get("recursive", False))})
    return {
        "protocol": PROTOCOL_VERSION,
        "source_roots": [str(r) for r in source_roots],
        "jar_dirs": dirs,
        "jar_files": [str(f) for f in (jar_files or [])],
        "files": [
            {"path": str(fe["path"]), "relpath": str(fe["relpath"]),
             "encoding": to_java_charset(fe.get("encoding"))}
            for fe in file_entries
        ],
        "options": options or {},
    }


def request_from_classpath(cp: ClasspathResult, file_entries: list[dict],
                           extra_source_roots: list[str] | None = None,
                           options: dict | None = None) -> dict:
    """从类路径发现结果直接组请求（extra_source_roots 供显式兜底场景补源码根）。"""
    roots = list(cp.source_roots())
    for r in extra_source_roots or []:
        if r not in roots:
            roots.append(r)
    return build_request(roots, cp.jar_dirs, file_entries,
                         jar_files=cp.jar_files, options=options)


# ── 运行结果 ────────────────────────────────────────────────────


@dataclass
class RunOutcome:
    """一次微工具运行的结果。永不上抛——status + reason 就是全部错误面。"""

    status: str                        # ok | partial | unavailable | failed
    reason: str | None = None          # 非 ok 时的人类可读原因
    table: SymbolTable | None = None   # partial 时是已收部分（照常可用）
    summary: dict | None = None        # 微工具 summary 事件原文
    solver: dict | None = None         # solver_ready 事件原文（jar_count/elapsed_ms）
    stderr_tail: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def usable(self) -> bool:
        """有可消费的符号表（ok 或 partial）。"""
        return self.table is not None and self.status in ("ok", "partial")


# ── 运行编排 ────────────────────────────────────────────────────


def run(request: dict,
        java_exe: str | None = None,
        jar_path: str | os.PathLike | None = None,
        solver_timeout_s: float = DEFAULT_SOLVER_TIMEOUT_S,
        idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
        on_event: Callable[[dict], None] | None = None,
        _popen=subprocess.Popen) -> RunOutcome:
    """跑一次微工具（一次批量不常驻）。java_exe/jar_path 不给则自动探测；
    on_event 收每条已解析事件（CLI 进度条用）；_popen 仅测试注入。"""
    started = time.monotonic()

    java = java_exe or find_java()
    if java is None:
        return RunOutcome(status="unavailable",
                          reason=java_error() or "找不到可用的 java")
    jar = Path(jar_path) if jar_path is not None else symsolver_jar()
    if jar is None or not Path(jar).is_file():
        return RunOutcome(status="unavailable",
                          reason="随包 vendor/symsolver.jar 缺失（安装损坏，重装可修复）")

    try:
        proc = _popen(
            [java, "-jar", str(jar)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return RunOutcome(status="failed", reason=f"进程启动失败：{exc}")

    # stdout 读线程 → 队列（Windows 管道阻塞读没有超时，只能靠线程 + 队列做看门狗）
    out_q: Queue = Queue()

    def _pump_stdout() -> None:
        try:
            for raw in iter(proc.stdout.readline, b""):
                out_q.put(("line", raw))
        except Exception as exc:  # 管道被 kill 掐断等
            out_q.put(("error", str(exc)))
        finally:
            out_q.put(("eof", None))

    stderr_tail: deque[str] = deque(maxlen=50)

    def _pump_stderr() -> None:
        try:
            for raw in iter(proc.stderr.readline, b""):
                stderr_tail.append(raw.decode("utf-8", errors="replace").rstrip())
        except Exception:
            pass

    threading.Thread(target=_pump_stdout, daemon=True).start()
    threading.Thread(target=_pump_stderr, daemon=True).start()

    # 请求单发即关（微工具第一步就是读完 stdin，不存在写读互锁）
    try:
        proc.stdin.write(json.dumps(request, ensure_ascii=False).encode("utf-8"))
        proc.stdin.close()
    except OSError as exc:
        proc.kill()
        return RunOutcome(status="failed", reason=f"请求写入失败：{exc}",
                          stderr_tail=list(stderr_tail))

    table = SymbolTable()
    summary: dict | None = None
    solver: dict | None = None
    solver_ready = False
    got_file = False
    outcome_reason: str | None = None
    status: str | None = None

    def _finish(st: str, reason: str | None = None) -> RunOutcome:
        try:
            proc.kill()
        except OSError:
            pass
        return RunOutcome(
            status=st, reason=reason,
            table=table if (got_file or st == "ok") else None,
            summary=summary, solver=solver, stderr_tail=list(stderr_tail),
            elapsed_ms=int((time.monotonic() - started) * 1000))

    while True:
        timeout = solver_timeout_s if not solver_ready else idle_timeout_s
        try:
            kind, payload = out_q.get(timeout=timeout)
        except Empty:
            phase = "solver 构建（jar farm 首读冷盘税）" if not solver_ready else "解析输出"
            return _finish("partial" if got_file else "failed",
                           f"看门狗超时：{phase}阶段 {int(timeout)}s 无输出，已击杀进程")
        if kind == "eof":
            break
        if kind == "error":
            outcome_reason = f"stdout 读取异常：{payload}"
            break

        line = payload.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            table.warnings.append(f"非 JSON 输出行已忽略：{line[:120]}")
            continue
        if on_event is not None:
            try:
                on_event(event)
            except Exception:
                pass

        etype = event.get("event")
        if etype == "start":
            proto = event.get("protocol")
            if proto != PROTOCOL_VERSION:
                return _finish("failed",
                               f"协议版本不符：微工具={proto}，本侧={PROTOCOL_VERSION}（请重装使 jar 与包配套）")
        elif etype == "solver_ready":
            solver_ready = True
            solver = event
        elif etype == "file":
            solver_ready = True  # 容错：即便 solver_ready 事件丢了，有产出就切静默阈值
            got_file = True
            table.add_file_event(event)
        elif etype == "summary":
            summary = event
            status = "ok"
            break
        elif etype == "warning":
            table.warnings.append(str(event.get("message") or ""))
        # progress 等其余事件仅供 on_event 消费

    # 收尾：等进程退出（拿退出码，但结果状态以事件流为准）
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

    if status == "ok":
        out = _finish("ok")
        if proc.returncode not in (0, None):
            out.table.warnings.append(f"微工具退出码非零：{proc.returncode}（summary 已完整收到，结果可用）")
        return out
    if outcome_reason is None:
        outcome_reason = "微工具在 summary 之前退出" + (
            f"（退出码 {proc.returncode}）" if proc.returncode is not None else "")
    return _finish("partial" if got_file else "failed", outcome_reason)

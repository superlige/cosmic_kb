"""阶段 12.1 · runner 单测 —— **不跑 JVM**：注入 FakePopen 回放 canned JSONL，
验证协议消费、SymbolTable 构建、col 消歧、看门狗、软降级路径与 find_java 探测。
"""

from __future__ import annotations

import io
import json
import time

import pytest

from cosmic_kb.java.symbols import runner as sym_runner
from cosmic_kb.java.symbols.runner import (
    PROTOCOL_VERSION,
    build_request,
    run,
    to_java_charset,
)


# ── 测试基建：FakePopen 回放 ──────────────────────────────────────


class _SinkStdin:
    """捕获写入 stdin 的请求字节。"""

    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, b: bytes):
        self.data += b
        return len(b)

    def close(self):
        self.closed = True


class _FakeProc:
    def __init__(self, canned: bytes, returncode: int = 0):
        self.stdin = _SinkStdin()
        self.stdout = io.BytesIO(canned)
        self.stderr = io.BytesIO(b"")
        self.returncode = returncode
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self.returncode


def _fake_popen(events: list[dict], returncode: int = 0):
    """返回 (popen 工厂, 取 proc 的函数)。events 逐条转 JSONL。"""
    canned = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events).encode("utf-8")
    box: list[_FakeProc] = []

    def factory(args, stdin=None, stdout=None, stderr=None):
        proc = _FakeProc(canned, returncode)
        box.append(proc)
        return proc

    return factory, lambda: box[0]


def _events_happy() -> list[dict]:
    return [
        {"event": "start", "protocol": PROTOCOL_VERSION},
        {"event": "solver_ready", "jar_count": 10, "jar_failed": 1, "elapsed_ms": 5},
        {"event": "warning", "message": "jar 加载失败示例"},
        {"event": "file", "relpath": "a/B.java", "status": "ok", "sites": [
            {"line": 10, "col": 9, "name": "save", "kind": "invocation",
             "resolution": "expr", "declaring": "kd.bos.servicehelper.operation.SaveServiceHelper",
             "signature": "kd.bos.servicehelper.operation.SaveServiceHelper.save(...)",
             "static": True, "target_kind": "jar", "argc": 2},
            # 同行同名双调用 → col 消歧锚点
            {"line": 12, "col": 9, "name": "doIt", "kind": "invocation",
             "resolution": "expr", "declaring": "com.x.A", "signature": "com.x.A.doIt()",
             "static": False, "target_kind": "project", "argc": 0},
            {"line": 12, "col": 21, "name": "doIt", "kind": "invocation",
             "resolution": "scope", "declaring": "com.x.C", "signature": "com.x.C.doIt()",
             "static": False, "target_kind": "project", "argc": 0},
            # 方法引用：argc=null
            {"line": 20, "col": 30, "name": "getKey", "kind": "method_reference",
             "resolution": "scope", "declaring": "com.x.P",
             "signature": "com.x.P.getKey(D)", "static": True, "target_kind": "project"},
            {"line": 25, "col": 9, "name": "mystery", "kind": "invocation",
             "resolution": "failed", "reason": "unsolved-symbol", "argc": 1},
        ]},
        {"event": "file", "relpath": "a/Bad.java", "status": "io-error",
         "note": "boom", "sites": []},
        {"event": "progress", "done": 2, "total": 2},
        {"event": "summary", "files": 2, "sites": 5,
         "by_resolution": {"expr": 2, "scope": 2, "failed": 1}, "elapsed_ms": 42},
    ]


def _run_canned(events, tmp_path, returncode: int = 0, **kwargs):
    """用 canned 事件跑一次 run()：java/jar 都给假的存在物，绕过环境探测。"""
    fake_jar = tmp_path / "fake.jar"
    fake_jar.write_bytes(b"PK")
    factory, get_proc = _fake_popen(events, returncode)
    req = build_request(["src"], [], [{"path": "x", "relpath": "a/B.java", "encoding": "utf-8-sig"}])
    outcome = run(req, java_exe="fake-java", jar_path=fake_jar, _popen=factory, **kwargs)
    return outcome, get_proc()


# ── happy path：表构建 + 查询 + 统计 ─────────────────────────────


def test_happy_path_table_and_summary(tmp_path):
    outcome, proc = _run_canned(_events_happy(), tmp_path)
    assert outcome.status == "ok" and outcome.usable
    assert outcome.summary["files"] == 2
    assert outcome.solver["jar_count"] == 10
    assert proc.stdin.closed  # 请求单发即关

    t = outcome.table
    site = t.lookup("a/B.java", 10, "save")
    assert site.declaring.endswith("SaveServiceHelper")
    assert site.target_kind == "jar" and site.static is True
    # warning 事件进表
    assert any("jar 加载失败" in w for w in t.warnings)


def test_stdin_request_is_protocol_v1_with_charset_translation(tmp_path):
    """写给微工具的请求：协议版本 + Python 编码名翻成 Java Charset（utf-8-sig → UTF-8）。"""
    _, proc = _run_canned(_events_happy(), tmp_path)
    req = json.loads(proc.stdin.data.decode("utf-8"))
    assert req["protocol"] == PROTOCOL_VERSION
    assert req["files"][0]["encoding"] == "UTF-8"


def test_col_disambiguation_same_line_same_name(tmp_path):
    """同行同名双调用：不带 col 查 → None（诚实拒答）；带 col → 精确命中。"""
    outcome, _ = _run_canned(_events_happy(), tmp_path)
    t = outcome.table
    assert t.lookup("a/B.java", 12, "doIt") is None
    assert t.lookup("a/B.java", 12, "doIt", col=9).declaring == "com.x.A"
    assert t.lookup("a/B.java", 12, "doIt", col=21).declaring == "com.x.C"
    assert t.lookup("a/B.java", 12, "doIt", col=99) is None


def test_method_reference_argc_none(tmp_path):
    outcome, _ = _run_canned(_events_happy(), tmp_path)
    site = outcome.table.lookup("a/B.java", 20, "getKey")
    assert site.kind == "method_reference"
    assert site.argc is None
    assert site.resolution == "scope" and site.resolved


def test_file_error_and_stats(tmp_path):
    """io-error 文件计入 files_failed；stats 覆盖率 = resolved/total。"""
    outcome, _ = _run_canned(_events_happy(), tmp_path)
    s = outcome.table.stats()
    assert s["files"] == 2 and s["files_failed"] == 1
    assert s["sites"] == 5 and s["resolved"] == 4
    assert s["coverage"] == 0.8
    assert s["by_resolution"] == {"expr": 2, "scope": 2, "failed": 1}
    assert s["by_failure_reason"] == {"unsolved-symbol": 1}


# ── 协议与降级路径 ───────────────────────────────────────────────


def test_protocol_mismatch_rejected(tmp_path):
    """微工具报的协议版本不符 → failed + 原因可读，且进程被击杀。"""
    events = [{"event": "start", "protocol": 99}] + _events_happy()[1:]
    outcome, proc = _run_canned(events, tmp_path)
    assert outcome.status == "failed"
    assert "协议版本不符" in outcome.reason
    assert proc.killed


def test_eof_before_summary_is_partial(tmp_path):
    """file 事件后没等到 summary 就 EOF → partial，已收部分照常可用。"""
    events = _events_happy()[:-1]  # 去掉 summary
    outcome, _ = _run_canned(events, tmp_path, returncode=1)
    assert outcome.status == "partial"
    assert outcome.usable
    assert outcome.table.lookup("a/B.java", 10, "save") is not None
    assert "summary 之前退出" in outcome.reason


def test_crash_before_any_file_is_failed(tmp_path):
    """连一个 file 事件都没有就死 → failed，无表。"""
    events = [{"event": "start", "protocol": PROTOCOL_VERSION}]
    outcome, _ = _run_canned(events, tmp_path, returncode=3)
    assert outcome.status == "failed"
    assert outcome.table is None and not outcome.usable


def test_non_json_lines_ignored_with_warning(tmp_path):
    """混入非 JSON 行（第三方库 println 漏网）→ 忽略 + warning，不炸协议。"""
    fake_jar = tmp_path / "fake.jar"
    fake_jar.write_bytes(b"PK")
    lines = [json.dumps({"event": "start", "protocol": PROTOCOL_VERSION}),
             "WARNING: stray log line",
             json.dumps({"event": "summary", "files": 0, "sites": 0,
                         "by_resolution": {}, "elapsed_ms": 1})]
    canned = ("\n".join(lines) + "\n").encode("utf-8")

    def factory(args, **kw):
        return _FakeProc(canned)

    outcome = run(build_request([], [], []), java_exe="j", jar_path=fake_jar, _popen=factory)
    assert outcome.status == "ok"
    assert any("非 JSON" in w for w in outcome.table.warnings)


def test_watchdog_kills_silent_process(tmp_path):
    """start 后长时间无输出 → 看门狗击杀，failed + 原因写明阶段。"""
    fake_jar = tmp_path / "fake.jar"
    fake_jar.write_bytes(b"PK")

    class _HangingStdout:
        def __init__(self):
            self._sent = False

        def readline(self):
            if not self._sent:
                self._sent = True
                return (json.dumps({"event": "start", "protocol": PROTOCOL_VERSION}) + "\n").encode()
            time.sleep(30)  # 假装挂死；daemon 线程随测试进程回收
            return b""

    class _HangProc(_FakeProc):
        def __init__(self):
            super().__init__(b"")
            self.stdout = _HangingStdout()

    proc_box = []

    def factory(args, **kw):
        p = _HangProc()
        proc_box.append(p)
        return p

    outcome = run(build_request([], [], []), java_exe="j", jar_path=fake_jar,
                  _popen=factory, solver_timeout_s=0.3, idle_timeout_s=0.3)
    assert outcome.status == "failed"
    assert "看门狗" in outcome.reason
    assert proc_box[0].killed


def test_unavailable_when_jar_missing(tmp_path):
    outcome = run(build_request([], [], []), java_exe="j",
                  jar_path=tmp_path / "no_such.jar")
    assert outcome.status == "unavailable"
    assert "symsolver.jar" in outcome.reason


# ── find_java 探测 ──────────────────────────────────────────────


@pytest.fixture()
def _clean_java_cache():
    sym_runner._reset_java_cache()
    yield
    sym_runner._reset_java_cache()


def test_find_java_absent_reason_readable(monkeypatch, _clean_java_cache):
    """三条探测路都空 → None，java_error() 给人读得懂的原因。"""
    monkeypatch.delenv("COSMIC_KB_JAVA", raising=False)
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr(sym_runner.shutil, "which", lambda _name: None)
    assert sym_runner.find_java() is None
    err = sym_runner.java_error()
    assert "COSMIC_KB_JAVA" in err and "JAVA_HOME" in err


def test_find_java_bad_candidate_reported(monkeypatch, _clean_java_cache):
    """COSMIC_KB_JAVA 指向不存在的可执行 → 验活失败，原因里带该候选。"""
    monkeypatch.setenv("COSMIC_KB_JAVA", "Z:/no/such/java.exe")
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr(sym_runner.shutil, "which", lambda _name: None)
    assert sym_runner.find_java() is None
    assert "Z:/no/such/java.exe" in sym_runner.java_error()


def test_run_unavailable_without_java(monkeypatch, _clean_java_cache, tmp_path):
    monkeypatch.delenv("COSMIC_KB_JAVA", raising=False)
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr(sym_runner.shutil, "which", lambda _name: None)
    fake_jar = tmp_path / "fake.jar"
    fake_jar.write_bytes(b"PK")
    outcome = run(build_request([], [], []), jar_path=fake_jar)
    assert outcome.status == "unavailable"


# ── 编码名翻译 ──────────────────────────────────────────────────


def test_to_java_charset():
    assert to_java_charset("utf-8-sig") == "UTF-8"
    assert to_java_charset("gb18030") == "gb18030"   # Java 原样可解
    assert to_java_charset(None) == "UTF-8"
    assert to_java_charset("cp936") == "GBK"

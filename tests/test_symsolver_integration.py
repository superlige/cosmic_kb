"""阶段 12.1 · symsolver 微工具集成测试 —— 跑**真 JVM**（无 java / 无随包 jar 自动 skip）。

两层：
    1. 合成小工程（tmp_path，零 jar）：协议全链 + 两层解析 + 同行双调用 col 消歧 + GBK 解码。
    2. 真实样本工程（仅本机有 D:\\kingdee\\asset_management_sys 时）：spike 同款锚点 ——
       ``getZqzdGroupKey`` 方法引用必须解析到 ``ContractArrearsYSRQPlugin``
       （表达式级 resolve 在泛型推断上下文的已知局限 → scope 兜底逮住）。
       注意：本用例要装载 3678 个 jar，热盘约 2 分钟，是本仓最重的测试。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cosmic_kb.java.symbols import (
    build_request,
    discover_classpath,
    find_java,
    request_from_classpath,
    run,
    symsolver_jar,
)

pytestmark = pytest.mark.skipif(
    find_java() is None or symsolver_jar() is None,
    reason="需要本机 java 运行时 + 随包 vendor/symsolver.jar")

SAMPLE_IDEA = Path(r"D:\kingdee\asset_management_sys")


# ── 合成小工程：协议全链（秒级）────────────────────────────────────


def _write_tiny_project(tmp_path: Path) -> tuple[Path, list[dict]]:
    src = tmp_path / "src" / "main" / "java" / "demo"
    src.mkdir(parents=True)
    a = src / "A.java"
    a.write_text(
        "package demo;\n"
        "public class A {\n"
        "    static String key(Object o) { return String.valueOf(o); }\n"
        "    void helper(int x) {}\n"
        "    void run() {\n"
        "        helper(1); helper(2);\n"  # 同行双调用：col 消歧锚点
        "        java.util.function.Function<Object, String> f = A::key;\n"
        "        f.apply(this);\n"
        "    }\n"
        "}\n",
        encoding="utf-8")
    # GBK 编码 + 中文注释：编码由 Python 侧传给 Java 按名解码
    b = src / "B.java"
    b.write_bytes("package demo;\n// 中文注释：GBK 编码样本\npublic class B { void go() { new A().helper(9); } }\n".encode("gbk"))
    files = [
        {"path": str(a), "relpath": "demo/A.java", "encoding": "utf-8"},
        {"path": str(b), "relpath": "demo/B.java", "encoding": "gbk"},
    ]
    return tmp_path / "src" / "main" / "java", files


def test_tiny_project_full_chain(tmp_path):
    source_root, files = _write_tiny_project(tmp_path)
    outcome = run(build_request([str(source_root)], [], files))
    assert outcome.status == "ok", f"reason={outcome.reason} stderr={outcome.stderr_tail}"
    t = outcome.table

    # 同行双调用：不带 col 拒答，带 col 精确命中，两处都解析到 demo.A.helper
    assert t.lookup("demo/A.java", 6, "helper") is None
    sites = [s for s in t.sites_in_file("demo/A.java") if s.name == "helper"]
    assert len(sites) == 2 and {s.col for s in sites} == {9, 20}
    assert all(s.resolution == "expr" and s.declaring == "demo.A" and
               s.target_kind == "project" for s in sites)

    # 方法引用 A::key 被扫到且 argc=None
    refs = [s for s in t.iter_sites() if s.kind == "method_reference"]
    assert len(refs) == 1 and refs[0].name == "key" and refs[0].argc is None
    assert refs[0].resolved and refs[0].declaring == "demo.A"

    # JDK 目标归类：String.valueOf → target_kind=jdk
    valueof = [s for s in t.iter_sites() if s.name == "valueOf"]
    assert valueof and valueof[0].target_kind == "jdk" and valueof[0].static is True

    # GBK 文件正常解码解析：跨文件调用 helper 解析到 demo.A
    b_sites = [s for s in t.sites_in_file("demo/B.java") if s.name == "helper"]
    assert b_sites and b_sites[0].declaring == "demo.A"

    # 覆盖率统计口径完整
    s = t.stats()
    assert s["files"] == 2 and s["files_failed"] == 0
    assert s["coverage"] > 0.9


# ── 真实样本：spike 锚点（重，~2 分钟；无样本自动 skip）──────────────


@pytest.mark.skipif(not SAMPLE_IDEA.is_dir(), reason="本机无真实样本工程 asset_management_sys")
def test_real_project_spike_anchor():
    cp = discover_classpath(SAMPLE_IDEA)
    assert cp.ok and cp.provider == "idea" and cp.jar_count > 3000

    rel = ("am_business/src/main/java/cqkd/am/assets/botp/rentarrearsmanagement/"
           "RentArrearsManagementQSRQDown.java")
    files = [{"path": str(SAMPLE_IDEA / Path(rel)), "relpath": rel, "encoding": "utf-8"}]
    outcome = run(request_from_classpath(cp, files))
    assert outcome.status == "ok", f"reason={outcome.reason} stderr={outcome.stderr_tail}"

    anchors = [s for s in outcome.table.sites_in_file(rel) if s.name == "getZqzdGroupKey"]
    assert anchors, "spike 锚点方法引用没被扫到"
    site = anchors[0]
    assert site.kind == "method_reference"
    assert site.resolved  # expr 或 scope 都算（spike 实测走 scope 兜底）
    assert site.declaring == "cqkd.am.assets.task.contractinfo.ContractArrearsYSRQPlugin"

    # 单文件抽样解析率对照 spike 区间（96%+；给足裕度防样本波动）
    assert outcome.table.stats()["coverage"] >= 0.9

"""阶段 12.1 · 类路径发现单测 —— 纯文本解析，无 tree-sitter/JVM 依赖，不需要 importorskip。

fixture 见 tests/data/classpath_idea/ 与 tests/data/classpath_gradle/（jar 是 0 字节占位，
只测发现/计数，不测加载）。真实工程回归锚点：IDEA 库名与文件名 mangle（cosmic-lib →
cosmic_lib.xml）、jarDirectory recursive、Gradle 三级回退链、include 混合引号写法。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from cosmic_kb.java.symbols import discover_classpath
from cosmic_kb.java.symbols.classpath import count_jars, JarDir

DATA = Path(__file__).parent / "data"
IDEA_PROJECT = DATA / "classpath_idea" / "project"
GRADLE_FIXTURE = DATA / "classpath_gradle"


def _attempt(result, provider: str):
    hits = [a for a in result.attempts if a.provider == provider]
    assert len(hits) == 1, f"attempts 里 {provider} 应恰好一条：{result.attempts}"
    return hits[0]


# ── IDEA 适配器 ──────────────────────────────────────────────


def test_idea_modules_and_dependency_edges():
    """modules.xml → 2 模块；orderEntry module → 依赖边；模块名 = iml 文件名 stem。"""
    r = discover_classpath(IDEA_PROJECT)
    assert r.ok and r.provider == "idea"
    by_name = {m.name: m for m in r.modules}
    assert set(by_name) == {"project", "mod_a"}
    assert by_name["project"].depends_on == ["mod_a"]
    assert by_name["mod_a"].depends_on == []


def test_idea_source_roots_exclude_test():
    """isTestSource="true" 的 sourceFolder 不进源码根。"""
    r = discover_classpath(IDEA_PROJECT)
    norm = [root.replace("\\", "/") for root in r.source_roots()]
    assert all(root.endswith("src/main/java") for root in norm)
    assert not any(root.endswith("src/test/java") for root in norm)
    # 两个模块各一个 main 源码根
    assert len(norm) == 2


def test_idea_jar_directory_recursive_and_single_jar():
    """jarDirectory recursive="true" 递归计数（含 nested/）；CLASSES 的 jar:// 单 jar 也收；
    宏 $PROJECT_DIR$/.. 展开到工程外目录。"""
    r = discover_classpath(IDEA_PROJECT)
    assert len(r.jar_dirs) == 1
    jd = r.jar_dirs[0]
    assert jd.recursive is True
    assert jd.path.endswith("jarfarm")
    assert len(r.jar_files) == 1 and r.jar_files[0].endswith("one.jar")
    # jarfarm 2 个（a.jar + nested/b.jar）+ 单 jar 1 个
    assert r.jar_count == 3


def test_idea_library_name_not_filename():
    """库按 <library name="cosmic-lib"> 索引（文件名是 mangle 过的 cosmic_lib.xml），
    引用不存在的库（ghost-lib）→ warning 不崩。"""
    r = discover_classpath(IDEA_PROJECT)
    assert r.ok
    assert any("ghost-lib" in w for w in r.warnings)


def test_idea_broken_library_xml_only_warns():
    """.idea/libraries/broken.xml 是非法 XML：单文件损坏只 warning，整体照常。"""
    r = discover_classpath(IDEA_PROJECT)
    assert r.ok
    assert any("broken.xml" in w for w in r.warnings)


def test_idea_missing_jar_directory_warns(tmp_path):
    """jarDirectory 指向不存在目录 → warning + 该目录不进结果；但另一个可用库仍让适配器成功。"""
    proj = tmp_path / "p"
    shutil.copytree(IDEA_PROJECT, proj)
    lib = proj / ".idea" / "libraries" / "cosmic_lib.xml"
    text = lib.read_text(encoding="utf-8").replace("../jarfarm", "../no_such_dir")
    lib.write_text(text, encoding="utf-8")
    # 单 jar one.jar 也在工程外，copytree 后同样失效 → 适配器应 failed（无任何 jar 来源）
    r = discover_classpath(proj)
    assert r.status == "none"
    att = _attempt(r, "idea")
    assert att.status == "failed"
    assert "没有任何库 jar 声明" in att.detail


# ── Gradle 模板适配器（三级回退链逐级）──────────────────────────


def _copy_gradle_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    shutil.copytree(GRADLE_FIXTURE / "project", proj)
    return proj


def test_gradle_level1_cosmic_libs_path(tmp_path, monkeypatch):
    """第一级：systemProp.cosmic_libs_path 直给全路径。"""
    monkeypatch.delenv("COSMIC_HOME", raising=False)
    proj = _copy_gradle_project(tmp_path)
    libfarm = (GRADLE_FIXTURE / "libfarm").resolve()
    (proj / "gradle.properties").write_text(
        f"systemProp.cosmic_libs_path={libfarm.as_posix()}\n", encoding="utf-8")

    r = discover_classpath(proj)
    assert r.ok and r.provider == "gradle"
    # config.gradle 的五个子目录（outputdir 排除）都存在 → 都进 jar_dirs，平铺语义
    assert len(r.jar_dirs) == 5
    assert all(not d.recursive for d in r.jar_dirs)
    assert r.jar_count == 5  # bos 2 + trd/biz/cus 各 1 + ext 0


def test_gradle_level2_cosmic_home(tmp_path, monkeypatch):
    """第二级：systemProp.cosmic_home 拼 /mservice-cosmic/lib；缺失子目录 warning 不崩。"""
    monkeypatch.delenv("COSMIC_HOME", raising=False)
    proj = _copy_gradle_project(tmp_path)
    home = (GRADLE_FIXTURE / "cosmic_home").resolve()
    (proj / "gradle.properties").write_text(
        f"systemProp.cosmic_home={home.as_posix()}\n", encoding="utf-8")

    r = discover_classpath(proj)
    assert r.ok and r.provider == "gradle"
    # cosmic_home 下只有 bos → 1 个 jar 目录 + 4 条子目录缺失 warning
    assert len(r.jar_dirs) == 1 and r.jar_dirs[0].path.endswith("bos")
    assert r.jar_count == 1
    assert sum(1 for w in r.warnings if "子目录不存在" in w) == 4


def test_gradle_level3_env_cosmic_home(tmp_path, monkeypatch):
    """第三级：gradle.properties 没有 → 读环境变量 COSMIC_HOME。"""
    proj = _copy_gradle_project(tmp_path)
    home = (GRADLE_FIXTURE / "cosmic_home").resolve()
    monkeypatch.setenv("COSMIC_HOME", str(home))

    r = discover_classpath(proj)
    assert r.ok and r.provider == "gradle"
    assert r.jar_count == 1


def test_gradle_chain_exhausted_fails_honestly(tmp_path, monkeypatch):
    """三级全空 → gradle attempt=failed 且败因写明三级都没配。"""
    monkeypatch.delenv("COSMIC_HOME", raising=False)
    proj = _copy_gradle_project(tmp_path)

    r = discover_classpath(proj)
    assert r.status == "none"
    att = _attempt(r, "gradle")
    assert att.status == "failed"
    assert "COSMIC_HOME" in att.detail


def test_gradle_modules_and_deps(tmp_path, monkeypatch):
    """settings.gradle 括号形 + 语句形 include 都收；project(':x') → 依赖边；
    无 src/main/java 的模块（mod-c）记 warning。"""
    monkeypatch.delenv("COSMIC_HOME", raising=False)
    proj = _copy_gradle_project(tmp_path)
    libfarm = (GRADLE_FIXTURE / "libfarm").resolve()
    (proj / "gradle.properties").write_text(
        f"systemProp.cosmic_libs_path={libfarm.as_posix()}\n", encoding="utf-8")

    r = discover_classpath(proj)
    by_name = {m.name: m for m in r.modules}
    assert set(by_name) == {"mod-a", "mod-b", "mod-c"}
    assert by_name["mod-a"].depends_on == ["mod-b"]
    assert by_name["mod-c"].source_roots == []
    assert any("mod-c" in w for w in r.warnings)


# ── 显式兜底 + 优先级 + attempts 轨迹 ───────────────────────────


def test_explicit_dirs_override_everything():
    """显式 --classpath-dir 压过 IDEA 自动探测；显式目录按递归语义枚举。"""
    jarfarm = (DATA / "classpath_idea" / "jarfarm").resolve()
    r = discover_classpath(IDEA_PROJECT, explicit_dirs=[str(jarfarm)])
    assert r.ok and r.provider == "explicit"
    assert r.jar_dirs[0].recursive is True
    assert r.jar_count == 2  # a.jar + nested/b.jar
    assert _attempt(r, "explicit").status == "ok"
    assert _attempt(r, "idea").status == "skipped"
    assert _attempt(r, "gradle").status == "skipped"


def test_explicit_dirs_all_missing_falls_through():
    """显式目录全不存在 → explicit=failed，落回 IDEA 自动探测。"""
    r = discover_classpath(IDEA_PROJECT, explicit_dirs=["Z:/no/such/dir"])
    assert r.ok and r.provider == "idea"
    assert _attempt(r, "explicit").status == "failed"


def test_no_project_markers_status_none(tmp_path, monkeypatch):
    """裸目录（无 .idea/iml/settings.gradle）→ status=none，attempts 三条轨迹齐全。"""
    monkeypatch.delenv("COSMIC_HOME", raising=False)
    r = discover_classpath(tmp_path)
    assert r.status == "none" and r.provider is None
    assert [a.provider for a in r.attempts] == ["explicit", "idea", "gradle"]
    assert all(a.status == "skipped" for a in r.attempts)
    assert r.jar_dirs == [] and r.modules == []


def test_count_jars_tolerates_missing_dir():
    """count_jars 对不存在目录静默计 0（软降级）。"""
    assert count_jars([JarDir(path="Z:/no/such/dir", recursive=True)]) == 0


def test_result_to_dict_roundtrip():
    """to_dict 产出可 JSON 序列化的完整结构（进 build stdout / kb_meta 的口径）。"""
    import json

    r = discover_classpath(IDEA_PROJECT)
    d = r.to_dict()
    json.dumps(d, ensure_ascii=False)  # 不抛即可
    assert d["status"] == "ok"
    assert d["jar_count"] == 3
    assert {a["provider"] for a in d["attempts"]} == {"explicit", "idea", "gradle"}

"""阶段 12.1 · 类路径发现 —— 从工程元数据解析"哪些 jar + 哪些源码目录"构成类路径。

苍穹二开工程的平台依赖本质是**本地磁盘上一堆已编译好的 jar**（bos/trd/biz/cus 分类平铺），
只是"声明入口"分两代格式（见 docs/设计方案/跨类调用链解析与编译期符号方案.md 实地调研）：

    IdeaClasspathAdapter        解析 .idea/modules.xml → *.iml（orderEntry module/library）
                                + .idea/libraries/*.xml（jarDirectory / CLASSES root）
    GradleCosmicTemplateAdapter 金蝶官方 Gradle 模板：gradle.properties 三级回退链
                                （cosmic_libs_path → cosmic_home 拼 /mservice-cosmic/lib →
                                环境变量 COSMIC_HOME）+ config.gradle 子目录 + settings.gradle 模块图
    显式兜底 explicit_dirs      两种都识别不出时（裸源码导出、无工程文件）手工指定 jar 目录

设计约束：
    - 纯文本解析（stdlib xml.etree + 正则），**不执行任何构建、不 eval Groovy**。
    - 诊断是一等产物：每个适配器成败都记 ``attempts``（进 build stdout 与 kb_meta），
      "为什么没发现类路径"必须能从 attempts 里读出来，不许静默。
    - 软降级：目录不存在 / XML 损坏 / 宏解不开 → warning 照常返回，绝不崩。
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# Gradle 模板 config.gradle 解析失败时的固定子目录兜底（outputdir 是工程自身构建输出，
# 不进类路径 —— 它装的是本工程源码编译产物，进了会与源码级 TypeSolver 相互遮蔽）。
_GRADLE_DEFAULT_SUBDIRS = ("bos", "trd", "biz", "cus", "ext")

# 搜 *.iml 时剪掉的目录（modules.xml 缺失的兜底路径才用得到）。
_IML_PRUNE_DIRS = {".git", ".idea", "out", "build", "target", "dist", "node_modules", "__pycache__"}


@dataclass(frozen=True)
class JarDir:
    """一个 jar 目录条目。recursive 对应 IDEA jarDirectory 的 recursive 属性；
    Gradle 模板的 fileTree(include:'*.jar') 是平铺语义 → recursive=False。"""

    path: str
    recursive: bool = False

    def to_dict(self) -> dict:
        return {"path": self.path, "recursive": self.recursive}


@dataclass
class ModuleInfo:
    """一个源码模块：名字、源码根（可多个）、依赖的兄弟模块名。"""

    name: str
    source_roots: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "source_roots": self.source_roots,
                "depends_on": self.depends_on}


@dataclass(frozen=True)
class Attempt:
    """一次适配器尝试的诊断轨迹。status: ok | skipped | failed。"""

    provider: str
    status: str
    detail: str

    def to_dict(self) -> dict:
        return {"provider": self.provider, "status": self.status, "detail": self.detail}


@dataclass
class ClasspathResult:
    """类路径发现结果。status="none" 时 jar_dirs/modules 为空，attempts 里有全部败因。"""

    status: str                      # ok | none
    provider: str | None             # explicit | idea | gradle | None
    project_root: str
    modules: list[ModuleInfo] = field(default_factory=list)
    jar_dirs: list[JarDir] = field(default_factory=list)
    jar_files: list[str] = field(default_factory=list)   # 单独指到 jar 文件的条目（IDEA jar:// root）
    jar_count: int = 0               # 实际枚举到的 jar 总数（诊断/验收用）
    warnings: list[str] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def source_roots(self) -> list[str]:
        """全部模块源码根（去重保序），供 runner 组请求。"""
        seen: dict[str, None] = {}
        for m in self.modules:
            for r in m.source_roots:
                seen.setdefault(r)
        return list(seen)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "provider": self.provider,
            "project_root": self.project_root,
            "modules": [m.to_dict() for m in self.modules],
            "jar_dirs": [d.to_dict() for d in self.jar_dirs],
            "jar_files": list(self.jar_files),
            "jar_count": self.jar_count,
            "warnings": list(self.warnings),
            "attempts": [a.to_dict() for a in self.attempts],
        }


# ── 公共小工具 ────────────────────────────────────────────────────


def count_jars(jar_dirs: list[JarDir], jar_files: list[str] | None = None) -> int:
    """枚举 jar 总数（recursive 目录 os.walk，平铺目录只看一层）。目录读不动算 0，不崩。"""
    n = 0
    for jd in jar_dirs:
        p = Path(jd.path)
        try:
            if jd.recursive:
                for _root, _dirs, files in os.walk(p):
                    n += sum(1 for f in files if f.lower().endswith(".jar"))
            else:
                n += sum(1 for f in p.iterdir()
                         if f.is_file() and f.name.lower().endswith(".jar"))
        except OSError:
            continue
    n += len(jar_files or [])
    return n


def _expand_url(url: str, macros: dict[str, str],
                warnings: list[str]) -> str | None:
    """IDEA url → 本地路径：剥 file:// / jar:// 协议头、展开 $PROJECT_DIR$/$MODULE_DIR$ 宏。
    含解不开的宏 → warning + 返回 None（诚实跳过，不猜）。"""
    path = url
    if path.startswith("jar://"):
        path = path[len("jar://"):]
        if path.endswith("!/"):
            path = path[:-2]
    elif path.startswith("file://"):
        path = path[len("file://"):]
    for macro, value in macros.items():
        path = path.replace(macro, value)
    m = re.search(r"\$[A-Z_]+\$", path)
    if m:
        warnings.append(f"IDEA url 含未知宏 {m.group(0)}，跳过：{url}")
        return None
    try:
        return str(Path(path).resolve())
    except OSError:
        warnings.append(f"IDEA url 无法规范化，跳过：{url}")
        return None


# ── IDEA 适配器 ──────────────────────────────────────────────────


class IdeaClasspathAdapter:
    """解析 IDEA 工程（.idea/ + *.iml）。样本实证：asset_management_sys（3 模块 +
    项目级库 cosmic-lib 的 jarDirectory recursive="true" 指向 3678 个 jar）。

    注意库的**名字在 XML 里、文件名是 mangle 过的**（库 "cosmic-lib" 存于
    cosmic_lib.xml），所以库表按 ``<library name=...>`` 建索引，不按文件名。"""

    provider = "idea"

    def __init__(self, project_root: Path):
        self.root = project_root

    def detect(self) -> bool:
        if (self.root / ".idea").is_dir():
            return True
        try:
            return any(f.suffix == ".iml" for f in self.root.iterdir() if f.is_file())
        except OSError:
            return False

    def parse(self) -> ClasspathResult:
        warnings: list[str] = []
        macros_project = {"$PROJECT_DIR$": str(self.root)}

        libraries = self._parse_library_table(macros_project, warnings)
        iml_paths = self._find_imls(macros_project, warnings)
        if not iml_paths:
            raise _AdapterFailed("未找到任何 *.iml 模块文件（.idea 存在但可能是非 Java/Gradle 导入工程）")

        modules: list[ModuleInfo] = []
        jar_dirs: list[JarDir] = []
        jar_files: list[str] = []
        seen_dirs: set[tuple[str, bool]] = set()
        seen_jars: set[str] = set()

        for iml in iml_paths:
            mod = self._parse_iml(iml, libraries, jar_dirs, jar_files,
                                  seen_dirs, seen_jars, macros_project, warnings)
            if mod is not None:
                modules.append(mod)

        if not jar_dirs and not jar_files:
            raise _AdapterFailed(
                "解析到 %d 个模块但没有任何库 jar 声明（orderEntry library / jarDirectory 均未命中）"
                % len(modules))

        return ClasspathResult(
            status="ok", provider=self.provider, project_root=str(self.root),
            modules=modules, jar_dirs=jar_dirs, jar_files=jar_files,
            warnings=warnings)

    # -- 内部 --

    def _parse_library_table(self, macros: dict[str, str],
                             warnings: list[str]) -> dict[str, tuple[list[JarDir], list[str]]]:
        """.idea/libraries/*.xml → {库名: (jar 目录, 单 jar 文件)}。单个文件损坏只 warning。"""
        table: dict[str, tuple[list[JarDir], list[str]]] = {}
        lib_dir = self.root / ".idea" / "libraries"
        if not lib_dir.is_dir():
            return table
        for xml_file in sorted(lib_dir.glob("*.xml")):
            try:
                root_el = ET.parse(xml_file).getroot()
            except (ET.ParseError, OSError) as exc:
                warnings.append(f"库描述文件解析失败，跳过：{xml_file.name}（{exc}）")
                continue
            for lib_el in root_el.iter("library"):
                name = lib_el.get("name")
                if not name:
                    continue
                table[name] = self._parse_library_element(lib_el, macros, warnings)
        return table

    def _parse_library_element(self, lib_el: ET.Element, macros: dict[str, str],
                               warnings: list[str]) -> tuple[list[JarDir], list[str]]:
        """<library> 元素 → (jar 目录, 单 jar 文件)。

        jarDirectory 是一等来源；CLASSES/root 的 jar:// 指单个 jar；CLASSES/root 的
        file:// 目录若与某 jarDirectory 重复则去重，否则是"类目录"——JavaParser 无
        对应 TypeSolver，如实 warning 忽略。"""
        dirs: list[JarDir] = []
        files: list[str] = []
        jar_dir_paths: set[str] = set()

        for jd_el in lib_el.iter("jarDirectory"):
            url = jd_el.get("url") or ""
            path = _expand_url(url, macros, warnings)
            if path is None:
                continue
            if not Path(path).is_dir():
                warnings.append(f"jarDirectory 目录不存在，跳过：{path}")
                continue
            recursive = (jd_el.get("recursive") or "").lower() == "true"
            dirs.append(JarDir(path=path, recursive=recursive))
            jar_dir_paths.add(path)

        classes_el = lib_el.find("CLASSES")
        if classes_el is not None:
            for root_el in classes_el.iter("root"):
                url = root_el.get("url") or ""
                path = _expand_url(url, macros, warnings)
                if path is None:
                    continue
                if url.startswith("jar://"):
                    if Path(path).is_file():
                        files.append(path)
                    else:
                        warnings.append(f"库指向的 jar 不存在，跳过：{path}")
                elif path not in jar_dir_paths:
                    # file:// 目录且不与 jarDirectory 重合 → 编译类目录，符号层不支持
                    warnings.append(f"库 CLASSES 指向类目录（非 jar），符号解析暂不支持，忽略：{path}")
        return dirs, files

    def _find_imls(self, macros: dict[str, str], warnings: list[str]) -> list[Path]:
        """优先 .idea/modules.xml 权威清单；缺失/损坏 → 有界 os.walk 兜底搜 *.iml。"""
        modules_xml = self.root / ".idea" / "modules.xml"
        found: list[Path] = []
        if modules_xml.is_file():
            try:
                root_el = ET.parse(modules_xml).getroot()
                for mod_el in root_el.iter("module"):
                    raw = mod_el.get("filepath") or mod_el.get("fileurl") or ""
                    path = _expand_url(raw, macros, warnings)
                    if path is None:
                        continue
                    p = Path(path)
                    if p.is_file():
                        found.append(p)
                    else:
                        warnings.append(f"modules.xml 声明的 .iml 不存在，跳过：{path}")
                if found:
                    return found
            except (ET.ParseError, OSError) as exc:
                warnings.append(f"modules.xml 解析失败，退回全盘搜 *.iml（{exc}）")
        # 兜底：剪枝遍历找 *.iml
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in _IML_PRUNE_DIRS]
            for fn in filenames:
                if fn.endswith(".iml"):
                    found.append(Path(dirpath) / fn)
        return sorted(found)

    def _parse_iml(self, iml: Path, libraries: dict[str, tuple[list[JarDir], list[str]]],
                   jar_dirs: list[JarDir], jar_files: list[str],
                   seen_dirs: set[tuple[str, bool]], seen_jars: set[str],
                   macros_project: dict[str, str], warnings: list[str]) -> ModuleInfo | None:
        """单个 .iml → ModuleInfo；沿途把引用到的库 jar 归并进 jar_dirs/jar_files。
        IDEA 模块名 = iml 文件名去扩展名（orderEntry module-name 引用的就是它）。"""
        try:
            root_el = ET.parse(iml).getroot()
        except (ET.ParseError, OSError) as exc:
            warnings.append(f"模块文件解析失败，跳过：{iml.name}（{exc}）")
            return None

        macros = dict(macros_project)
        macros["$MODULE_DIR$"] = str(iml.parent)
        mod = ModuleInfo(name=iml.stem)

        for sf_el in root_el.iter("sourceFolder"):
            if (sf_el.get("isTestSource") or "").lower() == "true":
                continue
            path = _expand_url(sf_el.get("url") or "", macros, warnings)
            if path is None:
                continue
            if Path(path).is_dir():
                mod.source_roots.append(path)
            else:
                warnings.append(f"模块 {mod.name} 源码根不存在，跳过：{path}")

        def _merge_library(pair: tuple[list[JarDir], list[str]]) -> None:
            lib_dirs, lib_files = pair
            for d in lib_dirs:
                key = (d.path, d.recursive)
                if key not in seen_dirs:
                    seen_dirs.add(key)
                    jar_dirs.append(d)
            for f in lib_files:
                if f not in seen_jars:
                    seen_jars.add(f)
                    jar_files.append(f)

        for oe_el in root_el.iter("orderEntry"):
            kind = oe_el.get("type") or ""
            if kind == "module":
                dep = oe_el.get("module-name")
                if dep:
                    mod.depends_on.append(dep)
            elif kind == "library":
                name = oe_el.get("name") or ""
                if name in libraries:
                    _merge_library(libraries[name])
                else:
                    warnings.append(f"模块 {mod.name} 引用的库在 .idea/libraries 里找不到：{name}")
            elif kind == "module-library":
                # 内联库：<orderEntry type="module-library"><library>…</library></orderEntry>
                for lib_el in oe_el.iter("library"):
                    _merge_library(self._parse_library_element(lib_el, macros, warnings))
        return mod


# ── Gradle 官方模板适配器 ─────────────────────────────────────────


class GradleCosmicTemplateAdapter:
    """解析金蝶苍穹开发助手插件生成的官方 Gradle 模板（正则文本解析，不 eval Groovy）。

    样本实证 zlgd_ygpt2.0：三级回退 systemProp.cosmic_libs_path →
    systemProp.cosmic_home 拼 /mservice-cosmic/lib → 环境变量 COSMIC_HOME，
    命中 4441 个 jar（bos/trd/biz/cus 平铺 + ext 空目录）。"""

    provider = "gradle"

    def __init__(self, project_root: Path):
        self.root = project_root

    def detect(self) -> bool:
        return (self.root / "settings.gradle").is_file() or \
               (self.root / "settings.gradle.kts").is_file()

    def parse(self) -> ClasspathResult:
        warnings: list[str] = []

        libs_path, chain_detail = self._resolve_libs_path()
        if libs_path is None:
            raise _AdapterFailed(
                "三级回退链均未命中（gradle.properties 无 systemProp.cosmic_libs_path / "
                "systemProp.cosmic_home，环境变量 COSMIC_HOME 也未设置）")
        libs_root = Path(libs_path)
        if not libs_root.is_dir():
            raise _AdapterFailed(f"类路径根不存在：{libs_path}（来源：{chain_detail}）")

        subdirs = self._parse_config_subdirs(warnings)
        jar_dirs: list[JarDir] = []
        for sub in subdirs:
            p = libs_root / sub
            if p.is_dir():
                # 官方约定插件 fileTree(include:'*.jar') 是平铺语义 → recursive=False
                jar_dirs.append(JarDir(path=str(p.resolve()), recursive=False))
            else:
                warnings.append(f"子目录不存在，跳过：{p}")
        if not jar_dirs:
            raise _AdapterFailed(f"类路径根 {libs_path} 下无任何已知子目录（{'/'.join(subdirs)}）")

        modules = self._parse_modules(warnings)
        return ClasspathResult(
            status="ok", provider=self.provider, project_root=str(self.root),
            modules=modules, jar_dirs=jar_dirs, warnings=warnings)

    # -- 内部 --

    def _read_properties(self) -> dict[str, str]:
        """gradle.properties → dict。systemProp. 前缀键与裸键都收（宽容），systemProp 优先。"""
        props: dict[str, str] = {}
        f = self.root / "gradle.properties"
        if not f.is_file():
            return props
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return props
        bare: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("!") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key.startswith("systemProp."):
                props[key[len("systemProp."):]] = value
            else:
                bare[key] = value
        for k, v in bare.items():
            props.setdefault(k, v)
        return props

    def _resolve_libs_path(self) -> tuple[str | None, str]:
        """复刻 config.gradle 的三级回退链，返回 (libs_path, 命中来源说明)。"""
        props = self._read_properties()
        libs = props.get("cosmic_libs_path")
        if libs:
            return libs, "gradle.properties systemProp.cosmic_libs_path"
        home = props.get("cosmic_home")
        if home:
            return f"{home}/mservice-cosmic/lib", "gradle.properties systemProp.cosmic_home"
        env_home = os.environ.get("COSMIC_HOME")
        if env_home:
            return f"{env_home}/mservice-cosmic/lib", "环境变量 COSMIC_HOME"
        return None, ""

    def _parse_config_subdirs(self, warnings: list[str]) -> list[str]:
        """config.gradle 的 ext.path 表 → 子目录名清单；解析不出退固定五目录。
        outputdir（工程自身构建输出）始终排除。"""
        f = self.root / "config.gradle"
        if not f.is_file():
            warnings.append("config.gradle 不存在，用固定子目录 bos/trd/biz/cus/ext")
            return list(_GRADLE_DEFAULT_SUBDIRS)
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            warnings.append("config.gradle 读取失败，用固定子目录 bos/trd/biz/cus/ext")
            return list(_GRADLE_DEFAULT_SUBDIRS)
        # 形如  trd : "${cosmic_libs_path}/trd"
        pairs = re.findall(r'(\w+)\s*:\s*"\$\{cosmic_libs_path\}/([\w./-]+)"', text)
        subs = [sub for name, sub in pairs if name != "outputdir"]
        if not subs:
            warnings.append("config.gradle 未解析出子目录表，用固定子目录 bos/trd/biz/cus/ext")
            return list(_GRADLE_DEFAULT_SUBDIRS)
        return subs

    def _parse_modules(self, warnings: list[str]) -> list[ModuleInfo]:
        """settings.gradle include(...) → 模块清单；各模块 build.gradle project(':x') → 依赖边。"""
        settings = self.root / "settings.gradle"
        modules: list[ModuleInfo] = []
        if not settings.is_file():
            return modules
        try:
            text = settings.read_text(encoding="utf-8", errors="replace")
        except OSError:
            warnings.append("settings.gradle 读取失败，模块清单缺失")
            return modules

        names: list[str] = []
        # include(...) 括号形 与 include 'a', 'b' 语句形都收
        for m in re.finditer(r"include\s*\(([^)]*)\)", text, re.S):
            names.extend(re.findall(r"""['"]([^'"]+)['"]""", m.group(1)))
        for m in re.finditer(r"^\s*include\s+([^\n(]+)$", text, re.M):
            names.extend(re.findall(r"""['"]([^'"]+)['"]""", m.group(1)))

        seen: set[str] = set()
        for raw in names:
            name = raw.lstrip(":")
            if not name or name in seen:
                continue
            seen.add(name)
            mod_dir = self.root / Path(*name.split(":"))
            mod = ModuleInfo(name=name)
            src = mod_dir / "src" / "main" / "java"
            if src.is_dir():
                mod.source_roots.append(str(src.resolve()))
            else:
                warnings.append(f"模块 {name} 无 src/main/java 源码根：{src}")
            bg = mod_dir / "build.gradle"
            if bg.is_file():
                try:
                    bg_text = bg.read_text(encoding="utf-8", errors="replace")
                    mod.depends_on = [d.lstrip(":") for d in re.findall(
                        r"""project\s*\(\s*['"](:?[^'"]+)['"]\s*\)""", bg_text)]
                except OSError:
                    pass
            modules.append(mod)
        return modules


# ── 编排：优先级 + attempts 诊断轨迹 ──────────────────────────────


class _AdapterFailed(Exception):
    """适配器识别到了工程形态但解析失败（败因进 attempts）。"""


def discover_classpath(project_root: str | os.PathLike,
                       explicit_dirs: list[str] | None = None) -> ClasspathResult:
    """类路径发现入口。优先级：显式 explicit_dirs > IDEA > Gradle 模板 > status="none"。

    - 显式目录：递归枚举 *.jar（使用者手工指定时通常给的是 jar farm 根）；
      不产模块图（源码根由 build 管线自己传）。存在的目录才收，全不存在继续走自动探测。
    - 无论命中与否，attempts 都记全三条轨迹（命中后靠后的记 skipped），
      "为什么是/不是这个适配器"要能从结果里读出来。
    """
    root = Path(project_root).resolve()
    attempts: list[Attempt] = []
    result: ClasspathResult | None = None

    # 1) 显式兜底优先（用户口头指定 > 一切启发式）
    if explicit_dirs:
        dirs: list[JarDir] = []
        warnings: list[str] = []
        for d in explicit_dirs:
            p = Path(d)
            if p.is_dir():
                dirs.append(JarDir(path=str(p.resolve()), recursive=True))
            else:
                warnings.append(f"--classpath-dir 目录不存在，跳过：{d}")
        if dirs:
            attempts.append(Attempt("explicit", "ok", f"显式指定 {len(dirs)} 个 jar 目录（递归枚举）"))
            result = ClasspathResult(
                status="ok", provider="explicit", project_root=str(root),
                jar_dirs=dirs, warnings=warnings, attempts=[])
        else:
            attempts.append(Attempt("explicit", "failed",
                                    "显式指定的目录全部不存在：" + ", ".join(explicit_dirs)))
    else:
        attempts.append(Attempt("explicit", "skipped", "未指定 --classpath-dir"))

    # 2) IDEA → 3) Gradle 模板
    for adapter in (IdeaClasspathAdapter(root), GradleCosmicTemplateAdapter(root)):
        if result is not None:
            attempts.append(Attempt(adapter.provider, "skipped", "已由更高优先级来源命中"))
            continue
        if not adapter.detect():
            marker = ".idea/ 或 *.iml" if adapter.provider == "idea" else "settings.gradle"
            attempts.append(Attempt(adapter.provider, "skipped", f"未发现工程标志（{marker}）"))
            continue
        try:
            result = adapter.parse()
            attempts.append(Attempt(
                adapter.provider, "ok",
                f"模块 {len(result.modules)} 个，jar 目录 {len(result.jar_dirs)} 个"
                + (f"，单 jar {len(result.jar_files)} 个" if result.jar_files else "")))
        except _AdapterFailed as exc:
            attempts.append(Attempt(adapter.provider, "failed", str(exc)))
        except Exception as exc:  # 适配器内部意外崩溃也不许炸穿（软降级红线）
            attempts.append(Attempt(adapter.provider, "failed",
                                    f"适配器异常：{type(exc).__name__}: {exc}"))

    if result is None:
        return ClasspathResult(status="none", provider=None, project_root=str(root),
                               attempts=attempts)

    result.attempts = attempts
    result.jar_count = count_jars(result.jar_dirs, result.jar_files)
    return result

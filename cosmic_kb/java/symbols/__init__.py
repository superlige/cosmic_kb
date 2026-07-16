"""symbols —— 编译期符号解析层（阶段 12，设计见 docs/设计方案/跨类调用链解析与编译期符号方案.md）。

在 tree-sitter 名字启发式之上叠一层**确定性类型绑定**：不执行任何构建，只做
① 类路径发现（读 IDEA/.iml 或金蝶官方 Gradle 模板的依赖声明，纯文本解析）+
② 本地 jar 绑定（JVM 微工具 = JavaParser Symbol Solver，随包 vendor/symsolver.jar）。
符号解不出一律退回名字匹配并如实标注来源（处处置信度），任一环节缺失整体软降级。

模块：classpath.py（类路径发现 + attempts 诊断轨迹）、runner.py（java 探测 +
subprocess JSONL 流式编排 + 看门狗，RunOutcome 永不上抛）、table.py（SymbolTable
(relpath,line,name) 索引 + col 消歧 + 覆盖率统计）。
12.1 只交付"喂路径收 JSON"的可独立验证组件；12.2 注入 java/ 管线（schema v18）；
12.3 call_edge 持久化 + callers 反查工具。
"""

from .classpath import (  # noqa: F401
    Attempt,
    ClasspathResult,
    JarDir,
    ModuleInfo,
    count_jars,
    discover_classpath,
)
from .runner import (  # noqa: F401
    PROTOCOL_VERSION,
    RunOutcome,
    build_request,
    find_java,
    java_error,
    request_from_classpath,
    run,
    symsolver_jar,
    to_java_charset,
)
from .table import FileSymbols, SymbolSite, SymbolTable  # noqa: F401

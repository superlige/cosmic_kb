package cosmic.symsolver;

import com.eclipsesource.json.JsonObject;
import com.github.javaparser.Position;
import com.github.javaparser.ast.body.TypeDeclaration;
import com.github.javaparser.ast.expr.Expression;
import com.github.javaparser.ast.expr.MethodCallExpr;
import com.github.javaparser.ast.expr.MethodReferenceExpr;
import com.github.javaparser.resolution.MethodUsage;
import com.github.javaparser.resolution.UnsolvedSymbolException;
import com.github.javaparser.resolution.declarations.ResolvedMethodDeclaration;
import com.github.javaparser.resolution.declarations.ResolvedReferenceTypeDeclaration;
import com.github.javaparser.resolution.types.ResolvedType;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * 两层符号解析（spike 已验证的策略，见设计方案文档"精确解析验证"节）：
 *
 * ① 表达式级 {@code .resolve()} 优先 —— 多数场景直接给出精确 FQN + 签名（resolution=expr）。
 * ② 失败退化：解析 scope/限定符的类型（无 scope 用所在类兜底），在该已确定类型的
 *    全部方法（含祖先）里按 名字 + 参数个数（方法引用不按 argc）筛候选 ——
 *    唯一命中 → resolution=scope（仍是确定性类型绑定，不是名字启发式）；
 *    多候选 → failed/ambiguous；零候选 → failed/unsolved-symbol。
 *
 * 处处置信度：解不出就如实 failed + reason，绝不臆造。
 */
final class Resolver {

    // ── 调用点入口 ─────────────────────────────────────────────

    /** 普通方法调用 a.foo(x)：kind=invocation，argc=实参个数。 */
    static JsonObject resolveInvocation(MethodCallExpr call) {
        JsonObject site = new JsonObject();
        fillCallPosition(site, call);
        site.add("name", call.getNameAsString());
        site.add("kind", "invocation");
        int argc = call.getArguments().size();
        site.add("argc", argc);

        Throwable exprError;
        try {
            ResolvedMethodDeclaration rmd = call.resolve();
            fillResolved(site, rmd, "expr");
            return site;
        } catch (Throwable t) {
            exprError = t;
        }
        // 层②：scope 类型兜底（无 scope 的裸调用 → 所在类，含继承来的方法）
        try {
            ResolvedReferenceTypeDeclaration scopeType = resolveScopeType(
                    call.getScope().orElse(null), call);
            if (scopeType == null) {
                fillFailure(site, classify(exprError));
                return site;
            }
            pickCandidate(site, scopeType, call.getNameAsString(), argc, exprError);
        } catch (Throwable t) {
            fillFailure(site, classify(exprError));
        }
        return site;
    }

    /** 方法引用 Class::method / obj::method：kind=method_reference，argc=null（无实参可解析）。 */
    static JsonObject resolveMethodReference(MethodReferenceExpr ref) {
        JsonObject site = new JsonObject();
        fillRefPosition(site, ref);
        site.add("name", ref.getIdentifier());
        site.add("kind", "method_reference");

        Throwable exprError;
        try {
            ResolvedMethodDeclaration rmd = ref.resolve();
            fillResolved(site, rmd, "expr");
            return site;
        } catch (Throwable t) {
            // 已知局限：泛型推断上下文（如 Collectors.groupingBy 的分类函数）里 resolve 常败
            exprError = t;
        }
        try {
            ResolvedReferenceTypeDeclaration scopeType = resolveScopeType(ref.getScope(), ref);
            if (scopeType == null) {
                fillFailure(site, classify(exprError));
                return site;
            }
            pickCandidate(site, scopeType, ref.getIdentifier(), -1, exprError);
        } catch (Throwable t) {
            fillFailure(site, classify(exprError));
        }
        return site;
    }

    // ── 层②：scope 类型 + 候选筛选 ────────────────────────────

    /** scope 表达式 → 已解析类型声明；scope 缺失（裸调用）→ 所在类型声明。解不出返回 null。 */
    private static ResolvedReferenceTypeDeclaration resolveScopeType(
            Expression scope, com.github.javaparser.ast.Node at) {
        if (scope != null) {
            ResolvedType rt = scope.calculateResolvedType();
            if (rt.isReferenceType()) {
                Optional<ResolvedReferenceTypeDeclaration> td =
                        rt.asReferenceType().getTypeDeclaration();
                return td.orElse(null);
            }
            return null; // 数组/基本类型等：调用面极窄，诚实放弃
        }
        Optional<TypeDeclaration> host = at.findAncestor(TypeDeclaration.class);
        if (host.isPresent()) {
            Object resolved = host.get().resolve();
            if (resolved instanceof ResolvedReferenceTypeDeclaration) {
                return (ResolvedReferenceTypeDeclaration) resolved;
            }
        }
        return null;
    }

    /**
     * 在已确定类型上按 名字（+ 参数个数，argc&lt;0 表示方法引用不筛）挑唯一候选。
     * 候选先按全限定签名去重（接口 + 实现类沿祖先链会把同一方法给两遍）。
     */
    private static void pickCandidate(JsonObject site, ResolvedReferenceTypeDeclaration scopeType,
                                      String name, int argc, Throwable exprError) {
        Map<String, ResolvedMethodDeclaration> candidates =
                new LinkedHashMap<String, ResolvedMethodDeclaration>();
        for (ResolvedMethodDeclaration rmd : allMethods(scopeType)) {
            if (!rmd.getName().equals(name)) {
                continue;
            }
            if (argc >= 0 && !arityMatches(rmd, argc)) {
                continue;
            }
            String key;
            try {
                key = rmd.getQualifiedSignature();
            } catch (Throwable t) { // 个别泛型签名算不出：退化用身份去重
                key = rmd.getClass().getName() + "@" + System.identityHashCode(rmd);
            }
            candidates.put(key, rmd);
        }
        if (candidates.size() == 1) {
            fillResolved(site, candidates.values().iterator().next(), "scope");
        } else if (candidates.size() > 1) {
            fillFailure(site, "ambiguous");
        } else {
            fillFailure(site, classify(exprError));
        }
    }

    /** 类型的全部方法（含祖先）。getAllMethods 遇祖先解析失败时退回本类声明方法。 */
    private static List<ResolvedMethodDeclaration> allMethods(ResolvedReferenceTypeDeclaration type) {
        List<ResolvedMethodDeclaration> out = new ArrayList<ResolvedMethodDeclaration>();
        try {
            for (MethodUsage mu : type.getAllMethods()) {
                out.add(mu.getDeclaration());
            }
        } catch (Throwable t) {
            try {
                out.addAll(type.getDeclaredMethods());
            } catch (Throwable ignored) {
                // 连本类方法都枚举不出（极端损坏 jar）：返回空 → unsolved-symbol
            }
        }
        return out;
    }

    private static boolean arityMatches(ResolvedMethodDeclaration rmd, int argc) {
        try {
            int params = rmd.getNumberOfParams();
            if (params == argc) {
                return true;
            }
            return rmd.hasVariadicParameter() && argc >= params - 1;
        } catch (Throwable t) {
            return false;
        }
    }

    // ── 结果填充 ───────────────────────────────────────────────

    /** 解析成功：declaring FQN / 签名 / static / target_kind（按声明实现类归类 project|jar|jdk）。 */
    private static void fillResolved(JsonObject site, ResolvedMethodDeclaration rmd,
                                     String resolution) {
        site.add("resolution", resolution);
        try {
            site.add("declaring", rmd.declaringType().getQualifiedName());
        } catch (Throwable t) {
            site.add("declaring", rmd.getClassName());
        }
        try {
            site.add("signature", rmd.getQualifiedSignature());
        } catch (Throwable t) {
            site.add("signature", rmd.getName());
        }
        try {
            site.add("static", rmd.isStatic());
        } catch (Throwable ignored) {
        }
        site.add("target_kind", targetKind(rmd));
    }

    /**
     * 目标来源归类：解析结果的实现类就带着来源信息 ——
     * javaparsermodel（源码 TypeSolver）→ project；reflectionmodel（JDK 反射）→ jdk；
     * javassistmodel（JarTypeSolver）→ jar。
     */
    private static String targetKind(ResolvedMethodDeclaration rmd) {
        String impl = rmd.getClass().getName();
        if (impl.contains("javaparsermodel")) {
            return "project";
        }
        if (impl.contains("reflectionmodel")) {
            return "jdk";
        }
        if (impl.contains("javassistmodel")) {
            return "jar";
        }
        return "project"; // JavaParser AST 侧其余声明实现均来自源码解析
    }

    private static void fillFailure(JsonObject site, String reason) {
        site.add("resolution", "failed");
        site.add("reason", reason);
    }

    /** 意外崩溃（含 StackOverflowError）时的最小可用 site（Main 兜底路径用）。 */
    static JsonObject internalFailure(String name, String kind, Throwable t) {
        JsonObject site = new JsonObject();
        site.add("name", name);
        site.add("kind", kind);
        site.add("resolution", "failed");
        site.add("reason", "internal");
        return site;
    }

    /** 失败归因 → 协议 reason 码（unsolved-symbol | ambiguous | generic-inference | internal）。 */
    private static String classify(Throwable t) {
        if (t == null) {
            return "internal";
        }
        String cls = t.getClass().getName();
        if (cls.contains("MethodAmbiguity")) {
            return "ambiguous";
        }
        if (t instanceof UnsolvedSymbolException) {
            String msg = String.valueOf(t.getMessage());
            // 泛型通配符/约束擦除是 Symbol Solver 已知局限（spike 归因过的同款消息）
            if (msg.contains("constraint")) {
                return "generic-inference";
            }
            return "unsolved-symbol";
        }
        if (t instanceof StackOverflowError) {
            return "internal";
        }
        String msg = String.valueOf(t.getMessage());
        if (msg.contains("constraint") || msg.contains("inference")) {
            return "generic-inference";
        }
        return "unsolved-symbol";
    }

    // ── 位置：方法名标识符起点，1-based 字符列 ───────────────────

    /** 普通调用：名字节点自带精确 Range。 */
    static void fillCallPosition(JsonObject site, MethodCallExpr call) {
        Optional<Position> begin = call.getName().getBegin();
        if (begin.isPresent()) {
            site.add("line", begin.get().line);
            site.add("col", begin.get().column);
        } else {
            site.add("line", -1);
            site.add("col", -1);
        }
    }

    /** 方法引用：identifier 是裸字符串无节点，从表达式末端倒推起点（标识符不含换行，安全）。 */
    static void fillRefPosition(JsonObject site, MethodReferenceExpr ref) {
        Optional<Position> end = ref.getEnd();
        if (end.isPresent()) {
            site.add("line", end.get().line);
            site.add("col", end.get().column - ref.getIdentifier().length() + 1);
        } else {
            site.add("line", -1);
            site.add("col", -1);
        }
    }

    private Resolver() {
    }
}

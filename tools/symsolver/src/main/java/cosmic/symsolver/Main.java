package cosmic.symsolver;

import com.eclipsesource.json.Json;
import com.eclipsesource.json.JsonArray;
import com.eclipsesource.json.JsonObject;
import com.eclipsesource.json.JsonValue;
import com.github.javaparser.JavaParser;
import com.github.javaparser.ParseResult;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.expr.MethodCallExpr;
import com.github.javaparser.ast.expr.MethodReferenceExpr;
import com.github.javaparser.symbolsolver.JavaSymbolSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.CombinedTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.JarTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.JavaParserTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.ReflectionTypeSolver;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.PrintStream;
import java.nio.ByteBuffer;
import java.nio.charset.Charset;
import java.nio.charset.CharsetDecoder;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import java.util.TreeMap;

/**
 * symsolver 主入口：stdin 单 JSON 请求 → stdout JSONL 事件流。
 *
 * 事件时序（协议 v1）：start → solver_ready → 逐文件 file（穿插 progress/warning）→ summary。
 * stdout 只放协议事件（显式 UTF-8，避免 Windows 默认 GBK 输出流污染）；
 * stderr 只放人读日志。任何单文件/单调用点/单 jar 的失败都被就地捕获，
 * 绝不让一个烂文件掐死整个批次 —— JSONL 流式输出即恢复边界，进程中途死掉
 * Python 侧已收到的 file 事件全部有效。
 */
public final class Main {

    static final int PROTOCOL = 1;
    private static final int PROGRESS_EVERY = 25;

    public static void main(String[] args) throws Exception {
        // 协议通道显式 UTF-8；把 System.out 重定向到 stderr，防第三方库 println 污染协议流
        PrintStream proto = new PrintStream(new FileOutputStream(FileDescriptor.out), true, "UTF-8");
        PrintStream log = new PrintStream(new FileOutputStream(FileDescriptor.err), true, "UTF-8");
        System.setOut(log);
        System.setErr(log);

        emit(proto, new JsonObject().add("event", "start").add("protocol", PROTOCOL));

        JsonObject request;
        try {
            request = Json.parse(readAll(System.in)).asObject();
        } catch (Exception e) {
            log.println("[symsolver] 请求解析失败: " + e);
            System.exit(2);
            return;
        }
        int reqProtocol = request.getInt("protocol", -1);
        if (reqProtocol != PROTOCOL) {
            log.println("[symsolver] 协议版本不符: 请求=" + reqProtocol + " 支持=" + PROTOCOL);
            System.exit(2);
            return;
        }

        long t0 = System.currentTimeMillis();

        // ── 组 CombinedTypeSolver：JDK 反射 → 各源码根（项目源码优先于 jar，防旧构建产物遮蔽）→ jar farm
        CombinedTypeSolver solver = new CombinedTypeSolver();
        solver.add(new ReflectionTypeSolver(true)); // jdkOnly：只认 java.* 等核心类，其余交给 jar
        for (JsonValue v : arr(request, "source_roots")) {
            File root = new File(v.asString());
            if (root.isDirectory()) {
                solver.add(new JavaParserTypeSolver(root));
            } else {
                emit(proto, warning("源码根不存在，跳过: " + v.asString()));
            }
        }

        List<Path> jars = collectJars(request, proto);
        int jarOk = 0, jarFailed = 0;
        for (Path jar : jars) {
            try {
                solver.add(JarTypeSolver.getJarTypeSolver(jar.toString()));
                jarOk++;
            } catch (Throwable t) { // 单 jar 损坏（非 zip/坏中央目录）跳过不崩
                jarFailed++;
                if (jarFailed <= 20) {
                    log.println("[symsolver] jar 加载失败，跳过: " + jar + " (" + t + ")");
                }
            }
        }
        emit(proto, new JsonObject().add("event", "solver_ready")
                .add("jar_count", jarOk).add("jar_failed", jarFailed)
                .add("elapsed_ms", System.currentTimeMillis() - t0));

        ParserConfiguration config = new ParserConfiguration()
                .setSymbolResolver(new JavaSymbolSolver(solver))
                .setLanguageLevel(ParserConfiguration.LanguageLevel.BLEEDING_EDGE);
        JavaParser parser = new JavaParser(config);

        // ── 逐文件解析 + 两层符号解析
        JsonArray filesReq = arr(request, "files");
        int total = filesReq.size();
        int done = 0;
        long sites = 0;
        TreeMap<String, Integer> byResolution = new TreeMap<String, Integer>();

        for (JsonValue v : filesReq) {
            JsonObject fileReq = v.asObject();
            String relpath = fileReq.getString("relpath", "");
            JsonObject event = new JsonObject().add("event", "file").add("relpath", relpath);
            try {
                JsonArray sitesJson = processFile(parser, fileReq, event);
                event.add("status", event.getString("status", "ok"));
                event.add("sites", sitesJson);
                sites += sitesJson.size();
                for (JsonValue sv : sitesJson) {
                    String res = sv.asObject().getString("resolution", "failed");
                    Integer n = byResolution.get(res);
                    byResolution.put(res, n == null ? 1 : n + 1);
                }
            } catch (Throwable t) { // 含 StackOverflowError：一个病态文件不掐死批次
                event.set("status", "internal-error");
                event.add("note", String.valueOf(t));
                event.add("sites", new JsonArray());
            }
            emit(proto, event);
            done++;
            if (done % PROGRESS_EVERY == 0 || done == total) {
                emit(proto, new JsonObject().add("event", "progress")
                        .add("done", done).add("total", total));
            }
        }

        JsonObject byRes = new JsonObject();
        for (String key : byResolution.keySet()) {
            byRes.add(key, byResolution.get(key));
        }
        emit(proto, new JsonObject().add("event", "summary")
                .add("files", total).add("sites", sites)
                .add("by_resolution", byRes)
                .add("elapsed_ms", System.currentTimeMillis() - t0));
    }

    /** 单文件：按 Python 侧探测的编码解码（解不动退 UTF-8）→ 解析 → 提取全部调用点。 */
    private static JsonArray processFile(JavaParser parser, JsonObject fileReq, JsonObject event) {
        JsonArray sitesJson = new JsonArray();
        String path = fileReq.getString("path", "");
        String encoding = fileReq.getString("encoding", "UTF-8");

        byte[] raw;
        try {
            raw = Files.readAllBytes(Paths.get(path));
        } catch (Exception e) {
            event.set("status", "io-error");
            event.add("note", String.valueOf(e));
            return sitesJson;
        }
        String content = decode(raw, encoding, event);

        ParseResult<CompilationUnit> pr = parser.parse(content);
        if (!pr.getResult().isPresent()) {
            event.set("status", "parse-error");
            if (!pr.getProblems().isEmpty()) {
                event.add("note", pr.getProblems().get(0).getMessage());
            }
            return sitesJson;
        }
        CompilationUnit cu = pr.getResult().get();

        for (MethodCallExpr call : cu.findAll(MethodCallExpr.class)) {
            JsonObject site;
            try {
                site = Resolver.resolveInvocation(call);
            } catch (Throwable t) {
                site = Resolver.internalFailure(call.getNameAsString(), "invocation", t);
                Resolver.fillCallPosition(site, call);
            }
            sitesJson.add(site);
        }
        for (MethodReferenceExpr ref : cu.findAll(MethodReferenceExpr.class)) {
            JsonObject site;
            try {
                site = Resolver.resolveMethodReference(ref);
            } catch (Throwable t) {
                site = Resolver.internalFailure(ref.getIdentifier(), "method_reference", t);
                Resolver.fillRefPosition(site, ref);
            }
            sitesJson.add(site);
        }
        return sitesJson;
    }

    /** 按给定 charset 解码（REPLACE 容错）；charset 名不认识退 UTF-8 并标 encoding_fallback；剥 BOM。 */
    private static String decode(byte[] raw, String encoding, JsonObject event) {
        Charset cs;
        try {
            cs = Charset.forName(encoding);
        } catch (Exception e) {
            cs = StandardCharsets.UTF_8;
            event.add("encoding_fallback", true);
        }
        CharsetDecoder dec = cs.newDecoder()
                .onMalformedInput(CodingErrorAction.REPLACE)
                .onUnmappableCharacter(CodingErrorAction.REPLACE);
        String content;
        try {
            content = dec.decode(ByteBuffer.wrap(raw)).toString();
        } catch (Exception e) { // REPLACE 下理论上不会到这，防御性兜底
            content = new String(raw, StandardCharsets.UTF_8);
            if (event.get("encoding_fallback") == null) {
                event.add("encoding_fallback", true);
            }
        }
        // UTF-8 BOM 解码后是 U+FEFF 打头，JavaParser 会把它当非法 token → 剥掉
        if (!content.isEmpty() && content.charAt(0) == '﻿') {
            content = content.substring(1);
        }
        return content;
    }

    /** jar_dirs（recursive 按声明走）+ jar_files → 待加载 jar 清单。 */
    private static List<Path> collectJars(JsonObject request, PrintStream proto) {
        List<Path> jars = new ArrayList<Path>();
        for (JsonValue v : arr(request, "jar_dirs")) {
            JsonObject jd = v.asObject();
            Path dir = Paths.get(jd.getString("path", ""));
            boolean recursive = jd.getBoolean("recursive", false);
            if (!Files.isDirectory(dir)) {
                emit(proto, warning("jar 目录不存在，跳过: " + dir));
                continue;
            }
            try (java.util.stream.Stream<Path> stream = recursive ? Files.walk(dir) : Files.list(dir)) {
                stream.filter(p -> p.toString().toLowerCase().endsWith(".jar")
                        && Files.isRegularFile(p)).forEach(jars::add);
            } catch (Exception e) {
                emit(proto, warning("jar 目录枚举失败: " + dir + " (" + e + ")"));
            }
        }
        for (JsonValue v : arr(request, "jar_files")) {
            Path jar = Paths.get(v.asString());
            if (Files.isRegularFile(jar)) {
                jars.add(jar);
            } else {
                emit(proto, warning("jar 文件不存在，跳过: " + jar));
            }
        }
        return jars;
    }

    private static JsonArray arr(JsonObject obj, String name) {
        JsonValue v = obj.get(name);
        return (v != null && v.isArray()) ? v.asArray() : new JsonArray();
    }

    private static JsonObject warning(String message) {
        return new JsonObject().add("event", "warning").add("message", message);
    }

    /** JSONL：单行紧凑 JSON + \n。协议流唯一出口。 */
    private static void emit(PrintStream proto, JsonObject event) {
        proto.println(event.toString());
    }

    private static String readAll(InputStream in) throws Exception {
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        byte[] chunk = new byte[8192];
        int n;
        while ((n = in.read(chunk)) != -1) {
            buf.write(chunk, 0, n);
        }
        return new String(buf.toByteArray(), StandardCharsets.UTF_8);
    }

    private Main() {
    }
}

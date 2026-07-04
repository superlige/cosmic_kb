"""编排层：把 `--vendor` 指定的原厂 fnumber 拉取、（若有匹配的本地扩展）合并，
回填进 build/bridge 用的 `models` 列表。

串联 `metadata/extension.py::detect_extension` + `DbMetaReader` + `metadata/merge.py`，
`build`/`bridge` 只需要调 `apply_vendor_metadata` 这一个入口（见 `cli/main.py`）。
不改 `linker.link`/`project_map.module_map`/`store.build_kb`——它们本来就是对
`list[MetaModel]` 泛化操作，感知不到某个模型是本地 dym 还是原厂 DB 来的。

2026-07-03 拍板①：有本地扩展命中时，不再拿 `detect_extension` 猜出的候选 fnumber 直查
原厂——本地扩展 key 若因平台标识长度限制被截断，猜出来的候选就是错的（如
`cqkd_cas_bankjournalf_ext` 猜出 `cas_bankjournalf`，真实原厂标识其实是
`cas_bankjournalformrpt`），查不到就整个原厂实体白丢。改按扩展**自身精确**的 fnumber
走 `DbMetaReader.read_model_via_local_ext`（库内 `fmasterid→fid` 关系回溯），不受命名
截断影响；无本地扩展命中的候选（纯 ORM/操作调用发现的）字符串本就来自源码字面量，
不存在截断问题，仍走 `read_model` 直查。

2026-07-03 拍板②（性能）：`fnumbers` 一次自动摄取常有几十个候选，若像上面描述的那样
逐个调 `read_model`/`read_model_via_local_ext`，就是"一个循环、每次一条网络往返"——
候选越多、DB 网络延迟越高，摄取越慢（红线 #3：规模大，要性能）。本函数改用
`read_models_bulk`/`read_models_via_local_ext_bulk` 一次性批量取回整批候选（各自固定
2 条 `WHERE fnumber = ANY(%s)` SQL），查完后再在内存里按 `fnumbers` 顺序逐个装配
merge/alias/notice——不管候选有多少个，`apply_vendor_metadata` 每次只发 4 条 SQL。

2026-07-03 修复（同 key 重复）：无本地扩展命中的分支此前直接 `result.append(vendor)`，
未检查 `result` 里是否已有同 key 的本地模型——手动 `--vendor` 会绕过自动摄取那层
`known_keys` 过滤，若指定的 fnumber 恰好命中本地已有 key（真实是扩展但命名/InheritPath
不合乎 `detect_extension` 的 `_ext` 探测规则），旧代码会让同一 key 在 `models` 里出现
两份，建库时该单据下的实体/字段被插入两遍（真实故障：同单据同字段 `trace` 返回两条一模
一样的 occurrence）。现改为：append 前检查 `result` 里的同 key 模型，若存在则整体移除、
改走 `merge_vendor_extension` 并入（保留本地可能存在的真实定制字段/插件，同时保证最终
key 唯一），而非直接丢弃本地内容或重复追加。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..bridge import namespace
from ..metadata.extension import detect_extension
from ..metadata.merge import build_extension_alias, merge_vendor_extension, strip_vendor_plugins
from .reader import DbMetaReader

if TYPE_CHECKING:
    from ..metadata.model import MetaModel
    from .config import DbConfig


def apply_vendor_metadata(
    models: list["MetaModel"],
    fnumbers: list[str],
    config: "DbConfig",
) -> tuple[list["MetaModel"], list[str]]:
    """返回 `(更新后的 models, 提示信息列表)`。

    查不到的 fnumber 跳过并如实提示，不中断整体 build（红线 #4：不臆造、不因单个原厂
    实体查不到就让整个 build 失败）。`fnumbers` 为空时原样返回，不碰 DB（纯 opt-in）。
    """
    if not fnumbers:
        return models, []

    notices: list[str] = []
    isv_prefixes = set(namespace.discover_meta_prefixes(models))

    # 按候选 vendor fnumber 分组本地扩展模型（一个 fnumber 可能对应 0..N 个扩展 dym）。
    ext_by_fnumber: dict[str, list["MetaModel"]] = {}
    for m in models:
        candidate = detect_extension(m, isv_prefixes)
        if candidate:
            ext_by_fnumber.setdefault(candidate, []).append(m)

    # 按"有无本地扩展命中"分两组：前者走 fmasterid 关联批量查，后者走候选 fnumber 批量
    # 直查。同一批候选无论多少个，各组固定各发 2 条 SQL（不逐个循环发请求）。
    ext_key_by_fnumber: dict[str, list[str]] = {}
    plain_fnumbers: list[str] = []
    for fnumber in fnumbers:
        exts = ext_by_fnumber.get(fnumber, [])
        keys = [e.key for e in exts if e.key]
        if keys:
            ext_key_by_fnumber[fnumber] = keys
        else:
            plain_fnumbers.append(fnumber)

    all_ext_keys = [k for keys in ext_key_by_fnumber.values() for k in keys]
    with DbMetaReader(config) as reader:
        ext_vendor_by_key = reader.read_models_via_local_ext_bulk(all_ext_keys) if all_ext_keys else {}
        plain_vendor_by_fnumber = reader.read_models_bulk(plain_fnumbers) if plain_fnumbers else {}

    result = list(models)
    for fnumber in fnumbers:
        exts = ext_by_fnumber.get(fnumber, [])
        if exts:
            vendor = next(
                (ext_vendor_by_key[k] for k in ext_key_by_fnumber.get(fnumber, []) if k in ext_vendor_by_key),
                None,
            )
            if vendor is None:
                notices.append(
                    f"原厂 fnumber={fnumber!r} 的本地扩展未能通过 fmasterid 关联到母体，跳过合并"
                    "（本地扩展行不存在、fmasterid 为空，或母体行不存在）"
                )
                continue
            vendor = strip_vendor_plugins(vendor)
            merged = merge_vendor_extension(vendor, exts)
            for ext in exts:
                result.remove(ext)
                result.append(build_extension_alias(ext, vendor.key or fnumber))
            result.append(merged)
            notices.append(
                f"原厂 fnumber={fnumber!r}（母体真实标识={vendor.key!r}）"
                f"已并入 {len(exts)} 个本地扩展模型"
                f"（{', '.join(e.key or '?' for e in exts)}）"
            )
        else:
            vendor = plain_vendor_by_fnumber.get(fnumber)
            if vendor is None:
                notices.append(f"原厂 fnumber={fnumber!r} 在底层库未查到，跳过合并")
                continue
            vendor = strip_vendor_plugins(vendor)
            # 同 key 去重（2026-07-03 修复）：`fnumber` 未被 detect_extension 识别为标准
            # `_ext` 命名扩展，不代表本地一定没有同 key 模型——手动 `--vendor` 会绕过
            # 自动摄取那层 known_keys 过滤，若指定的 fnumber 恰好是本地已有 key（真实扩展但
            # 命名/InheritPath 不合乎`_ext`探测规则），直接 append 会让同一 key 在 `result`
            # 里出现两份，建库时字段/实体被插入两遍（用户实测复现：同单据同字段返回两次）。
            # 走 `merge_vendor_extension` 而非直接丢弃本地份——保留本地可能存在的真实定制
            # 字段/插件，同时确保最终 key 唯一。
            local_dupes = [m for m in result if m.key == fnumber]
            if local_dupes:
                for m in local_dupes:
                    result.remove(m)
                merged = merge_vendor_extension(vendor, local_dupes)
                result.append(merged)
                notices.append(
                    f"原厂 fnumber={fnumber!r} 本地已存在 {len(local_dupes)} 个同 key 模型"
                    "（未被识别为标准 _ext 扩展命名），已并入原厂内容而非重复追加"
                )
            else:
                result.append(vendor)
                notices.append(
                    f"原厂 fnumber={fnumber!r} 未匹配到本地扩展，作为引用实体并入（无项目插件）"
                )

    return result, notices

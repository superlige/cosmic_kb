"""编排层：项目自己（二开）ISV 的增量元数据同步——`build --db-config` 自动触发。

与 `dbmeta/integrate.py` 的区别（务必分清，两者不可混用）：
    目标      `integrate.py` 拉的是**原厂**标准单据（供扩展母体补全结构性半盲）；
              本模块拉的是**本项目自己**的二开 ISV（`fisv` 与本地元数据前缀同源，
              是同一个实体的最新版本，不是"父子"关系）。
    合并语义  `integrate.py` 遇到同 key 走 `merge_vendor_extension`（父子结构并集，
              双方字段/插件都留）；本模块遇到同 key 是"整条替换成 DB 上的最新版本"——
              旧版本里已经删掉/改名的字段继续留着就是残留脏数据，不能合并。
    适用范围  `integrate.py` 服务 build 和 bridge；本模块**只服务 build**——增量判定
              要靠 KB 的 `kb_meta` 记的上次同步水位，`bridge` 不建库、没有这个持久
              状态，做不了"增量"，不接这个功能。

增量判定用 `fmodifydate > since_ts`；`since_ts=None`（首次同步 / `--full-refresh`）
自然退化成"该 isv 下全量"，同一套查询逻辑天然覆盖"新增二开元数据"和"纯 DB 零 zip
冷启动建库"两个场景，不需要额外的全量枚举差集逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable

from .reader import DbMetaReader

if TYPE_CHECKING:
    from ..metadata.model import MetaModel
    from .config import DbConfig

# 平台厂商自己的 ISV，任何苍穹环境通用排除。区别于"环境特有的第三方模块 ISV"
# （如某次实测见到的 ysq）——那类不能硬编码，换个客户环境可能没有、也可能是别的名字。
_HARDCODED_EXCLUDED_ISV = {"kingdee"}


class IsvAmbiguousError(RuntimeError):
    """ISV 无法唯一确定：候选为空，或候选 >1 个且本地材料消歧不了。

    两种情况合并成同一个错误类型——对调用方（CLI）来说都是"需要用户显式给
    --isv"，没必要拆两种错误分别处理。
    """

    def __init__(self, candidates: list[tuple[str, int]]) -> None:
        self.candidates = candidates
        if candidates:
            listing = "、".join(f"{isv}({count}个表单)" for isv, count in candidates)
            msg = f"无法唯一确定本项目二开 ISV，候选：{listing}；请显式指定 --isv"
        else:
            msg = (
                "库里没有找到除 kingdee 外的任何 ISV，请检查 --db-config 连接配置，"
                "或显式指定 --isv"
            )
        super().__init__(msg)


def resolve_isv(
    reader: DbMetaReader, *, explicit: str | None, local_prefixes: Iterable[str] = (),
) -> str:
    """确定本项目自己的二开 ISV。

    `explicit` 给了直接采信（不查库校验——信任用户，省一次往返）。否则查库现存
    ISV 分布，排除空值 + 平台通用内建 `kingdee`；剩 1 个直接用；剩 >1 个先用
    `local_prefixes`（本地已知 key 前缀，去尾下划线）尝试匹配，唯一命中则用；
    否则/候选为空，一律 `IsvAmbiguousError`，绝不在多个候选里静默猜一个。
    """
    if explicit:
        return explicit

    counts = reader.list_isv_form_counts()
    candidates = {isv: n for isv, n in counts.items() if isv not in _HARDCODED_EXCLUDED_ISV}
    if len(candidates) == 1:
        return next(iter(candidates))

    if len(candidates) > 1:
        stripped_prefixes = {p.rstrip("_") for p in local_prefixes if p}
        matched = [isv for isv in candidates if isv in stripped_prefixes]
        if len(matched) == 1:
            return matched[0]

    ranked = sorted(candidates.items(), key=lambda kv: -kv[1])
    raise IsvAmbiguousError(ranked)


@dataclass
class SyncResult:
    """一轮同步的结果：更新后的 models、实际使用的 isv、这轮同步的水位、提示信息。"""

    models: list["MetaModel"]
    isv: str
    sync_ts: str
    notices: list[str]


def _replace_by_key(
    models: list["MetaModel"], fresh: dict[str, "MetaModel"],
) -> tuple[list["MetaModel"], tuple[int, int]]:
    """同 key 整条替换（不是 `merge_vendor_extension` 那种父子结构并集）：DB 现取的
    是这个实体的最新完整版本，旧版本里已经删掉/改名的字段留着就是残留脏数据，该
    整条丢弃而不是合并。key 不在本地已有集合里的，直接当新增 append。

    返回 `(替换后的 models, (新增数, 替换数))`。
    """
    if not fresh:
        return models, (0, 0)
    existing_keys = {m.key for m in models if m.key}
    kept = [m for m in models if not (m.key and m.key in fresh)]
    kept.extend(fresh.values())
    replaced = sum(1 for k in fresh if k in existing_keys)
    added = len(fresh) - replaced
    return kept, (added, replaced)


def sync_own_isv_metadata(
    models: list["MetaModel"],
    config: "DbConfig",
    *,
    isv: str | None,
    since_ts: str | None,
    local_prefixes: Iterable[str] = (),
) -> SyncResult:
    """按本项目二开 ISV，从底层库同步 form/entity/转换规则的变更（或全量）内容，
    整条替换进 `models`（同 key 覆盖，新 key 追加），供 build 装库前调用。
    """
    with DbMetaReader(config) as reader:
        resolved = resolve_isv(reader, explicit=isv, local_prefixes=local_prefixes)
        # 必须先拿水位、再查变更——否则同步过程中新提交的变更会被这一轮漏掉，
        # 且下一轮 since_ts 已经晚于它，永久漏同步（宁可"多拿"不可"漏拿"）。
        sync_ts = reader.server_now_iso()

        form_entity_keys = reader.list_changed_form_and_entity_keys(resolved, since_ts)
        fresh_fe = reader.read_models_bulk(form_entity_keys) if form_entity_keys else {}

        convert_ids = reader.list_changed_convert_rule_ids(resolved, since_ts)
        fresh_cr = reader.read_convert_rules_bulk(convert_ids) if convert_ids else {}

    result, (added_fe, replaced_fe) = _replace_by_key(list(models), fresh_fe)
    result, (added_cr, replaced_cr) = _replace_by_key(result, fresh_cr)

    notices = [
        f"ISV={resolved!r} 同步 form/entity：新增 {added_fe} 个、替换 {replaced_fe} 个；"
        f"转换规则：新增 {added_cr} 个、替换 {replaced_cr} 个"
    ]
    if not fresh_fe and not fresh_cr:
        notices.append(f"ISV={resolved!r} 本次未同步到任何变更")

    return SyncResult(models=result, isv=resolved, sync_ts=sync_ts, notices=notices)

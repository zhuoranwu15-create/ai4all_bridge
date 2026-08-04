"""候选实例名的运营名池与确定性选名（NAME-001）。

设计要点（对应 plan §2.7「不要做命名子流程，做运营配置 + 快照持久化」）：

- **不在线生成**。运营为每个模板配 3–5 个已审核候选名，选名只是从名池里确定性挑一个，
  因此不存在「命名失败」这种运行期错误——``naming_status`` 只在模板没配名池时是
  ``unavailable``，且这种情况下 bootstrap 仍须成功。
- **选名结果必须快照**。本模块只负责「怎么挑」，挑完由 ``universe_residents`` 落库；
  之后一律读回。所以这里的算法可以升级，已发出去的名字不会静默变化。
- **哈希必须跨进程稳定**。绝不能用内建 ``hash()``——CPython 对 str 的哈希按进程加盐
  （``PYTHONHASHSEED``），重启即变，会让「重复 bootstrap 返回同值」在快照写入前的
  竞态窗口里失效，也会让测试在不同机器上表现不一致。
"""
import hashlib
from typing import Optional, Sequence, Tuple

from app.products.mingchan.domain.companion_world.persona_catalog import (
    is_valid_display_name,
)

# 名池容量。低于 3 个失去「同一模板不同用户拿到不同实例名」的意义；高于 5 个运营审核成本
# 上升而收益递减（plan §2.7 冻结口径）。
NAME_POOL_MIN = 3
NAME_POOL_MAX = 5

# 候选命名状态。``ready`` = 已快照到具体实例名；``unavailable`` = 模板未配名池或该候选
# 在 m0049 之前就已快照，客户端按契约回落到自己的本地兜底名池。
NAMING_STATUS_READY = "ready"
NAMING_STATUS_UNAVAILABLE = "unavailable"

# 选名输入的分隔符：用不可能出现在 id / 版本号里的 US(0x1f)，避免
# ("uni_a", "tmpl_1b") 与 ("uni_a1", "tmpl_b") 拼出同一个串。
_SEPARATOR = "\x1f"


class NamePoolError(ValueError):
    """名池不合法；只在运营导入路径抛出，运行期读取不做二次校验。"""


def normalize_name_pool(names: Sequence[str]) -> Tuple[str, ...]:
    """校验并规整运营名池：3–5 个、去空白、互不相同、逐个过展示名白名单。

    复用 ``is_valid_display_name`` 而不是另立一套规则：候选名最终会成为用户看到的展示名，
    两处标准不一致就会出现「运营配了但用户改不回同样的名字」这种荒谬状态。
    """
    cleaned = tuple((str(name) or "").strip() for name in names)
    if not NAME_POOL_MIN <= len(cleaned) <= NAME_POOL_MAX:
        raise NamePoolError(
            f"name_pool requires {NAME_POOL_MIN}-{NAME_POOL_MAX} names, got {len(cleaned)}"
        )
    if len(set(cleaned)) != len(cleaned):
        raise NamePoolError("name_pool contains duplicates")
    for name in cleaned:
        if not is_valid_display_name(name):
            raise NamePoolError(f"invalid name in name_pool: {name!r}")
    return cleaned


def select_suggested_name(
    *,
    universe_id: str,
    template_id: str,
    name_pool: Sequence[str],
    name_pool_version: str,
) -> Optional[str]:
    """从名池确定性选一个实例名；名池为空返回 None（调用方据此走 unavailable）。

    输入含 ``name_pool_version``：运营换名池时版本号必须一起换，于是新世界的选名分布随之
    改变，而老世界因为已经快照过所以完全不受影响。
    """
    pool = tuple(name for name in name_pool if name)
    if not pool:
        return None
    digest = hashlib.sha256(
        _SEPARATOR.join((universe_id, template_id, name_pool_version or "")).encode("utf-8")
    ).digest()
    return pool[int.from_bytes(digest[:8], "big") % len(pool)]


def naming_status(suggested_display_name: Optional[str]) -> str:
    """候选 DTO 的 ``naming_status``；只由「有没有快照到名字」决定。"""
    return (
        NAMING_STATUS_READY
        if (suggested_display_name or "").strip()
        else NAMING_STATUS_UNAVAILABLE
    )

"""账号级八字资料工具，持久化到 MEMORY.md 的受管段。"""
import re
from datetime import datetime
from typing import Dict, TYPE_CHECKING

from app.user_profiles import read_bazi_profile, write_bazi_profile

if TYPE_CHECKING:
    from app.turn_context import TurnContext


_FIELDS = frozenset({
    "birth_date", "birth_time_text", "birth_time_precision", "birth_place",
    "gender", "calendar_type", "subject_type", "living_status",
})
_PRECISIONS = {"exact_time", "shichen", "part_of_day", "unknown"}
_ENUMS = {
    "gender": {"男", "女", "未知"},
    "calendar_type": {"solar", "lunar"},
    "subject_type": {"self", "other"},
    "living_status": {"alive", "deceased", "unknown"},
}


def _normalize_birth_date(value: str) -> str:
    normalized = re.sub(r"\s+", "", value).replace("年", "-").replace("月", "-").replace("日", "")
    normalized = normalized.replace(".", "-").replace("/", "-")
    try:
        return datetime.strptime(normalized, "%Y-%m-%d").date().isoformat()
    except ValueError as err:
        raise ValueError("birth_date 必须是完整日期，如 1994-01-25") from err


def _validate_patch(args: dict, current: Dict[str, str]) -> Dict[str, str]:
    unknown = sorted(str(key) for key in args if key not in _FIELDS)
    if unknown:
        raise ValueError("不支持的八字资料字段: " + "、".join(unknown))
    patch = {}
    for key, value in args.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} 必须是非空字符串")
        patch[key] = value.strip()
    if not patch:
        raise ValueError("没有可更新的八字资料字段")
    if "birth_date" in patch:
        patch["birth_date"] = _normalize_birth_date(patch["birth_date"])
    for field, values in _ENUMS.items():
        if field in patch and patch[field] not in values:
            raise ValueError(f"{field} 的值不合法")
    if "birth_time_precision" in patch and patch["birth_time_precision"] not in _PRECISIONS:
        raise ValueError("birth_time_precision 的值不合法")
    time_text = patch.get("birth_time_text")
    if time_text is not None and "birth_time_precision" not in patch and "birth_time_precision" not in current:
        raise ValueError("保存 birth_time_text 时必须同时提供 birth_time_precision")
    if "birth_time_precision" in patch and "birth_time_text" not in patch and "birth_time_text" not in current:
        raise ValueError("保存 birth_time_precision 时必须同时提供 birth_time_text")
    return patch


def handle_get_bazi_profile(args: dict, ctx: "TurnContext") -> dict:
    """返回当前账号的八字资料，不接受外部 account_id。"""
    return {"success": True, "profile": read_bazi_profile(ctx.account_id)}


def handle_update_bazi_profile(args: dict, ctx: "TurnContext") -> dict:
    """校验并部分更新八字资料；失败时不写入。"""
    current = read_bazi_profile(ctx.account_id)
    try:
        patch = _validate_patch(args, current)
    except ValueError as err:
        return {"success": False, "error": str(err), "profile": current}
    updated = dict(current)
    updated.update(patch)
    changed_fields = sorted(key for key in patch if current.get(key) != updated[key])
    if changed_fields:
        write_bazi_profile(ctx.account_id, updated)
    return {"success": True, "written": bool(changed_fields), "changed_fields": changed_fields, "profile": updated}


def handle_clear_bazi_profile_field(args: dict, ctx: "TurnContext") -> dict:
    """删除单个八字资料字段，其他字段保持不变。"""
    field = str(args.get("field") or "").strip()
    if field not in _FIELDS:
        return {"success": False, "error": "field 必须是支持的八字资料字段"}
    current = read_bazi_profile(ctx.account_id)
    updated = dict(current)
    existed = updated.pop(field, None) is not None
    if existed:
        write_bazi_profile(ctx.account_id, updated)
    return {"success": True, "cleared": existed, "profile": updated}


def handle_delete_bazi_profile(args: dict, ctx: "TurnContext") -> dict:
    """删除当前账号全部受管八字资料，不影响普通 MEMORY.md 内容。"""
    existing = read_bazi_profile(ctx.account_id)
    if existing:
        write_bazi_profile(ctx.account_id, {})
    return {"success": True, "deleted": bool(existing), "profile": {}}

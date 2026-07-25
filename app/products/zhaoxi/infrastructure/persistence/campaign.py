"""app.products.zhaoxi.infrastructure.persistence.campaign — 营销活码配置与注册归因快照。

见 docs/tech_design/campaign_codes_technical_design.md §1/§2。活码与个人邀请码
（referral_codes）是两套独立业务，不共表、不触发拉新奖励逻辑。

account_campaign_attribution 是注册时刻的策略快照：活码后续被编辑/下线不影响已归因
账号，只影响新注册——因此本模块不提供“重算归因”之类的函数，写入即定型。
"""
import calendar
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.db._core import _clean_text, connect
from app.time_utils import beijing_now_str

logger = logging.getLogger("ai4all")

__all__ = [
    "create_campaign_code",
    "get_campaign_code",
    "list_campaign_codes",
    "update_campaign_code",
    "validate_campaign_code",
    "increment_campaign_code_used",
    "write_campaign_attribution",
    "get_campaign_attribution",
    "apply_campaign_code_attribution",
]

_EDITABLE_FIELDS = (
    "campaign_key",
    "status",
    "valid_from",
    "expires_at",
    "mission_id",
    "onboarding_script_variant",
    "soul_preset_key",
    "ai_name_preset",
)

# 活码要拼进营销链接，限定 URL-safe 短码字符集——同时堵住管理 UI 把 code 拼进
# HTML/JS 属性时的注入面（见 campaign_codes_admin.html，不依赖前端转义兜底）。
_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ALLOWED_STATUSES = ("active", "disabled")
_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_DEFAULT_VALIDITY_MONTHS = 3


def _new_campaign_code_id() -> str:
    return f"camp_{uuid.uuid4().hex}"


def _validate_code_format(code: str) -> None:
    if not _CODE_PATTERN.match(code):
        raise ValueError("code must match ^[A-Za-z0-9_-]{1,64}$")


def _validate_status(status: Optional[str]) -> None:
    if status is not None and status not in _ALLOWED_STATUSES:
        raise ValueError(f"status must be one of {_ALLOWED_STATUSES}")


def _parse_datetime(value: Optional[str], *, field_name: str) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return datetime.strptime(value, _DATETIME_FORMAT)
    except ValueError:
        raise ValueError(f"{field_name} must match format 'YYYY-MM-DD HH:MM:SS'")


def _validate_time_window(valid_from: Optional[str], expires_at: Optional[str]) -> None:
    parsed_from = _parse_datetime(valid_from, field_name="valid_from")
    parsed_expires = _parse_datetime(expires_at, field_name="expires_at")
    if parsed_from is not None and parsed_expires is not None and parsed_from > parsed_expires:
        raise ValueError("valid_from must not be after expires_at")


def _add_months(dt: datetime, months: int) -> datetime:
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _default_expires_at(created_at: str) -> str:
    """活码默认有效期 3 个月（campaign_codes_prd.md §4.1），创建时可通过 expires_at 覆盖。"""
    created_dt = datetime.strptime(created_at, _DATETIME_FORMAT)
    return _add_months(created_dt, _DEFAULT_VALIDITY_MONTHS).strftime(_DATETIME_FORMAT)


def _validate_mission_id(mission_id: Optional[str]) -> None:
    if mission_id is None:
        return
    from app.products.zhaoxi.domain.missions.registry import MISSION_TEMPLATES

    if mission_id not in MISSION_TEMPLATES:
        raise ValueError(f"unknown mission_id: {mission_id}")


def _validate_soul_preset_key(soul_preset_key: Optional[str]) -> None:
    if soul_preset_key is None:
        return
    from app.products.zhaoxi.infrastructure.profiles import _SOUL_TEMPLATES

    if soul_preset_key not in _SOUL_TEMPLATES:
        raise ValueError(f"unknown soul_preset_key: {soul_preset_key}")


# AI 名字预设是运营自由填写的短字符串（会写进 IDENTITY.md/SOUL.md 自称），不是白名单枚举。
# 限定单行 + 长度上限，堵住多行文本注入 profile 文件的面（与活码 code 字符集限制同理）。
_MAX_AI_NAME_PRESET_LEN = 24


def _normalize_ai_name_preset(ai_name_preset: Optional[str]) -> Optional[str]:
    """校验并规范化 AI 名字预设：strip；空→None；含换行或超长→ValueError。"""
    cleaned = _clean_text(ai_name_preset)
    if not cleaned:
        return None
    if "\n" in cleaned or "\r" in cleaned:
        raise ValueError("ai_name_preset must be single line")
    if len(cleaned) > _MAX_AI_NAME_PRESET_LEN:
        raise ValueError(f"ai_name_preset must be at most {_MAX_AI_NAME_PRESET_LEN} characters")
    return cleaned


def _row_to_dict(row) -> Dict[str, Any]:
    return dict(row)


def create_campaign_code(
    *,
    code: str,
    campaign_key: str,
    status: str = "active",
    valid_from: Optional[str] = None,
    expires_at: Optional[str] = None,
    mission_id: Optional[str] = None,
    onboarding_script_variant: Optional[str] = None,
    soul_preset_key: Optional[str] = None,
    ai_name_preset: Optional[str] = None,
    created_by_admin_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    cleaned_code = _clean_text(code)
    if not cleaned_code:
        raise ValueError("code is required")
    _validate_code_format(cleaned_code)
    cleaned_campaign_key = _clean_text(campaign_key)
    if not cleaned_campaign_key:
        raise ValueError("campaign_key is required")
    _validate_status(status)
    _validate_mission_id(mission_id)
    _validate_soul_preset_key(soul_preset_key)
    normalized_ai_name_preset = _normalize_ai_name_preset(ai_name_preset)

    now = beijing_now_str()
    resolved_expires_at = expires_at if expires_at is not None else _default_expires_at(now)
    _validate_time_window(valid_from, resolved_expires_at)

    campaign_id = _new_campaign_code_id()
    with connect() as conn:
        existing = conn.execute(
            "SELECT 1 FROM campaign_codes WHERE code = ?", (cleaned_code,)
        ).fetchone()
        if existing is not None:
            raise ValueError("campaign code already exists")
        conn.execute(
            """
            INSERT INTO campaign_codes (
                id, code, campaign_key, status, valid_from, expires_at,
                mission_id, onboarding_script_variant, soul_preset_key, ai_name_preset,
                created_by_admin_user_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                campaign_id,
                cleaned_code,
                cleaned_campaign_key,
                status,
                valid_from,
                resolved_expires_at,
                mission_id,
                onboarding_script_variant,
                soul_preset_key,
                normalized_ai_name_preset,
                created_by_admin_user_id,
                now,
                now,
            ),
        )
    return get_campaign_code(code=cleaned_code)


def get_campaign_code(*, code: str) -> Optional[Dict[str, Any]]:
    cleaned_code = _clean_text(code)
    if not cleaned_code:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM campaign_codes WHERE code = ?", (cleaned_code,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_campaign_codes(*, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    safe_limit = max(1, min(int(limit or 100), 500))
    with connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM campaign_codes WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, safe_limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM campaign_codes ORDER BY created_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
    return [_row_to_dict(row) for row in rows]


def update_campaign_code(*, code: str, **fields: Any) -> Dict[str, Any]:
    cleaned_code = _clean_text(code)
    if not cleaned_code:
        raise ValueError("code is required")
    unknown = set(fields) - set(_EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"unsupported fields: {sorted(unknown)}")

    existing = get_campaign_code(code=cleaned_code)
    if existing is None:
        raise ValueError("campaign code not found")
    if not fields:
        return existing

    if "status" in fields:
        _validate_status(fields["status"])
    if "mission_id" in fields:
        _validate_mission_id(fields["mission_id"])
    if "soul_preset_key" in fields:
        _validate_soul_preset_key(fields["soul_preset_key"])
    if "ai_name_preset" in fields:
        fields["ai_name_preset"] = _normalize_ai_name_preset(fields["ai_name_preset"])
    if "valid_from" in fields or "expires_at" in fields:
        effective_valid_from = fields.get("valid_from", existing["valid_from"])
        effective_expires_at = fields.get("expires_at", existing["expires_at"])
        _validate_time_window(effective_valid_from, effective_expires_at)

    set_clause = ", ".join(f"{field} = ?" for field in fields)
    values = list(fields.values())
    now = beijing_now_str()
    with connect() as conn:
        conn.execute(
            f"UPDATE campaign_codes SET {set_clause}, updated_at = ? WHERE code = ?",
            (*values, now, cleaned_code),
        )
    return get_campaign_code(code=cleaned_code)


def validate_campaign_code(*, code: str) -> Dict[str, Any]:
    """校验活码是否可用，返回 {valid, reason?, mission_id, onboarding_script_variant, soul_preset_key}。"""
    cleaned_code = _clean_text(code)
    if not cleaned_code:
        return {"valid": False, "reason": "not_found"}
    record = get_campaign_code(code=cleaned_code)
    if record is None:
        return {"valid": False, "reason": "not_found"}
    if record["status"] != "active":
        return {"valid": False, "reason": "disabled"}
    now = beijing_now_str()
    if record.get("valid_from") and now < record["valid_from"]:
        return {"valid": False, "reason": "not_yet_valid"}
    if record.get("expires_at") and now > record["expires_at"]:
        return {"valid": False, "reason": "expired"}
    return {
        "valid": True,
        "mission_id": record.get("mission_id"),
        "onboarding_script_variant": record.get("onboarding_script_variant"),
        "soul_preset_key": record.get("soul_preset_key"),
        "ai_name_preset": record.get("ai_name_preset"),
    }


def increment_campaign_code_used(*, code: str) -> None:
    cleaned_code = _clean_text(code)
    if not cleaned_code:
        raise ValueError("code is required")
    with connect() as conn:
        conn.execute(
            "UPDATE campaign_codes SET used_count = used_count + 1, updated_at = ? WHERE code = ?",
            (beijing_now_str(), cleaned_code),
        )


def write_campaign_attribution(
    *,
    account_id: str,
    campaign_code: str,
    mission_id: Optional[str],
    onboarding_script_variant: Optional[str],
    soul_preset_key: Optional[str],
    ai_name_preset: Optional[str] = None,
) -> None:
    cleaned_account_id = _clean_text(account_id)
    if not cleaned_account_id:
        raise ValueError("account_id is required")
    cleaned_campaign_code = _clean_text(campaign_code)
    if not cleaned_campaign_code:
        raise ValueError("campaign_code is required")

    now = beijing_now_str()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO account_campaign_attribution (
                account_id, campaign_code, mission_id, onboarding_script_variant,
                soul_preset_key, ai_name_preset, attributed_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO NOTHING
            """,
            (
                cleaned_account_id,
                cleaned_campaign_code,
                mission_id,
                onboarding_script_variant,
                soul_preset_key,
                ai_name_preset,
                now,
                now,
            ),
        )


def get_campaign_attribution(*, account_id: str) -> Optional[Dict[str, Any]]:
    cleaned_account_id = _clean_text(account_id)
    if not cleaned_account_id:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM account_campaign_attribution WHERE account_id = ?",
            (cleaned_account_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def apply_campaign_code_attribution(
    *,
    account_id: str,
    campaign_code: Optional[str],
    increment_usage: bool = True,
) -> Dict[str, Any]:
    """校验营销活码并落地其注册效果：写归因快照 + 应用强制 AI 名字（IDENTITY.md）与强制 SOUL 人设。

    生产注册（app/db/billing.py，increment_usage=True）与 onboarding 调试建号
    （app/products/zhaoxi/api/debug.py，increment_usage=False，调试流量不进活码转化统计）共用此函数，
    确保调试面板忠实复现真实注册效果，避免两处逻辑抄写漂移。

    返回 {applied: bool, reason?: str, campaign_code?, mission_id?, onboarding_script_variant?,
    soul_preset_key?}。语义：
    - 空 code → applied=False, reason=empty，不处理。
    - 账号已归因过 → applied=False, reason=already_attributed，直接跳过（写入即定型，
      不重复写快照、不覆盖 SOUL）。
    - 活码无效（不存在/过期/disabled）→ 记 warning，applied=False，调用方主流程照常继续。
    - 任何意外异常 → 记 error，applied=False，fail-open，绝不阻断调用方主流程
      （活码无金钱奖励含义，不应因失效码挡住真实注册）。
    """
    cleaned_campaign_code = _clean_text(campaign_code)
    if not cleaned_campaign_code:
        return {"applied": False, "reason": "empty"}
    # 幂等/写入即定型：已归因账号不重复应用，避免二次不同活码覆盖 SOUL 与快照漂移。
    if get_campaign_attribution(account_id=account_id) is not None:
        return {"applied": False, "reason": "already_attributed"}
    try:
        validation = validate_campaign_code(code=cleaned_campaign_code)
        if not validation.get("valid"):
            logger.warning(
                "campaign_code invalid, skip attribution account=%s code=%s reason=%s",
                account_id, cleaned_campaign_code, validation.get("reason"),
            )
            return {"applied": False, "reason": validation.get("reason")}
        write_campaign_attribution(
            account_id=account_id,
            campaign_code=cleaned_campaign_code,
            mission_id=validation.get("mission_id"),
            onboarding_script_variant=validation.get("onboarding_script_variant"),
            soul_preset_key=validation.get("soul_preset_key"),
            ai_name_preset=validation.get("ai_name_preset"),
        )
        if increment_usage:
            increment_campaign_code_used(code=cleaned_campaign_code)
        ai_name_preset = validation.get("ai_name_preset")
        soul_preset_key = validation.get("soul_preset_key")
        # 先写 AI 名字进 IDENTITY.md，再渲染 SOUL——render_soul_preset 会从 IDENTITY 读取
        # AI 名字拼进人设自称，顺序反了则强制人设首轮自称仍是"我"。
        if ai_name_preset:
            from app.products.zhaoxi.infrastructure.profiles import write_ai_name_to_identity  # noqa: PLC0415 (lazy, avoid circular)
            write_ai_name_to_identity(account_id=account_id, name=ai_name_preset)
        if soul_preset_key:
            from app.products.zhaoxi.infrastructure.profiles import apply_soul_preset  # noqa: PLC0415 (lazy, avoid circular)
            apply_soul_preset(account_id=account_id, preset_name=soul_preset_key)
        return {
            "applied": True,
            "campaign_code": cleaned_campaign_code,
            "mission_id": validation.get("mission_id"),
            "onboarding_script_variant": validation.get("onboarding_script_variant"),
            "soul_preset_key": soul_preset_key,
            "ai_name_preset": ai_name_preset,
        }
    except Exception as err:
        logger.error(
            "campaign_code attribution failed account=%s code=%s error=%s",
            account_id, cleaned_campaign_code, err,
        )
        return {"applied": False, "reason": "error"}

"""阿里云文本审核 PLUS（green20220302.text_moderation_plus）封装。

仅用于入站用户内容的同步云审核。解析逻辑移植自 test_tools/test_content_detect.py，
返回统一的 MachineReviewResult（reviewer_type='cloud'）。调用失败/超时返回 level='error'，
由上层 service 降级为本地规则判定。SDK 采用惰性导入，默认关闭时无需该依赖。
"""

import json
import logging
import time
from functools import lru_cache
from typing import Any, Dict, List, Optional

from app.config import settings
from app.platform.moderation import aliyun_alerting
from app.platform.moderation.models import MachineReviewResult, max_risk_level

logger = logging.getLogger("ai4all.moderation.aliyun_review")

# 阿里云判定为“安全”的等级取值（大小写不敏感）。
_SAFE_LEVELS = {"", "none", "normal", "pass", "safe"}

# 阿里云对“未检出风险”的内容仍会返回占位标签 nonLabel（Description=未检测出风险），
# 必须当作安全，不能计入命中，否则所有正常对话都会被判 review。
_NON_RISK_LABELS = {"nonlabel"}

# 阿里云 RiskLevel/敏感等级 → 内部审核等级。
_ALIYUN_LEVEL_TO_INTERNAL = {
    "": "pass",
    "none": "pass",
    "normal": "pass",
    "pass": "pass",
    "safe": "pass",
    "low": "review",
    "medium": "review",
    "high": "block",
}

# 阿里云细分 label → 内部风险大类（见 PRD §6.4）。前缀匹配，覆盖 PLUS 的 10 大类。
_LABEL_CATEGORY_PREFIXES = (
    ("pornographic", "sexual_content"),
    ("sexual", "sexual_content"),
    ("political", "political"),
    ("violent", "violence_terror"),
    ("contraband", "contraband"),
    ("inappropriate", "inappropriate"),
    ("privacy", "privacy"),
    ("religion", "religion"),
    ("pt_", "promotion"),
    ("ad_", "promotion"),
    ("customized", "customized"),
)


def _category_for_label(label: str) -> Optional[str]:
    """把阿里云细分 label 归一到内部风险大类。"""

    normalized = str(label or "").strip().lower()
    if not normalized:
        return None
    for prefix, category in _LABEL_CATEGORY_PREFIXES:
        if normalized.startswith(prefix):
            return category
    return "other"


def _get_field(value: Any, *names: str, default: Any = None) -> Any:
    """兼容 SDK 对象（snake_case）与 to_map()（PascalCase）两种结构。"""

    if value is None:
        return default
    if isinstance(value, dict):
        for name in names:
            if name in value and value[name] is not None:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            field_value = getattr(value, name)
            if field_value is not None:
                return field_value
    if hasattr(value, "to_map"):
        mapped = value.to_map()
        for name in names:
            if name in mapped and mapped[name] is not None:
                return mapped[name]
    return default


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _normalize_level(level: Any) -> str:
    if level is None:
        return "none"
    return str(level).strip().lower() or "none"


def _redact_response(value: Any) -> Any:
    """脱敏原始响应：去掉 accountId 等无关字段，避免写入审计日志。"""

    if isinstance(value, dict):
        return {
            key: "<redacted>" if key.lower() == "accountid" else _redact_response(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_response(item) for item in value]
    return value


def _parse_moderation_data(data: Any) -> Dict[str, Any]:
    """解析阿里云 data，归一出风险等级、命中 label 和片段。"""

    risk_level = _normalize_level(_get_field(data, "risk_level", "RiskLevel", "riskLevel"))
    sensitive_level = _normalize_level(_get_field(data, "sensitive_level", "SensitiveLevel", "sensitiveLevel"))
    attack_level = _normalize_level(_get_field(data, "attack_level", "AttackLevel", "attackLevel"))

    labels: List[str] = []
    risk_words: List[str] = []

    def _append_label(label: Any) -> None:
        # 过滤 nonLabel 等“无风险”占位标签，避免把安全内容计入命中。
        if label and str(label).strip().lower() not in _NON_RISK_LABELS:
            labels.append(str(label))

    for item in _as_list(_get_field(data, "result", "Result", default=[])):
        _append_label(_get_field(item, "label", "Label"))
        word = _get_field(item, "risk_words", "RiskWords", "riskWords")
        if word:
            risk_words.append(str(word))

    for item in _as_list(_get_field(data, "sensitive_result", "SensitiveResult", "sensitiveResult", default=[])):
        _append_label(_get_field(item, "label", "Label"))
        level = _get_field(item, "sensitive_level", "SensitiveLevel", "sensitiveLevel")
        if level:
            sensitive_level = _normalize_level(level)

    for item in _as_list(_get_field(data, "attack_result", "AttackResult", "attackResult", default=[])):
        _append_label(_get_field(item, "label", "Label"))
        level = _get_field(item, "attack_level", "AttackLevel", "attackLevel")
        if level:
            attack_level = _normalize_level(level)

    blocked = (
        risk_level not in _SAFE_LEVELS
        or sensitive_level not in _SAFE_LEVELS
        or attack_level not in _SAFE_LEVELS
        or bool(labels)
    )
    return {
        "blocked": blocked,
        "risk_level": risk_level,
        "sensitive_level": sensitive_level,
        "attack_level": attack_level,
        "labels": list(dict.fromkeys(labels)),
        "risk_words": list(dict.fromkeys(risk_words)),
    }


def _aggregate_internal_level(parsed: Dict[str, Any]) -> str:
    """把阿里云三类等级映射为内部等级；命中但等级为安全时兜底 review。"""

    internal_levels = [
        _ALIYUN_LEVEL_TO_INTERNAL.get(parsed["risk_level"], "review"),
        _ALIYUN_LEVEL_TO_INTERNAL.get(parsed["sensitive_level"], "review"),
        _ALIYUN_LEVEL_TO_INTERNAL.get(parsed["attack_level"], "review"),
    ]
    level = max_risk_level(internal_levels)
    if parsed["blocked"] and level == "pass":
        level = "review"
    return level


def _build_categories(parsed: Dict[str, Any]) -> List[str]:
    """生成 categories：保留阿里云原始 label，并附内部大类（cat: 前缀）。"""

    categories: List[str] = []
    for label in parsed["labels"]:
        categories.append(f"cloud:{label}")
        mapped = _category_for_label(label)
        if mapped:
            categories.append(f"cat:{mapped}")
    return list(dict.fromkeys(categories))


@lru_cache(maxsize=4)
def _build_client(access_key_id: str, access_key_secret: str, endpoint: str, timeout_ms: int):
    """按 (ak, sk, endpoint) 进程级缓存阿里云客户端，避免每条消息重建连接。"""

    from alibabacloud_green20220302.client import Client
    from alibabacloud_tea_openapi import models as open_api_models

    config = open_api_models.Config(
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
        endpoint=endpoint,
    )
    config.read_timeout = timeout_ms
    config.connect_timeout = timeout_ms
    return Client(config)


def _error_result(*, reason: str, error: str, latency_ms: Optional[int] = None, raw: Optional[Dict[str, Any]] = None) -> MachineReviewResult:
    return MachineReviewResult(
        reviewer_type="cloud",
        engine="aliyun_text_moderation_plus",
        engine_version=str(getattr(settings, "moderation_aliyun_service", "") or ""),
        level="error",
        reason=reason,
        error=error,
        latency_ms=latency_ms,
        raw_result=raw or {},
    )


def review_text_with_aliyun(
    *,
    account_id: str,
    text: str,
    data_id: str,
) -> MachineReviewResult:
    """同步调用阿里云文本审核 PLUS，返回归一结果；失败/超时返回 level='error'。

    在运行时失败/成功上报失败率监控；配置缺失（not_configured）不计入失败率，避免误配长期刷屏。
    """

    result = _review_text_with_aliyun_impl(account_id=account_id, text=text, data_id=data_id)
    if result.error != "moderation_aliyun_not_configured":
        try:
            aliyun_alerting.record_outcome(
                success=result.error is None,
                error=result.error,
                account_id=account_id,
            )
        except Exception as err:  # 监控/告警异常不得影响审核结果
            logger.warning("aliyun moderation alerting record failed error=%s", err)
    return result


def _review_text_with_aliyun_impl(
    *,
    account_id: str,
    text: str,
    data_id: str,
) -> MachineReviewResult:
    access_key_id = str(getattr(settings, "aliyun_access_key_id", "") or "").strip()
    access_key_secret = str(getattr(settings, "aliyun_access_key_secret", "") or "").strip()
    endpoint = str(getattr(settings, "moderation_aliyun_endpoint", "") or "").strip()
    service = str(getattr(settings, "moderation_aliyun_service", "") or "").strip()
    try:
        timeout_ms = int(getattr(settings, "moderation_aliyun_timeout_ms", 1000))
    except (TypeError, ValueError):
        timeout_ms = 1000

    if not access_key_id or not access_key_secret or not endpoint or not service:
        return _error_result(
            reason="阿里云审核已启用但凭证或 endpoint/service 未配置",
            error="moderation_aliyun_not_configured",
        )

    started = time.monotonic()
    try:
        from alibabacloud_green20220302 import models as green_models

        client = _build_client(access_key_id, access_key_secret, endpoint, timeout_ms)
        service_parameters = {
            "content": text,
            "dataId": data_id,
            "userId": account_id,
        }
        request = green_models.TextModerationPlusRequest(
            service=service,
            service_parameters=json.dumps(service_parameters, ensure_ascii=False),
        )
        response = client.text_moderation_plus(request)
    except Exception as err:  # SDK 异常、网络超时等统一降级
        latency_ms = int((time.monotonic() - started) * 1000)
        logger.warning(
            "aliyun moderation call failed account=%s latency_ms=%s error=%s",
            account_id,
            latency_ms,
            err,
        )
        return _error_result(
            reason="阿里云审核调用异常",
            error="moderation_aliyun_request_failed",
            latency_ms=latency_ms,
            raw={"error": str(err)[:500]},
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    status_code = getattr(response, "status_code", None)
    if status_code != 200:
        return _error_result(
            reason=f"阿里云审核 HTTP 异常 status={status_code}",
            error=f"moderation_aliyun_http_{status_code}",
            latency_ms=latency_ms,
        )

    body = getattr(response, "body", None)
    code = _get_field(body, "code", "Code")
    if code != 200:
        message = _get_field(body, "message", "Message")
        return _error_result(
            reason=f"阿里云审核业务异常 code={code} msg={message}",
            error=f"moderation_aliyun_code_{code}",
            latency_ms=latency_ms,
        )

    data = _get_field(body, "data", "Data")
    if data is None:
        return _error_result(
            reason="阿里云审核返回 data 为空",
            error="moderation_aliyun_empty_data",
            latency_ms=latency_ms,
        )

    parsed = _parse_moderation_data(data)
    level = _aggregate_internal_level(parsed)
    categories = _build_categories(parsed)
    mapped = data.to_map() if hasattr(data, "to_map") else data
    logger.info(
        "aliyun moderation done account=%s level=%s labels=%s latency_ms=%s",
        account_id,
        level,
        parsed["labels"],
        latency_ms,
    )
    return MachineReviewResult(
        reviewer_type="cloud",
        engine="aliyun_text_moderation_plus",
        engine_version=service,
        level=level,
        categories=categories,
        confidence=None,
        matched_terms=[{"label": label} for label in parsed["labels"]],
        reason="阿里云文本审核命中" if parsed["blocked"] else "阿里云文本审核通过",
        raw_result={
            "risk_level": parsed["risk_level"],
            "sensitive_level": parsed["sensitive_level"],
            "attack_level": parsed["attack_level"],
            "labels": parsed["labels"],
            "data": _redact_response(mapped),
        },
        latency_ms=latency_ms,
    )

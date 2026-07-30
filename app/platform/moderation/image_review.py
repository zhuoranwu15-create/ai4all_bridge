"""阿里云图片审核（green20220302.image_moderation）封装。

与文本侧 :mod:`app.platform.moderation.aliyun_review` 是同一家云服务、同一套 label 口径，
因此解析件（label→内部大类、等级映射、响应脱敏、客户端缓存）直接复用那边的实现，
**不另写一份**——两份映射一旦漂移，同一个 label 在文本与图片链路上会归出不同大类。

一个绕不开的部署约束：阿里云 ``ImageModeration`` 的 ``serviceParameters`` 只接受
``imageUrl``（公网可取）或 OSS 对象，**不收字节流**。我们的图落在本机磁盘、只经签名 URL
暴露，所以调用方必须先签出一个绝对 URL 交给阿里云回源取图。这就是
``MEDIA_PUBLIC_BASE_URL`` 存在的唯一理由，见
``docs/ops/platform/image_moderation_setup.md``。

三重门控，缺一即"未配置"，链路整体不阻塞、不报错（D-7 先发后审）：

1. ``MODERATION_IMAGE_SAFETY_ENABLED``
2. ``MODERATION_IMAGE_SAFETY_MODEL``（阿里云图片审核 2.0 的 service 场景码）
3. ``ALIYUN_ACCESS_KEY_ID`` / ``ALIYUN_ACCESS_KEY_SECRET`` / ``MODERATION_ALIYUN_ENDPOINT``
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

from app.config import settings
from app.platform.moderation.aliyun_review import (
    _ALIYUN_LEVEL_TO_INTERNAL,
    _NON_RISK_LABELS,
    _SAFE_LEVELS,
    _build_client,
    _category_for_label,
    _get_field,
    _normalize_level,
    _redact_response,
)
from app.platform.moderation.models import MachineReviewResult, max_risk_level

logger = logging.getLogger("ai4all.moderation.image_review")

ENGINE = "aliyun_image_moderation"

# 未配置时的既有错误码，S4 前后逐字不变（运维按这个串判"能力没开"）。
ERROR_NOT_CONFIGURED = "moderation_image_safety_not_configured"
ERROR_REQUEST_FAILED = "moderation_image_request_failed"
ERROR_NO_IMAGE_URL = "moderation_image_no_url"


def _timeout_ms() -> int:
    """图片回源比文本慢得多，取文本超时的 5 倍兜底（文本默认 1s → 图片 5s）。"""
    try:
        base = int(getattr(settings, "moderation_aliyun_timeout_ms", 1000))
    except (TypeError, ValueError):
        base = 1000
    return max(base, 1) * 5


def _aliyun_credentials() -> Optional[tuple]:
    """齐备则返回 ``(ak, sk, endpoint, service)``，缺任一项返回 None。"""
    access_key_id = str(getattr(settings, "aliyun_access_key_id", "") or "").strip()
    access_key_secret = str(getattr(settings, "aliyun_access_key_secret", "") or "").strip()
    endpoint = str(getattr(settings, "moderation_aliyun_endpoint", "") or "").strip()
    service = str(getattr(settings, "moderation_image_safety_model", "") or "").strip()
    if not access_key_id or not access_key_secret or not endpoint or not service:
        return None
    return access_key_id, access_key_secret, endpoint, service


def image_review_configured() -> bool:
    """图片机审此刻是否真的可用（开关 + 凭证 + 场景码全齐）。

    批处理用它决定"要不要入队"：未配置时连 ``pending`` 都不置，资产的
    ``moderation_status`` 恒为 ``skipped``，不产生任何待办堆积。
    """
    if not bool(getattr(settings, "moderation_image_safety_enabled", False)):
        return False
    return _aliyun_credentials() is not None


def _error_result(
    *,
    reason: str,
    error: str,
    latency_ms: Optional[int] = None,
    raw: Optional[Dict[str, Any]] = None,
) -> MachineReviewResult:
    return MachineReviewResult(
        reviewer_type="image_safety",
        engine=ENGINE,
        engine_version=str(getattr(settings, "moderation_image_safety_model", "") or ""),
        level="error",
        reason=reason,
        error=error,
        latency_ms=latency_ms,
        raw_result=raw or {},
    )


def _parse_image_data(data: Any) -> Dict[str, Any]:
    """解析阿里云图片 data，归一出风险等级、命中 label 与最高置信度。

    图片接口的 ``data.result`` 是「label + confidence + riskLevel」数组；``nonLabel``
    是"未检出风险"的占位标签，必须当安全，否则每张正常照片都会被判 review。
    """
    overall_level = _normalize_level(_get_field(data, "risk_level", "RiskLevel", "riskLevel"))
    levels: List[str] = [overall_level]
    labels: List[str] = []
    confidences: List[float] = []
    for item in _get_field(data, "result", "Result", default=[]) or []:
        label = _get_field(item, "label", "Label")
        if label and str(label).strip().lower() not in _NON_RISK_LABELS:
            labels.append(str(label))
            # 单个 label 自带等级，可能比整体更重；一并参与聚合，取最重的那一档。
            levels.append(
                _normalize_level(_get_field(item, "risk_level", "RiskLevel", "riskLevel"))
            )
        confidence = _get_field(item, "confidence", "Confidence")
        if confidence is not None:
            try:
                confidences.append(float(confidence))
            except (TypeError, ValueError):
                pass
    return {
        "blocked": any(level not in _SAFE_LEVELS for level in levels) or bool(labels),
        "risk_level": overall_level,
        "internal_level": max_risk_level(
            [_ALIYUN_LEVEL_TO_INTERNAL.get(level, "review") for level in levels]
        ),
        "labels": list(dict.fromkeys(labels)),
        # 阿里云图片置信度是 0~100，内部 confidence 统一 0~1。
        "confidence": (max(confidences) / 100.0) if confidences else None,
    }


def review_image_url(
    *,
    image_url: str,
    data_id: str,
    user_id: str = "",
) -> MachineReviewResult:
    """同步调用阿里云图片审核，返回归一结果；失败/超时返回 ``level='error'``。

    :param image_url: **公网可取**的绝对图片地址（阿里云侧回源下载）。
    :param data_id: 业务侧对象 id（这里传 media_id），阿里云原样回传便于对账。
    :param user_id: 可选的业务用户标识，仅用于阿里云侧的风险画像。
    """
    cleaned_url = str(image_url or "").strip()
    if not cleaned_url:
        return _error_result(reason="缺少可供阿里云回源的图片地址", error=ERROR_NO_IMAGE_URL)
    credentials = _aliyun_credentials()
    if credentials is None:
        return _error_result(
            reason="图片机审已启用但凭证或 endpoint/场景码未配置",
            error=ERROR_NOT_CONFIGURED,
        )
    access_key_id, access_key_secret, endpoint, service = credentials

    started = time.monotonic()
    try:
        from alibabacloud_green20220302 import models as green_models

        client = _build_client(access_key_id, access_key_secret, endpoint, _timeout_ms())
        service_parameters: Dict[str, Any] = {"imageUrl": cleaned_url, "dataId": data_id}
        if user_id:
            service_parameters["userId"] = user_id
        request = green_models.ImageModerationRequest(
            service=service,
            service_parameters=json.dumps(service_parameters, ensure_ascii=False),
        )
        response = client.image_moderation(request)
    except Exception as err:  # SDK 异常、网络超时等统一降级为 error，由调用方重试
        latency_ms = int((time.monotonic() - started) * 1000)
        # 不打 URL：它带签名，进日志等于把可访问凭据写进日志文件。
        logger.warning(
            "aliyun image moderation call failed data_id=%s latency_ms=%s error_type=%s",
            data_id,
            latency_ms,
            type(err).__name__,
        )
        return _error_result(
            reason="阿里云图片审核调用异常",
            error=ERROR_REQUEST_FAILED,
            latency_ms=latency_ms,
            raw={"error": str(err)[:500]},
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    status_code = getattr(response, "status_code", None)
    if status_code != 200:
        return _error_result(
            reason=f"阿里云图片审核 HTTP 异常 status={status_code}",
            error=f"moderation_image_http_{status_code}",
            latency_ms=latency_ms,
        )
    body = getattr(response, "body", None)
    code = _get_field(body, "code", "Code")
    if code != 200:
        message = _get_field(body, "msg", "Msg", "message", "Message")
        return _error_result(
            reason=f"阿里云图片审核业务异常 code={code} msg={message}",
            error=f"moderation_image_code_{code}",
            latency_ms=latency_ms,
        )
    data = _get_field(body, "data", "Data")
    if data is None:
        return _error_result(
            reason="阿里云图片审核返回 data 为空",
            error="moderation_image_empty_data",
            latency_ms=latency_ms,
        )

    parsed = _parse_image_data(data)
    level = parsed["internal_level"]
    if parsed["blocked"] and level == "pass":
        # 有命中 label 但等级落在安全档：不放过，压到 review 留待人工。
        level = "review"
    categories: List[str] = []
    for label in parsed["labels"]:
        categories.append(f"cloud:{label}")
        mapped = _category_for_label(label)
        if mapped:
            categories.append(f"cat:{mapped}")
    mapped_data = data.to_map() if hasattr(data, "to_map") else data
    logger.info(
        "aliyun image moderation done data_id=%s level=%s labels=%s latency_ms=%s",
        data_id,
        level,
        parsed["labels"],
        latency_ms,
    )
    return MachineReviewResult(
        reviewer_type="image_safety",
        engine=ENGINE,
        engine_version=service,
        level=level,
        categories=list(dict.fromkeys(categories)),
        confidence=parsed["confidence"],
        matched_terms=[{"label": label} for label in parsed["labels"]],
        reason="阿里云图片审核命中" if parsed["blocked"] else "阿里云图片审核通过",
        raw_result={
            "risk_level": parsed["risk_level"],
            "labels": parsed["labels"],
            "data": _redact_response(mapped_data),
        },
        latency_ms=latency_ms,
    )


def review_image_task(task: Dict[str, Any]) -> Optional[MachineReviewResult]:
    """审核队列 worker 的适配器：从审核任务里取图片地址再走 :func:`review_image_url`。

    只处理 ``content_kind == 'image'`` 的任务，其余返回 None（worker 据此跳过）。
    图片地址取自任务快照的 ``media.url``——微信侧入站图片带的就是可取的 URL；缺地址时
    返回 error，由 worker 按既有重试/转人工逻辑处置。
    """
    if str(task.get("content_kind") or "") != "image":
        return None
    if not bool(getattr(settings, "moderation_image_safety_enabled", False)):
        return None
    media = task.get("media") or {}
    image_url = str(media.get("url") or "").strip() if isinstance(media, dict) else ""
    return review_image_url(
        image_url=image_url,
        data_id=str(task.get("id") or ""),
        user_id=str(task.get("account_id") or ""),
    )


__all__ = [
    "ENGINE",
    "ERROR_NOT_CONFIGURED",
    "ERROR_NO_IMAGE_URL",
    "ERROR_REQUEST_FAILED",
    "image_review_configured",
    "review_image_task",
    "review_image_url",
]


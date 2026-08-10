import logging
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from app.bootstrap.product_registry import ZHAOXI_APP_ID
from app.platform.channels import get_channel_capability
from app.config import settings
from app.time_utils import beijing_naive_now, beijing_now
from app.products.zhaoxi.application.memory.session_lifecycle import business_day_for
from app.db import (
    claim_pending_outbound_message,
    create_outbound_message,
    insert_outbound_delivery_message,
    mark_outbound_message_failed,
    mark_outbound_message_sent,
    update_outbound_message_metadata,
)
from app.platform.gateways.persistence import (
    resolve_node_for_account,
    should_inline_dispatch_for_account,
)
from app.platform.moderation.sensitive_words import check_sync_guard
from app.platform.moderation.service import create_sync_block_task, enqueue_outbound_for_moderation
from app.platform.gateways.openclaw import OpenClawRateLimited, send_weixin_text
from app.products.zhaoxi.proactive.delivery.policy import (
    POLICY_VERSION,
    evaluate_outbound_policy,
    normalize_outbound_category,
)


logger = logging.getLogger("ai4all.proactive.messaging")


def record_outbound_message_sent(
    *,
    outbound_message_id: int,
    gateway_message_id: Optional[str],
    fallback: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """主动消息「已发送(sent)」的**唯一记录入口**：翻转状态为 sent + 写入账号会话时间线。

    口径对齐 get_ops_metrics（运营报告）的 ``outbound_messages.status='sent'``——即"网关未回
    业务错误"。直发路径与节点回报路径都经此一处，避免"运营报告算已发、会话时间线却没这条"
    （或反之）的漂移。会话落库带上与 turn_service 相同口径的 business_day，使主动创建的 session
    也能进入正常的每日轮转 / dreaming（否则 business_day 为空会让 session 永不轮转）。

    注意：这里的"已发送"仅代表**网关接受、未回业务错误**，并非用户端 100% 收到（受微信 24 小时
    送达窗口等影响，仍可能软丢）。当前这是可得的最准口径；日后若网关侧能提供可靠送达回执，
    应在此处收紧判断（例如仅在确信送达时才写入会话时间线）。

    insert 失败只记日志、不影响已 sent 的状态与调用方返回（保留原 bridge 容错语义）。
    """
    sent = mark_outbound_message_sent(
        outbound_message_id=outbound_message_id,
        gateway_message_id=gateway_message_id,
    ) or fallback
    if sent is None:
        return None
    try:
        business_day = business_day_for(
            beijing_now(),
            start_hour=int(
                getattr(settings, "conversation_session_business_day_start_hour", 4)
            ),
        )
        insert_outbound_delivery_message(outbound_message=sent, business_day=business_day)
    except Exception:
        logger.exception(
            "record_outbound_message_sent delivery insert failed id=%s",
            outbound_message_id,
        )
    return sent


def enqueue_proactive_text(
    *,
    account_id: str,
    channel: str,
    channel_account_id: Optional[str],
    to_user_id: str,
    session_key: Optional[str],
    source: str,
    text: str,
    idempotency_key: Optional[str] = None,
    now: Optional[datetime] = None,
    bypass_quiet_hours: bool = False,
    product_category: Optional[str] = None,
    scheduled_at: Optional[datetime] = None,
    node_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    current = now or beijing_naive_now()
    # 多机出站:解析归属节点写入 outbound_messages.node_id,供节点按 node 认领。
    # 显式 node_id 优先;否则查账号归属;再回落 default_node_id(迁移期=aliyun1)。
    # standalone 下 default_node_id 通常为空 → node_id=None,send_proactive_text 仍按 id 直发,行为不变。
    effective_node_id = (
        node_id
        or resolve_node_for_account(account_id)
        or (getattr(settings, "default_node_id", "") or None)
    )
    merged_metadata = {
        **(metadata or {}),
    }
    if bypass_quiet_hours:
        merged_metadata["bypass_quiet_hours"] = True

    category = normalize_outbound_category(
        source=source,
        product_category=product_category,
    )
    decision = evaluate_outbound_policy(
        account_id=account_id,
        category=category,
        source=source,
        scheduled_at=scheduled_at,
        now=current,
        metadata=merged_metadata,
    )
    merged_metadata.update(decision.metadata)
    merged_metadata["product_category"] = category.value
    merged_metadata["policy_version"] = POLICY_VERSION
    merged_metadata["policy_decision"] = "allowed" if decision.allowed else "blocked"
    if decision.reason:
        merged_metadata["policy_reason"] = decision.reason
        merged_metadata["policy_error"] = decision.reason
    if decision.counts:
        merged_metadata.update(decision.counts)
    if decision.next_allowed_at:
        merged_metadata["next_allowed_at"] = decision.next_allowed_at

    effective_idempotency_key = idempotency_key or f"proactive-{account_id}-{uuid.uuid4().hex}"
    if decision.allowed:
        sync_decision = check_sync_guard(
            account_id=account_id,
            app_id=ZHAOXI_APP_ID,
            text=text,
            direction="outbound",
            content_kind="text",
            source_type="outbound_message",
            source_id=effective_idempotency_key,
        )
        if not sync_decision.allowed:
            merged_metadata.update(
                {
                    "moderation_sync_blocked": True,
                    "moderation_risk_level": sync_decision.level,
                    "moderation_categories": sync_decision.categories,
                    "policy_reason": "moderation_sync_blocked",
                    "policy_error": "moderation_sync_blocked",
                }
            )
            outbound = create_outbound_message(
                account_id=account_id,
                channel=channel,
                channel_account_id=channel_account_id,
                to_user_id=to_user_id,
                session_key=session_key,
                source=source,
                text=str(getattr(settings, "moderation_blocked_placeholder", "") or "[blocked by moderation]"),
                idempotency_key=effective_idempotency_key,
                quota_date=decision.quota_date,
                status="cancelled",
                error="moderation_sync_blocked",
                product_category=category.value,
                policy_version=POLICY_VERSION,
                policy_reason="moderation_sync_blocked",
                scheduled_at=(
                    scheduled_at.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
                    if scheduled_at
                    else None
                ),
                node_id=effective_node_id,
                metadata=merged_metadata,
            )
            try:
                blocked_task = create_sync_block_task(
                    account_id=account_id,
                    app_id=ZHAOXI_APP_ID,
                    session_id=None,
                    source_type="outbound_message",
                    source_id=str(outbound["id"]),
                    direction="outbound",
                    content_kind="text",
                    text=text,
                    decision=sync_decision,
                    outbound_message_id=int(outbound["id"]),
                    metadata={
                        "source": source,
                        "product_category": category.value,
                        "to_user_id": to_user_id,
                        "idempotency_key": effective_idempotency_key,
                    },
                )
                updated = update_outbound_message_metadata(
                    outbound_message_id=int(outbound["id"]),
                    metadata_patch={"moderation_task_id": blocked_task.get("id")},
                )
                return updated or outbound
            except Exception as err:
                logger.exception(
                    "proactive sync moderation block task failed account=%s outbound_id=%s error=%s",
                    account_id,
                    outbound.get("id"),
                    err,
                )
                return outbound

    outbound = create_outbound_message(
        account_id=account_id,
        channel=channel,
        channel_account_id=channel_account_id,
        to_user_id=to_user_id,
        session_key=session_key,
        source=source,
        text=text,
        idempotency_key=effective_idempotency_key,
        quota_date=decision.quota_date,
        status=decision.status,
        error=decision.reason,
        product_category=category.value,
        policy_version=POLICY_VERSION,
        policy_reason=decision.reason,
        scheduled_at=(
            scheduled_at.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
            if scheduled_at
            else None
        ),
        node_id=effective_node_id,
        metadata=merged_metadata,
    )
    if outbound["status"] == "pending":
        try:
            enqueue_outbound_for_moderation(outbound_message=outbound, app_id=ZHAOXI_APP_ID)
        except Exception as err:
            logger.exception(
                "proactive moderation enqueue failed account=%s outbound_id=%s error=%s",
                account_id,
                outbound.get("id"),
                err,
            )
    return outbound


def send_proactive_text(
    *,
    account_id: str,
    channel: str,
    channel_account_id: Optional[str],
    to_user_id: str,
    session_key: Optional[str],
    source: str,
    text: str,
    idempotency_key: Optional[str] = None,
    now: Optional[datetime] = None,
    bypass_quiet_hours: bool = False,
    product_category: Optional[str] = None,
    scheduled_at: Optional[datetime] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    outbound = enqueue_proactive_text(
        account_id=account_id,
        channel=channel,
        channel_account_id=channel_account_id,
        to_user_id=to_user_id,
        session_key=session_key,
        source=source,
        text=text,
        idempotency_key=idempotency_key,
        now=now,
        bypass_quiet_hours=bypass_quiet_hours,
        product_category=product_category,
        scheduled_at=scheduled_at,
        metadata=metadata,
    )
    if outbound["status"] != "pending":
        return outbound

    claimed = claim_pending_outbound_message(outbound_message_id=int(outbound["id"]))
    if claimed is None:
        return outbound

    # 限速（ret=-2 / rate limited）按账号退避重试；其它异常立即落 failed。
    # 重试复用同一 idempotency_key，网关侧幂等，不会重复投递。本路径为后台主动消息，
    # 阻塞数秒可接受，不影响用户同步回复。
    max_retries = max(0, int(getattr(settings, "proactive_send_rate_limit_max_retries", 2) or 0))
    backoff_seconds = float(getattr(settings, "proactive_send_rate_limit_backoff_seconds", 3.0) or 0.0)
    result: Optional[Dict[str, Any]] = None
    for attempt in range(max_retries + 1):
        try:
            result = send_weixin_text(
                to_user_id=to_user_id,
                text=text,
                gateway_timeout_ms=settings.openclaw_gateway_call_timeout_ms,
                account_id=channel_account_id,
                idempotency_key=claimed["idempotency_key"],
                session_key=session_key,
                channel=channel,
            )
            break
        except OpenClawRateLimited as err:
            # 还有重试次数则再发；退避秒数 > 0 时先线性退避（backoff * 次数）。
            if attempt < max_retries:
                if backoff_seconds > 0:
                    time.sleep(backoff_seconds * (attempt + 1))
                continue
            failed = mark_outbound_message_failed(
                outbound_message_id=int(claimed["id"]),
                error=f"rate_limited: {err}",
            )
            if failed is None:
                raise
            return failed
        except Exception as err:
            failed = mark_outbound_message_failed(
                outbound_message_id=int(claimed["id"]),
                error=str(err),
            )
            if failed is None:
                raise
            return failed
    assert result is not None  # 循环要么 break 成功，要么在 except 内 return

    gateway_message_id = result.get("messageId") if isinstance(result, dict) else None
    sent = record_outbound_message_sent(
        outbound_message_id=int(claimed["id"]),
        gateway_message_id=str(gateway_message_id) if gateway_message_id else None,
        fallback=claimed,
    )
    return sent or claimed


def dispatch_proactive_text(
    *,
    account_id: str,
    channel: str,
    channel_account_id: Optional[str],
    to_user_id: str,
    session_key: Optional[str],
    source: str,
    text: str,
    idempotency_key: Optional[str] = None,
    now: Optional[datetime] = None,
    bypass_quiet_hours: bool = False,
    product_category: Optional[str] = None,
    scheduled_at: Optional[datetime] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """主动消息出站派发(多机)。

    - `should_inline_dispatch_for_account`(standalone,或 central+node 同机且开 inline
      **且账号归属本机 node**)→ 走 `send_proactive_text`,enqueue+claim+本机即时发送,
      行为与单机完全一致(零回归)。
    - 否则(central 非 inline,或账号归属**远程** node)→ 只 `enqueue_proactive_text`
      建 pending 行;由归属节点的出站 pull 循环 claim+send(设计附录 B.4),中心保持单写者、
      不直接发送。**关键:远程账号必须走这里,否则本机无该会话会误发失败。**

    两分支返回同形 outbound dict(状态可能为 pending/cancelled/sent 等)。
    """
    current = now or beijing_naive_now()
    # 投递 fail-fast（§8.3，原则一硬需求）:``supports_proactive=False`` 的渠道(如 web)
    # 绝不进出站发送——底层 send_weixin_text 无条件走微信网关,不看 channel 参数,若放行
    # 会把 channel="web" 的主动消息误投微信网关。V1 只有 openclaw-weixin 可投,故对微信
    # 行为等价现状。这里只拦截,不建 outbound 行(调用方经 _select_route 能力过滤后本就不
    # 应到达此分支;此为第二道防线)。
    if not get_channel_capability(channel).supports_proactive:
        logger.warning(
            "dispatch_proactive_text blocked non-proactive channel account=%s channel=%s source=%s",
            account_id,
            channel,
            source,
        )
        return {
            "status": "cancelled",
            "error": "channel_not_proactive",
            "account_id": account_id,
            "channel": channel,
        }
    if should_inline_dispatch_for_account(account_id, settings):
        return send_proactive_text(
            account_id=account_id,
            channel=channel,
            channel_account_id=channel_account_id,
            to_user_id=to_user_id,
            session_key=session_key,
            source=source,
            text=text,
            idempotency_key=idempotency_key,
            now=now,
            bypass_quiet_hours=bypass_quiet_hours,
            product_category=product_category,
            scheduled_at=scheduled_at,
            metadata=metadata,
        )
    return enqueue_proactive_text(
        account_id=account_id,
        channel=channel,
        channel_account_id=channel_account_id,
        to_user_id=to_user_id,
        session_key=session_key,
        source=source,
        text=text,
        idempotency_key=idempotency_key,
        now=now,
        bypass_quiet_hours=bypass_quiet_hours,
        product_category=product_category,
        scheduled_at=scheduled_at,
        metadata=metadata,
    )


def enqueue_onboarding_welcome(
    *,
    account_id: str,
    channel: str,
    channel_account_id: Optional[str],
    to_user_id: str,
    session_key: Optional[str],
    text: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """central 非 inline 时的 onboarding 欢迎语入队(turn/binding 在中心跑,中心不持有会话)。

    解析账号归属节点,建一条 `status="pending"` 出站行,由该节点的 pull 循环发送、并经中心
    `/node/outbound/{id}/result` 落交付记录。inline/standalone 路径仍在调用方本机直发,不进此函数。

    欢迎语是固定安全常量,**不过** proactive 政策/配额闸门,保「必发」语义与今天一致。
    `idempotency_key` 取账号维度常量,outbound_messages 的 UNIQUE 约束天然去重,避免重复欢迎。
    """
    current = now or beijing_naive_now()
    effective_node_id = resolve_node_for_account(account_id) or (
        getattr(settings, "default_node_id", "") or None
    )
    return create_outbound_message(
        account_id=account_id,
        channel=channel,
        channel_account_id=channel_account_id,
        to_user_id=to_user_id,
        session_key=session_key,
        source="onboarding_welcome",
        text=text,
        idempotency_key=f"onboarding-welcome-{account_id}",
        quota_date=current.date().isoformat(),
        status="pending",
        node_id=effective_node_id,
        metadata={"source": "onboarding_welcome", "outbound_source": "onboarding_welcome"},
    )

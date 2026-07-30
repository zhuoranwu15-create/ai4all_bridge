import asyncio
import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from app.products.zhaoxi.proactive.obligations.commitments import dispatch_due_commitments
from app.products.zhaoxi.proactive.obligations.content_invitations import expire_stale_content_invitations
from app.products.zhaoxi.proactive.delivery.dispatch import dispatch_due_reactivation_candidates
from app.products.zhaoxi.proactive.obligations.reminders import (
    dispatch_due_dynamic_reminders,
    dispatch_due_reminders,
    reconcile_enqueued_reminder_content_runs,
)
from app.products.zhaoxi.proactive.orchestration.planning import scan_due_proactive_account_checks
from app.products.zhaoxi.proactive.recall.hot_topic import refresh_hot_topic_pool
from app.products.zhaoxi.proactive.store.account_state import DEFAULT_ACCOUNT_CHECK_INTERVAL_SECONDS
from app.db import reclaim_expired_reservations, record_scheduler_heartbeat
from app.time_utils import beijing_naive_now


DispatchDueReminders = Callable[..., List[Dict[str, Any]]]
DispatchDueDynamicReminders = Callable[..., List[Dict[str, Any]]]
ReconcileDynamicRuns = Callable[..., List[Dict[str, Any]]]
DispatchDueCommitments = Callable[..., List[Dict[str, Any]]]
DispatchDueReactivation = Callable[..., List[Dict[str, Any]]]
ExpireContentInvitations = Callable[..., List[Dict[str, Any]]]
ScanDueAccountChecks = Callable[..., List[Dict[str, Any]]]
RefreshHotTopicPool = Callable[..., Dict[str, Any]]
ReclaimExpiredReservations = Callable[..., int]
CleanupAppNotifications = Callable[..., Dict[str, Any]]
ReclaimOrphanMedia = Callable[..., Dict[str, Any]]
ReviewPendingMedia = Callable[..., Dict[str, Any]]

logger = logging.getLogger("ai4all.proactive.scheduler")

_M3_OBSERVABILITY_KEYS = (
    "human_claim_success",
    "human_claim_blocked_24h",
    "human_claim_blocked_inflight",
    "human_speaker_cancelled",
    "human_speaker_reselected",
)


def _collect_m3_observability(*values: Any) -> Dict[str, int]:
    """递归汇总 dispatcher 返回的低基数真人级观测字段。"""

    totals = {key: 0 for key in _M3_OBSERVABILITY_KEYS}

    def _visit(value: Any) -> None:
        if isinstance(value, dict):
            observed = value.get("m3_observability")
            if isinstance(observed, dict):
                for key in _M3_OBSERVABILITY_KEYS:
                    totals[key] += int(observed.get(key) or 0)
            for key, nested in value.items():
                if key != "m3_observability":
                    _visit(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                _visit(nested)

    for value in values:
        _visit(value)
    return totals


class ProactiveScheduler:
    """System-level scheduler for cheap proactive work scans."""

    def __init__(
        self,
        *,
        interval_seconds: float,
        batch_size: int,
        bypass_quiet_hours: bool = False,
        planning_interval_seconds: int = DEFAULT_ACCOUNT_CHECK_INTERVAL_SECONDS,
        node_id: Optional[str] = None,
        dispatch_reminders: DispatchDueReminders = dispatch_due_reminders,
        dispatch_dynamic_reminders: DispatchDueDynamicReminders = dispatch_due_dynamic_reminders,
        reconcile_dynamic_runs: ReconcileDynamicRuns = reconcile_enqueued_reminder_content_runs,
        dispatch_commitments: DispatchDueCommitments = dispatch_due_commitments,
        dispatch_reactivation: DispatchDueReactivation = dispatch_due_reactivation_candidates,
        expire_content_invitations: ExpireContentInvitations = expire_stale_content_invitations,
        scan_account_checks: ScanDueAccountChecks = scan_due_proactive_account_checks,
        refresh_hot_topics: RefreshHotTopicPool = refresh_hot_topic_pool,
        reclaim_quota_reservations: ReclaimExpiredReservations = reclaim_expired_reservations,
        cleanup_app_notifications: Optional[CleanupAppNotifications] = None,
        notification_cleanup_batch_size: int = 100,
        reclaim_orphan_media: Optional[ReclaimOrphanMedia] = None,
        media_reclaim_batch_size: int = 200,
        review_pending_media: Optional[ReviewPendingMedia] = None,
    ) -> None:
        self.interval_seconds = max(float(interval_seconds), 1.0)
        self.batch_size = max(int(batch_size), 1)
        self.bypass_quiet_hours = bypass_quiet_hours
        self.planning_interval_seconds = max(int(planning_interval_seconds), 1)
        # 厚节点改造 P4：节点角色时只扫本节点账号（assigned_node_id = node_id）
        self.node_id: Optional[str] = node_id or None
        self._dispatch_reminders = dispatch_reminders
        self._dispatch_dynamic_reminders = dispatch_dynamic_reminders
        self._reconcile_dynamic_runs = reconcile_dynamic_runs
        self._dispatch_commitments = dispatch_commitments
        self._dispatch_reactivation = dispatch_reactivation
        self._expire_content_invitations = expire_content_invitations
        self._scan_account_checks = scan_account_checks
        self._refresh_hot_topics = refresh_hot_topics
        self._reclaim_quota_reservations = reclaim_quota_reservations
        self._cleanup_app_notifications = cleanup_app_notifications
        self.notification_cleanup_batch_size = max(
            int(notification_cleanup_batch_size), 1
        )
        # v1.5 D-10：孤儿媒体回收。每 tick 都调用，函数内部按
        # media_reclaim_interval_seconds 自节流（同 hot topic pool 的做法），不另起进程。
        self._reclaim_orphan_media = reclaim_orphan_media
        self.media_reclaim_batch_size = max(int(media_reclaim_batch_size), 1)
        # v1.5 S4 图片机审：同样每 tick 都调，函数内部按 media_moderation_interval_seconds
        # 自节流；机审未配置时它直接返回 disabled，连库都不读。批量大小走配置，不在这里覆写。
        self._review_pending_media = review_pending_media
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event: Optional[asyncio.Event] = None
        self.last_run: Optional[Dict[str, Any]] = None
        self.last_error: Optional[str] = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> Dict[str, Any]:
        return {
            "running": self.is_running,
            "interval_seconds": self.interval_seconds,
            "batch_size": self.batch_size,
            "bypass_quiet_hours": self.bypass_quiet_hours,
            "planning_interval_seconds": self.planning_interval_seconds,
            "last_run": self.last_run,
            "last_error": self.last_error,
        }

    async def run_once(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        started_at = beijing_naive_now()
        current = now or started_at
        # 各步骤相互隔离：单个 dispatcher 抛错只记录并继续，不再让前面的步骤
        # （如 reminders）持续失败时把后面的 reactivation/commitment 发送整轮饿死。
        step_errors: Dict[str, str] = {}

        reclaimed_quota_reservations = 0
        try:
            reclaimed_quota_reservations = await asyncio.to_thread(
                self._reclaim_quota_reservations,
                now=current.strftime("%Y-%m-%d %H:%M:%S"),
                limit=self.batch_size,
            )
        except Exception as err:  # noqa: BLE001 — 维护步骤失败不拖垮主动消息主链路
            logger.exception("proactive scheduler step quota_reservations failed: %s", err)
            step_errors["quota_reservations"] = str(err)

        notification_cleanup: Dict[str, Any] = {}
        if self._cleanup_app_notifications is not None:
            try:
                notification_cleanup = await asyncio.to_thread(
                    self._cleanup_app_notifications,
                    now=current.strftime("%Y-%m-%d %H:%M:%S"),
                    limit=self.notification_cleanup_batch_size,
                )
                if notification_cleanup.get("errors"):
                    step_errors["app_notification_cleanup"] = str(
                        notification_cleanup["errors"]
                    )
            except Exception as err:  # noqa: BLE001 — cleanup 与投递步骤相互隔离
                logger.exception(
                    "proactive scheduler step app_notification_cleanup failed: %s",
                    err,
                )
                step_errors["app_notification_cleanup"] = str(err)

        media_reclaim: Dict[str, Any] = {}
        if self._reclaim_orphan_media is not None:
            try:
                media_reclaim = await asyncio.to_thread(
                    self._reclaim_orphan_media,
                    now=current.strftime("%Y-%m-%d %H:%M:%S"),
                    limit=self.media_reclaim_batch_size,
                )
            except Exception as err:  # noqa: BLE001 — 回收与投递步骤相互隔离
                logger.exception("proactive scheduler step media_reclaim failed: %s", err)
                step_errors["media_reclaim"] = str(err)

        media_moderation: Dict[str, Any] = {}
        if self._review_pending_media is not None:
            try:
                media_moderation = await asyncio.to_thread(self._review_pending_media)
            except Exception as err:  # noqa: BLE001 — 机审与投递步骤相互隔离
                logger.exception(
                    "proactive scheduler step media_moderation failed: %s", err
                )
                step_errors["media_moderation"] = str(err)

        async def _step(name: str, fn: Callable[..., List[Dict[str, Any]]], **kwargs: Any) -> List[Dict[str, Any]]:
            try:
                return await asyncio.to_thread(fn, **kwargs)
            except Exception as err:  # noqa: BLE001 — 步骤级隔离，单步失败不拖垮整轮
                logger.exception("proactive scheduler step %s failed: %s", name, err)
                step_errors[name] = str(err)
                return []

        reminder_results = await _step(
            "reminders",
            self._dispatch_reminders,
            now=current,
            limit=self.batch_size,
            bypass_quiet_hours=self.bypass_quiet_hours,
            node_id=self.node_id,
        )
        # 动态提醒（例行简报）：统一 aliyun1 调度，扫描内部忽略 node 分片以覆盖远程账号。
        # 总开关关闭时函数内部立即返回空，无额外开销。
        dynamic_reminder_results = await _step(
            "dynamic_reminders",
            self._dispatch_dynamic_reminders,
            now=current,
            limit=self.batch_size,
            bypass_quiet_hours=self.bypass_quiet_hours,
            node_id=self.node_id,
        )
        # 远程出站对账：把 enqueued 的履约 run 按 outbound 最终态翻成 sent/failed。
        dynamic_reconcile_results = await _step(
            "dynamic_reminder_reconcile",
            self._reconcile_dynamic_runs,
            now=current,
            limit=self.batch_size,
        )
        commitment_results = await _step(
            "commitments",
            self._dispatch_commitments,
            now=current,
            limit=self.batch_size,
            bypass_quiet_hours=self.bypass_quiet_hours,
            node_id=self.node_id,
        )
        # 全局热点池刷新：每 tick 都调用，函数内部按绝对时钟档（hot_topic_pool_refresh_slots）
        # 做幂等门控，未到档/当档已生成时函数自身立即 no_op，不产生额外 DB/LLM 开销。
        hot_topic_result: Dict[str, Any] = {}
        try:
            hot_topic_result = await asyncio.to_thread(self._refresh_hot_topics, now=current)
        except Exception as err:  # noqa: BLE001 — 步骤级隔离，单步失败不拖垮整轮
            logger.exception("proactive scheduler step hot_topic_pool failed: %s", err)
            step_errors["hot_topic_pool"] = str(err)

        account_results = await _step(
            "account_checks",
            self._scan_account_checks,
            now=current,
            limit=self.batch_size,
            planning_interval_seconds=self.planning_interval_seconds,
            node_id=self.node_id,
        )
        reactivation_results = await _step(
            "reactivation",
            self._dispatch_reactivation,
            now=current,
            limit=self.batch_size,
            node_id=self.node_id,
        )
        expired_content_results = await _step(
            "expire_content_invitations",
            self._expire_content_invitations,
            now=current,
            limit=self.batch_size,
        )
        m3_observability = _collect_m3_observability(
            reminder_results,
            dynamic_reminder_results,
            commitment_results,
            account_results,
            reactivation_results,
        )
        finished_at = beijing_naive_now()
        result = {
            "status": "ok" if not step_errors else "partial_error",
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
            "reclaimed_quota_reservations": reclaimed_quota_reservations,
            "app_notification_cleanup": notification_cleanup,
            "media_reclaim": media_reclaim,
            "media_moderation": media_moderation,
            "reminder_count": len(reminder_results),
            "reminders": reminder_results,
            "dynamic_reminder_count": len(dynamic_reminder_results),
            "dynamic_reminders": dynamic_reminder_results,
            "dynamic_reminder_reconcile_count": len(dynamic_reconcile_results),
            "dynamic_reminder_reconciles": dynamic_reconcile_results,
            "commitment_count": len(commitment_results),
            "commitments": commitment_results,
            "account_check_count": len(account_results),
            "account_checks": account_results,
            "reactivation_count": len(reactivation_results),
            "reactivations": reactivation_results,
            "expired_content_invitation_count": len(expired_content_results),
            "expired_content_invitations": expired_content_results,
            "hot_topic_pool": hot_topic_result,
            "m3_observability": m3_observability,
            "errors": step_errors or None,
        }
        self.last_run = result
        # 部分步骤失败时保留错误供 heartbeat/监控感知，不再静默吞掉。
        self.last_error = (
            None
            if not step_errors
            else "; ".join(f"{name}: {msg}" for name, msg in step_errors.items())
        )
        return result

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop(), name="ai4all-proactive-scheduler")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            self._task = None
            self._stop_event = None

    async def _run_loop(self) -> None:
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
        while not self._stop_event.is_set():
            try:
                self._record_heartbeat(status="running")
                run_result = await self.run_once()
                # run_once 现在做步骤级隔离，不再抛步骤错误；部分失败时仍要让
                # heartbeat 反映出来，否则监控会把"部分步骤一直失败"当成健康。
                if run_result.get("errors"):
                    self._record_heartbeat(status="error", error=self.last_error)
                else:
                    self._record_heartbeat(status="ok")
            except Exception as err:
                self.last_error = str(err)
                self._record_heartbeat(status="error", error=str(err))
                logger.exception("proactive scheduler run failed: %s", err)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

    def _record_heartbeat(self, *, status: str, error: Optional[str] = None) -> None:
        try:
            record_scheduler_heartbeat(
                service="proactive_scheduler",
                status=status,
                error=error,
                metadata={
                    "interval_seconds": self.interval_seconds,
                    "batch_size": self.batch_size,
                    "bypass_quiet_hours": self.bypass_quiet_hours,
                    "planning_interval_seconds": self.planning_interval_seconds,
                    "notification_cleanup_enabled": self._cleanup_app_notifications
                    is not None,
                    "notification_cleanup_batch_size": self.notification_cleanup_batch_size,
                    "last_notification_cleanup": (
                        self.last_run.get("app_notification_cleanup")
                        if self.last_run
                        else None
                    ),
                    "last_m3_observability": (
                        self.last_run.get("m3_observability")
                        if self.last_run
                        else None
                    ),
                },
            )
        except Exception as err:
            logger.warning("proactive scheduler heartbeat write failed: %s", err)


_scheduler: Optional[ProactiveScheduler] = None


def get_proactive_scheduler() -> Optional[ProactiveScheduler]:
    return _scheduler


def start_proactive_scheduler(
    *,
    interval_seconds: float,
    batch_size: int,
    bypass_quiet_hours: bool = False,
    planning_interval_seconds: int = DEFAULT_ACCOUNT_CHECK_INTERVAL_SECONDS,
    node_id: Optional[str] = None,
    cleanup_app_notifications: Optional[CleanupAppNotifications] = None,
    notification_cleanup_batch_size: int = 100,
    reclaim_orphan_media: Optional[ReclaimOrphanMedia] = None,
    media_reclaim_batch_size: int = 200,
    review_pending_media: Optional[ReviewPendingMedia] = None,
) -> ProactiveScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = ProactiveScheduler(
            interval_seconds=interval_seconds,
            batch_size=batch_size,
            bypass_quiet_hours=bypass_quiet_hours,
            planning_interval_seconds=planning_interval_seconds,
            node_id=node_id,
            cleanup_app_notifications=cleanup_app_notifications,
            notification_cleanup_batch_size=notification_cleanup_batch_size,
            reclaim_orphan_media=reclaim_orphan_media,
            media_reclaim_batch_size=media_reclaim_batch_size,
            review_pending_media=review_pending_media,
        )
    if not _scheduler.is_running:
        _scheduler.start()
    return _scheduler


async def stop_proactive_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    await _scheduler.stop()
    _scheduler = None


async def run_proactive_scheduler_once(
    *,
    batch_size: int,
    bypass_quiet_hours: bool = False,
    planning_interval_seconds: int = DEFAULT_ACCOUNT_CHECK_INTERVAL_SECONDS,
    node_id: Optional[str] = None,
    now: Optional[datetime] = None,
    cleanup_app_notifications: Optional[CleanupAppNotifications] = None,
    notification_cleanup_batch_size: int = 100,
    reclaim_orphan_media: Optional[ReclaimOrphanMedia] = None,
    media_reclaim_batch_size: int = 200,
    review_pending_media: Optional[ReviewPendingMedia] = None,
) -> Dict[str, Any]:
    scheduler = ProactiveScheduler(
        interval_seconds=60,
        batch_size=batch_size,
        bypass_quiet_hours=bypass_quiet_hours,
        planning_interval_seconds=planning_interval_seconds,
        node_id=node_id,
        cleanup_app_notifications=cleanup_app_notifications,
        notification_cleanup_batch_size=notification_cleanup_batch_size,
        reclaim_orphan_media=reclaim_orphan_media,
        media_reclaim_batch_size=media_reclaim_batch_size,
        review_pending_media=review_pending_media,
    )
    return await scheduler.run_once(now=now)

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.config import settings  # kept for existing test/settings patching conventions
from app.db import (
    compute_message_intensity,
    compute_safety_risk_count_30d,
    fetch_recent_inbound_messages,
    get_account_user_meta,
    insert_account_user_meta_daily,
    list_accounts_for_meta_refresh,
    record_scheduler_heartbeat,
    upsert_account_user_meta,
)
from app.llm import generate_completion
from app.prompts.user_meta_companion_type import (
    COMPANION_TYPE_ENUM,
    build_companion_classify_prompt,
)
from app.time_utils import BEIJING_TZ, beijing_naive_now


logger = logging.getLogger("ai4all.user_meta.scheduler")

_MIN_SLEEP_SECONDS = 60.0
_IDLE_HEARTBEAT_INTERVAL_SECONDS = 300.0


def _raw_seconds_until_next_window(now: datetime, *, start_hour: int = 3) -> float:
    today_boundary = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if now >= today_boundary:
        next_boundary = today_boundary + timedelta(days=1)
    else:
        next_boundary = today_boundary
    return max((next_boundary - now).total_seconds(), 0.0)


def _seconds_until_next_window(now: datetime, *, start_hour: int = 3) -> float:
    """Return seconds until the next user-meta refresh window."""
    return max(_raw_seconds_until_next_window(now, start_hour=start_hour), _MIN_SLEEP_SECONDS)


def _as_beijing_naive(value: Optional[datetime]) -> datetime:
    if value is None:
        return beijing_naive_now()
    if value.tzinfo is not None:
        return value.astimezone(BEIJING_TZ).replace(tzinfo=None)
    return value


def _parse_db_datetime(value: Optional[str]) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def _extract_json_object(raw: str) -> Dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("LLM output is not JSON")
        payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("LLM output must be a JSON object")
    return payload


def _clean_text(value: Any, *, max_chars: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:max_chars]


def _normalize_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))


def _normalize_companion_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    primary_type = _clean_text(payload.get("primary_type"), max_chars=80)
    if primary_type not in COMPANION_TYPE_ENUM:
        raise ValueError(f"invalid primary_type: {primary_type!r}")

    secondary_types = []
    raw_secondary = payload.get("secondary_types")
    if isinstance(raw_secondary, list):
        for item in raw_secondary:
            text = _clean_text(item, max_chars=80)
            if text in COMPANION_TYPE_ENUM and text != primary_type and text not in secondary_types:
                secondary_types.append(text)
            if len(secondary_types) >= 3:
                break

    return {
        "primary_type": primary_type,
        "secondary_types": secondary_types,
        "confidence": _normalize_confidence(payload.get("confidence")),
        "reasoning": _clean_text(payload.get("reasoning"), max_chars=500),
    }


def classify_companion_type(*, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Call the configured default LLM and return a normalized companion type payload."""
    if not messages:
        raise ValueError("messages are required")
    prompt = build_companion_classify_prompt(messages=messages)
    raw = generate_completion([{"role": "user", "content": prompt}])
    return _normalize_companion_payload(_extract_json_object(raw))


def _existing_companion(meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "primary_type": meta.get("companion_primary_type") if meta else None,
        "secondary_types": list(meta.get("companion_secondary_types") or []) if meta else [],
        "confidence": meta.get("companion_type_confidence") if meta else None,
        "last_evaluated_at": meta.get("companion_type_last_evaluated_at") if meta else None,
        "source": (meta.get("companion_type_source") if meta else None) or "auto",
        "expires_at": meta.get("companion_type_expires_at") if meta else None,
        "reasoning": meta.get("companion_type_reasoning") if meta else None,
    }


def _needs_companion_evaluation(
    *,
    meta: Optional[Dict[str, Any]],
    now: datetime,
) -> bool:
    if meta is None:
        return True
    last_evaluated = _parse_db_datetime(meta.get("companion_type_last_evaluated_at"))
    source = str(meta.get("companion_type_source") or "auto")
    if last_evaluated is None:
        return True
    if source == "manual":
        expires_at = _parse_db_datetime(meta.get("companion_type_expires_at"))
        return expires_at is not None and expires_at <= now
    return now - last_evaluated >= timedelta(days=7)


class UserMetaScheduler:
    """Daily scheduler for account user meta refreshes."""

    def __init__(
        self,
        *,
        page_size: int = 100,
        inter_account_sleep: float = 0.5,
        start_hour: int = 3,
    ) -> None:
        self.page_size = max(1, int(page_size))
        self.inter_account_sleep = max(0.0, float(inter_account_sleep))
        self.start_hour = max(0, min(int(start_hour), 23))
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._is_running = False
        self.last_run: Optional[Dict[str, Any]] = None
        self.last_error: Optional[str] = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def is_refresh_running(self) -> bool:
        return self._is_running

    def status(self) -> Dict[str, Any]:
        now = beijing_naive_now()
        return {
            "running": self.is_running,
            "refresh_running": self.is_refresh_running,
            "start_hour": self.start_hour,
            "page_size": self.page_size,
            "inter_account_sleep": self.inter_account_sleep,
            "seconds_until_next_window": round(
                _seconds_until_next_window(now, start_hour=self.start_hour)
            ),
            "last_run": self.last_run,
            "last_error": self.last_error,
        }

    async def run_once(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        if self._is_running:
            return {"status": "already_running"}
        self._is_running = True
        current = _as_beijing_naive(now)
        today_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        thirty_days_ago = (current.replace(second=0, microsecond=0) - timedelta(days=30))
        started_at = beijing_naive_now()
        result: Dict[str, Any] = {
            "status": "ok",
            "started_at": started_at.isoformat(timespec="seconds"),
            "snapshot_date": current.strftime("%Y-%m-%d"),
            "today_start": today_start.strftime("%Y-%m-%d %H:%M:%S"),
            "thirty_days_ago": thirty_days_ago.strftime("%Y-%m-%d %H:%M:%S"),
            "processed": 0,
            "skipped": 0,
            "companion_evaluated": 0,
            "companion_failed": 0,
            "errors": [],
        }
        self._record_heartbeat(status="running")
        try:
            offset = 0
            while True:
                accounts = list_accounts_for_meta_refresh(
                    offset=offset,
                    limit=self.page_size,
                )
                if not accounts:
                    break
                for account in accounts:
                    account_id = str(account["id"])
                    try:
                        account_result = await self._refresh_account(
                            account=account,
                            now=current,
                            today_start=result["today_start"],
                            thirty_days_ago=result["thirty_days_ago"],
                            snapshot_date=result["snapshot_date"],
                        )
                        result["processed"] += 1
                        if account_result.get("companion_evaluated"):
                            result["companion_evaluated"] += 1
                        if account_result.get("companion_failed"):
                            result["companion_failed"] += 1
                            result["errors"].append(
                                {
                                    "account_id": account_id,
                                    "step": "companion_classification",
                                    "error": account_result.get("companion_error")
                                    or "companion classification failed",
                                }
                            )
                    except Exception as err:  # noqa: BLE001 - account-level isolation
                        logger.exception(
                            "user meta refresh failed account=%s error=%s",
                            account_id,
                            err,
                        )
                        result["skipped"] += 1
                        result["errors"].append(
                            {"account_id": account_id, "error": str(err)}
                        )
                    if self.inter_account_sleep > 0:
                        await asyncio.sleep(self.inter_account_sleep)
                offset += len(accounts)
        finally:
            self._is_running = False

        result["finished_at"] = beijing_naive_now().isoformat(timespec="seconds")
        if result["errors"]:
            result["status"] = "partial_error"
            self.last_error = "; ".join(
                f"{item['account_id']}: {item['error']}" for item in result["errors"][:5]
            )
            self._record_heartbeat(status="error", error=self.last_error)
        else:
            result["errors"] = []
            self.last_error = None
            self._record_heartbeat(status="ok")
        self.last_run = result
        return result

    async def _refresh_account(
        self,
        *,
        account: Dict[str, Any],
        now: datetime,
        today_start: str,
        thirty_days_ago: str,
        snapshot_date: str,
    ) -> Dict[str, Any]:
        account_id = str(account["id"])
        evaluated_at = beijing_naive_now().isoformat(timespec="seconds")
        intensity = compute_message_intensity(
            account_id=account_id,
            today_start=today_start,
        )
        safety_count = compute_safety_risk_count_30d(
            account_id=account_id,
            thirty_days_ago=thirty_days_ago,
        )
        current_meta = get_account_user_meta(account_id=account_id)
        companion = _existing_companion(current_meta)
        companion_failed = False
        companion_evaluated = False
        companion_error = None
        needs_companion_evaluation = _needs_companion_evaluation(meta=current_meta, now=now)
        if (
            not needs_companion_evaluation
            and intensity >= 2
            and companion["source"] == "auto"
            and not companion["primary_type"]
        ):
            needs_companion_evaluation = True
        if needs_companion_evaluation:
            if intensity >= 2:
                recent_messages = fetch_recent_inbound_messages(account_id=account_id, limit=50)
                try:
                    classified = await asyncio.to_thread(
                        classify_companion_type,
                        messages=recent_messages,
                    )
                    companion = {
                        "primary_type": classified["primary_type"],
                        "secondary_types": classified["secondary_types"],
                        "confidence": classified["confidence"],
                        "last_evaluated_at": evaluated_at,
                        "source": "auto",
                        "expires_at": None,
                        "reasoning": classified["reasoning"],
                    }
                    companion_evaluated = True
                except Exception as err:  # noqa: BLE001 - LLM failure keeps existing values
                    logger.warning(
                        "companion classification failed account=%s error=%s",
                        account_id,
                        err,
                    )
                    companion_failed = True
                    companion_error = str(err)
            else:
                companion = {
                    "primary_type": None,
                    "secondary_types": [],
                    "confidence": None,
                    "last_evaluated_at": evaluated_at,
                    "source": "auto",
                    "expires_at": None,
                    "reasoning": None,
                }

        upsert_account_user_meta(
            account_id=account_id,
            registered_at=str(account["created_at"]),
            message_intensity_level=intensity,
            companion_primary_type=companion["primary_type"],
            companion_secondary_types=companion["secondary_types"],
            companion_type_confidence=companion["confidence"],
            companion_type_last_evaluated_at=companion["last_evaluated_at"],
            companion_type_source=companion["source"],
            companion_type_expires_at=companion["expires_at"],
            companion_type_reasoning=companion["reasoning"],
            safety_risk_trigger_count_30d=safety_count,
            last_evaluated_at=evaluated_at,
        )
        insert_account_user_meta_daily(
            account_id=account_id,
            snapshot_date=snapshot_date,
            registered_at=str(account["created_at"]),
            message_intensity_level=intensity,
            companion_primary_type=companion["primary_type"],
            companion_secondary_types=companion["secondary_types"],
            companion_type_confidence=companion["confidence"],
            companion_type_last_evaluated_at=companion["last_evaluated_at"],
            companion_type_source=companion["source"],
            companion_type_expires_at=companion["expires_at"],
            companion_type_reasoning=companion["reasoning"],
            safety_risk_trigger_count_30d=safety_count,
            last_evaluated_at=evaluated_at,
        )
        return {
            "companion_evaluated": companion_evaluated,
            "companion_failed": companion_failed,
            "companion_error": companion_error,
        }

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop(), name="ai4all-user-meta-scheduler")

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
                await self.run_once()
            except Exception as err:
                self.last_error = str(err)
                self._record_heartbeat(status="error", error=str(err))
                logger.exception("user meta scheduler run failed: %s", err)
            await self._sleep_until_next_window()

    async def _sleep_until_next_window(self) -> None:
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
        while not self._stop_event.is_set():
            sleep_seconds = _raw_seconds_until_next_window(
                beijing_naive_now(),
                start_hour=self.start_hour,
            )
            if sleep_seconds <= 0:
                return
            timeout = min(sleep_seconds, _IDLE_HEARTBEAT_INTERVAL_SECONDS)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
                return
            except asyncio.TimeoutError:
                if sleep_seconds <= _IDLE_HEARTBEAT_INTERVAL_SECONDS:
                    return
                self._record_heartbeat(status="idle")

    def _record_heartbeat(self, *, status: str, error: Optional[str] = None) -> None:
        try:
            record_scheduler_heartbeat(
                service="user_meta_scheduler",
                status=status,
                error=error,
                metadata={
                    "start_hour": self.start_hour,
                    "page_size": self.page_size,
                    "inter_account_sleep": self.inter_account_sleep,
                },
            )
        except Exception as err:
            logger.warning("user meta scheduler heartbeat write failed: %s", err)


_scheduler: Optional[UserMetaScheduler] = None


def get_user_meta_scheduler() -> Optional[UserMetaScheduler]:
    return _scheduler


def start_user_meta_scheduler(
    *,
    page_size: int,
    inter_account_sleep: float,
    start_hour: int = 3,
) -> UserMetaScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = UserMetaScheduler(
            page_size=page_size,
            inter_account_sleep=inter_account_sleep,
            start_hour=start_hour,
        )
    if not _scheduler.is_running:
        _scheduler.start()
    return _scheduler


async def stop_user_meta_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    await _scheduler.stop()
    _scheduler = None


async def run_user_meta_scheduler_once(
    *,
    page_size: int = 100,
    inter_account_sleep: float = 0.0,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    scheduler = get_user_meta_scheduler() or UserMetaScheduler(
        page_size=page_size,
        inter_account_sleep=inter_account_sleep,
    )
    return await scheduler.run_once(now=now)

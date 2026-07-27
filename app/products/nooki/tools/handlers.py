"""Nooki Tool handlers：可信身份解析、operation_id 生成和领域错误翻译。"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from app.db import get_platform_user_id_for_account
from app.products.nooki.application.ui_projection import project_task_view
from app.products.nooki.domain.goal_breakdown.contracts import NookiDomainError, PlanDraft
from app.products.nooki.domain.goal_breakdown.service import GoalBreakdownService
from app.products.nooki.infrastructure.repositories.goal_breakdown import SqlTaskRepository
from app.products.nooki.infrastructure.repositories.later_items import NookiLaterItemRepository

if TYPE_CHECKING:
    from app.agent_runtime.context.models import TurnContext


def _service() -> GoalBreakdownService:
    return GoalBreakdownService(SqlTaskRepository())


def _platform_user_id(ctx: "TurnContext") -> str:
    platform_user_id = get_platform_user_id_for_account(account_id=ctx.account_id)
    if not platform_user_id:
        raise NookiDomainError("platform_user_not_found")
    return platform_user_id


def _operation_id(ctx: "TurnContext", action: str, *resource_ids: str) -> str:
    suffix = ":".join(str(item) for item in resource_ids if item)
    return f"message:{ctx.message_id}:{action}" + (f":{suffix}" if suffix else "")


def _expected_version(args: dict) -> Optional[int]:
    value = args.get("expected_version")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise NookiDomainError("expected_version_invalid")
    return value


def _plan_draft(item: dict) -> PlanDraft:
    return PlanDraft(
        mode=str(item.get("mode") or ""),
        title=str(item.get("title") or ""),
        description=item.get("description"),
        estimated_minutes=item.get("estimated_minutes"),
    )


def _ok(
    service: GoalBreakdownService,
    *,
    platform_user_id: str,
    focus_task_id: str,
    operation: str,
) -> dict:
    projection = service.get_authoritative_state(platform_user_id, focus_task_id)
    state, cards = project_task_view(projection)
    return {"status": "ok", "operation": operation, "state": state, "cards": cards}


def _failed(err: NookiDomainError) -> dict:
    return {"status": "failed", "error": err.code}


def handle_nooki_capture_later_item(
    args: dict, ctx: "TurnContext", tool_invocation_id: Optional[int] = None
) -> dict:
    """把当前消息明确提到的待办幂等收进当前用户的稍后盒子。"""

    del tool_invocation_id
    try:
        platform_user_id = _platform_user_id(ctx)
        content = str(args.get("content") or "").strip()
        if not content or len(content) > 1000:
            raise NookiDomainError("later_item_content_invalid")
        item, deduplicated = NookiLaterItemRepository().create(
            platform_user_id=platform_user_id,
            content=content,
            client_request_id=_operation_id(ctx, "capture_later"),
            source_message_id=ctx.message_id,
        )
        return {
            "status": "ok",
            "operation": "capture_later_item",
            "item": item,
            "metadata": {"deduplicated": deduplicated},
        }
    except NookiDomainError as err:
        return _failed(err)


def handle_nooki_create_task_with_options(
    args: dict, ctx: "TurnContext", tool_invocation_id: Optional[int] = None
) -> dict:
    """原子创建一次行动任务；tool_invocation_id 仅用于 Runtime 审计，不作为幂等键。"""

    del tool_invocation_id
    try:
        platform_user_id = _platform_user_id(ctx)
        service = _service()
        result = service.create_task_with_options(
            platform_user_id=platform_user_id,
            title=str(args.get("title") or ""),
            raw_goal=str(args.get("raw_goal") or ""),
            options=tuple(_plan_draft(item) for item in (args.get("options") or [])),
            source_message_id=ctx.message_id,
            operation_id=_operation_id(ctx, "create_task"),
        )
        return _ok(
            service,
            platform_user_id=platform_user_id,
            focus_task_id=result.task.id,
            operation="create_task_with_options",
        )
    except NookiDomainError as err:
        return _failed(err)


def handle_nooki_convert_later_item_with_options(
    args: dict, ctx: "TurnContext", tool_invocation_id: Optional[int] = None
) -> dict:
    """把 prompt 中的 inbox 稍后项原子转换为新任务。"""

    del tool_invocation_id
    try:
        platform_user_id = _platform_user_id(ctx)
        item_id = str(args.get("later_item_id") or "")
        service = _service()
        result = service.convert_later_item_with_options(
            item_id,
            platform_user_id=platform_user_id,
            options=tuple(_plan_draft(item) for item in (args.get("options") or [])),
            operation_id=_operation_id(ctx, "convert_later", item_id),
            expected_version=_expected_version(args),
            source_message_id=ctx.message_id,
        )
        return _ok(
            service,
            platform_user_id=platform_user_id,
            focus_task_id=result.task.id,
            operation="convert_later_item_with_options",
        )
    except NookiDomainError as err:
        return _failed(err)


def handle_nooki_select_task_plan(
    args: dict, ctx: "TurnContext", tool_invocation_id: Optional[int] = None
) -> dict:
    del tool_invocation_id
    try:
        platform_user_id = _platform_user_id(ctx)
        task_id = str(args.get("task_id") or "")
        plan_id = str(args.get("plan_id") or "")
        service = _service()
        service.select_task_plan(
            task_id,
            plan_id,
            platform_user_id=platform_user_id,
            operation_id=_operation_id(ctx, "select_plan", task_id, plan_id),
            expected_version=_expected_version(args),
            source_message_id=ctx.message_id,
        )
        return _ok(
            service,
            platform_user_id=platform_user_id,
            focus_task_id=task_id,
            operation="select_task_plan",
        )
    except NookiDomainError as err:
        return _failed(err)


def handle_nooki_start_step(
    args: dict, ctx: "TurnContext", tool_invocation_id: Optional[int] = None
) -> dict:
    del tool_invocation_id
    try:
        platform_user_id = _platform_user_id(ctx)
        step_id = str(args.get("step_id") or "")
        service = _service()
        step = service.start_step(
            step_id,
            platform_user_id=platform_user_id,
            operation_id=_operation_id(ctx, "start_step", step_id),
            expected_version=_expected_version(args),
            source_message_id=ctx.message_id,
        )
        return _ok(
            service,
            platform_user_id=platform_user_id,
            focus_task_id=step.task_id,
            operation="start_step",
        )
    except NookiDomainError as err:
        return _failed(err)


def handle_nooki_complete_step(
    args: dict, ctx: "TurnContext", tool_invocation_id: Optional[int] = None
) -> dict:
    del tool_invocation_id
    try:
        platform_user_id = _platform_user_id(ctx)
        step_id = str(args.get("step_id") or "")
        service = _service()
        step = service.complete_step(
            step_id,
            platform_user_id=platform_user_id,
            operation_id=_operation_id(ctx, "complete_step", step_id),
            expected_version=_expected_version(args),
            source_message_id=ctx.message_id,
        )
        return _ok(
            service,
            platform_user_id=platform_user_id,
            focus_task_id=step.task_id,
            operation="complete_step",
        )
    except NookiDomainError as err:
        return _failed(err)


def handle_nooki_shrink_step(
    args: dict, ctx: "TurnContext", tool_invocation_id: Optional[int] = None
) -> dict:
    del tool_invocation_id
    try:
        platform_user_id = _platform_user_id(ctx)
        step_id = str(args.get("step_id") or "")
        service = _service()
        step = service.shrink_step(
            step_id,
            _plan_draft(args.get("replacement") or {}),
            platform_user_id=platform_user_id,
            operation_id=_operation_id(ctx, "shrink_step", step_id),
            expected_version=_expected_version(args),
            source_message_id=ctx.message_id,
        )
        return _ok(
            service,
            platform_user_id=platform_user_id,
            focus_task_id=step.task_id,
            operation="shrink_step",
        )
    except NookiDomainError as err:
        return _failed(err)


def handle_nooki_abandon_task(
    args: dict, ctx: "TurnContext", tool_invocation_id: Optional[int] = None
) -> dict:
    del tool_invocation_id
    try:
        platform_user_id = _platform_user_id(ctx)
        task_id = str(args.get("task_id") or "")
        service = _service()
        service.abandon_task(
            task_id,
            platform_user_id=platform_user_id,
            operation_id=_operation_id(ctx, "abandon_task", task_id),
            expected_version=_expected_version(args),
            source_message_id=ctx.message_id,
        )
        return _ok(
            service,
            platform_user_id=platform_user_id,
            focus_task_id=task_id,
            operation="abandon_task",
        )
    except NookiDomainError as err:
        return _failed(err)


def handle_nooki_list_state(args: dict, ctx: "TurnContext") -> dict:
    """只读查询不需要 invocation call style。"""

    try:
        platform_user_id = _platform_user_id(ctx)
        service = _service()
        projection = service.get_authoritative_state(
            platform_user_id, args.get("focus_task_id") or None
        )
        state, cards = project_task_view(projection)
        return {"status": "ok", "operation": "list_state", "state": state, "cards": cards}
    except NookiDomainError as err:
        return _failed(err)


__all__ = [
    "handle_nooki_abandon_task",
    "handle_nooki_capture_later_item",
    "handle_nooki_complete_step",
    "handle_nooki_convert_later_item_with_options",
    "handle_nooki_create_task_with_options",
    "handle_nooki_list_state",
    "handle_nooki_select_task_plan",
    "handle_nooki_shrink_step",
    "handle_nooki_start_step",
]

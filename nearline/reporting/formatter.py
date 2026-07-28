"""Markdown 日报渲染（Phase 1：四个分析域）。

输入为各域指标 dict 的组合，对齐 ANALYTICS_PLAN §七 目标格式。
"""

from typing import Dict, List, Optional

from nearline.reporting.scope import ReportScope


def _retention_line(m: Dict) -> str:
    status = m["d1_status"]
    if status == "ok":
        return f"{m['d1_retained']}/{m['d1_cohort_size']} = {m['d1_retention_rate'] * 100:.1f}%"
    if status == "window_open":
        return f"窗口未闭合（cohort {m['d1_cohort_size']}，待次日数据）"
    return (
        f"仅观察（cohort {m['d1_cohort_size']} < 10，不展示比率；次日命中 {m['d1_retained']}）"
    )


def _pct(rate: Optional[float]) -> str:
    return f"{rate * 100:.1f}%" if rate is not None else "—"


def _num(value: Optional[int]) -> str:
    return str(value) if value is not None else "—"


def _users_section(m: Dict, scoped: bool = False) -> list:
    headline = (
        f"- 新增真人：{m['new_users']} | 真人 DAU：{m['dau']} | "
        f"活跃 AI 账号：{m['active_accounts']} | 入站消息：{m['inbound_messages']}"
        if scoped else
        f"- 新注册：{m['new_users']} | DAU：{m['dau']} | 入站消息：{m['inbound_messages']}"
    )
    return [
        "## 用户增长",
        headline,
        f"- D1 留存（首聊 cohort）：{_retention_line(m)}",
        "",
    ]


def _proactive_section(m: Dict) -> list:
    cats = m.get("by_category", {})
    if cats:
        cat_parts = [f"{c}: {v['sent']}" for c, v in sorted(cats.items())]
        cat_line = " / ".join(cat_parts)
    else:
        cat_line = "无"
    reply_parts = []
    for c, v in sorted(cats.items()):
        if v["resolved"]:
            reply_parts.append(f"{c} {v['replied']}/{v['resolved']}")
    reply_line = " / ".join(reply_parts) if reply_parts else "窗口未闭合或无样本"
    win = m.get("reply_window_hours") or 24
    return [
        "## 主动消息",
        f"- 总发送：{m['total_sent']} 条（{cat_line}）",
        f"- 覆盖账号：{m['covered_accounts']} | 策略拦截：{m['blocked_count']}"
        f" | 发送失败：{m.get('failed_count', 0)}（下游拒收/限速，非策略拦截）",
        f"- 回复率（{win}h，已闭合分类别）：{reply_line}",
        f"- 整体回复率：{_pct(m['reply_rate_overall'])}"
        f"（{m['replied_total']}/{m['resolved_sent']}）| 首回 P50：{_num(m['reply_latency_p50_sec'])}s",
        "",
    ]


def _dreaming_section(m: Dict) -> list:
    skip = m.get("items_by_skip_reason", {})
    skip_line = (
        " / ".join(f"{k}: {v}" for k, v in sorted(skip.items())) if skip else "无"
    )
    return [
        "## Dreaming",
        f"- 运行：{m['runs_total']} 次（成功 {m['runs_succeeded']} / partial "
        f"{m['runs_partial']} / 失败 {m['runs_failed']}）",
        f"- 覆盖账号：{m['accounts_covered']} | 平均时长：{_num(m['avg_duration_sec'])}s "
        f"| Token：{_num(m['tokens_input'])} in / {_num(m['tokens_output'])} out",
        f"- 记忆产出：{m['items_generated']} 条（应用 {m['items_applied']} / 跳过 "
        f"{m['items_skipped']}，应用率 {_pct(m['items_applied_rate'])}）",
        f"- 跳过原因：{skip_line}",
        f"- 调度健康：{m.get('scheduler_status') or '未知'}"
        f"（最近成功 {m.get('scheduler_last_success_at') or '—'}）",
        "",
    ]


def _onboarding_section(m: Dict, scope: Optional[ReportScope] = None) -> list:
    if scope is not None and scope.onboarding_mode == "not_configured":
        return [
            "## Onboarding",
            "- 当前渠道不使用微信首次聊天 Onboarding；App Onboarding 指标待事件接入。",
            "",
        ]
    return [
        "## Onboarding",
        f"- 存量状态：complete {m['cnt_complete']} / pending {m['cnt_pending']} / "
        f"step1 {m['cnt_step1_sent']} / step2 {m['cnt_step2_sent']} / step3 {m['cnt_step3_sent']} "
        f"/ timed_out {m['cnt_timed_out']}",
        f"- 完成率：{_pct(m['completion_rate'])}"
        f"（complete {m['cnt_complete']} / (complete+timed_out) {m['cnt_complete'] + m['cnt_timed_out']}）",
        f"- 当日注册 cohort：{m['cohort_registered']}，其中已完成 {m['cohort_completed']}",
        "- 人设分布：数据待积累",
        "",
    ]


def render_feishu_summary(sections: Dict,
                          quality_notes: Optional[List[str]] = None,
                          scope: Optional[ReportScope] = None) -> str:
    """渲染推送飞书群的精简纯文本摘要（四域核心数字）。

    飞书自定义机器人 webhook 用 msg_type=text，不渲染 Markdown，故输出纯文本；
    完整 Markdown 日报仍落 nearline/data/reports/。
    """
    u = sections["users"]
    p = sections["proactive"]
    d = sections["dreaming"]
    o = sections["onboarding"]
    growth_metrics = (
        f"新增真人 {u['new_users']} | 真人DAU {u['dau']} | "
        f"活跃AI账号 {u['active_accounts']} | 入站 {u['inbound_messages']} "
        if scope else
        f"新注册 {u['new_users']} | DAU {u['dau']} | 入站 {u['inbound_messages']} "
    )
    lines = [
        f"{scope.title if scope else 'AI4ALL'} 每日运营报告 — {u['date']}",
        f"【用户增长】{growth_metrics}| D1留存 {_retention_line(u)}",
        "【主动消息】"
        f"发送 {p['total_sent']} | 覆盖账号 {p['covered_accounts']} | 策略拦截 {p['blocked_count']} "
        f"| 发送失败 {p.get('failed_count', 0)} "
        f"| 整体回复率 {_pct(p['reply_rate_overall'])}（{p['replied_total']}/{p['resolved_sent']}）",
        "【Dreaming】"
        f"运行 {d['runs_total']}（成功 {d['runs_succeeded']}/partial {d['runs_partial']}/失败 "
        f"{d['runs_failed']}）| 记忆 {d['items_generated']} 条（应用率 {_pct(d['items_applied_rate'])}）"
        f"| 调度 {d.get('scheduler_status') or '未知'}",
    ]
    if scope is not None and scope.onboarding_mode == "not_configured":
        lines.append("【Onboarding】App 指标待事件接入")
    else:
        lines.append(
            "【Onboarding】"
            f"complete {o['cnt_complete']}/pending {o['cnt_pending']} | "
            f"完成率 {_pct(o['completion_rate'])} | 当日注册 cohort "
            f"{o['cohort_registered']}（完成 {o['cohort_completed']}）"
        )
    if scope:
        lines.append("【口径】增长/入站按事件渠道；Dreaming 按 AI 账号归属渠道。")
    if quality_notes:
        lines.append("【数据质量提示】" + "；".join(quality_notes))
    return "\n".join(lines)


def render_daily(sections: Dict, quality_notes: Optional[List[str]] = None,
                 quality_skipped: bool = False,
                 scope: Optional[ReportScope] = None) -> str:
    """把四个域的指标 dict 组合渲染成完整 Markdown 日报。

    sections: {'users':..., 'proactive':..., 'dreaming':..., 'onboarding':...}
    quality_notes: 软质量提示（硬失败不会走到这里——报告已被阻断）。
    quality_skipped: --skip-quality 应急模式时为 True，页脚如实标注。
    """
    date = sections["users"]["date"]
    title = scope.title if scope else "AI4ALL"
    lines = [f"# {title} 每日运营报告 — {date}", ""]
    lines += _users_section(sections["users"], scoped=scope is not None)
    lines += _proactive_section(sections["proactive"])
    lines += _dreaming_section(sections["dreaming"])
    lines += _onboarding_section(sections["onboarding"], scope=scope)
    if quality_notes:
        lines += ["## 数据质量提示（软）"]
        lines += [f"- ⚠️ {note}" for note in quality_notes]
        lines += [""]
    if scope:
        lines += [
            "## 统计口径",
            "- 新增真人、真人 DAU、D1：按产品内真人 UID 去重；未解析 owner 的历史账号按账号兜底。",
            "- 入站与主动消息：按事件实际渠道；Onboarding：按账号归属渠道。",
            "- Dreaming 当前无事件级渠道，按 AI 账号的初始/归属渠道统计。",
            "",
        ]
    quality_mark = "硬质量检查已跳过（--skip-quality）" if quality_skipped else "硬质量检查已通过"
    lines += ["---", f"*由 nearline/run_daily.py 自动生成（已排除 debug 账号；{quality_mark}）*"]
    return "\n".join(lines) + "\n"

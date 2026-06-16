"""数据质量断言（foundation §7）。

在 ETL 之后、报告生成之前运行，读 facts + 操作库交叉对账。
severity='hard'：失败应阻断报告并告警；'soft'：失败只记入报告元数据。
"""

from dataclasses import dataclass
from typing import List, Optional

from nearline.analytics.facts_db import connect_facts
from nearline.analytics.source_db import connect_source


@dataclass
class QualityResult:
    name: str
    severity: str  # 'hard' | 'soft'
    passed: bool
    detail: str


def run_checks(target_date: str, source_db_override: Optional[str] = None,
               facts_db_override: Optional[str] = None) -> List[QualityResult]:
    """跑全部质量检查，返回结果列表（不抛异常，由调用方按 severity 决策）。"""
    facts = connect_facts(facts_db_override)
    source = connect_source(source_db_override)
    results: List[QualityResult] = []
    try:
        # H1：fct_message 账号都能在 dim_account 找到。
        n = facts.execute(
            "SELECT COUNT(*) AS c FROM fct_message f "
            "LEFT JOIN dim_account a ON f.account_id = a.account_id "
            "WHERE a.account_id IS NULL"
        ).fetchone()["c"]
        results.append(QualityResult(
            "fct_message_account_fk", "hard", n == 0,
            f"{n} 条 fct_message 的 account_id 不在 dim_account"))

        # H2：direction/role 一致（inbound→user，outbound→assistant）。
        n = facts.execute(
            "SELECT COUNT(*) AS c FROM fct_message "
            "WHERE (direction = 'inbound' AND role != 'user') "
            "   OR (direction = 'outbound' AND role != 'assistant')"
        ).fetchone()["c"]
        results.append(QualityResult(
            "message_direction_role", "hard", n == 0,
            f"{n} 条 fct_message 的 direction/role 不一致"))

        # H3：status='sent' 的主动消息必须有 sent_at。
        n = facts.execute(
            "SELECT COUNT(*) AS c FROM fct_proactive_message "
            "WHERE status = 'sent' AND (sent_at IS NULL OR sent_at = '')"
        ).fetchone()["c"]
        results.append(QualityResult(
            "proactive_sent_has_sent_at", "hard", n == 0,
            f"{n} 条 sent 主动消息缺 sent_at"))

        # H4：归因到的 reply_message_id 必须在源 messages 存在。
        # 注意：facts 是 append-only 持久基线，可比源行活得更久——账号被解绑清空
        # （wipe_account_data）后，其历史归因到的 reply_message_id 会从源 messages 消失，
        # 这是预期的，不应硬阻断。因此只对“账号在源库仍有存活消息”的孤儿硬失败，
        # 已清空账号的孤儿降级豁免（仅记入 detail）。
        reply_rows = facts.execute(
            "SELECT reply_message_id, account_id FROM fct_proactive_message "
            "WHERE replied = 1 AND reply_message_id IS NOT NULL"
        ).fetchall()
        reply_ids = [r["reply_message_id"] for r in reply_rows]
        hard_missing = 0
        exempt_missing = 0
        if reply_ids:
            placeholders = ",".join("?" * len(reply_ids))
            present = {
                r["id"]
                for r in source.execute(
                    f"SELECT id FROM messages WHERE id IN ({placeholders})", reply_ids
                ).fetchall()
            }
            # 缓存每个账号在源库是否仍有消息，避免重复查询。
            account_has_messages: dict = {}
            for row in reply_rows:
                mid = row["reply_message_id"]
                if mid in present:
                    continue
                aid = row["account_id"]
                if aid not in account_has_messages:
                    cnt = source.execute(
                        "SELECT COUNT(*) AS c FROM messages WHERE account_id = ?", (aid,)
                    ).fetchone()["c"]
                    account_has_messages[aid] = cnt > 0
                if account_has_messages[aid]:
                    hard_missing += 1  # 账号仍存活却缺消息 → 真实归因外键 bug
                else:
                    exempt_missing += 1  # 账号已清空/解绑 → 预期，豁免
        detail = f"{hard_missing} 个 reply_message_id 在源 messages 缺失（活跃账号）"
        if exempt_missing:
            detail += f"；另 {exempt_missing} 条属已清空账号，已豁免"
        results.append(QualityResult(
            "proactive_reply_fk", "hard", hard_missing == 0, detail))

        # H5：memory item 的 dreaming_run_id 关联 run 且 account 一致。
        n = facts.execute(
            "SELECT COUNT(*) AS c FROM fct_dreaming_memory_item i "
            "LEFT JOIN fct_dreaming_run r ON i.dreaming_run_id = r.id "
            "WHERE i.dreaming_run_id IS NOT NULL "
            "  AND (r.id IS NULL OR r.account_id != i.account_id)"
        ).fetchone()["c"]
        results.append(QualityResult(
            "memory_run_fk", "hard", n == 0,
            f"{n} 条 memory item 的 run 关联缺失或账号不一致"))

        # S1（soft）：跨层 DAU 对账——facts 与源直算应一致。
        facts_dau = facts.execute(
            "SELECT COUNT(DISTINCT account_id) AS c FROM fct_message "
            "WHERE event_date = ? AND direction = 'inbound' AND role = 'user'",
            (target_date,),
        ).fetchone()["c"]
        src_dau = source.execute(
            "SELECT COUNT(DISTINCT account_id) AS c FROM messages "
            "WHERE DATE(created_at) = ? AND direction = 'inbound' AND role = 'user'",
            (target_date,),
        ).fetchone()["c"]
        results.append(QualityResult(
            "dau_reconciliation", "soft", facts_dau == src_dau,
            f"facts DAU={facts_dau} vs 源 DAU={src_dau}"))

        # S2（soft，信息性）：daily_usage.message_count 与 facts 入站数差异（口径差异可解释）。
        try:
            du = source.execute(
                "SELECT COALESCE(SUM(message_count), 0) AS c FROM daily_usage WHERE date = ?",
                (target_date,),
            ).fetchone()["c"]
            facts_in = facts.execute(
                "SELECT COUNT(*) AS c FROM fct_message "
                "WHERE event_date = ? AND direction = 'inbound' AND role = 'user'",
                (target_date,),
            ).fetchone()["c"]
            results.append(QualityResult(
                "daily_usage_vs_inbound", "soft", True,
                f"daily_usage.message_count={du} vs facts 入站={facts_in}（口径差异可解释，仅记录）"))
        except Exception as err:
            results.append(QualityResult(
                "daily_usage_vs_inbound", "soft", True, f"跳过（daily_usage 不可读：{err}）"))

        return results
    finally:
        facts.close()
        source.close()


def summarize(results: List[QualityResult]) -> dict:
    """汇总：硬失败列表、软失败列表、是否全部硬检查通过。"""
    hard_failed = [r for r in results if r.severity == "hard" and not r.passed]
    soft_failed = [r for r in results if r.severity == "soft" and not r.passed]
    return {
        "hard_ok": len(hard_failed) == 0,
        "hard_failed": hard_failed,
        "soft_failed": soft_failed,
        "all": results,
    }

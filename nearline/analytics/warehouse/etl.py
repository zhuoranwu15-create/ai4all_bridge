"""ETL 编排：增量刷新基础表（dim_/fct_）。

Phase 0 覆盖 dim_date / dim_account / fct_message。
后续 fct_proactive_message / fct_dreaming_* / fct_onboarding_journey 在此追加。
"""

from datetime import date, datetime
from typing import Dict, Optional

from nearline.analytics.facts_db import connect_facts, init_facts
from nearline.analytics.source_db import connect_source
from nearline.analytics.warehouse import (
    dim_account,
    dim_date,
    fct_dreaming,
    fct_message,
    fct_onboarding,
    fct_proactive,
)


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def refresh_all(source_db_override: Optional[str] = None,
                facts_db_override: Optional[str] = None) -> Dict[str, int]:
    """跑一次完整增量刷新，返回各表处理行数统计。

    facts_db_override 非 None 时写入指定路径，配合 source_db_override 使用可避免污染
    生产 facts.sqlite3（见 run_daily.py / run_backfill.py --facts-db）。
    """
    init_facts(facts_db_override)
    source = connect_source(source_db_override)
    facts = connect_facts(facts_db_override)
    stats: Dict[str, int] = {}
    try:
        now_iso = datetime.now().isoformat(timespec="seconds")

        # dim_date：从最早消息日覆盖到今天（北京自然日）。
        row = source.execute("SELECT MIN(DATE(created_at)) AS d FROM messages").fetchone()
        start = _parse_date(row["d"] if row else None) or date.today()
        stats["dim_date"] = dim_date.upsert_date_range(facts, start, date.today())

        stats["dim_account"] = dim_account.load(source, facts, now_iso)
        stats["fct_message"] = fct_message.load(source, facts)

        # Phase 1 事实表
        proactive = fct_proactive.load(source, facts)
        stats["fct_proactive_inserted"] = proactive["inserted"]
        stats["fct_proactive_refreshed"] = proactive["refreshed"]
        stats["fct_proactive_resolved"] = proactive["resolved"]
        dreaming_runs = fct_dreaming.load_runs(source, facts)
        stats["fct_dreaming_run_inserted"] = dreaming_runs["inserted"]
        stats["fct_dreaming_run_refreshed"] = dreaming_runs["refreshed"]
        stats["fct_dreaming_memory_item"] = fct_dreaming.load_memory_items(source, facts)
        stats["fct_onboarding_journey"] = fct_onboarding.load(source, facts, now_iso)

        facts.commit()
    finally:
        source.close()
        facts.close()
    return stats

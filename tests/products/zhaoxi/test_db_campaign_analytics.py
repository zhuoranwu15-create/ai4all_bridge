"""app.products.zhaoxi.infrastructure.persistence.campaign_analytics：营销活码漏斗埋点分析层
（campaign_funnel_analytics_technical_design.md §3/§7）。

用 connect() 直插并控制时间戳，精确验证按天分桶、campaign 隔离、转化率与除零。
"""
from app.db import (
    get_campaign_funnel,
    get_campaign_visit_stats,
    get_or_create_session,
    record_campaign_visit,
)
from app.db._core import connect
from app.time_utils import beijing_now


def _account(account_id: str) -> None:
    # get_or_create_session 会建 accounts + sessions + profiles，满足下游 FK；不建 channel_bindings。
    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="s",
        sender_name=None,
        chat_id="c",
        session_key=f"sk-{account_id}",
    )


def _platform_user(pid: str, phone: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO platform_users(id, phone) VALUES (?, ?)", (pid, phone))


def _visit(code: str, token: str, visit_date: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO campaign_visits(campaign_code, visitor_token, page, visit_date) "
            "VALUES (?, ?, ?, ?)",
            (code, token, "onboarding", visit_date),
        )


def _attribute(account_id: str, code: str, attributed_at: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO account_campaign_attribution(account_id, campaign_code, mission_id, "
            "onboarding_script_variant, soul_preset_key, ai_name_preset, attributed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (account_id, code, None, None, None, None, attributed_at, attributed_at),
        )


def _binding_completed(account_id: str, pid: str, completed_at: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO binding_intents(id, platform_user_id, account_id, "
            "openclaw_login_session_key, channel, status, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"bi-{account_id}", pid, account_id, f"lsk-{account_id}",
             "openclaw-weixin", "completed", completed_at),
        )


def _channel_binding(account_id: str, first_seen_at: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO channel_bindings(account_id, channel, session_key, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (account_id, "openclaw-weixin", f"csk-{account_id}", first_seen_at, first_seen_at),
        )


def _onboarding_event(account_id: str, to_state: str, event_time: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO analytics_events(account_id, event_name, from_state, to_state, source, event_time) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (account_id, "onboarding_state_changed", None, to_state, "test", event_time),
        )


# ---------------------------------------------------------------------------
# S0 曝光 PV/UV
# ---------------------------------------------------------------------------

def test_record_visit_pv_uv_and_dedup(fresh_db):
    today = beijing_now().date().isoformat()
    # 同一 visitor_token 计 2 次 PV、1 个 UV；另一 token 再 +1 PV/+1 UV。
    record_campaign_visit(campaign_code="C1", visitor_token="v1", page="onboarding")
    record_campaign_visit(campaign_code="C1", visitor_token="v1", page="onboarding")
    record_campaign_visit(campaign_code="C1", visitor_token="v2", page="home")

    stats = get_campaign_visit_stats(campaign_code="C1", date_from=today, date_to=today)
    assert stats["pv"] == 3
    assert stats["uv"] == 2
    assert stats["by_day"] == [{"date": today, "pv": 3, "uv": 2}]


def test_visit_stats_isolated_by_code(fresh_db):
    today = beijing_now().date().isoformat()
    record_campaign_visit(campaign_code="C1", visitor_token="v1")
    record_campaign_visit(campaign_code="C2", visitor_token="v9")
    record_campaign_visit(campaign_code="C2", visitor_token="v8")

    assert get_campaign_visit_stats(campaign_code="C1", date_from=today, date_to=today)["pv"] == 1
    c2 = get_campaign_visit_stats(campaign_code="C2", date_from=today, date_to=today)
    assert c2["pv"] == 2 and c2["uv"] == 2


def test_visit_stats_range_filters_by_day(fresh_db):
    _visit("C1", "v1", "2026-07-01")
    _visit("C1", "v2", "2026-07-02")
    _visit("C1", "v3", "2026-07-05")  # 区间外

    stats = get_campaign_visit_stats(campaign_code="C1", date_from="2026-07-01", date_to="2026-07-02")
    assert stats["pv"] == 2
    assert [d["date"] for d in stats["by_day"]] == ["2026-07-01", "2026-07-02"]


def test_visit_stats_empty_code_returns_zero(fresh_db):
    stats = get_campaign_visit_stats(campaign_code="", date_from="2026-07-01", date_to="2026-07-31")
    assert stats == {"pv": 0, "uv": 0, "by_day": []}


# ---------------------------------------------------------------------------
# S1–S5 漏斗
# ---------------------------------------------------------------------------

def test_funnel_full_counts_and_rates(fresh_db):
    day = "2026-07-10 12:00:00"
    d = "2026-07-10"

    # 3 个账号归因到 C1，下游深度递减
    for i in (1, 2, 3):
        _account(f"a{i}")
        _platform_user(f"pu{i}", f"1380000000{i}")
        _attribute(f"a{i}", "C1", day)
    # 曝光：4 PV / 3 UV（用于 visit_to_register 分母）
    for token in ("v1", "v1", "v2", "v3"):
        _visit("C1", token, d)

    # a1：扫码 + 激活 + step1/step2/complete
    _binding_completed("a1", "pu1", day)
    _channel_binding("a1", day)
    _onboarding_event("a1", "step1_sent", day)
    _onboarding_event("a1", "step2_sent", day)
    _onboarding_event("a1", "complete", day)
    # a2：扫码 + 激活 + step1（卡住，未完成）
    _binding_completed("a2", "pu2", day)
    _channel_binding("a2", day)
    _onboarding_event("a2", "step1_sent", day)
    # a3：仅注册（未扫码）
    # 隔离：另一账号归因到 C2，不应计入 C1
    _account("b1")
    _platform_user("pub", "13911111111")
    _attribute("b1", "C2", day)
    _binding_completed("b1", "pub", day)

    funnel = get_campaign_funnel(campaign_code="C1", date_from="2026-07-01", date_to="2026-07-31")
    t = funnel["totals"]
    assert t["registered"] == 3
    assert t["scanned"] == 2
    assert t["activated"] == 2
    assert t["onboarding_step1"] == 2
    assert t["onboarding_step2"] == 1   # step2 < step1：强制人设活码跳步属正常
    assert t["completed"] == 1
    assert t["pv"] == 4 and t["uv"] == 3

    r = funnel["rates"]
    assert r["visit_to_register"] == round(3 / 3, 4)   # registered / uv
    assert r["register_to_scan"] == round(2 / 3, 4)
    assert r["register_to_complete"] == round(1 / 3, 4)

    # by_day 单日聚合正确
    assert len(funnel["by_day"]) == 1
    assert funnel["by_day"][0]["date"] == d
    assert funnel["by_day"][0]["completed"] == 1


def test_funnel_rate_none_when_no_visits(fresh_db):
    day = "2026-07-10 09:00:00"
    _account("a1")
    _attribute("a1", "C1", day)  # 有注册、无曝光

    funnel = get_campaign_funnel(campaign_code="C1", date_from="2026-07-01", date_to="2026-07-31")
    assert funnel["totals"]["registered"] == 1
    assert funnel["totals"]["uv"] == 0
    assert funnel["rates"]["visit_to_register"] is None       # 分母 0 → None，不除零
    assert funnel["rates"]["register_to_scan"] == 0.0          # 0/1
    assert funnel["rates"]["register_to_complete"] == 0.0


def test_funnel_empty_when_code_absent(fresh_db):
    funnel = get_campaign_funnel(campaign_code="NOPE", date_from="2026-07-01", date_to="2026-07-31")
    assert funnel["totals"] == {
        "pv": 0, "uv": 0, "registered": 0, "scanned": 0, "activated": 0,
        "onboarding_step1": 0, "onboarding_step2": 0, "completed": 0,
    }
    assert funnel["rates"]["visit_to_register"] is None
    assert funnel["by_day"] == []


def test_funnel_range_excludes_out_of_window(fresh_db):
    _account("a1")
    _attribute("a1", "C1", "2026-06-30 23:00:00")  # 区间外

    funnel = get_campaign_funnel(campaign_code="C1", date_from="2026-07-01", date_to="2026-07-31")
    assert funnel["totals"]["registered"] == 0


def test_funnel_scanned_uses_earliest_completed_per_account(fresh_db):
    # 同账号多次完成绑定（重绑），只应计一次扫码，取最早完成日。
    _account("a1")
    _platform_user("pu1", "13800000001")
    _attribute("a1", "C1", "2026-07-10 10:00:00")
    with connect() as conn:
        conn.execute(
            "INSERT INTO binding_intents(id, platform_user_id, account_id, "
            "openclaw_login_session_key, channel, status, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("bi-a1-1", "pu1", "a1", "lsk-a1-1", "openclaw-weixin", "completed", "2026-07-10 10:00:00"),
        )
        conn.execute(
            "INSERT INTO binding_intents(id, platform_user_id, account_id, "
            "openclaw_login_session_key, channel, status, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("bi-a1-2", "pu1", "a1", "lsk-a1-2", "openclaw-weixin", "completed", "2026-07-12 10:00:00"),
        )

    funnel = get_campaign_funnel(campaign_code="C1", date_from="2026-07-01", date_to="2026-07-31")
    assert funnel["totals"]["scanned"] == 1
    # 计入最早完成日 07-10
    by_day = {d["date"]: d for d in funnel["by_day"]}
    assert by_day["2026-07-10"]["scanned"] == 1


def test_funnel_totals_uv_is_range_distinct_not_daily_sum(fresh_db):
    # 同一 visitor_token 跨两天各出现一次：区间 UV 应为 1（去重），而非逐日相加=2。
    _visit("C1", "vX", "2026-07-10")
    _visit("C1", "vX", "2026-07-11")
    _account("a1")
    _attribute("a1", "C1", "2026-07-10 10:00:00")

    funnel = get_campaign_funnel(campaign_code="C1", date_from="2026-07-01", date_to="2026-07-31")
    assert funnel["totals"]["pv"] == 2          # PV 可加
    assert funnel["totals"]["uv"] == 1          # UV 跨天去重，不是 1+1
    # by_day 仍保留当天去重值（各 1）
    assert {d["date"]: d["uv"] for d in funnel["by_day"]} == {
        "2026-07-10": 1, "2026-07-11": 1,
    }
    # 分母用区间级 UV：registered(1)/uv(1) = 1.0，若用逐日和(2)会被错压到 0.5
    assert funnel["rates"]["visit_to_register"] == 1.0


def test_record_visit_caps_oversized_visitor_token(fresh_db):
    today = beijing_now().date().isoformat()
    record_campaign_visit(campaign_code="C1", visitor_token="x" * 500)
    with connect() as conn:
        stored = conn.execute(
            "SELECT visitor_token FROM campaign_visits WHERE campaign_code = 'C1'"
        ).fetchone()["visitor_token"]
    assert len(stored) == 64  # _MAX_VISITOR_TOKEN_LEN，防公开端点塞超大值胀库
    stats = get_campaign_visit_stats(campaign_code="C1", date_from=today, date_to=today)
    assert stats["pv"] == 1 and stats["uv"] == 1


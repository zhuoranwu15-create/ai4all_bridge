"""app.mission_assignment：使命分配编排（agent_mission_and_orchestration_design.md §5）。"""
from app.agent_runtime.persistence import profile_storage
from app.mission_assignment import _pick_mission_id, assign_mission_if_absent
from app.mission_registry import get_mission_template


from tests.factories import create_account as _create_account


def test_pick_mission_id_is_deterministic():
    assert _pick_mission_id("acc-stable") == _pick_mission_id("acc-stable")


def test_pick_mission_id_returns_registered_id():
    from app.mission_registry import MISSION_TEMPLATES

    assert _pick_mission_id("acc-any") in MISSION_TEMPLATES


def test_assign_writes_mission_md_and_db_row(fresh_db):
    _create_account("acc-onboard-complete")

    mission_id = assign_mission_if_absent(account_id="acc-onboard-complete")

    from app.db import get_account_mission

    db_row = get_account_mission(account_id="acc-onboard-complete")
    assert db_row is not None
    assert db_row["mission_id"] == mission_id

    template = get_mission_template(mission_id)
    content = profile_storage.read_file("acc-onboard-complete", "MISSION.md")
    assert content is not None
    assert content.strip() == template.prose.strip()


def test_second_call_is_idempotent_and_returns_same_mission(fresh_db):
    _create_account("acc-idempotent")

    first = assign_mission_if_absent(account_id="acc-idempotent")
    second = assign_mission_if_absent(account_id="acc-idempotent")

    assert first == second


def test_second_call_does_not_rewrite_mission_md(fresh_db, monkeypatch):
    _create_account("acc-no-rewrite")
    assign_mission_if_absent(account_id="acc-no-rewrite")

    calls = []
    original = profile_storage.write_file

    def _spy(account_id, filename, content):
        calls.append(filename)
        return original(account_id, filename, content)

    monkeypatch.setattr(profile_storage, "write_file", _spy)
    assign_mission_if_absent(account_id="acc-no-rewrite")

    assert "MISSION.md" not in calls


def test_different_accounts_can_get_different_missions(fresh_db):
    """账号哈希轮询应覆盖两个模板（大样本下近似均匀，这里只验证两种 id 都出现过）。"""
    _create_account("acc-a")
    _create_account("acc-b")
    seen = set()
    for i in range(20):
        acc = f"acc-spread-{i}"
        _create_account(acc)
        seen.add(assign_mission_if_absent(account_id=acc))
        if len(seen) == 2:
            break
    assert len(seen) == 2


# ---------------------------------------------------------------------------
# 营销活码归因优先命中（campaign_codes_technical_design.md §4.1）
# ---------------------------------------------------------------------------

def test_campaign_attribution_mission_id_takes_priority(fresh_db):
    from app.db.campaign import write_campaign_attribution

    account_id = "acc-campaign-mission"
    _create_account(account_id)
    write_campaign_attribution(
        account_id=account_id,
        campaign_code="MCODE",
        mission_id="mission_002",
        onboarding_script_variant=None,
        soul_preset_key=None,
    )

    # 即使哈希算出的默认值可能是另一个模板，归因指定的 mission_002 应始终优先命中。
    assigned = assign_mission_if_absent(account_id=account_id)

    assert assigned == "mission_002"


def test_no_attribution_falls_back_to_hash_pick(fresh_db):
    account_id = "acc-no-campaign"
    _create_account(account_id)

    assigned = assign_mission_if_absent(account_id=account_id)

    assert assigned == _pick_mission_id(account_id)

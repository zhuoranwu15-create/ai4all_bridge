def test_moderation_sampling_is_deterministic(fresh_db):
    from app.moderation.policy import should_run_llm_review

    first = should_run_llm_review(
        account_id="acc-policy",
        direction="inbound",
        content_kind="text",
        source_type="message",
        source_id="1",
        text_length=80,
        rule_level="pass",
    )
    second = should_run_llm_review(
        account_id="acc-policy",
        direction="inbound",
        content_kind="text",
        source_type="message",
        source_id="1",
        text_length=80,
        rule_level="pass",
    )

    assert first == second
    assert first.sample_rate_percent == 15
    assert first.sampling_reason == "inbound_default"


def test_moderation_policy_short_text_skips_llm_for_normal_user(fresh_db):
    from app.moderation.policy import should_run_llm_review

    decision = should_run_llm_review(
        account_id="acc-policy",
        direction="inbound",
        content_kind="text",
        source_type="message",
        source_id="short",
        text_length=2,
        rule_level="pass",
    )

    assert decision.run_llm_review is False
    assert decision.sample_rate_percent == 0
    assert decision.sampling_reason == "short_text_skip"


def test_moderation_policy_proactive_defaults_to_full_sampling(fresh_db):
    from app.moderation.policy import should_run_llm_review

    decision = should_run_llm_review(
        account_id="acc-policy",
        direction="outbound",
        content_kind="text",
        source_type="outbound_message",
        source_id="out-1",
        text_length=2,
        rule_level="pass",
    )

    assert decision.run_llm_review is True
    assert decision.sample_rate_percent == 100
    assert decision.sampling_reason == "proactive_default"


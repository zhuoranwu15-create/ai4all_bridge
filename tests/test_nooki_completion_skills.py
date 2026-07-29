from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _skill(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_chat_reply_skill_can_complete_an_unstarted_context_task():
    content = _skill("skills/nooki-chat-reply/skill.md")

    assert "未开始" in content
    assert "上一轮" in content or "上一句" in content
    assert '"taskTitle"' in content
    assert "收拾沙发" in content


def test_orchestrator_skill_uses_the_same_completion_contract():
    content = _skill("skills/nooki-orchestrator/skill.md")

    assert "未开始" in content
    assert '"taskTitle"' in content
    assert "收拾沙发" in content


def test_completion_skills_distinguish_claims_from_negation_and_questions():
    for relative_path in (
        "skills/nooki-chat-reply/skill.md",
        "skills/nooki-orchestrator/skill.md",
    ):
        content = _skill(relative_path)
        assert "还没完成" in content, relative_path
        assert "完成了吗" in content, relative_path

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_task_planning_skills_prioritize_dependencies_before_ease():
    skill_paths = [
        "skills/nooki-extract-tasks/skill.md",
        "skills/nooki-chat-reply/skill.md",
        "skills/nooki-orchestrator/skill.md",
        "skills/task-understanding/skill.md",
    ]

    for relative_path in skill_paths:
        content = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "洗脸" in content and "化妆" in content, relative_path
        assert "新增" in content, relative_path


def test_task_skills_preserve_user_stated_duration():
    skill_paths = [
        "skills/nooki-extract-tasks/skill.md",
        "skills/nooki-chat-reply/skill.md",
        "skills/nooki-orchestrator/skill.md",
        "skills/nooki-micro-goal/skill.md",
        "skills/task-understanding/skill.md",
    ]

    for relative_path in skill_paths:
        content = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "明确" in content and "时长" in content, relative_path
        assert "总时长" in content or "suggestedMinutes" in content, relative_path

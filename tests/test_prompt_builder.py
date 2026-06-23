"""Tests for PromptBuilder (TDD — written before implementation)."""
import pytest

from app.prompt_builder import PromptBuilder, extract_section


# Inline profile used by extract_section tests — no dependency on user_profiles module.
_INLINE_PROFILE = """# User Profile

## Soul
你是这个微信账号的个人 AI 陪伴与生活助理。回应要自然、温和、简洁，优先提供情绪陪伴、日常建议和生活协助。

## User Preferences
- 暂无

## Long-term Memory
- 暂无
"""


# ---------------------------------------------------------------------------
# extract_section helper
# ---------------------------------------------------------------------------

class TestExtractSection:
    def test_extracts_soul(self):
        result = extract_section(_INLINE_PROFILE, "Soul")
        assert "AI 陪伴与生活助理" in result

    def test_extracts_user_preferences(self):
        result = extract_section(_INLINE_PROFILE, "User Preferences")
        assert "暂无" in result

    def test_extracts_long_term_memory(self):
        result = extract_section(_INLINE_PROFILE, "Long-term Memory")
        assert "暂无" in result

    def test_missing_section_returns_empty(self):
        result = extract_section(_INLINE_PROFILE, "NonExistent")
        assert result == ""

    def test_strips_surrounding_whitespace(self):
        md = "## Foo\n\n  hello  \n\n## Bar\nother"
        result = extract_section(md, "Foo")
        assert result == "hello"

    def test_section_at_end_of_string(self):
        md = "## Alpha\nfirst\n## Beta\nlast content here"
        result = extract_section(md, "Beta")
        assert result == "last content here"


# ---------------------------------------------------------------------------
# PromptBuilder
# ---------------------------------------------------------------------------

class TestPromptBuilderBasicBuild:
    def setup_method(self):
        self.pb = PromptBuilder()

    def test_returns_string(self):
        out = self.pb.build()
        assert isinstance(out, str)

    def test_empty_build_non_empty(self):
        """Even with all None, safety + output directives should appear."""
        out = self.pb.build()
        assert len(out) > 0

    def test_execution_bias_not_hardcoded_without_agent_context(self):
        out = self.pb.build()
        assert "请积极、主动地提供帮助" not in out
        assert "遇到不清晰的输入时" not in out

    def test_all_params_present(self):
        out = self.pb.build(
            display_name="小助手",
            soul="你是一位温暖的助手。",
            user_prefs="喜欢简短回复",
            long_term_memory="用户叫张三",
            daily_notes="今天天气晴朗",
            carryover_summary="上一段会话说到项目启动",
            system_prompt_override="紧急覆盖指令",
            style="活泼",
            tools=["search", "calendar"],
            skills=["写作", "翻译"],
            model_name="claude-3",
            today="2026-05-17",
        )
        # Identity block
        assert "小助手" in out
        # Soul block
        assert "温暖的助手" in out
        # User prefs
        assert "简短回复" in out
        # Long-term memory
        assert "张三" in out
        # Daily notes
        assert "天气晴朗" in out
        # Session carryover
        assert "项目启动" in out
        # Override
        assert "紧急覆盖指令" in out
        # Style
        assert "活泼" in out
        # Skills
        assert "写作" in out
        # Model name
        assert "claude-3" in out
        # Today
        assert "2026-05-17" in out

    def test_blocks_joined_by_double_newline(self):
        out = self.pb.build(display_name="X", soul="Y")
        assert "\n\n" in out

    def test_project_context_injected_with_openclaw_style_files(self):
        out = self.pb.build(
            agent_context={
                "AGENTS": "# AGENTS\n遵循主 agent 行为准则",
                "SOUL": "# SOUL\n语气自然",
                "IDENTITY": "# IDENTITY\n我是 AI4ALL 个人助手",
                "USER": "# USER\n用户喜欢简洁",
                "RELATIONSHIP": "# RELATIONSHIP\n当前关系处于破冰阶段",
                "TOOLS": "# TOOLS\n无外部工具",
                "MEMORY": "# MEMORY\n用户是工程师",
            }
        )
        assert "【Project Context】" in out
        assert "### AGENTS.md" in out
        assert "### SOUL.md" in out
        assert "### IDENTITY.md" in out
        assert "### USER.md" in out
        # RELATIONSHIP 已不在注入顺序里：即便传入也不进 prompt（关系状态由 DB 维护）
        assert "### RELATIONSHIP.md" not in out
        assert "当前关系处于破冰阶段" not in out
        assert "### TOOLS.md" in out
        assert "### MEMORY.md" in out
        assert "AI4ALL 个人助手" in out

    def test_project_context_ignores_account_level_heartbeat_even_if_passed(self):
        out = self.pb.build(
            agent_context={
                "HEARTBEAT": "# HEARTBEAT\n不主动定时触达",
                "MEMORY": "# MEMORY\n用户是工程师",
            }
        )
        assert "### HEARTBEAT.md" not in out
        assert "不主动定时触达" not in out
        assert "### MEMORY.md" in out

    def test_project_context_suppresses_legacy_profile_duplicates(self):
        out = self.pb.build(
            display_name="旧名字",
            soul="旧 soul",
            user_prefs="旧偏好",
            long_term_memory="旧记忆",
            agent_context={
                "IDENTITY": "新身份",
                "USER": "新用户信息",
                "MEMORY": "新记忆",
            },
        )
        assert "新身份" in out
        assert "新用户信息" in out
        assert "新记忆" in out
        assert "旧名字" not in out
        assert "旧 soul" not in out
        assert "旧偏好" not in out
        assert "旧记忆" not in out

    def test_default_agents_context_contains_execution_bias(self):
        from app.user_profiles import _default_system_templates

        agents = _default_system_templates()["AGENTS.md"]
        out = self.pb.build(agent_context={"AGENTS": agents})
        assert "### AGENTS.md" in out
        assert "请积极、主动地提供帮助" in out
        assert "遇到不清晰的输入时" in out
        assert "你可以主动发送消息，但会比较克制" in out


class TestPromptBuilderTruncation:
    def setup_method(self):
        self.pb = PromptBuilder()

    def test_soul_truncated_at_3000(self):
        long_soul = "A" * 3001
        out = self.pb.build(soul=long_soul)
        # The truncated text should end with the truncation marker
        assert "...[已截断]" in out
        # Verify the boundary precisely: exactly 3000 A's are present, not 3001
        assert "A" * 3000 in out
        assert "A" * 3001 not in out

    def test_soul_not_truncated_at_3000(self):
        exact_soul = "B" * 3000
        out = self.pb.build(soul=exact_soul)
        assert "...[已截断]" not in out
        assert exact_soul in out

    def test_user_prefs_truncated_at_2000(self):
        long_prefs = "P" * 2001
        out = self.pb.build(user_prefs=long_prefs)
        assert "...[已截断]" in out

    def test_long_term_memory_truncated_at_3000(self):
        long_mem = "M" * 3001
        out = self.pb.build(long_term_memory=long_mem)
        assert "...[已截断]" in out

    def test_daily_notes_truncated_at_2000(self):
        long_notes = "N" * 2001
        out = self.pb.build(daily_notes=long_notes)
        assert "...[已截断]" in out

    def test_carryover_summary_truncated_at_2000(self):
        long_carryover = "C" * 2001
        out = self.pb.build(carryover_summary=long_carryover)
        assert "...[已截断]" in out

    def test_agent_context_file_truncated(self):
        long_user = "U" * 2001
        out = self.pb.build(agent_context={"USER": long_user})
        assert "...[已截断]" in out

    def test_default_tools_context_not_truncated(self):
        from app.user_profiles import _default_system_templates

        tools = _default_system_templates()["TOOLS.md"]
        out = self.pb.build(agent_context={"TOOLS": tools})
        assert "web_search" in out
        assert "主动消息能力" not in out
        assert "...[已截断]" not in out


class TestPromptBuilderSkips:
    def setup_method(self):
        self.pb = PromptBuilder()

    def test_identity_skip_when_display_name_none(self):
        out = self.pb.build(display_name=None, soul="some soul")
        # Should not contain "你的名字是"
        assert "你的名字是" not in out

    def test_identity_present_when_display_name_given(self):
        out = self.pb.build(display_name="测试")
        assert "你的名字是" in out
        assert "测试" in out

    def test_daily_notes_skip_when_none(self):
        out = self.pb.build(daily_notes=None)
        assert "【今日备注】" not in out

    def test_daily_notes_present_when_provided(self):
        out = self.pb.build(daily_notes="今日备忘：买菜")
        assert "买菜" in out

    def test_carryover_summary_present_when_provided(self):
        out = self.pb.build(carryover_summary="上一段说到签证材料")
        assert "【会话延续摘要】" in out
        assert "签证材料" in out

    def test_tooling_skip_when_tools_none(self):
        out = self.pb.build(tools=None)
        assert "【可用工具】" not in out

    def test_tooling_skip_when_tools_empty(self):
        out = self.pb.build(tools=[])
        assert "【可用工具】" not in out

    def test_tooling_present_when_tools_provided(self):
        out = self.pb.build(tools=["search", "weather"])
        assert "search" in out
        assert "weather" in out

    def test_skills_skip_when_none(self):
        out = self.pb.build(skills=None)
        assert "【技能列表】" not in out

    def test_skills_skip_when_empty(self):
        out = self.pb.build(skills=[])
        assert "【技能列表】" not in out

    def test_skills_present_when_provided(self):
        out = self.pb.build(skills=["写作辅助", "代码生成"])
        assert "写作辅助" in out
        assert "代码生成" in out


class TestPromptBuilderOverride:
    def setup_method(self):
        self.pb = PromptBuilder()

    def test_override_appears_in_output(self):
        override = "【最高优先级指令：只用英文回复】"
        out = self.pb.build(system_prompt_override=override)
        assert override in out

    def test_override_none_no_error(self):
        out = self.pb.build(system_prompt_override=None)
        assert isinstance(out, str)


class TestPromptBuilderOutputDirectives:
    def setup_method(self):
        self.pb = PromptBuilder()

    def test_no_markdown_instruction_present(self):
        out = self.pb.build()
        assert "Markdown" in out

    def test_style_injected_into_output_directives(self):
        out = self.pb.build(style="正式严肃")
        assert "正式严肃" in out

    def test_fixed_output_directives_before_project_context(self):
        out = self.pb.build(
            agent_context={
                "SOUL": "# SOUL\n温和陪伴",
            }
        )
        assert out.index("【回复格式要求】") < out.index("【Project Context】")

    def test_style_none_no_error(self):
        out = self.pb.build(style=None)
        assert isinstance(out, str)


class TestPromptBuilderRuntime:
    def setup_method(self):
        self.pb = PromptBuilder()

    def test_today_injected(self):
        out = self.pb.build(today="2026-01-15")
        assert "2026-01-15" in out

    def test_model_name_injected(self):
        out = self.pb.build(model_name="gpt-4o")
        assert "gpt-4o" in out

    def test_today_none_no_error(self):
        # When today is not provided, build should still succeed
        out = self.pb.build(today=None)
        assert isinstance(out, str)

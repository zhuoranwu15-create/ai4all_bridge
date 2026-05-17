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
        """Even with all None, execution bias + safety + output directives should appear."""
        out = self.pb.build()
        assert len(out) > 0

    def test_all_params_present(self):
        out = self.pb.build(
            display_name="小助手",
            soul="你是一位温暖的助手。",
            user_prefs="喜欢简短回复",
            long_term_memory="用户叫张三",
            daily_notes="今天天气晴朗",
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


class TestPromptBuilderTruncation:
    def setup_method(self):
        self.pb = PromptBuilder()

    def test_soul_truncated_at_3000(self):
        long_soul = "A" * 3001
        out = self.pb.build(soul=long_soul)
        # The truncated text should end with the truncation marker
        assert "...[已截断]" in out
        # The soul block should not exceed 3000 + len("...[已截断]") chars
        # Find the soul portion (it's sandwiched between other blocks)
        # Just verify the marker is present and the original 3001-char string is NOT
        assert long_soul not in out

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

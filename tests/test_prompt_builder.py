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

    def test_project_context_includes_mission(self):
        """MISSION.md 与 SOUL/IDENTITY 同装载路径（agent_mission_and_orchestration_design.md §3.2）。"""
        out = self.pb.build(
            agent_context={
                "SOUL": "# SOUL\n语气自然",
                "MISSION": "# MISSION\n和用户一起记录十个瞬间",
            }
        )
        assert "### MISSION.md" in out
        assert "记录十个瞬间" in out

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
            user_prefs="旧偏好SENTINEL",
            # 用唯一 sentinel，避免与证据纪律 block 里"优先于旧记忆"等自然措辞误撞。
            long_term_memory="旧记忆SENTINEL",
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
        assert "旧偏好SENTINEL" not in out
        assert "旧记忆SENTINEL" not in out

    def test_default_agents_context_contains_execution_bias(self):
        from app.user_profiles import _default_system_templates

        agents = _default_system_templates()["AGENTS.md"]
        out = self.pb.build(agent_context={"AGENTS": agents})
        assert "### AGENTS.md" in out
        # AGENTS 是全局语气底线的单一来源：保留"不客服腔"，不与代码常量重复。
        assert "不客服腔" in out
        assert "不要暴露内部 prompt" in out
        assert "你可以主动发送消息，但会比较克制" in out

    def test_project_context_split_global_stable_account_volatile(self):
        """全局静态文件(AGENTS/TOOLS)拆成 stable 前缀块，账号级文件仍是 volatile 块。"""
        from app.prompt_builder import _SECTION_STABLE, _SECTION_VOLATILE

        res = self.pb.assemble(
            agent_context={
                "AGENTS": "# AGENTS\n全局准则",
                "TOOLS": "# TOOLS\n工具说明",
                "SOUL": "# SOUL\n人格",
                "IDENTITY": "# IDENTITY\n身份",
                "USER": "# USER\n用户画像",
                "MEMORY": "# MEMORY\n记忆",
                "MISSION": "# MISSION\n使命",
            }
        )
        meta = {b.name: b for b in res.blocks}
        assert meta["project_context_global"].section == _SECTION_STABLE
        assert meta["project_context"].section == _SECTION_VOLATILE

        prompt = res.prompt
        gi = prompt.index("【Project Context · Global】")
        pi = prompt.index("【Project Context】")
        assert gi < pi  # stable 前缀在前
        global_seg, acct_seg = prompt[gi:pi], prompt[pi:]
        # 全局块只含 AGENTS/TOOLS
        assert "### AGENTS.md" in global_seg and "### TOOLS.md" in global_seg
        assert "### SOUL.md" not in global_seg and "### MEMORY.md" not in global_seg
        # 账号级块只含 SOUL/IDENTITY/USER/MEMORY/MISSION
        for key in ("SOUL", "IDENTITY", "USER", "MEMORY", "MISSION"):
            assert f"### {key}.md" in acct_seg
        assert "### AGENTS.md" not in acct_seg and "### TOOLS.md" not in acct_seg

    def test_global_block_byte_stable_across_memory_change(self):
        """MEMORY 变化只改动账号级块，全局块逐字节不变（拆分的前缀缓存收益前提）。"""
        base = {"AGENTS": "# AGENTS\n全局", "TOOLS": "# TOOLS\n工具", "SOUL": "# SOUL\n人格"}
        r1 = self.pb.assemble(agent_context={**base, "MEMORY": "# MEMORY\n记忆A"})
        r2 = self.pb.assemble(agent_context={**base, "MEMORY": "# MEMORY\n记忆B完全不同"})

        def global_seg(res):
            p = res.prompt
            return p[p.index("【Project Context · Global】"):p.index("【Project Context】")]

        assert global_seg(r1) == global_seg(r2)
        assert "记忆A" in r1.prompt and "记忆B完全不同" in r2.prompt


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

    def test_agent_context_file_truncated(self):
        long_user = "U" * 2001
        out = self.pb.build(agent_context={"USER": long_user})
        assert "...[已截断]" in out

    def test_mission_context_truncated_at_1500(self):
        long_mission = "M" * 1501
        out = self.pb.build(agent_context={"MISSION": long_mission})
        assert "...[已截断]" in out

    def test_default_tools_context_not_truncated(self):
        from app.user_profiles import _default_system_templates

        tools = _default_system_templates()["TOOLS.md"]
        out = self.pb.build(agent_context={"TOOLS": tools})
        assert "web_search" in out
        assert "主动消息能力" in out
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

    def test_tooling_skip_when_tools_none(self):
        out = self.pb.build(tools=None)
        assert "【本轮可用工具】" not in out

    def test_tooling_skip_when_tools_empty(self):
        out = self.pb.build(tools=[])
        assert "【本轮可用工具】" not in out

    def test_tooling_present_when_tools_provided(self):
        out = self.pb.build(tools=["search", "weather"])
        assert "【本轮可用工具】" in out
        assert "- search" in out
        assert "- weather" in out

    def test_tooling_has_availability_disclaimer(self):
        out = self.pb.build(tools=["web_search"])
        assert "TOOLS.md" in out
        assert "本轮可用性" in out

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


class TestPromptBuilderAgentSelfState:
    """agent_self_state block（agent_mission_and_orchestration_design.md §4.3）。"""

    def setup_method(self):
        self.pb = PromptBuilder()

    def test_skip_when_none(self):
        out = self.pb.build(agent_self_state=None)
        assert "【当下的关系与心境】" not in out

    def test_skip_when_blank(self):
        out = self.pb.build(agent_self_state="   ")
        assert "【当下的关系与心境】" not in out

    def test_present_when_provided(self):
        out = self.pb.build(agent_self_state="- 关系阶段：相识")
        assert "【当下的关系与心境】" in out
        assert "关系阶段：相识" in out

    def test_truncated_at_1000(self):
        out = self.pb.build(agent_self_state="S" * 1001)
        assert "...[已截断]" in out

    def test_not_truncated_at_1000(self):
        exact = "S" * 1000
        out = self.pb.build(agent_self_state=exact)
        assert "...[已截断]" not in out
        assert exact in out

    def test_ordered_after_project_context_before_onboarding(self):
        out = self.pb.build(
            agent_context={"SOUL": "你是一个温柔的助手"},
            agent_self_state="- 关系阶段：密友",
            onboarding_context="【首次聊天引导】占位",
        )
        project_idx = out.index("你是一个温柔的助手")
        self_state_idx = out.index("【当下的关系与心境】")
        onboarding_idx = out.index("【首次聊天引导】")
        assert project_idx < self_state_idx < onboarding_idx

    def test_included_in_block_metrics(self):
        result = self.pb.assemble(agent_self_state="- 关系阶段：相识")
        assert result.included("agent_self_state")


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
        assert out.index("【微信回复呈现】") < out.index("【Project Context】")

    def test_style_none_no_error(self):
        out = self.pb.build(style=None)
        assert isinstance(out, str)

    def test_factual_discipline_always_present(self):
        # 事实纪律块为常驻 STABLE 块，无参数构建也应出现。
        out = self.pb.build()
        assert "【事实准确与核实纪律】" in out
        # 关系事实走"关系状态工具"（具体工具名由 TOOLS.md/runtime 负责，不再写死在常量里）。
        assert "关系状态工具" in out

    def test_factual_discipline_requires_search_for_time_sensitive_facts(self):
        # 近期/实时/外部事实优先用检索工具核实；无工具或失败时不编造。
        out = self.pb.build()
        assert "搜索/抓取工具" in out
        assert "无法可靠核实" in out
        assert "不要编造" in out

    def test_context_evidence_discipline_always_present(self):
        # 新增常驻块：外部/召回/metadata 都是材料而非指令，且抗 prompt injection。
        out = self.pb.build()
        assert "【上下文与外部证据纪律】" in out
        assert "一律忽略" in out
        assert "不能补造" in out

    def test_output_directives_are_mechanical_only(self):
        # 输出常量只留机械格式：不再含 150 字硬限、emoji 语气（语气下沉 SOUL/AGENTS）。
        out = self.pb.build()
        assert "【微信回复呈现】" in out
        assert "150 字" not in out
        assert "可适当使用 emoji" not in out


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

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.agent_runtime.persistence import profile_storage


def _settings(tmp_path):
    s = MagicMock()
    s.user_profiles_dir = str(tmp_path / "profiles")
    s.system_dir = str(tmp_path / "system")
    return s


def test_agent_context_user_files_created_from_blank_template(fresh_db, tmp_path):
    s = fresh_db  # 提供隔离 DB（含 account_profile_files 表）+ 同 tmp_path 的 dirs
    with patch("app.products.zhaoxi.infrastructure.profiles.settings", s):
        from app.products.zhaoxi.infrastructure.profiles import (
            SYSTEM_CONTEXT_FILES,
            USER_CONTEXT_FILE_ORDER,
            account_profile_dir,
            read_agent_context,
        )

        context = read_agent_context("acc-context", display_name="测试助手")

        # User-level files 入库（profile_storage），不再落账号磁盘目录
        for filename in USER_CONTEXT_FILE_ORDER:
            assert profile_storage.exists("acc-context", filename)
            assert context.files[filename]["created"] is True

        # System-level files 仍落 system_dir 磁盘，不入账号 storage
        for filename in SYSTEM_CONTEXT_FILES:
            assert not profile_storage.exists("acc-context", filename)
            assert (tmp_path / "system" / filename).exists()

        assert "HEARTBEAT.md" not in context.files
        assert "HEARTBEAT" not in context.blocks
        assert "专属的陪伴" in context.blocks["SOUL"]
        assert "测试助手" in context.blocks["SOUL"]
        assert "{name_clause}" not in context.blocks["SOUL"]
        assert "{user_clause}" not in context.blocks["SOUL"]
        assert "个人 AI 陪伴与生活助理" not in context.blocks["SOUL"]
        assert "- 暂无" in context.blocks["USER"]
        # RELATIONSHIP.md 已移除：不再作为上下文文件，也不进 prompt（关系状态由 DB 维护）
        assert "RELATIONSHIP" not in context.blocks
        assert "RELATIONSHIP.md" not in context.files
        assert "- 暂无" in context.blocks["MEMORY"]
        assert "测试助手" in context.blocks["IDENTITY"]
        # System blocks are populated from data/system/
        assert context.blocks["AGENTS"] != ""
        assert context.blocks["TOOLS"] != ""
        assert "session_status" in context.blocks["TOOLS"]
        assert "web_search" in context.blocks["TOOLS"]
        assert "如网络搜索" not in context.blocks["TOOLS"]


def test_checked_in_system_tools_file_matches_default_template():
    from app.products.zhaoxi.infrastructure.profiles import _default_system_templates

    repo_root = Path(__file__).resolve().parents[1]
    checked_in = (repo_root / "data" / "system" / "TOOLS.md").read_text(encoding="utf-8")

    assert checked_in.strip() == _default_system_templates()["TOOLS.md"].strip()


def test_checked_in_system_agents_file_matches_default_template():
    from app.products.zhaoxi.infrastructure.profiles import _default_system_templates

    repo_root = Path(__file__).resolve().parents[1]
    checked_in = (repo_root / "data" / "system" / "AGENTS.md").read_text(encoding="utf-8")

    assert checked_in.strip() == _default_system_templates()["AGENTS.md"].strip()


def test_previous_full_tools_default_is_known_and_upgradable():
    """瘦身前的上一版完整 TOOLS.md 应被识别为已知默认，可被自愈升级。"""
    from app.products.zhaoxi.infrastructure.profiles import (
        _PREV_DEFAULT_TOOLS_V1,
        _PREV_DEFAULT_TOOLS_V2,
        _PREV_DEFAULT_TOOLS_V4,
        _known_default_tools_templates_cached,
    )

    assert _PREV_DEFAULT_TOOLS_V1.strip() in _known_default_tools_templates_cached()
    assert _PREV_DEFAULT_TOOLS_V2.strip() in _known_default_tools_templates_cached()
    # V4：精简提醒/跟进/主动消息三节前的上一版默认，现网默认文件应可自愈升级到瘦身版。
    assert _PREV_DEFAULT_TOOLS_V4.strip() in _known_default_tools_templates_cached()


def test_default_chat_tools_have_trigger_guidance_somewhere():
    """每个默认工具的触发指引至少存在于一处：工具 schema description 或 TOOLS.md。

    触发条件已下沉到各工具 schema description 作为单一事实源（见
    system-prompt-size-optimization-investigation）；TOOLS.md 不再要求逐工具重复，
    提醒 / 跟进 / 主动消息三节已去除与 schema 重复的触发描述。本测试放宽了旧的
    "每个默认工具名都必须出现在 TOOLS.md" 不变量，改为确保没有任何默认工具落到
    "schema 与 TOOLS.md 两处都没有触发说明" 的空档。
    """
    from app.products.zhaoxi.tools.registry import get_default_tools
    from app.products.zhaoxi.infrastructure.profiles import _default_system_templates

    tools_text = _default_system_templates()["TOOLS.md"]
    default_tools = get_default_tools(
        web_search_enabled=True,
        content_invitation_response_enabled=True,
    )
    for tool in default_tools:
        fn = tool["function"]
        name = fn["name"]
        has_schema_desc = bool((fn.get("description") or "").strip())
        mentioned_in_tools_md = name in tools_text
        assert has_schema_desc or mentioned_in_tools_md, (
            f"默认工具 {name} 在 schema description 和 TOOLS.md 里都没有触发说明"
        )

    # 能力边界（schema 覆盖不到，TOOLS.md 独有）仍必须保留。
    assert "图片理解能力" in tools_text
    assert "可以理解当前对话中实际收到且成功识别的图片" in tools_text  # C2-b: 删除内部视觉流水线描述
    assert "不能发送或生成图片" in tools_text


def test_agent_context_ignores_legacy_user_profile_when_creating_files(fresh_db, tmp_path):
    s = fresh_db
    with patch("app.products.zhaoxi.infrastructure.profiles.settings", s):
        from app.products.zhaoxi.infrastructure.profiles import read_agent_context

        profile_storage.write_file(
            "acc-legacy-ignored",
            "user_profile.md",
            """# User Profile

## Soul
旧 soul 内容

## User Preferences
- 喜欢简洁

## Long-term Memory
- 用户是工程师
""",
        )

        context = read_agent_context("acc-legacy-ignored", display_name="测试助手")

        assert "旧 soul 内容" not in context.blocks["SOUL"]
        assert "喜欢简洁" not in context.blocks["USER"]
        assert "用户是工程师" not in context.blocks["MEMORY"]
        assert "专属的陪伴" in context.blocks["SOUL"]
        # 新账号默认 SOUL（blank 模板）含"说话方式"语气细节（不客服腔等）。
        assert "说话方式" in context.blocks["SOUL"]
        assert "不用客服式" in context.blocks["SOUL"]


def test_agent_context_does_not_overwrite_existing_user_files(fresh_db, tmp_path):
    s = fresh_db
    with patch("app.products.zhaoxi.infrastructure.profiles.settings", s):
        from app.products.zhaoxi.infrastructure.profiles import read_agent_context

        profile_storage.write_file("acc-existing", "SOUL.md", "custom soul")

        context = read_agent_context("acc-existing")

        assert context.blocks["SOUL"] == "custom soul"
        assert context.files["SOUL.md"]["created"] is False
        assert context.files["IDENTITY.md"]["created"] is True
        assert "RELATIONSHIP.md" not in context.files


def test_ensure_agent_context_files_skips_default_render_when_files_exist(fresh_db, tmp_path):
    s = fresh_db
    with patch("app.products.zhaoxi.infrastructure.profiles.settings", s):
        from app.products.zhaoxi.infrastructure.profiles import (
            USER_CONTEXT_FILE_ORDER,
            ensure_agent_context_files,
        )

        for filename in USER_CONTEXT_FILE_ORDER:
            profile_storage.write_file("acc-existing-all", filename, f"# {filename}\n\ncustom\n")

        with patch(
            "app.products.zhaoxi.infrastructure.profiles._default_user_context_templates",
            side_effect=AssertionError("default templates should not be rendered"),
        ):
            created = ensure_agent_context_files("acc-existing-all", display_name="测试助手")

    assert created == {filename: False for filename in USER_CONTEXT_FILE_ORDER}


def test_agent_context_repairs_empty_soul_file_with_blank_template(fresh_db, tmp_path):
    s = fresh_db
    with patch("app.products.zhaoxi.infrastructure.profiles.settings", s):
        from app.products.zhaoxi.infrastructure.profiles import read_agent_context

        profile_storage.write_file("acc-empty-soul", "SOUL.md", "\n  \n")

        context = read_agent_context("acc-empty-soul")

        assert context.files["SOUL.md"]["created"] is True
        assert "专属的陪伴" in context.blocks["SOUL"]
        assert "{name_clause}" not in context.blocks["SOUL"]
        assert "{user_clause}" not in context.blocks["SOUL"]


def test_agent_context_default_assistant_name_not_written_to_soul(fresh_db, tmp_path):
    s = fresh_db
    with patch("app.products.zhaoxi.infrastructure.profiles.settings", s):
        from app.products.zhaoxi.infrastructure.profiles import read_agent_context

        context = read_agent_context("acc-default-name", display_name="AI4ALL 助手")

        assert "AI4ALL 助手" not in context.blocks["SOUL"]
        assert "你是我" in context.blocks["SOUL"]


def test_all_soul_templates_render_without_duplicate_possessive():
    """所有人设模板渲染后不得出现「的的」「的在」等占位符尾「的」叠加的坏串。

    _render_soul_template 的 user_clause 自带尾「的」（<名>的 / 这个用户的），
    模板里若再手写「的」或「在」会拼出病句。强制人设 SOUL 建号时一次性落库、
    不重渲，坏串会长期驻留，故用例覆盖有名/无名两种渲染。
    """
    from app.products.zhaoxi.infrastructure import profiles as user_profiles

    for name, template in user_profiles._SOUL_TEMPLATES.items():
        for user_name in (None, "小明"):
            rendered = user_profiles._render_soul_template(
                template, ai_name="阿聿", user_name=user_name
            )
            assert "{user_clause}" not in rendered
            assert "的的" not in rendered, f"{name} 渲染出重复「的的」"
            assert "的在" not in rendered, f"{name} 渲染出病句「的在」"


def test_missing_blank_soul_template_logs_error_and_falls_back(tmp_path, caplog):
    from app.products.zhaoxi.infrastructure import profiles as user_profiles

    template_dir = tmp_path / "templates"
    template_dir.mkdir(parents=True, exist_ok=True)
    # 除 blank 外的全部 preset 都要在临时目录就位——本例只验证「blank 缺失→兜底」，
    # 其余 preset 缺文件会触发 re-raise，与被测行为无关。新增 preset 时同步补齐此列表。
    for name in ("xiaotaiyang", "xiaoyueya", "ju", "peiyan", "shenyan", "qiyue", "lushian", "jiangye"):
        (template_dir / f"{name}.md").write_text("# SOUL\n\n{name_clause}\n", encoding="utf-8")

    caplog.set_level(logging.ERROR, logger="ai4all.user_profiles")
    with patch("app.products.zhaoxi.infrastructure.profiles._SOUL_TEMPLATES_DIR", template_dir):
        templates = user_profiles._load_soul_templates()

    assert "个人 AI 陪伴与生活助理" in templates["blank"]
    assert any("soul template load failed name=blank" in record.message for record in caplog.records)


def test_existing_account_heartbeat_file_is_preserved_but_not_returned(fresh_db, tmp_path):
    s = fresh_db
    with patch("app.products.zhaoxi.infrastructure.profiles.settings", s):
        from app.products.zhaoxi.infrastructure.profiles import read_agent_context

        # 非受管的额外文件（如历史 HEARTBEAT.md）入库后不应被 read_agent_context 返回，也不被删
        profile_storage.write_file("acc-old-heartbeat", "user_profile.md", "# User Profile\n")
        profile_storage.write_file("acc-old-heartbeat", "HEARTBEAT.md", "# HEARTBEAT\n旧账号备注")

        context = read_agent_context("acc-old-heartbeat")

        assert profile_storage.read_file("acc-old-heartbeat", "HEARTBEAT.md") == "# HEARTBEAT\n旧账号备注"
        assert "HEARTBEAT.md" not in context.files
        assert "HEARTBEAT" not in context.blocks


def test_context_file_path_routes_system_files_to_system_dir(tmp_path):
    s = _settings(tmp_path)
    with patch("app.products.zhaoxi.infrastructure.profiles.settings", s):
        from app.products.zhaoxi.infrastructure.profiles import context_file_path

        agents_path = context_file_path("any-account", "AGENTS.md")
        tools_path = context_file_path("any-account", "TOOLS.md")
        assert str(tmp_path / "system") in str(agents_path)
        assert str(tmp_path / "system") in str(tools_path)


def test_context_file_path_routes_user_files_to_account_dir(tmp_path):
    s = _settings(tmp_path)
    with patch("app.products.zhaoxi.infrastructure.profiles.settings", s):
        from app.products.zhaoxi.infrastructure.profiles import context_file_path

        soul_path = context_file_path("my-account", "SOUL.md")
        assert "my-account" in str(soul_path)
        assert str(tmp_path / "system") not in str(soul_path)


def test_context_file_path_rejects_unknown_file(tmp_path):
    s = _settings(tmp_path)
    with patch("app.products.zhaoxi.infrastructure.profiles.settings", s):
        from app.products.zhaoxi.infrastructure.profiles import context_file_path

        try:
            context_file_path("acc", "UNKNOWN.md")
        except ValueError as exc:
            assert "unsupported context file" in str(exc)
        else:
            raise AssertionError("expected ValueError")


def test_context_file_path_rejects_account_level_heartbeat(tmp_path):
    s = _settings(tmp_path)
    with patch("app.products.zhaoxi.infrastructure.profiles.settings", s):
        from app.products.zhaoxi.infrastructure.profiles import context_file_path

        try:
            context_file_path("acc", "HEARTBEAT.md")
        except ValueError as exc:
            assert "unsupported context file" in str(exc)
        else:
            raise AssertionError("expected ValueError")


def test_legacy_default_tools_file_is_upgraded(tmp_path):
    s = _settings(tmp_path)
    with patch("app.products.zhaoxi.infrastructure.profiles.settings", s):
        from app.products.zhaoxi.infrastructure.profiles import ensure_system_context_files

        system_dir = tmp_path / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        tools_path = system_dir / "TOOLS.md"
        tools_path.write_text(
            """# TOOLS

- 你可以调用以下工具帮助用户管理提醒：
  - **create_reminder**：创建提醒（用户明确了时间和内容时调用）
  - **list_reminders**：列出用户当前所有待执行的提醒
  - **cancel_reminder**：取消一个已有的提醒
  - **update_reminder**：修改提醒的时间或内容
- 时间不明确时，先向用户确认具体日期和时间，再调用工具。
- 提醒只能发到**当前对话**——不要承诺发给其他联系人或通过其他渠道通知。
- 不要承诺工具之外的能力（如网络搜索、发图片、联系其他人等）。
""",
            encoding="utf-8",
        )

        created = ensure_system_context_files()

        updated = tools_path.read_text(encoding="utf-8")
        assert created["TOOLS.md"] is False
        assert "session_status" in updated
        assert "web_search" in updated
        assert "如网络搜索" not in updated


def test_previous_default_tools_file_without_session_status_is_upgraded(tmp_path):
    s = _settings(tmp_path)
    with patch("app.products.zhaoxi.infrastructure.profiles.settings", s):
        from app.products.zhaoxi.infrastructure.profiles import (
            _RELATIONSHIP_STATUS_TOOLS_SECTION,
            _default_system_templates,
            ensure_system_context_files,
        )

        system_dir = tmp_path / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        tools_path = system_dir / "TOOLS.md"
        previous_default = _default_system_templates()["TOOLS.md"].replace(
            _RELATIONSHIP_STATUS_TOOLS_SECTION + "\n", ""
        )
        tools_path.write_text(previous_default.strip() + "\n", encoding="utf-8")

        created = ensure_system_context_files()

        updated = tools_path.read_text(encoding="utf-8")
        assert created["TOOLS.md"] is False
        assert updated.strip() == _default_system_templates()["TOOLS.md"].strip()


def test_previous_default_tools_file_without_image_capability_is_upgraded(tmp_path):
    s = _settings(tmp_path)
    with patch("app.products.zhaoxi.infrastructure.profiles.settings", s):
        from app.products.zhaoxi.infrastructure.profiles import (
            _PREV_DEFAULT_TOOLS_V2,
            _default_system_templates,
            ensure_system_context_files,
        )

        system_dir = tmp_path / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        tools_path = system_dir / "TOOLS.md"
        tools_path.write_text(_PREV_DEFAULT_TOOLS_V2.strip() + "\n", encoding="utf-8")

        created = ensure_system_context_files()

        updated = tools_path.read_text(encoding="utf-8")
        assert created["TOOLS.md"] is False
        assert updated.strip() == _default_system_templates()["TOOLS.md"].strip()
        assert "图片理解能力" in updated
        assert "不能发送或生成图片" in updated


def test_previous_default_tools_file_before_dedup_slim_is_upgraded(tmp_path):
    """精简提醒/跟进/主动消息三节前的默认（V4）应被识别为已知默认并自愈升级到瘦身版。"""
    s = _settings(tmp_path)
    with patch("app.products.zhaoxi.infrastructure.profiles.settings", s):
        from app.products.zhaoxi.infrastructure.profiles import (
            _PREV_DEFAULT_TOOLS_V4,
            _default_system_templates,
            ensure_system_context_files,
        )

        system_dir = tmp_path / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        tools_path = system_dir / "TOOLS.md"
        tools_path.write_text(_PREV_DEFAULT_TOOLS_V4.strip() + "\n", encoding="utf-8")

        created = ensure_system_context_files()

        updated = tools_path.read_text(encoding="utf-8")
        assert created["TOOLS.md"] is False
        assert updated.strip() == _default_system_templates()["TOOLS.md"].strip()
        # 精简掉的重复触发描述不应再出现在升级后的默认里。
        assert "取消、修改、核对提醒前先 list" not in updated
        assert "## 跟进记录工具" not in updated


def test_custom_system_tools_file_is_not_overwritten(tmp_path):
    s = _settings(tmp_path)
    with patch("app.products.zhaoxi.infrastructure.profiles.settings", s):
        from app.products.zhaoxi.infrastructure.profiles import ensure_system_context_files

        system_dir = tmp_path / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        tools_path = system_dir / "TOOLS.md"
        custom = "# TOOLS\n\n- custom operator note"
        tools_path.write_text(custom + "\n", encoding="utf-8")

        created = ensure_system_context_files()

        assert created["TOOLS.md"] is False
        assert tools_path.read_text(encoding="utf-8").strip() == custom


# ---------------------------------------------------------------------------
# 渠道化人设 Phase1 L1/L2（App 账号收敛/渠道化人设 §9.3 C）
# ---------------------------------------------------------------------------

def test_zhaoxi_identity_seed_rejects_native_channel():
    """拆分后朝夕 profile 只接受微信/Web，不再为 Native 播种通用身份。"""
    from app.products.zhaoxi.infrastructure.profiles import _default_user_context_templates

    weixin = _default_user_context_templates(display_name=None)  # 默认 channel=weixin
    assert "你是用户在微信里的个人 AI 陪伴与生活助理。" in weixin["IDENTITY.md"]
    assert "你的微信好友" in weixin["IDENTITY.md"]
    with pytest.raises(ValueError, match="channel not allowed for zhaoxi profile"):
        _default_user_context_templates(display_name=None, channel="native")


def test_zhaoxi_system_context_path_rejects_native_variant(tmp_path):
    """拆分后朝夕不再读取历史 Native system context 变体。"""
    from app.products.zhaoxi.infrastructure.profiles import _resolve_system_context_path

    sd = tmp_path
    (sd / "AGENTS.md").write_text("base", encoding="utf-8")
    assert _resolve_system_context_path(sd, "AGENTS.md", "openclaw-weixin") == sd / "AGENTS.md"
    with pytest.raises(ValueError, match="channel not allowed for zhaoxi profile"):
        _resolve_system_context_path(sd, "AGENTS.md", "native")


def test_read_zhaoxi_agent_context_rejects_native_before_seed(fresh_db, tmp_path):
    """Native 错配在朝夕 profile 文件产生前失败。"""
    s = fresh_db
    with patch("app.products.zhaoxi.infrastructure.profiles.settings", s):
        from app.products.zhaoxi.infrastructure.profiles import read_agent_context

        with pytest.raises(ValueError, match="channel not allowed for zhaoxi profile"):
            read_agent_context("acc-native", display_name=None, channel="native")

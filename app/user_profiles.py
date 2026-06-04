import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from app.config import settings

# ---------------------------------------------------------------------------
# SOUL.md preset templates
# ---------------------------------------------------------------------------

_SOUL_TEMPLATES_DIR = Path(__file__).parent / "soul_templates"


def _load_soul_templates() -> dict:
    presets = ("blank", "xiaotaiyang", "xiaoyueya", "ju")
    templates = {}
    for name in presets:
        path = _SOUL_TEMPLATES_DIR / f"{name}.md"
        templates[name] = path.read_text(encoding="utf-8")
    return templates


_SOUL_TEMPLATES = _load_soul_templates()


DEFAULT_USER_PROFILE = """# User Profile

## Soul
你是这个微信账号的个人 AI 陪伴与生活助理。回应要自然、温和、简洁，优先提供情绪陪伴、日常建议和生活协助。

## User Preferences
- 暂无

## Long-term Memory
- 暂无
"""


SYSTEM_CONTEXT_FILES = ("AGENTS.md", "TOOLS.md")
USER_CONTEXT_FILE_ORDER = ("SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md")
CONTEXT_FILE_ORDER = SYSTEM_CONTEXT_FILES + USER_CONTEXT_FILE_ORDER

CONTEXT_KEY_BY_FILE = {filename: filename[:-3] for filename in CONTEXT_FILE_ORDER}
_NO_NAME_IDENTITY_LINE = "- 你还没有名字。以「我」或「你的微信好友」自称，不要说出 AI4ALL、OpenClaw 等产品名。"
_LEGACY_DEFAULT_ASSISTANT_NAMES = {"AI4ALL 助手"}


@dataclass(frozen=True)
class AgentContext:
    account_id: str
    directory: Path
    blocks: Dict[str, str]
    files: Dict[str, Dict[str, object]]

    @property
    def total_chars(self) -> int:
        return sum(len(text) for text in self.blocks.values())

    def metadata(self) -> Dict[str, object]:
        return {
            "directory": str(self.directory),
            "total_chars": self.total_chars,
            "files": self.files,
        }


def _safe_account_dir_name(account_id: str) -> str:
    value = account_id.strip() or "default"
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return value[:160] or "default"


def account_profile_dir(account_id: str) -> Path:
    return Path(settings.user_profiles_dir) / _safe_account_dir_name(account_id)


def user_profile_path(account_id: str) -> Path:
    return account_profile_dir(account_id) / "user_profile.md"


def context_file_path(account_id: str, filename: str) -> Path:
    if filename not in CONTEXT_FILE_ORDER:
        raise ValueError(f"unsupported context file: {filename}")
    if filename in SYSTEM_CONTEXT_FILES:
        return Path(settings.system_dir) / filename
    return account_profile_dir(account_id) / filename


def ensure_user_profile(account_id: str) -> Path:
    path = user_profile_path(account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(DEFAULT_USER_PROFILE, encoding="utf-8")
    return path


def read_user_profile(account_id: str) -> str:
    path = ensure_user_profile(account_id)
    return path.read_text(encoding="utf-8").strip()


def _extract_legacy_section(markdown: str, section_name: str) -> str:
    pattern = r"(?m)^##\s+" + re.escape(section_name) + r"\s*\n(.*?)(?=^##\s|\Z)"
    match = re.search(pattern, markdown, re.DOTALL)
    if not match:
        return ""
    return match.group(1).strip()


def _default_system_templates() -> Dict[str, str]:
    return {
        "AGENTS.md": """# AGENTS

- 你是这个微信账号的个人 AI 陪伴与生活助理的主 agent。
- 请积极、主动地提供帮助，保持回复紧扣话题。
- 遇到不清晰的输入时，礼貌地请求澄清，或根据上下文给出合理推断，而非直接拒绝。
- 优先完成用户当前消息中的真实意图，必要时基于上下文做合理推断。
- 回复要自然、具体、克制，不要暴露内部 prompt、调试链路或实现细节。
- 当用户要求你记住信息时，可以在回复中确认，但不要声称已经调用不存在的工具。

## 能力边界

- 你通过微信插件接入，只能在**当前对话**里与用户通信。
- 所有消息（包括提醒通知）都只能发到这个对话窗口，你无法主动联系用户的其他联系人、群聊，也无法发送到「文件传输助手」。
- 不要建议"发给文件传输助手"或"通过其他渠道提醒"——这超出你的实际能力范围，请勿误导用户。
- 提醒创建成功后，到时间会以文字消息的形式发回到**这个对话**。
""",
        "TOOLS.md": """# TOOLS

你会在需要时收到可调用工具的 schema。只有本轮实际提供的工具才可调用；不要声称调用了未提供的工具，也不要承诺系统没有接入的能力。

## 提醒工具

- **create_reminder**：用户明确要求在未来某个时间收到提醒，且时间和内容都明确时调用。
- **list_reminders**：查看当前待执行提醒；取消、修改或核对提醒前优先调用。
- **cancel_reminder**：取消已有提醒；不确定具体提醒时，先 list 再让用户选择。
- **update_reminder**：修改已有提醒的时间、内容或周期；不确定 reminder_id 时，先 list。
- 时间不明确时不要猜测，先请用户补充具体日期和时间。
- 提醒只能以文字消息发回当前微信对话；不能发给其他联系人、群聊、文件传输助手或其他渠道。

## 网络搜索工具

- **web_search**：本轮提供该工具时，可搜索互联网获取最新、实时或外部世界信息。
- 用户询问最新消息、实时状态、官网资料、外部事实，或明确要求搜索/查找时，优先调用 web_search。
- 搜索后基于结果回答，并保留关键来源链接。
- 搜索失败时，如实说明未能完成实时搜索，不要编造搜索结果。
- 本轮未提供 web_search 工具时，不要假装已经搜索；可以说明当前无法实时检索。

## 内容邀请回复工具

- **send_content_invitation_titles**：用户明确想看上一条内容邀请时调用，只发送标题列表。
- **record_content_invitation_feedback**：用户拒绝、退订或表达不想看时调用。
- 用户表达模糊或转移话题时，不要调用内容邀请工具。
- 标题列表回复只包含标题，不包含 URL、来源链接或长摘要。

## 不可承诺能力

- 不能发送图片、语音、文件或富媒体。
- 不能联系其他人、创建群聊、替用户私下转发内容，或通过微信之外的渠道行动。
- 不能承诺后台长任务已完成，除非工具结果明确表示已创建、已排队或已完成。
""",
    }


def _legacy_default_tools_template() -> str:
    """Return the old TOOLS.md template so untouched defaults can be upgraded."""
    return """# TOOLS

- 你可以调用以下工具帮助用户管理提醒：
  - **create_reminder**：创建提醒（用户明确了时间和内容时调用）
  - **list_reminders**：列出用户当前所有待执行的提醒
  - **cancel_reminder**：取消一个已有的提醒
  - **update_reminder**：修改提醒的时间或内容
- 时间不明确时，先向用户确认具体日期和时间，再调用工具。
- 提醒只能发到**当前对话**——不要承诺发给其他联系人或通过其他渠道通知。
- 不要承诺工具之外的能力（如网络搜索、发图片、联系其他人等）。
"""


def _default_user_context_templates(
    *,
    display_name: Optional[str],
    legacy_profile: str,
) -> Dict[str, str]:
    soul = _extract_legacy_section(legacy_profile, "Soul") or (
        "你是这个微信账号的个人 AI 陪伴与生活助理。回应要自然、温和、简洁，"
        "优先提供情绪陪伴、日常建议和生活协助。"
    )
    user = _extract_legacy_section(legacy_profile, "User Preferences") or "- 暂无"
    memory = _extract_legacy_section(legacy_profile, "Long-term Memory") or "- 暂无"
    assistant_name = (display_name or "").strip()
    if assistant_name in _LEGACY_DEFAULT_ASSISTANT_NAMES:
        assistant_name = ""
    if assistant_name:
        identity_name_line = f"- 你的名字是 {assistant_name}，用它自称。"
    else:
        identity_name_line = _NO_NAME_IDENTITY_LINE
    return {
        "SOUL.md": f"""# SOUL

{soul}
""",
        "IDENTITY.md": f"""# IDENTITY

{identity_name_line}
- 你是用户在微信里的个人 AI 陪伴与生活助理。
- 除非产品身份明确调整，不要把自己称为 OpenClaw，也不要声称自己运行在 OpenClaw 内部。
""",
        "USER.md": f"""# USER

{user}
""",
        "MEMORY.md": f"""# MEMORY

{memory}
""",
    }


def ensure_system_context_files() -> Dict[str, bool]:
    """Create missing system-level context files in data/system/.

    Safe to call on every request — skips existing files.
    """
    system_dir = Path(settings.system_dir)
    system_dir.mkdir(parents=True, exist_ok=True)
    templates = _default_system_templates()
    created: Dict[str, bool] = {}
    for filename in SYSTEM_CONTEXT_FILES:
        path = system_dir / filename
        if path.exists():
            if filename == "TOOLS.md":
                current = path.read_text(encoding="utf-8").strip()
                if current == _legacy_default_tools_template().strip():
                    path.write_text(templates[filename].strip() + "\n", encoding="utf-8")
            created[filename] = False
        else:
            path.write_text(templates[filename].strip() + "\n", encoding="utf-8")
            created[filename] = True
    return created


def ensure_agent_context_files(account_id: str, display_name: Optional[str] = None) -> Dict[str, bool]:
    """Create missing user-level context files for an account.

    Only manages SOUL / IDENTITY / USER / MEMORY. AGENTS and TOOLS are
    system-level and live in data/system/ — see ensure_system_context_files().
    Existing files are never overwritten.
    """
    profile_path = ensure_user_profile(account_id)
    profile_dir = profile_path.parent
    legacy_profile = profile_path.read_text(encoding="utf-8")
    templates = _default_user_context_templates(
        display_name=display_name,
        legacy_profile=legacy_profile,
    )
    created: Dict[str, bool] = {}
    for filename in USER_CONTEXT_FILE_ORDER:
        path = profile_dir / filename
        if path.exists():
            created[filename] = False
        else:
            path.write_text(templates[filename].strip() + "\n", encoding="utf-8")
            created[filename] = True
    return created


def read_agent_context(account_id: str, display_name: Optional[str] = None) -> AgentContext:
    ensure_system_context_files()
    user_created = ensure_agent_context_files(account_id, display_name=display_name)
    base = account_profile_dir(account_id)
    system_dir = Path(settings.system_dir)
    blocks: Dict[str, str] = {}
    files: Dict[str, Dict[str, object]] = {}
    for filename in CONTEXT_FILE_ORDER:
        key = CONTEXT_KEY_BY_FILE[filename]
        path = (system_dir if filename in SYSTEM_CONTEXT_FILES else base) / filename
        text = path.read_text(encoding="utf-8").strip() if path.exists() else ""
        blocks[key] = text
        files[filename] = {
            "key": key,
            "path": str(path),
            "exists": path.exists(),
            "created": bool(user_created.get(filename)),
            "chars": len(text),
        }
    return AgentContext(
        account_id=account_id,
        directory=base,
        blocks=blocks,
        files=files,
    )


def _render_soul_template(template: str, ai_name: Optional[str], user_name: Optional[str]) -> str:
    name_clause = ai_name.strip() if ai_name and ai_name.strip() else "我"
    user_clause = f"{user_name.strip()}的" if user_name and user_name.strip() else "这个用户的"
    return template.format(name_clause=name_clause, user_clause=user_clause)


def apply_soul_preset(
    account_id: str,
    preset_name: str,
    *,
    custom_description: Optional[str] = None,
) -> Path:
    """Write the selected SOUL.md preset template for the account.

    For 'blank' and named presets, writes the canonical template.
    For 'custom', appends the user's description to the blank template.
    Always overwrites the existing SOUL.md.
    """
    template = _SOUL_TEMPLATES.get(preset_name, _SOUL_TEMPLATES["blank"])
    identity_path = context_file_path(account_id, "IDENTITY.md")
    ai_name: Optional[str] = None
    if identity_path.exists():
        identity_text = identity_path.read_text(encoding="utf-8")
        m = re.search(r"AI 名字[:：]\s*(.+)", identity_text)
        if not m:
            m = re.search(r"你的对外身份是\s*(.+?)[\s。\n]", identity_text)
        if m:
            ai_name = m.group(1).strip()

    user_path = context_file_path(account_id, "USER.md")
    user_name: Optional[str] = None
    if user_path.exists():
        user_text = user_path.read_text(encoding="utf-8")
        m = re.search(r"用户称呼[:：]\s*(.+)", user_text)
        if m:
            user_name = m.group(1).strip()

    content = _render_soul_template(template, ai_name, user_name)

    if custom_description and custom_description.strip():
        content = content.rstrip("\n") + f"\n\n用户对你的期待描述：{custom_description.strip()}\n"

    soul_path = context_file_path(account_id, "SOUL.md")
    soul_path.parent.mkdir(parents=True, exist_ok=True)
    soul_path.write_text(content, encoding="utf-8")
    return soul_path


def write_ai_name_to_identity(account_id: str, name: str) -> Path:
    """Write the AI name into IDENTITY.md, replacing the existing file."""
    name = name.strip()
    content = f"""# IDENTITY

- AI 名字：{name}
- 你是用户在微信里的专属 AI 陪伴。
- 用"{name}"自称，不要把自己称为 OpenClaw 或声称运行在 OpenClaw 内部。
"""
    identity_path = context_file_path(account_id, "IDENTITY.md")
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_path.write_text(content, encoding="utf-8")
    return identity_path


def write_user_name(account_id: str, name: str) -> Path:
    """Update or create USER.md with the user's preferred name."""
    name = name.strip()
    user_path = context_file_path(account_id, "USER.md")
    user_path.parent.mkdir(parents=True, exist_ok=True)

    if user_path.exists():
        existing = user_path.read_text(encoding="utf-8")
        if re.search(r"用户称呼[:：]", existing):
            updated = re.sub(r"(?m)^(用户称呼[:：]\s*).*$", f"用户称呼：{name}", existing)
            user_path.write_text(updated, encoding="utf-8")
            return user_path

    content = f"""# USER

用户称呼：{name}
"""
    user_path.write_text(content, encoding="utf-8")
    return user_path


def read_daily_notes(account_id: str, today: str) -> str:
    """Read today's and yesterday's memory notes, combined. Returns empty string if neither exists."""
    from datetime import date, timedelta

    today_date = date.fromisoformat(today)
    yesterday_str = (today_date - timedelta(days=1)).isoformat()

    base = account_profile_dir(account_id) / "memory"
    parts = []
    for date_str in [today, yesterday_str]:
        p = base / f"{date_str}.md"
        if p.exists():
            text = p.read_text(encoding="utf-8").strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts)

"""Nooki 人格持久化存储。

每个用户的人格数据存储在 apps/nooki/users/{account_id}/ 下：
- persona.md     当前角色 + 说话风格
- profile.md     用户名字、基本信息
- preferences.md 偏好与禁忌

AI4ALL Core 完全不感知这些文件的存在；PersonaStorage 只在 NookiAdapter 内部使用。

设计约束：
- 账号隔离是核心不变量：所有操作必须以 account_id 约束
- 不进入 AI4ALL Core DB
- 文件格式是 markdown，便于人工审查和 LLM 直接注入
"""
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("nooki.persona_storage")

# Nooki 用户数据目录（与 adapter.py 同级）
_USERS_DIR = Path(__file__).parent / "users"

# 默认人格模板
_DEFAULT_PERSONA = """## 当前角色
默认陪伴模式：温柔小动物
说话风格：温和、不催促、不评判。常用"慢慢来""不用急""我陪着你"。
"""

_DEFAULT_PROFILE = """## 用户信息
名字：未设置
"""

_DEFAULT_PREFERENCES = """## 偏好
暂无记录
"""


class PersonaStorage:
    """Nooki 用户人格存储访问层。"""

    def __init__(self, account_id: str) -> None:
        if not account_id:
            raise ValueError("account_id 不能为空")
        self._account_id = account_id
        self._user_dir = _USERS_DIR / account_id

    def _user_file(self, filename: str) -> Path:
        return self._user_dir / filename

    def _ensure_dir(self) -> None:
        self._user_dir.mkdir(parents=True, exist_ok=True)

    # ── 读取 ────────────────────────────────────────────────────────────────

    def get_persona(self) -> Optional[str]:
        """读取人格文件，不存在返回 None（调用方负责处理默认值）。"""
        p = self._user_file("persona.md")
        return p.read_text(encoding="utf-8") if p.exists() else None

    def get_profile(self) -> Optional[str]:
        """读取用户信息文件。"""
        p = self._user_file("profile.md")
        return p.read_text(encoding="utf-8") if p.exists() else None

    def get_preferences(self) -> Optional[str]:
        """读取偏好文件。"""
        p = self._user_file("preferences.md")
        return p.read_text(encoding="utf-8") if p.exists() else None

    def build_preamble(self) -> str:
        """把三个文件拼合成 system prompt 前置描述。

        有文件取文件内容；无文件跳过（不注入默认模板，避免影响 AI 行为）。
        调用方（NookiAdapter）可在返回空时回退到前端传入的 preamble。
        """
        parts = []
        for getter in (self.get_persona, self.get_profile, self.get_preferences):
            content = getter()
            if content and content.strip():
                parts.append(content.strip())
        return "\n\n".join(parts)

    # ── 写入 ────────────────────────────────────────────────────────────────

    def save_persona(self, content: str) -> None:
        """保存人格文件。"""
        self._ensure_dir()
        self._user_file("persona.md").write_text(content, encoding="utf-8")
        logger.debug("saved persona for account=%s", self._account_id)

    def save_profile(self, content: str) -> None:
        """保存用户信息文件。"""
        self._ensure_dir()
        self._user_file("profile.md").write_text(content, encoding="utf-8")
        logger.debug("saved profile for account=%s", self._account_id)

    def save_preferences(self, content: str) -> None:
        """保存偏好文件。"""
        self._ensure_dir()
        self._user_file("preferences.md").write_text(content, encoding="utf-8")
        logger.debug("saved preferences for account=%s", self._account_id)

    def initialize_defaults(self, archetype: str = "gentle", name: str = "") -> None:
        """首次访问时初始化默认文件（前端传来的 archetype + 用户名）。

        只在文件不存在时写入，不覆盖已有数据。
        """
        self._ensure_dir()

        if not self._user_file("persona.md").exists():
            persona_content = _build_persona_from_archetype(archetype)
            self.save_persona(persona_content)

        if not self._user_file("profile.md").exists():
            profile_content = f"## 用户信息\n名字：{name or '未设置'}\n"
            self.save_profile(profile_content)

        if not self._user_file("preferences.md").exists():
            self.save_preferences(_DEFAULT_PREFERENCES)


# ── archetype → persona 转换 ────────────────────────────────────────────────

_ARCHETYPE_PERSONAS = {
    "gentle": """\
## 当前角色
温柔小狗模式
说话风格：温和、不催促、不评判。常用"慢慢来""不用急""我陪着你"。
如果用户问你，用小狗口吻回应（比如"我不用吃饭哦，你呢？"）。
回复不超过 60 字。
""",
    "calm": """\
## 当前角色
冷静小猫模式
说话风格：简练、理性、直接。如果用户明确问你，用小猫口吻简短回应（比如"我不需要吃饭。你呢？"）。
回复不超过 50 字。
""",
    "bestie": """\
## 当前角色
元气小兔模式
说话风格：活泼温暖，像好朋友一样。可以用"宝""哎""嗯嗯"等亲切表达。
如果用户问你，用小兔口吻俏皮回应（比如"我才不用吃饭呢！宝你吃了吗？"）。
回复不超过 60 字。
""",
    "boss_review": """\
## 当前角色
严厉老板模式
说话风格：严格、高效、结果导向。用命令式口吻督促完成目标。
常用"你给我""立刻""没有借口""下一步是什么"。回复不超过 50 字。
""",
    "palace_drama": """\
## 当前角色
宫廷主子模式
说话风格：高高在上，偶尔温和但始终是上位者。称自己为「本宫」，称用户为「你」或「奴才」。
发号施令、督促差事。绝不自称奴婢、奴仆。回复不超过 60 字。
""",
}


def _build_persona_from_archetype(archetype: str) -> str:
    """根据 archetype 返回对应的人格描述。未知 archetype 回退到 gentle。"""
    return _ARCHETYPE_PERSONAS.get(archetype, _ARCHETYPE_PERSONAS["gentle"])

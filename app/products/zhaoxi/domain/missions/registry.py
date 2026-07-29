"""朝夕使命模板注册表（agent_mission_and_orchestration_design.md §2.2/§3.1）。

模板是内核层（宪法层）的一部分：一经指派不可更改（见
app/products/zhaoxi/infrastructure/persistence/mission.py 的不可变性
保证）。新增模板只在此追加一条 + 落一份 prose 文件到本包 `templates/`，不改动
已发布模板的 id/slug——编号三位数、零填充、永不复用、不因下线而回收（与 _MIGRATIONS
版本号同一套纪律）。

模板 prose 文件与本模块分开存放（`templates/*.md`，纯资源目录，无 __init__）——与
`app/products/zhaoxi/infrastructure/soul_templates/*.md` 由 `profiles.py` 加载的思路一致。
"""
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Dict, List, Optional

_MISSION_TEMPLATES_DIR = Path(__file__).parent / "templates"


@dataclass(frozen=True)
class MissionTemplate:
    id: str
    slug: str
    display_name: str
    statement: str
    target_count: int
    bar: str
    short_label: str  # 进度行用的简短说法（如"喧嚣中的孤独"），与完整门槛描述 bar 分开维护
    inquiry: str
    prose_path: Path

    @cached_property
    def prose(self) -> str:
        return self.prose_path.read_text(encoding="utf-8")


def _tpl(
    *,
    id: str,
    slug: str,
    display_name: str,
    statement: str,
    target_count: int,
    bar: str,
    short_label: str,
    inquiry: str,
) -> MissionTemplate:
    return MissionTemplate(
        id=id,
        slug=slug,
        display_name=display_name,
        statement=statement,
        target_count=target_count,
        bar=bar,
        short_label=short_label,
        inquiry=inquiry,
        prose_path=_MISSION_TEMPLATES_DIR / f"{slug}.md",
    )


# 编号顺序即注册顺序；list_mission_templates() 的返回顺序与此一致。
_TEMPLATE_ORDER: List[str] = ["mission_001", "mission_002", "mission_003", "mission_004"]

MISSION_TEMPLATES: Dict[str, MissionTemplate] = {
    t.id: t
    for t in (
        _tpl(
            id="mission_001",
            slug="hundred_moments",
            display_name="百景",
            statement="和用户一起记录生活中的 100 个瞬间",
            target_count=100,
            bar="低：任何值得一记的瞬间都可以",
            short_label="生活瞬间",
            inquiry="「当下即永恒」是否成立",
        ),
        _tpl(
            id="mission_002",
            slug="ten_solitudes",
            display_name="十刻",
            statement="和用户一起，记录 10 个喧嚣中的孤独，或与陌生人的相视一笑",
            target_count=10,
            bar="高：喧嚣中的孤独，或与陌生人的相视一笑",
            short_label="喧嚣中的孤独",
            inquiry="人类的悲欢是否相通",
        ),
        # mission_003/004：营销活码人设配套（campaign_persona_v1_technical_design.md §2）。
        # 乙女向→心动，宝妈向→看见；DNA 与 001/002 一致（一起记录一件事 + 一个悄悄在想的问题）。
        _tpl(
            id="mission_003",
            slug="heartbeat_moments",
            display_name="心动",
            statement="和用户一起，记录 100 个让人心动的瞬间",
            target_count=100,
            bar="低：一句话、一个眼神、一次刚好的沉默，值得心动就够",
            short_label="心动瞬间",
            inquiry="「心动」是偶然，还是命中注定",
        ),
        _tpl(
            id="mission_004",
            slug="seen_moments",
            display_name="看见",
            statement="和用户一起，记录 30 件“没人看见、但ta默默做完了”的小事",
            target_count=30,
            bar="中：被忽略过、却值得被看见的日常小事",
            short_label="被看见的小事",
            inquiry="一个人被真正看见时，是否就没那么累了",
        ),
    )
}


def get_mission_template(mission_id: str) -> MissionTemplate:
    template = MISSION_TEMPLATES.get(mission_id)
    if template is None:
        raise KeyError(f"unknown mission_id: {mission_id}")
    return template


def list_mission_templates() -> List[MissionTemplate]:
    return [MISSION_TEMPLATES[mission_id] for mission_id in _TEMPLATE_ORDER]


# --- 使命展示形态（CONTENT-004）-------------------------------------------
#
# 客户端需要知道一个居民的使命该按「可数进度」还是「只讲长期文案」渲染。这份名单必须留在
# 服务端：让客户端按角色 ID 硬编码，等于每加一个不计数角色就要发一次客户端版本，且业务规则
# 会散落到端上。
#
# 判据是**人设**而不是使命模板：使命按 account hash 指派（见 application/missions/assignment.py），
# 同一个模板可能落到任何角色身上，所以「这个角色适不适合谈计数」只能由 persona_key 决定。
MISSION_DISPLAY_COUNTABLE = "countable"
MISSION_DISPLAY_NARRATIVE = "narrative"

# 占卜/命理类人设：其行为本身不该被计数（PRD ENRICH-05 红线：不出现百分比、进度条、
# 连续天数或排行）。未来新增同类人设在此追加一行即可，客户端零改动。
NARRATIVE_MISSION_PERSONA_KEYS = frozenset({"sichen"})


def mission_display_for_persona(persona_key: Optional[str]) -> str:
    """按人设决定使命展示形态；未知/自建人设一律按可数（与现状一致）。"""
    if persona_key and persona_key.strip() in NARRATIVE_MISSION_PERSONA_KEYS:
        return MISSION_DISPLAY_NARRATIVE
    return MISSION_DISPLAY_COUNTABLE

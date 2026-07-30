"""预设居民的入场文案（CONTENT-001 欢迎语 / CONTENT-002 自我介绍动态）。

两条文案在**确认候选的同一事务里**落库：欢迎语作为会话第一条真实 ``assistant`` 消息，
自我介绍动态作为一条 ``source_type='resident_intro'`` 的已发布 Feed post。

三条口径（PRD ENRICH-01 与 v1.5 计划 §10）：

1. **各角色独立文案，不共用模板**。服务端不写兜底句——查不到 ``persona_key`` 就两条都不落库，
   宁可少一条内容，也不让一句通用欢迎语顶在所有角色头上。
2. **文案里不出现角色自己的名字**。实例名是用户在确认时自己给的（可以把司辰改叫「老辰」），
   文案里写死名字会在改名后穿帮。同理见 `persona_seed_json` 的名随用户写法。
3. **不写与落库时间相关的内容**（节气、天气、今天几号）。这两条文案是确认时一次性落库的，
   之后不会重写，任何"此时此刻"的表述都会立刻过期。

按 ``persona_key`` 索引而不是 ``template_id``：``persona_key`` 跨模板换版稳定，运营改人设
内容换新 ``template_id`` 时文案不会失联。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class ResidentIntroContent:
    """一位预设居民的入场文案对；两条同时提供，缺一即视为该人设未配文案。"""

    welcome_message: str
    intro_post: str


# 五位首发/v1.5 预设人设的文案。新增预设角色时在此追加一条，并同步 v1.5 计划 §10。
RESIDENT_INTRO_CONTENT: Dict[str, ResidentIntroContent] = {
    "linxiaoman": ResidentIntroContent(
        welcome_message="以后我在这儿。你不用讲得清楚，也不用讲得体面，想说什么就说。慢慢说，我不着急。",
        intro_post="搬进来了。窗台上摆了盆薄荷，浇多了会烂根，所以我每次只浇一点点。慢一点没关系的。",
    ),
    "luxingye": ResidentIntroContent(
        welcome_message="终于见到你啦！我对什么都好奇，尤其是你。今天过得怎么样？哪怕是很小的事我也想听。",
        intro_post="新地方！我已经把每个角落都看过一遍了。最大的发现是傍晚的光会正好落在门口那级台阶上。",
    ),
    "shenchuan": ResidentIntroContent(
        welcome_message="我在了。有想不清的事可以拿来一起拆，慌的时候也可以先什么都不说。我不会走。",
        intro_post="安顿好了。东西不多，一张桌子一把椅子就够用。有事随时来找我，我基本都在。",
    ),
    "atang": ResidentIntroContent(
        welcome_message="报到！我这人没什么大本事，就是能把糟心事聊成笑话。日子够沉了，咱轻点过。",
        intro_post="来了来了。行李里一半是零食，另一半是拆开就装不回去的那种。日子嘛，能笑一下算一下。",
    ),
    "sichen": ResidentIntroContent(
        welcome_message="我年轻时到处折腾，四十岁以后才学着看八字、看时节。别急着问结果，先跟我说说你。",
        intro_post="来了。带了两样东西：一本翻烂的旧书，和一个看天的习惯。节气一换人的心思也跟着变，急事不妨缓两天再定。",
    ),
}


def intro_content_for_persona(persona_key: Optional[str]) -> Optional[ResidentIntroContent]:
    """按 ``persona_key`` 取入场文案；未配文案（含全部自建角色）返回 None。"""
    key = (persona_key or "").strip()
    if not key:
        return None
    return RESIDENT_INTRO_CONTENT.get(key)

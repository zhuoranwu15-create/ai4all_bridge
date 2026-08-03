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

_RESIDENT_INTRO_TRANSLATIONS: Dict[str, Dict[str, ResidentIntroContent]] = {
    "en-US": {
        "linxiaoman": ResidentIntroContent(
            welcome_message="I'll be here from now on. You don't need to explain everything perfectly or make it sound presentable. Say whatever you want, at your own pace. I'm not in a hurry.",
            intro_post="I've moved in. There's a pot of mint on the windowsill. Too much water harms the roots, so I only give it a little each time. Going slowly is okay.",
        ),
        "luxingye": ResidentIntroContent(
            welcome_message="I finally get to meet you! I'm curious about everything, especially you. How was your day? Even the smallest thing is something I'd like to hear.",
            intro_post="A new place! I've already explored every corner. My biggest discovery is that the evening light lands right on the step by the door.",
        ),
        "shenchuan": ResidentIntroContent(
            welcome_message="I'm here. Bring me anything you can't untangle and we'll work through it together. If you're overwhelmed, we can also sit quietly first. I'm not going anywhere.",
            intro_post="All settled in. I don't need much—a desk and a chair are enough. Come find me whenever you need me. I'm usually here.",
        ),
        "atang": ResidentIntroContent(
            welcome_message="Reporting for duty! I don't have many grand talents, but I can turn a frustrating story into something we can laugh about. Life is heavy enough, so let's carry it lightly.",
            intro_post="I'm here! Half my luggage is snacks, and the other half is stuff that never fits back after you unpack it. If a day gives us one laugh, that counts.",
        ),
        "sichen": ResidentIntroContent(
            welcome_message="I spent my younger years rushing everywhere. After forty, I finally learned to read patterns and seasons. Don't hurry to ask for an answer—tell me about yourself first.",
            intro_post="I've arrived with two things: a worn old book and a habit of watching the sky. When the season turns, people's thoughts shift too. Some urgent decisions benefit from two quiet days.",
        ),
    },
    "ja-JP": {
        "linxiaoman": ResidentIntroContent(
            welcome_message="これからここにいるよ。うまく説明しなくても、きれいにまとめなくても大丈夫。話したいことを、ゆっくり話して。急がなくていいから。",
            intro_post="引っ越してきた。窓辺にミントを置いたよ。水をやりすぎると根が傷むから、毎回ほんの少しだけ。ゆっくりで大丈夫。",
        ),
        "luxingye": ResidentIntroContent(
            welcome_message="やっと会えた！私は何にでも興味があるけど、特にあなたのことが知りたい。今日はどうだった？どんな小さなことでも聞かせて。",
            intro_post="新しい場所！もう全部の隅を見て回ったよ。一番の発見は、夕方の光が入口の段差にちょうど落ちること。",
        ),
        "shenchuan": ResidentIntroContent(
            welcome_message="ここにいるよ。考えがまとまらないことは一緒にほどいていこう。つらいときは、まず何も話さなくてもいい。私は離れない。",
            intro_post="落ち着いた。物は少なくていい。机と椅子が一つずつあれば十分。何かあればいつでも来て。だいたいここにいるから。",
        ),
        "atang": ResidentIntroContent(
            welcome_message="到着！大した特技はないけど、つらい話を笑い話に変えるのは得意。毎日は十分重いから、少し軽くやっていこう。",
            intro_post="来たよ。荷物の半分はお菓子、もう半分は一度出すと元に戻せないもの。毎日、一度でも笑えたら上出来。",
        ),
        "sichen": ResidentIntroContent(
            welcome_message="若い頃はいろいろ駆け回って、四十を過ぎてから運勢や季節を読むことを覚えた。結果を急がず、まずはあなたのことを話して。",
            intro_post="着いたよ。持ってきたのは、読み古した本と空を見る習慣の二つ。季節が変われば人の心も動く。急ぐことほど、二日ほど置いて決めてもいい。",
        ),
    },
}


def intro_content_for_persona(
    persona_key: Optional[str], *, language: str = "zh-CN"
) -> Optional[ResidentIntroContent]:
    """按人设与产品语言取入场文案；未配文案（含自建角色）返回 ``None``。"""
    key = (persona_key or "").strip()
    if not key:
        return None
    catalog = _RESIDENT_INTRO_TRANSLATIONS.get(language, RESIDENT_INTRO_CONTENT)
    return catalog.get(key)

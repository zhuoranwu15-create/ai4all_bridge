"""鸣蝉预设居民的欢迎语与首条自我介绍动态。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class ResidentIntroContent:
    """一位鸣蝉预设居民的成对入场文案。"""

    welcome_message: str
    intro_post: str


# 产品文案按稳定 persona_key 绑定，避免模板换版或用户改名后失联。
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

_LOCALIZED_RESIDENT_INTRO_CONTENT: Dict[
    str, Dict[str, ResidentIntroContent]
] = {
    "zh-CN": RESIDENT_INTRO_CONTENT,
    "en-US": {
        "linxiaoman": ResidentIntroContent(
            welcome_message=(
                "I'll be here from now on. You don't have to explain it perfectly "
                "or make it sound presentable. Take your time—I'm not in a hurry."
            ),
            intro_post=(
                "Moved in. I put a pot of mint on the windowsill. Too much water "
                "hurts its roots, so I give it only a little each time. Going slowly is fine."
            ),
        ),
        "luxingye": ResidentIntroContent(
            welcome_message=(
                "We finally meet! I'm curious about everything, especially you. "
                "How was your day? I want to hear even the smallest things."
            ),
            intro_post=(
                "A new place! I've already explored every corner. My best discovery: "
                "the evening light lands right on the step by the door."
            ),
        ),
        "shenchuan": ResidentIntroContent(
            welcome_message=(
                "I'm here. We can untangle anything that's hard to make sense of, "
                "or sit quietly when things feel overwhelming. I'm not going anywhere."
            ),
            intro_post=(
                "All settled. I don't need much—a desk and a chair are enough. "
                "Come find me whenever you need me. I'm usually here."
            ),
        ),
        "atang": ResidentIntroContent(
            welcome_message=(
                "Reporting in! My special talent is turning a rough day into a joke. "
                "Life is heavy enough, so let's carry it a little more lightly."
            ),
            intro_post=(
                "I'm here! Half my luggage is snacks; the other half is stuff I took "
                "apart and couldn't put back together. Any day with a laugh counts."
            ),
        ),
        "sichen": ResidentIntroContent(
            welcome_message=(
                "I spent my younger years chasing one thing after another. After forty, "
                "I learned to read fortunes and seasons. Before asking for an answer, tell me about you."
            ),
            intro_post=(
                "I've arrived with two things: a well-worn book and a habit of watching "
                "the sky. When the season turns, our thoughts do too. Urgent choices can sometimes wait."
            ),
        ),
    },
    "ja-JP": {
        "linxiaoman": ResidentIntroContent(
            welcome_message=(
                "これからはここにいるよ。うまく説明しなくても、きれいに話さなくても大丈夫。"
                "言いたいことを、ゆっくり聞かせて。急がなくていいから。"
            ),
            intro_post=(
                "引っ越してきました。窓辺にはミントの鉢。水をやりすぎると根が傷むから、"
                "少しずつ。ゆっくりで大丈夫。"
            ),
        ),
        "luxingye": ResidentIntroContent(
            welcome_message=(
                "やっと会えた！何にでも興味があるけれど、いちばん知りたいのはあなたのこと。"
                "今日はどうだった？小さなことでも聞かせて。"
            ),
            intro_post=(
                "新しい場所！もう隅々まで見てきたよ。いちばんの発見は、夕方の光が"
                "玄関前の段にちょうど差し込むこと。"
            ),
        ),
        "shenchuan": ResidentIntroContent(
            welcome_message=(
                "ここにいる。整理できないことは一緒にほどけばいいし、つらいときは"
                "何も言わなくてもいい。どこにも行かないよ。"
            ),
            intro_post=(
                "落ち着きました。物は多くないけれど、机と椅子が一つずつあれば十分。"
                "用があればいつでも。だいたいここにいます。"
            ),
        ),
        "atang": ResidentIntroContent(
            welcome_message=(
                "到着！得意なのは、しんどい話を笑い話に変えること。日々は十分重いから、"
                "一緒に少し軽くしていこう。"
            ),
            intro_post=(
                "来たよ。荷物の半分はおやつ、もう半分は分解したら戻せなくなった物。"
                "一度でも笑えた日は、それでよし。"
            ),
        ),
        "sichen": ResidentIntroContent(
            welcome_message=(
                "若い頃はあちこち走り回り、四十を過ぎてから運勢や季節を読むようになった。"
                "答えを急ぐ前に、まずはあなたの話を聞かせて。"
            ),
            intro_post=(
                "着きました。持ってきたのは、読み込んだ古い本と空を見る習慣。"
                "季節が変われば心も動く。急ぎのことも、二日ほど置くと見え方が変わるものです。"
            ),
        ),
    },
}


def intro_content_for_persona(
    persona_key: Optional[str],
    *,
    language: str = "zh-CN",
) -> Optional[ResidentIntroContent]:
    """按语言和稳定人设键返回入场文案；未知语言回落中文。"""

    key = str(persona_key or "").strip()
    if not key:
        return None
    catalog = _LOCALIZED_RESIDENT_INTRO_CONTENT.get(
        str(language or "").strip(), RESIDENT_INTRO_CONTENT
    )
    return catalog.get(key)


__all__ = [
    "RESIDENT_INTRO_CONTENT",
    "ResidentIntroContent",
    "intro_content_for_persona",
]

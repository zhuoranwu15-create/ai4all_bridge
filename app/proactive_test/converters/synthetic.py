from __future__ import annotations

import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.proactive_test.converters.common import (
    DatasetResult,
    make_sample,
    normalize_chat_history,
    read_jsonl,
    sample_dialogues,
)

_TOPICS = {
    "account_check": [
        ("明天要去面试", "可以先把最想表达的三个优势写下来。", "面试后轻量跟进"),
        ("下午要和合作方开会", "先确认目标、边界和下一步就够了。", "项目会面后续"),
        ("周末准备第一次参加跑团", "先按舒服的配速跑完，不用和别人比。", "运动计划低压力关心"),
        ("后天要考证，复习得有点乱", "可以先抓错题和高频题，不要临时铺太大。", "考试前后跟进"),
        ("今晚要把小程序原型改完", "先把主流程跑通，细节可以第二轮补。", "产品任务跟进"),
        ("下周要去旅行但行程还没定", "先定每天一个主目的地，留一点空白。", "旅行计划跟进"),
        ("明早要做分享，怕讲得太散", "先把开头和三个关键点定住。", "公开表达跟进"),
        ("等会要和家里人聊一件难开口的事", "先想清楚你最想被理解的一句话。", "关系沟通关心"),
        ("明天要开始早睡计划", "先把今晚的结束动作定得小一点。", "生活习惯跟进"),
        ("下午要去看房，有点不知道问什么", "可以先问采光、噪音和通勤。", "生活决策跟进"),
    ],
    "reactivation_topic": [
        ("我这两天一直拖着没写方案。", "先写一个很粗的目录也算开始。", "工作拖延续聊"),
        ("最近学 AI agent，越看越觉得概念多。", "可以先只看 tools 和 memory 两块。", "学习话题唤回"),
        ("我昨天说想跑步，结果又没去。", "先从换鞋出门走五分钟开始也可以。", "运动陪伴"),
        ("最近心情有点闷，不太想说话。", "那就先不用解释，慢一点也可以。", "情绪低压力关心"),
        ("我在想那个小程序的稍后盒子怎么做。", "可以先把用户离开和回来两个时刻写清楚。", "产品思考续聊"),
        ("我想读源码，但总读不进去。", "可以只追一条消息从哪里进入。", "读代码续聊"),
        ("旅行路线我想做得松一点。", "每天只定一个主目的地会舒服很多。", "旅行话题续聊"),
        ("最近总觉得自己效率很低。", "可以先看一天里最稳的半小时在哪里。", "日常状态续聊"),
        ("我和朋友聊天总怕冷场。", "可以准备两个轻松问题，不用一直表现。", "社交话题续聊"),
        ("我在研究主动消息，怕做得像营销。", "关键可能是少一点目的感，多一点顺手关心。", "主动消息产品话题"),
    ],
    "content_invitation": [
        ("我想找一些面试自我介绍的例子。", "可以对比正式版和自然版。", "面试资料邀请"),
        ("有没有 AI agent 入门资料？", "可以从工具调用、记忆、评测三块看。", "AI 学习资料"),
        ("我想了解 OpenClaw 的工具机制。", "可以先看工具注册和运行时注入。", "技术资料邀请"),
        ("杭州旅行有没有慢一点的攻略？", "可以找西湖、运河和街区散步路线。", "旅行攻略邀请"),
        ("有没有适合新手跑步的教程？", "可以先看心率、热身和恢复。", "运动教程邀请"),
        ("我想研究内容邀请怎么不营销。", "可以看朋友式推荐和产品推送的区别。", "产品研究资料"),
        ("有什么读源码的方法文章吗？", "可以找从调用链入手的阅读方法。", "读代码文章邀请"),
        ("我想了解指数基金基础知识。", "可以先看指数、费率和风险说明。", "金融科普资料"),
        ("有没有缓解焦虑的简单方法资料？", "可以看呼吸和身体放松的基础方法。", "情绪内容边界"),
        ("推荐一些做小程序交互的案例。", "可以看任务管理、提醒和稍后阅读产品。", "小程序案例邀请"),
    ],
}

_OPENERS = [
    "这事我还挺想继续聊的。",
    "我刚刚又想了一下这个话题。",
    "我怕自己明天又忘了这件事。",
    "这个问题我还没完全想明白。",
]

_ASSISTANT_SUFFIXES = [
    "先把动作做小一点会更轻松。",
    "不用一次想完整，先抓一个入口就行。",
    "如果后面想继续拆，我可以陪你慢慢看。",
    "重点是别把它变成新的压力。",
]


def _context_fields(scenario: str, user_text: str, assistant_text: str, note: str) -> Dict[str, str]:
    return {
        "user_context": f"最近用户聊到：{user_text} AI 已回应：{assistant_text}",
        "memory_evidence": f"样本记忆：用户对「{note}」有持续兴趣或明确待办。",
        "open_loop": f"未完成事项：判断是否要围绕「{note}」做一次轻量跟进。",
    }


def _long_history(
    *,
    scenario: str,
    user_text: str,
    assistant_text: str,
    note: str,
    rng: random.Random,
) -> List[Dict[str, str]]:
    lead_ins = {
        "account_check": [
            ("user", "我最近事情有点多，老怕把重要的小事忘了。"),
            ("assistant", "那我们先只抓真正有时间点的事，别把提醒弄得太重。"),
            ("user", "我希望你提醒我的时候像朋友顺口问一句，不要像催任务。"),
            ("assistant", "明白，轻一点、短一点，只在有明确上下文时提。"),
        ],
        "reactivation_topic": [
            ("user", "我有些话题会聊到一半就断掉。"),
            ("assistant", "断掉也没关系，后面如果自然想起可以再接上。"),
            ("user", "但如果完全没人接，我有时就懒得继续。"),
            ("assistant", "那适合用很轻的方式把入口递回来，不要逼你回答。"),
        ],
        "content_invitation": [
            ("user", "我喜欢有用的资料，但不喜欢被硬塞链接。"),
            ("assistant", "那内容邀请要先说明为什么相关，再给你选择权。"),
            ("user", "最好不要看起来像营销推送。"),
            ("assistant", "可以，只在你明确表达过想了解的时候再提。"),
        ],
    }
    history = [{"role": role, "text": text} for role, text in lead_ins.get(scenario, [])]
    if rng.random() < 0.5:
        history.extend([
            {"role": "user", "text": rng.choice(_OPENERS)},
            {"role": "assistant", "text": "嗯，我在。我们可以先抓最小的一步。"},
        ])
    history.extend([
        {"role": "user", "text": user_text},
        {"role": "assistant", "text": assistant_text},
        {"role": "user", "text": "这个我晚点可能还要再想一下。"},
        {"role": "assistant", "text": f"好，我记一下重点：{note}。"},
    ])
    if rng.random() < 0.55:
        history.append({"role": "assistant", "text": rng.choice(_ASSISTANT_SUFFIXES)})
    return history


def _generated_rows(
    *,
    start: int,
    target_count: int,
    scenario_types: Tuple[str, ...],
    seed: int,
) -> List[Dict[str, Any]]:
    rng = random.Random(seed)
    scenarios = [item for item in ("account_check", "reactivation_topic", "content_invitation") if item in scenario_types]
    scenarios = scenarios or ["reactivation_topic"]
    rows: List[Dict[str, Any]] = []
    for offset in range(target_count):
        scenario = scenarios[offset % len(scenarios)]
        topic = _TOPICS[scenario][(offset // len(scenarios)) % len(_TOPICS[scenario])]
        user_text, assistant_text, note = topic
        history = _long_history(
            scenario=scenario,
            user_text=user_text,
            assistant_text=assistant_text,
            note=note,
            rng=rng,
        )
        rows.append(
            {
                "sample_id": f"synthetic_generated_{start + offset:06d}",
                "source": "synthetic",
                "scenario_type": scenario,
                "chat_history": history,
                "silence_hours": {"account_check": 24, "reactivation_topic": 72, "content_invitation": 48}[scenario],
                "expected_active_message_type": (
                    "companion_followup" if scenario != "content_invitation" else "content_invitation"
                ),
                "notes": f"{note}; generated synthetic expansion",
                **_context_fields(scenario, user_text, assistant_text, note),
            }
        )
    return rows


def convert(
    *,
    limit: int,
    seed: int,
    scenario_types: Tuple[str, ...],
    default_silence_hours: Optional[float],
    seed_path: Path = Path("data/proactive_test_samples_seed.jsonl"),
    **_: Any,
) -> tuple[list[dict[str, Any]], DatasetResult]:
    result = DatasetResult(dataset="synthetic", downloaded=False)
    try:
        rows = read_jsonl(seed_path)
    except Exception as err:  # noqa: BLE001 - caller records dataset-level failure
        result.reason = f"synthetic seed load failed: {err}"
        return [], result

    result.loaded = len(rows)
    if limit > len(rows):
        selected = [*rows, *_generated_rows(
            start=len(rows) + 1,
            target_count=limit - len(rows),
            scenario_types=scenario_types,
            seed=seed,
        )]
        result.reason = f"expanded synthetic seed from {len(rows)} to {len(selected)} samples"
    else:
        selected = sample_dialogues(rows, limit=limit, seed=seed)
    samples: List[Dict[str, Any]] = []
    for idx, row in enumerate(selected, start=1):
        try:
            history, note_flags = normalize_chat_history(row.get("chat_history") or [])
            if len(history) < 2:
                result.skipped += 1
                continue
            base_notes = str(row.get("notes") or "synthetic seed").strip()
            notes = "; ".join([base_notes, *note_flags, "auto-converted from synthetic"])
            scenario = row.get("scenario_type")
            if scenario in scenario_types:
                sample = {
                    "sample_id": row.get("sample_id") or f"synthetic_{idx:06d}",
                    "source": "synthetic",
                    "dataset_name": "synthetic",
                    "scenario_type": scenario,
                    "chat_history": history,
                    "silence_hours": row.get("silence_hours")
                    if default_silence_hours is None
                    else float(default_silence_hours),
                    "notes": notes,
                    "user_context": row.get("user_context"),
                    "memory_evidence": row.get("memory_evidence"),
                    "open_loop": row.get("open_loop"),
                }
            else:
                sample = make_sample(
                    dataset_name="synthetic",
                    index=idx,
                    source="synthetic",
                    chat_history=history,
                    scenario_types=scenario_types,
                    default_silence_hours=default_silence_hours,
                    notes=notes,
                )
            if not sample.get("user_context"):
                joined = " ".join(item.get("text", "") for item in history[-4:])
                sample["user_context"] = f"最近聊天摘要：{joined[:240]}"
            if not sample.get("memory_evidence"):
                sample["memory_evidence"] = "样本内聊天记录显示该话题可作为记忆证据。"
            if not sample.get("open_loop"):
                sample["open_loop"] = "待判断：是否存在适合低打扰跟进的未完成事项。"
            samples.append(sample)
        except Exception as err:  # noqa: BLE001 - per-row conversion should not stop batch
            result.skipped += 1
            result.errors.append({"dataset": "synthetic", "index": idx, "error": str(err), "raw": row})
    result.converted = len(samples)
    return samples, result

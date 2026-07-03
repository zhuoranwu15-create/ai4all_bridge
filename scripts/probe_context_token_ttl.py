#!/usr/bin/env python
"""context_token 服务端有效期（TTL）每日探针。

背景：微信主动消息发不到"长期沉默用户"的根因已实验坐实——沉默 → context_token
陈旧 → ilink 静默拒收（真人收不到），而网关 `send` 返回体只有 messageId、不含 ret，
我们这层记"假成功"。参见 docs/troubleshooting/weixin_context_token_send_semantics.md。

本探针每天向一个固定账号发一条"第N天 + 日期 + 冷笑话"的消息，目的是观测
context_token 在"用户始终不回复、token 一直不刷新"的前提下，第几天开始送不达
（真人收不到），从而反推服务端有效窗口。

关键设计约束：
- 只读凭证文件、只记指纹（len/sha12/head/tail/mtime），绝不落 token 明文。
- 发送不刷新 context_token（已实证）；只有"用户主动入站"才刷新。若某天指纹变了，
  说明用户回复过、计时窗口被重置——脚本会显式标注 window_reset。
- 是否真送达网关层无法判断，需真人在收件端确认"哪天起收不到"，与本日志的
  day/token 指纹对齐即可定位 TTL。
- LLM 生成失败不阻断当日发送：回退到内置冷笑话，保证探针连续性。

用法：
    # 自测，不真发（默认）：
    .venv/bin/python scripts/probe_context_token_ttl.py
    # 正式发送（cron 每日调用）：
    .venv/bin/python scripts/probe_context_token_ttl.py --send
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional

# 允许以 `python scripts/xxx.py` 直接运行；并 chdir 到项目根，
# 使 app.config 的 `env_file=".env"`（相对 cwd）在 cron 等任意工作目录下也能加载。
# 必须在导入 app.* 之前 chdir——settings 在模块导入时即读取 .env。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
os.chdir(_PROJECT_ROOT)

from app.proactive.contract.common import _select_route  # noqa: E402
from app import openclaw_gateway as og  # noqa: E402
from app.openclaw_gateway import OpenClawGatewayError, OpenClawRateLimited  # noqa: E402

TARGET_ACCOUNT_ID = "aid_544704489"
GATEWAY_TIMEOUT_MS = 45_000

STATE_DIR = Path(__file__).resolve().parent.parent / "data" / "context_token_probe"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "probe.jsonl"

# LLM 抖动时的兜底冷笑话，保证当日探针不因生成失败而中断。
FALLBACK_JOKES = [
    "为什么程序员分不清万圣节和圣诞节？因为 Oct 31 == Dec 25。",
    "一只鸭子走进药店说：给我来点唇膏。店员问：付现金吗？鸭子说：记我账上（bill）。",
    "我把 Wi-Fi 密码设成了“扫地”，这样每次有人蹭网前都得先扫一遍。",
    "为什么海带很谦虚？因为它总是很“低（碘）调”。",
    "有个数学家怕黑，因为黑暗里有“负数”。",
    "我告诉冰箱一个笑话，它冷场了。",
    "为什么电脑很冷？因为它开着好多“窗”（windows）。",
    "煤球和雪球打架，结果煤球赢了——因为它比较“黑”。",
]


def _fingerprint(s: str) -> Dict[str, Any]:
    return {
        "len": len(s),
        "sha12": hashlib.sha256(s.encode()).hexdigest()[:12],
        "head": s[:6],
        "tail": s[-6:],
    }


def _resolve_state_dir() -> Path:
    return Path(
        os.environ.get("OPENCLAW_STATE_DIR", "").strip()
        or os.environ.get("CLAWDBOT_STATE_DIR", "").strip()
        or (Path.home() / ".openclaw")
    )


def _context_token_snapshot(bot_account_id: str, user_id: str) -> Dict[str, Any]:
    """读取该 (bot账号, 用户) 的 context_token 指纹与文件 mtime；只读、不落明文。"""
    fpath = (
        _resolve_state_dir()
        / "openclaw-weixin"
        / "accounts"
        / f"{bot_account_id}.context-tokens.json"
    )
    if not fpath.exists():
        return {"present": False, "file": str(fpath)}
    st = fpath.stat()
    try:
        data = json.loads(fpath.read_text("utf-8"))
    except Exception as err:  # noqa: BLE001
        return {"present": True, "file": str(fpath), "read_error": str(err)}
    tok = data.get(user_id)
    snap: Dict[str, Any] = {
        "present": True,
        "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(),
        "keys_in_file": len(data),
        "token_present": tok is not None,
    }
    if isinstance(tok, str) and tok:
        snap.update(_fingerprint(tok))
    return snap


def _load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text("utf-8"))
    return {}


def _save_state(state: Dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


def _append_log(record: Dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _generate_joke() -> Dict[str, Any]:
    """调 LLM 生成一条冷笑话；失败回退内置列表。返回 {text, source}。"""
    try:
        from app.llm_providers import get_llm_provider
        from app.llm_adapters import chat_completion

        provider = get_llm_provider()
        messages = [
            {
                "role": "system",
                "content": "你是一个只会讲中文冷笑话的助手。只输出一条简短、干净、无攻击性的冷笑话本身，不加任何前后缀、不加解释、不超过40字。",
            },
            {"role": "user", "content": "来一条今天的冷笑话。"},
        ]
        payload = chat_completion(provider, messages, tools=None, tool_choice="none")
        text = (payload["choices"][0]["message"]["content"] or "").strip()
        if text:
            return {"text": text, "source": f"llm:{provider.id}"}
    except Exception as err:  # noqa: BLE001
        # 生成失败不阻断当日探针
        return {"text": None, "source": "llm_error", "error": str(err)}
    return {"text": None, "source": "llm_empty"}


def _pick_fallback_joke(day_index: int) -> str:
    return FALLBACK_JOKES[(day_index - 1) % len(FALLBACK_JOKES)]


def main() -> int:
    parser = argparse.ArgumentParser(description="context_token TTL 每日探针")
    parser.add_argument(
        "--send",
        action="store_true",
        help="真正发送；不加则为 dry-run（只生成与记录，不发）",
    )
    args = parser.parse_args()

    now = datetime.now()
    today = date.today()

    route = _select_route(TARGET_ACCOUNT_ID)
    if not route:
        print(f"[ERROR] 无法解析 {TARGET_ACCOUNT_ID} 的 channel binding，退出", file=sys.stderr)
        return 2
    bot_account_id = route["channel_account_id"]
    user_id = route["to_user_id"]

    # 状态：起始日 + 基线 token sha（用于计算第N天、检测窗口重置）
    state = _load_state()
    if not state:
        state = {
            "target_account_id": TARGET_ACCOUNT_ID,
            "bot_account_id": bot_account_id,
            "user_id": user_id,
            "start_date": today.isoformat(),
            "runs": 0,
        }
    day_index = (today - date.fromisoformat(state["start_date"])).days + 1

    token_snap = _context_token_snapshot(bot_account_id, user_id)
    baseline_sha = state.get("baseline_token_sha12")
    current_sha = token_snap.get("sha12")
    window_reset = bool(baseline_sha and current_sha and current_sha != baseline_sha)

    # 生成消息
    joke = _generate_joke()
    joke_text = joke.get("text") or _pick_fallback_joke(day_index)
    joke_source = joke.get("source") if joke.get("text") else f"fallback({joke.get('source')})"
    message = f"【第{day_index}天 · {today.isoformat()}】\n今天的冷笑话：{joke_text}"

    record: Dict[str, Any] = {
        "ts": now.isoformat(),
        "day_index": day_index,
        "mode": "send" if args.send else "dry-run",
        "bot_account_id": bot_account_id,
        "user_id": user_id,
        "token": token_snap,
        "window_reset": window_reset,
        "joke_source": joke_source,
        "message": message,
    }

    if args.send:
        try:
            result = og.send_weixin_text(
                to_user_id=user_id,
                text=message,
                gateway_timeout_ms=GATEWAY_TIMEOUT_MS,
                account_id=bot_account_id,
                session_key=route.get("session_key"),
                channel=route["channel"],
                idempotency_key=f"ctxtoken-probe-day{day_index}-{today.isoformat()}",
            )
            record["send_result"] = {
                "ok": True,
                "message_id": result.get("messageId"),
                "raw": result,
                "note": "网关回 messageId≠真送达；真送达需收件端确认",
            }
        except OpenClawRateLimited as err:
            record["send_result"] = {"ok": False, "error": "rate_limited", "ret": getattr(err, "ret", None), "message": str(err)}
        except OpenClawGatewayError as err:
            record["send_result"] = {"ok": False, "error": "gateway_error", "message": str(err)}
        except Exception as err:  # noqa: BLE001
            record["send_result"] = {"ok": False, "error": type(err).__name__, "message": str(err)}

        # 首个真实发送日固化基线 token sha（用于后续窗口重置检测）
        if not state.get("baseline_token_sha12") and current_sha:
            state["baseline_token_sha12"] = current_sha
            state["baseline_captured_at"] = now.isoformat()
        state["runs"] = state.get("runs", 0) + 1
        state["last_run_ts"] = now.isoformat()
        _save_state(state)
    else:
        record["send_result"] = {"ok": None, "note": "dry-run，未发送"}

    _append_log(record)

    # 控制台摘要
    print(json.dumps(record, ensure_ascii=False, indent=2))
    if window_reset:
        print("\n[!] window_reset=True：context_token 指纹与基线不同 → 用户期间回复过，计时窗口已重置。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

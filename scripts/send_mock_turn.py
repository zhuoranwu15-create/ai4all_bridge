import argparse
import json
import time
from urllib.parse import urlparse
import urllib.request


def resolve_turn_url(raw_url: str) -> str:
    """Accept either a base backend URL or the full /openclaw/turn endpoint."""
    cleaned = (raw_url or "").strip().rstrip("/")
    if not cleaned:
        return "http://127.0.0.1:8000/openclaw/turn"
    parsed = urlparse(cleaned)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("--url must be an absolute http(s) URL")
    if parsed.path.endswith("/openclaw/turn"):
        return cleaned
    return f"{cleaned}/openclaw/turn"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000/openclaw/turn")
    parser.add_argument("--secret", default="dev-secret")
    parser.add_argument("--sender", default="wxid_mock")
    parser.add_argument("--text", default="你好")
    parser.add_argument("--message-id", default=None)
    args = parser.parse_args()

    now = int(time.time())
    message_id = args.message_id or f"msg_{now}"
    payload = {
        "event_id": f"evt_{message_id}",
        "message_id": message_id,
        "channel": "openclaw-weixin",
        "account_id": "local",
        "sender_id": args.sender,
        "chat_id": args.sender,
        "chat_type": "private",
        "session_key": f"openclaw-weixin:local:{args.sender}",
        "message_type": "text",
        "text": args.text,
        "timestamp": now,
        "raw": {},
    }
    turn_url = resolve_turn_url(args.url)
    request = urllib.request.Request(
        turn_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {args.secret}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        print(response.status)
        print(response.read().decode("utf-8"))


if __name__ == "__main__":
    main()

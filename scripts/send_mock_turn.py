import argparse
import json
import time
import urllib.request


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
    request = urllib.request.Request(
        args.url,
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

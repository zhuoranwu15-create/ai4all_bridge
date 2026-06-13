import argparse
import base64
import json
import mimetypes
import os
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
    parser.add_argument(
        "--message-type",
        default="text",
        help="消息类型，如 text / image / voice。给定 --image-path/--image-url/--image-bytes 时自动置为 image。",
    )
    parser.add_argument("--image-path", default=None, help="本地图片绝对路径（绕过 OpenClaw 自测图片轮）")
    parser.add_argument("--image-url", default=None, help="远程图片 URL")
    parser.add_argument(
        "--image-bytes",
        default=None,
        help="本地图片文件：读成 base64 内联进 media.data_base64，模拟多机 node bridge 传字节路径。",
    )
    args = parser.parse_args()

    now = int(time.time())
    message_id = args.message_id or f"msg_{now}"
    message_type = args.message_type
    media = None
    if args.image_bytes:
        message_type = "image"
        with open(os.path.expanduser(args.image_bytes), "rb") as fh:
            raw = fh.read()
        mime, _ = mimetypes.guess_type(args.image_bytes)
        media = {
            "data_base64": base64.b64encode(raw).decode("ascii"),
            "format": mime or "image/jpeg",
            "size": len(raw),
        }
    elif args.image_path or args.image_url:
        message_type = "image"
        media = {
            "path": args.image_path,
            "url": args.image_url,
            "format": "image",
        }
    payload = {
        "event_id": f"evt_{message_id}",
        "message_id": message_id,
        "channel": "openclaw-weixin",
        "account_id": "local",
        "sender_id": args.sender,
        "chat_id": args.sender,
        "chat_type": "private",
        "session_key": f"openclaw-weixin:local:{args.sender}",
        "message_type": message_type,
        "text": args.text,
        "timestamp": now,
        "raw": {},
    }
    if media is not None:
        payload["media"] = media
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

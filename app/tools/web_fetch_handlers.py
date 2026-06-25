"""web_fetch 工具 handler：抓取公网 HTTP(S) URL，返回文本/JSON/HTML 可读内容。

- 只走公网；内网 URL 由 _url_guard.assert_public_url 拦截
- 不使用环境代理（trust_env=False）
- 手动逐跳跟随 redirect，每跳重新检查 guard
- 返回结果已带 externalContent.wrapped=true，Batch A projection 不会二次包裹
"""
import json
import re
import secrets
import time
from typing import Any, Dict
from urllib.parse import urljoin

import httpx

from app.config import settings
from app.tools._url_guard import SSRFError, assert_public_url


def _strip_html(html: str) -> str:
    """粗粒度去除 HTML 标签和脚本块，返回可读文本。"""
    # 移除 <script> / <style> 内容
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    # 移除注释
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    # 移除所有标签
    text = re.sub(r"<[^>]+>", " ", text)
    # 合并连续空白
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_text(content_bytes: bytes, content_type: str, extract_mode: str) -> str:
    """根据 content-type 和 extractMode 返回可读文本。"""
    ct = content_type.lower().split(";")[0].strip()

    if "json" in ct:
        try:
            obj = json.loads(content_bytes)
            # 紧凑输出：不做 indent 美化，避免把体量翻倍浪费 token / 触发截断
            return json.dumps(obj, ensure_ascii=False)
        except Exception:
            pass

    raw = content_bytes.decode("utf-8", errors="replace")

    if "html" in ct:
        return _strip_html(raw)

    return raw  # text/plain 等


def handle_web_fetch(args: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
    """执行 web_fetch 工具调用。"""
    url: str = (args.get("url") or "").strip()
    if not url:
        return {"status": "failed", "error": "url is required"}

    extract_mode: str = (args.get("extractMode") or args.get("extract_mode") or "text").lower()
    if extract_mode not in ("markdown", "text"):
        extract_mode = "text"

    max_chars: int = int(args.get("maxChars") or args.get("max_chars") or getattr(settings, "web_fetch_max_chars", 60000))
    max_chars = max(200, min(max_chars, 60000))

    timeout: float = float(getattr(settings, "web_fetch_timeout_seconds", 8.0))
    max_response_bytes: int = int(getattr(settings, "web_fetch_max_response_bytes", 524288))
    max_redirects: int = int(getattr(settings, "web_fetch_max_redirects", 3))

    # SSRF 检查
    try:
        assert_public_url(url)
    except SSRFError as exc:
        return {"status": "failed", "error": f"URL 被安全策略拒绝: {exc}"}

    started = time.monotonic()
    current_url = url
    redirect_count = 0

    try:
        with httpx.Client(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(
                timeout,
                connect=float(getattr(settings, "web_fetch_connect_timeout_seconds", 3.0)),
            ),
        ) as client:
            while True:
                resp = client.get(
                    current_url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; AI4ALL-bot/1.0)"},
                )

                if resp.is_redirect:
                    if redirect_count >= max_redirects:
                        return {
                            "status": "failed",
                            "error": f"超过最大重定向次数 ({max_redirects})",
                            "url": url,
                        }
                    next_url = urljoin(current_url, str(resp.headers.get("location", "")))
                    if not next_url:
                        return {"status": "failed", "error": "重定向 Location 为空", "url": url}
                    # 每跳都重新检查 SSRF
                    try:
                        assert_public_url(next_url)
                    except SSRFError as exc:
                        return {"status": "failed", "error": f"重定向目标被安全策略拒绝: {exc}", "url": url}
                    current_url = next_url
                    redirect_count += 1
                    continue

                # 读取响应体（限制大小）
                content_bytes = resp.read()[:max_response_bytes]
                break

    except SSRFError as exc:
        return {"status": "failed", "error": str(exc), "url": url}
    except httpx.TimeoutException:
        return {"status": "failed", "error": "请求超时", "url": url}
    except httpx.HTTPError as exc:
        return {"status": "failed", "error": f"HTTP 错误: {exc}", "url": url}

    took_ms = int((time.monotonic() - started) * 1000)
    content_type = resp.headers.get("content-type", "text/plain")
    raw_text = _extract_text(content_bytes, content_type, extract_mode)

    truncated = len(raw_text) > max_chars
    text_out = raw_text[:max_chars]

    marker_id = secrets.token_hex(8)
    wrapped_text = (
        f'<<<EXTERNAL_UNTRUSTED_CONTENT source="web_fetch" id="{marker_id}">>>\n'
        f"{text_out}\n"
        f'<<<END_EXTERNAL_UNTRUSTED_CONTENT id="{marker_id}">>>'
    )

    return {
        "url": url,
        "finalUrl": current_url,
        "status": resp.status_code,
        "contentType": content_type,
        "extractMode": extract_mode,
        "extractor": "basic-html" if "html" in content_type.lower() else "raw",
        "externalContent": {
            "untrusted": True,
            "source": "web_fetch",
            "id": marker_id,
            "wrapped": True,
        },
        "truncated": truncated,
        "length": len(text_out),
        "rawLength": len(raw_text),
        "tookMs": took_ms,
        "text": wrapped_text,
    }

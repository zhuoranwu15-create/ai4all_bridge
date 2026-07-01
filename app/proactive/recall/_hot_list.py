"""热榜数据抓取工具 — 为 hot_topic 全局召回提供结构化热点条目。

支持两个来源（按 hot_topic_sources 顺序）：
- toutiao：今日头条热榜，字段 title + hot_value
- zhihu：知乎热榜，字段 title + detail（问题详情，质量高）

每个来源返回格式化好的文字行列表（供 LLM 直接消费），失败返回 None，
由调用方决定是否降级 web search。

HTTP 层直接用 httpx（项目已有依赖，handle_web_fetch 同款），SSRF 保护复用
app.tools._url_guard.assert_public_url。handle_web_fetch 本身是 HTML→text 转换器，
不适合 JSON API 场景，故不复用其主逻辑。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from app.tools._url_guard import SSRFError, assert_public_url

logger = logging.getLogger("ai4all.proactive.hot_list")

_DETAIL_MAX_CHARS = 150


def _parse_items(data: Any, max_items: int) -> List[str]:
    """从 API data 数组解析条目，转为 '- title: detail' 行列表。"""
    if not isinstance(data, list):
        return []
    lines: List[str] = []
    for item in data[:max_items]:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        detail = (item.get("detail") or "").strip()
        if detail and len(detail) > 10:
            lines.append(f"- {title}: {detail[:_DETAIL_MAX_CHARS]}")
        else:
            lines.append(f"- {title}")
    return lines


def fetch_hot_list(
    url: str,
    *,
    timeout: float = 5.0,
    max_items: int = 20,
) -> Optional[List[str]]:
    """从单个 URL 抓取热榜 JSON，返回格式化行列表；失败返回 None。"""
    try:
        assert_public_url(url)
    except SSRFError as exc:
        logger.warning("hot_list SSRF guard blocked url=%s: %s", url, exc)
        return None
    try:
        resp = httpx.get(
            url,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; AI4ALL-bot/1.0)"},
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning("hot_list fetch failed url=%s: %s", url, exc)
        return None

    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    if code is not None and int(code) != 200:
        logger.warning("hot_list non-200 code=%s url=%s", code, url)
        return None

    lines = _parse_items(payload.get("data") or [], max_items)
    return lines if lines else None


def collect_hot_list_lines(
    *,
    sources: List[str],
    source_urls: Dict[str, str],
    source_backup_urls: Optional[Dict[str, str]] = None,
    timeout: float = 5.0,
    max_items_per_source: int = 20,
) -> List[str]:
    """按 sources 顺序抓取各来源，合并去重后返回行列表；全部失败返回空列表。

    source_urls:        {"toutiao": url, "zhihu": url, ...}
    source_backup_urls: {"zhihu": backup_url, ...}  主 URL 失败时尝试备用
    """
    seen: set = set()
    all_lines: List[str] = []
    backup = source_backup_urls or {}

    for src in sources:
        primary = source_urls.get(src, "").strip()
        if not primary:
            continue
        lines = fetch_hot_list(primary, timeout=timeout, max_items=max_items_per_source)
        if lines is None:
            fallback = backup.get(src, "").strip()
            if fallback:
                lines = fetch_hot_list(fallback, timeout=timeout, max_items=max_items_per_source)
        if not lines:
            logger.warning("hot_list source=%s: all URLs failed or empty", src)
            continue
        for line in lines:
            key = line.strip().lower()
            if key not in seen:
                seen.add(key)
                all_lines.append(line)

    return all_lines

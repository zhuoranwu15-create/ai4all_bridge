"""图片理解模块：调用 DashScope qwen3-vl-plus 产出多维描述文本。

设计要点（见 docs/tech_design/image_understanding_design.md）：
- 两段式架构的第一段，stateless，对 app/llm.py 零侵入。
- 读取本机本地图片（仅限 settings.image_inbound_dir 内）或远程 URL，
  调用 DashScope OpenAI 兼容接口产出【内容/OCR/品牌/场景/情绪】多维描述。
- 任何失败（无 key、路径越界、超时、HTTP 错误）都返回 None，绝不抛到主链路。
- 复用 httpx + settings.dashscope_api_key，不引入新依赖。
"""

import base64
import json
import logging
import mimetypes
import os
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger("ai4all.image_understanding")

# DashScope OpenAI 兼容接口（与 test3.py 验证一致）。
_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

# 多维描述 system prompt：产出紧凑、可被主对话模型引用的描述，
# 覆盖五个维度，支撑"图后追问"（C 场景）只靠文字描述回溯。
_SYSTEM_PROMPT = """你是一个细腻、懂生活的视觉理解助手。请观察用户发来的这张图片，用中文输出一段紧凑的描述，覆盖以下维度（有则写，没有就跳过，不要编造）：
1. 内容：画面主体是什么，在做什么。
2. 可见文字：图中清晰可读的文字、招牌、标语（逐字摘录关键文字）。
3. 品牌/物品：可辨认的品牌、商品、型号或专有名词。
4. 场景：地点、时间、天气或环境氛围。
5. 情绪：图片传达的情绪与氛围色彩。
要求：像一个懂生活的人那样自然描述，2-4 句话，不要分点罗列、不要加"维度"等标签，不要复述本提示。"""

_DEFAULT_USER_TEXT = "帮我看看这张图片，描述一下它的内容、文字、品牌、场景和情绪。"


def _resolve_data_url_from_path(image_path: str) -> Optional[str]:
    """把本地图片读成 data URL；越界/过大/读失败返回 None。

    安全约束：只允许读取 settings.image_inbound_dir（展开 ~ 后）目录内的文件，
    经 realpath 解析以防符号链接 / `..` 路径穿越导致任意文件读取。
    """
    allowed_root = os.path.realpath(os.path.expanduser(settings.image_inbound_dir))
    real_path = os.path.realpath(os.path.expanduser(image_path))
    # 必须落在允许目录内（含目录本身的子路径前缀校验）。
    if real_path != allowed_root and not real_path.startswith(allowed_root + os.sep):
        logger.warning(
            "image path outside allowed inbound dir, refusing to read path=%r allowed_root=%r",
            image_path,
            allowed_root,
        )
        return None
    if not os.path.isfile(real_path):
        logger.warning("image path not a file path=%r", image_path)
        return None
    try:
        size = os.path.getsize(real_path)
    except OSError as err:
        logger.warning("image stat failed path=%r error=%s", image_path, err)
        return None
    if size > int(settings.image_max_bytes):
        logger.warning(
            "image too large path=%r size=%s max=%s", image_path, size, settings.image_max_bytes
        )
        return None
    mime, _ = mimetypes.guess_type(real_path)
    if not mime or not mime.startswith("image/"):
        mime = "image/jpeg"
    try:
        with open(real_path, "rb") as fh:
            raw = fh.read()
    except OSError as err:
        logger.warning("image read failed path=%r error=%s", image_path, err)
        return None
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def describe_image(
    *,
    image_path: Optional[str] = None,
    image_url: Optional[str] = None,
    caption: Optional[str] = None,
) -> Optional[str]:
    """调用 VL 模型返回图片的多维描述文本；任何失败返回 None。

    参数 image_path（本地文件，优先）与 image_url（远程）二选一。
    caption 为用户随图发送的文字（可空），作为提问上下文一并传给模型。
    """
    if not settings.dashscope_api_key:
        logger.warning("DASHSCOPE_API_KEY empty; skip image understanding")
        return None

    resolved_image_url: Optional[str] = None
    if image_path:
        resolved_image_url = _resolve_data_url_from_path(image_path)
    elif image_url:
        resolved_image_url = image_url.strip() or None

    if not resolved_image_url:
        logger.warning(
            "no usable image reference (path=%r url=%r)", image_path, image_url
        )
        return None

    caption_text = (caption or "").strip()
    user_text = (
        f"{_DEFAULT_USER_TEXT}\n用户随图说的话：{caption_text}"
        if caption_text
        else _DEFAULT_USER_TEXT
    )

    payload = {
        "model": settings.image_understanding_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": resolved_image_url}},
                    {"type": "text", "text": user_text},
                ],
            },
        ],
        "stream": False,
    }

    try:
        response = httpx.post(
            _API_URL,
            headers={
                "Authorization": f"Bearer {settings.dashscope_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=float(settings.image_understanding_timeout_seconds),
        )
    except httpx.HTTPError as err:
        logger.warning("image understanding request failed error=%s", err)
        return None

    if response.status_code >= 400:
        logger.warning(
            "image understanding http error status=%s body=%s",
            response.status_code,
            response.text[:500],
        )
        return None

    try:
        data = response.json()
        description = data["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as err:
        logger.warning("image understanding parse failed error=%s", err)
        return None

    description = (description or "").strip()
    if not description:
        logger.warning("image understanding returned empty content")
        return None
    return description

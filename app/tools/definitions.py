"""跨产品共享工具 schema。"""

def get_web_fetch_tools() -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": "web_fetch",
                "description": (
                    "抓取一个公网 HTTP(S) URL 的文本/JSON/HTML 可读内容。"
                    "用于读取已知 URL（如 skill 中指定的数据接口）；搜索未知信息用 web_search。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "目标 URL，必须是公网 HTTP(S) 地址",
                        },
                        "extractMode": {
                            "type": "string",
                            "enum": ["markdown", "text"],
                            "description": "内容提取模式；默认 text",
                        },
                        "maxChars": {
                            "type": "integer",
                            "minimum": 200,
                            "maximum": 20000,
                            "description": "返回文本的最大字符数；默认 6000",
                        },
                    },
                    "required": ["url"],
                },
            },
        }
    ]

def get_read_tools() -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": (
                    "读取允许的文本文件。主要用途：按 skills catalog 中的 <location> 加载 SKILL.md，"
                    "再按其说明调用其他工具。只能读取 skills/ 前缀的路径；不能读取任意本地文件。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "虚拟路径，如 'skills/weather/SKILL.md'",
                        },
                        "offset": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "从第几行开始（1-based），默认 1",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 400,
                            "description": "返回行数上限，默认 200",
                        },
                    },
                    "required": ["path"],
                },
            },
        }
    ]


def get_web_search_tools() -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": (
                    "搜索互联网获取当前或外部信息。用于最新消息、实时状态、官网资料、"
                    "需要来源的问题。普通搜索应同步返回；长耗时或深度整理可以排队后台处理。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索关键词或问题",
                        },
                        "count": {
                            "type": "integer",
                            "description": "返回结果数量，1-10",
                            "minimum": 1,
                            "maximum": 10,
                        },
                        "freshness": {
                            "type": "string",
                            "description": "可选时间过滤：day/week/month/year",
                            "enum": ["day", "week", "month", "year"],
                        },
                        "date_after": {
                            "type": "string",
                            "description": "可选，发布日期晚于 YYYY-MM-DD",
                        },
                        "date_before": {
                            "type": "string",
                            "description": "可选，发布日期早于 YYYY-MM-DD",
                        },
                        "language": {
                            "type": "string",
                            "description": "可选，ISO 639-1 语言代码",
                        },
                        "country": {
                            "type": "string",
                            "description": "可选，2 位国家/地区代码",
                        },
                    },
                    "required": ["query"],
                },
            },
        }
    ]


def get_tdai_search_tools() -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": "tdai_memory_search",
                "description": (
                    "检索关于用户的长期记忆结论（已消化的偏好、事件、指令、关系状态）。"
                    "当你在组织回复时发现需要某条用户长期信息、但当前上下文里没有给出时使用；"
                    "用你自己的措辞描述你要找的信息，不受用户原话限制。"
                    "与 tdai_conversation_search 合计每轮最多调用 3 次。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "语义化检索词，描述你要找的用户长期信息",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回条数，1-20，默认 5",
                            "minimum": 1,
                            "maximum": 20,
                        },
                        "type": {
                            "type": "string",
                            "description": "可选，限定记忆类型",
                            "enum": ["persona", "episodic", "instruction"],
                        },
                        "scene": {
                            "type": "string",
                            "description": "可选，限定场景",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "tdai_conversation_search",
                "description": (
                    "检索与用户的原始逐轮对话历史（未提纯的原文）。"
                    "用于核对用户当时的具体措辞、确认时间线、或查找刚说过还没被提纯进长期记忆的内容——"
                    "当 tdai_memory_search 拿不到你要的信息时使用。"
                    "与 tdai_memory_search 合计每轮最多调用 3 次。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "检索词，描述你要找的历史对话内容",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回条数，1-20，默认 5",
                            "minimum": 1,
                            "maximum": 20,
                        },
                    },
                    "required": ["query"],
                },
            },
        },
    ]

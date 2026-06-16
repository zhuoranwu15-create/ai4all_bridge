def get_reminder_tools() -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": "create_reminder",
                "description": (
                    "创建一个提醒。用于用户明确要求在未来某个时间收到提醒的场景。"
                    "due_at 格式为 YYYY-MM-DD HH:MM:SS。"
                    "recur_rule 可选：daily（每天）、weekly:N（每周，N=0 周一…6 周日）、"
                    "monthly:D（每月第 D 日）。不填为一次性提醒。"
                    "时间不明确时不要猜测，告知用户需要补充具体日期和时间。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "提醒内容，简短描述要提醒的事情",
                        },
                        "due_at": {
                            "type": "string",
                            "description": "触发时间，格式 YYYY-MM-DD HH:MM:SS",
                        },
                        "recur_rule": {
                            "type": "string",
                            "description": "周期规则，可选。daily / weekly:N / monthly:D",
                        },
                    },
                    "required": ["text", "due_at"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_reminders",
                "description": (
                    "列出用户当前所有待发送的提醒。"
                    "查看、取消或修改提醒前应先调用此工具确认。"
                    "返回的每条提醒包含 id 字段，cancel_reminder 和 update_reminder 需要用到。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "cancel_reminder",
                "description": (
                    "取消一个待发送的提醒。"
                    "如不确定 reminder_id，先调用 list_reminders 确认。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reminder_id": {
                            "type": "string",
                            "description": "要取消的提醒的 id（从 list_reminders 获得）",
                        },
                    },
                    "required": ["reminder_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "update_reminder",
                "description": (
                    "修改已有提醒的内容、时间或周期。只传要修改的字段，其余保持不变。"
                    "如不确定 reminder_id，先调用 list_reminders 确认。"
                    "recur_rule 传 null 表示改为一次性提醒。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reminder_id": {
                            "type": "string",
                            "description": "要修改的提醒的 id",
                        },
                        "text": {
                            "type": "string",
                            "description": "新的提醒内容，可选",
                        },
                        "due_at": {
                            "type": "string",
                            "description": "新的触发时间，可选，格式 YYYY-MM-DD HH:MM:SS",
                        },
                        "recur_rule": {
                            "type": ["string", "null"],
                            "description": "新的周期规则，可选。传 null 改为一次性。",
                        },
                    },
                    "required": ["reminder_id"],
                },
            },
        },
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


def get_content_invitation_generation_tools() -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": "create_content_invitation_candidate",
                "description": "为当前账号创建一条朋友式内容邀请候选。只能创建邀请，不发送标题列表。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "invitation_text": {"type": "string"},
                        "title_items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "source_name": {"type": "string"},
                                    "url": {"type": "string"},
                                    "published_at": {"type": "string"},
                                },
                                "required": ["title"],
                            },
                            "minItems": 3,
                            "maxItems": 10,
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["topic", "invitation_text", "title_items"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "skip_content_invitation",
                "description": "当前账号不适合创建内容邀请候选时调用，记录跳过原因。",
                "parameters": {
                    "type": "object",
                    "properties": {"reason": {"type": "string"}},
                    "required": ["reason"],
                },
            },
        },
    ]


def get_content_invitation_response_tools() -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": "send_content_invitation_titles",
                "description": "当用户正向确认想看上一条内容邀请时，发送该邀请对应的标题列表。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "invitation_id": {"type": "string"},
                        "max_titles": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["invitation_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "record_content_invitation_feedback",
                "description": "记录用户对内容邀请的拒绝、退订或偏好反馈。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "invitation_id": {"type": "string"},
                        "feedback_type": {
                            "type": "string",
                            "enum": ["decline", "block_topic", "less_like_this", "more_like_this"],
                        },
                        "topic": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["feedback_type"],
                },
            },
        },
    ]


def get_session_status_tools() -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": "session_status",
                "description": (
                    "查询你和这个用户之间的关系状态事实。"
                    "当用户问到这类问题时调用，例如：『我们认识多久了』『我们第一次聊天是什么时候』"
                    "『我连续找你聊了几天』『最近多久没断过』。"
                    "返回：认识天数、首次聊天日期、连续聊天天数(streak)。"
                    "边界：用户没有主动询问关系/会话状态时不要调用，不要为了寒暄或开场白调用；"
                    "当前时间、日期、星期请直接参考系统提示里的运行时信息，不要用本工具；"
                    "本工具不返回任何系统内部运行指标。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        }
    ]


def get_proactive_message_settings_tools() -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": "get_proactive_message_settings",
                "description": (
                    "查询当前用户的主动消息设定（你会不会、什么时候主动找 TA）。"
                    "当用户问『你现在会什么时候主动找我』『我是不是关了主动消息』这类问题时调用。"
                    "返回：总开关、各分类开关、静默时段、临时静默截止时间。"
                    "边界：这只影响系统主动触达（陪伴跟进、内容邀请、重新激活），不影响用户提醒。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "update_proactive_message_settings",
                "description": (
                    "更新当前用户的主动消息设定。"
                    "**收到变更指令后直接调用本工具，无需向用户确认，不可口头声称已完成。**\n"
                    "触发场景（用户说下列任何一句，立即调用，不要问『确定吗』）：\n"
                    "- 『以后别主动找我了』→ master_enabled=false\n"
                    "- 『每天最多X条 / 总共X条 / 一天就发X次』→ total_per_day=X\n"
                    "- 『一周最多X次』→ frequency={\"reactivation\":{\"max_per_week\":X}}\n"
                    "- 『只在XX时间段找我』→ allowed_windows\n"
                    "- 『晚上X点后别发』→ quiet_hours\n"
                    "- 『这周先别主动发』→ muted_until\n"
                    "- 『别再发陪伴跟进/内容』→ category_updates\n"
                    "边界：只管主动触达，不影响用户提醒。"
                    "调用后回复必须说明变更结果并点明『提醒不受影响』。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "master_enabled": {
                            "type": "boolean",
                            "description": "主动消息总开关。false=以后不再主动找用户（提醒除外）。",
                        },
                        "category_updates": {
                            "type": "object",
                            "description": (
                                "按分类开关主动消息。键为分类名，值为 {\"enabled\": true/false}。"
                                "可用分类：companion_followup(陪伴跟进)、content_invitation(内容邀请)、"
                                "reactivation_topic_followup(话题唤回)、reactivation_content_invitation(内容唤回)、"
                                "legacy_proactive。"
                            ),
                        },
                        "quiet_hours": {
                            "type": "object",
                            "description": (
                                "静默时段，时段内不发主动消息。"
                                "{\"enabled\": true, \"start\": \"22:00\", \"end\": \"08:00\"}，时间为 HH:MM。"
                                "enabled=false 表示取消静默时段限制。"
                            ),
                        },
                        "muted_until": {
                            "type": "string",
                            "description": (
                                "临时静默截止时间，格式 'YYYY-MM-DD HH:MM:SS'（北京时间）。"
                                "在此之前不发主动消息。传空字符串或过去时间表示取消临时静默。"
                            ),
                        },
                        "total_per_day": {
                            "type": "integer",
                            "description": (
                                "所有主动消息每日总条数上限（陪伴跟进+唤醒邀请合计）。"
                                "用户说『每天最多X条』『总共X条』『一天就发X次』时使用此参数。"
                                "提醒不计入此上限。受系统硬上限约束，超出自动封顶。"
                            ),
                        },
                        "frequency": {
                            "type": "object",
                            "description": (
                                "精细频次控制（按分组）。键为 companion_followup/reactivation/default；"
                                "值为 {\"max_per_day\": N, \"max_per_week\": M}。"
                                "如只需总量限制，优先用 total_per_day 参数，不必用 frequency。"
                            ),
                        },
                        "allowed_windows": {
                            "type": "array",
                            "description": (
                                "允许推送时段窗口（只在这些时段发主动消息）。"
                                "每项 {\"days\": [\"SAT\",\"SUN\"], \"start\": \"09:00\", \"end\": \"12:00\"}；"
                                "days 取值 MON/TUE/WED/THU/FRI/SAT/SUN，时间 HH:MM，start 必须早于 end（不支持跨午夜）。"
                                "传空数组表示取消时段限制。例如『只在周末上午找我』。"
                            ),
                            "items": {"type": "object"},
                        },
                        "reason": {
                            "type": "string",
                            "description": "本次变更的简短原因，用于审计，例如 user_requested。",
                        },
                    },
                    "required": [],
                },
            },
        },
    ]

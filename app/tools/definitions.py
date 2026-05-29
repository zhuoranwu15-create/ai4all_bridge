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

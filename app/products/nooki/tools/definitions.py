"""Nooki P1 的七个目标拆解工具 schema。"""

_PLAN_OPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["tiny", "light", "normal"]},
        "title": {"type": "string", "minLength": 1, "maxLength": 60},
        "description": {"type": "string", "maxLength": 300},
        "estimated_minutes": {"type": "integer", "minimum": 1, "maximum": 30},
    },
    "required": ["mode", "title", "estimated_minutes"],
}


def get_goal_breakdown_tools() -> list:
    """返回稳定顺序的 P1 Tool schemas。"""

    return [
        {
            "type": "function",
            "function": {
                "name": "nooki_create_task_with_options",
                "description": "原子创建一次开始行动任务和 tiny/light/normal 三档方案。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "minLength": 1, "maxLength": 80},
                        "raw_goal": {"type": "string", "minLength": 1, "maxLength": 500},
                        "options": {
                            "type": "array",
                            "items": _PLAN_OPTION_SCHEMA,
                            "minItems": 3,
                            "maxItems": 3,
                        },
                    },
                    "required": ["title", "raw_goal", "options"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "nooki_select_task_plan",
                "description": "用户明确选择一档方案，创建 pending Step；不会自动开始。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "plan_id": {"type": "string"},
                        "expected_version": {"type": "integer", "minimum": 1},
                    },
                    "required": ["task_id", "plan_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "nooki_start_step",
                "description": "用户明确开始当前 pending Step。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "step_id": {"type": "string"},
                        "expected_version": {"type": "integer", "minimum": 1},
                    },
                    "required": ["step_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "nooki_complete_step",
                "description": "用户完成当前行动；P1 同时结束本次行动任务。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "step_id": {"type": "string"},
                        "expected_version": {"type": "integer", "minimum": 1},
                    },
                    "required": ["step_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "nooki_shrink_step",
                "description": "当前行动太难时，用耗时更短的行动替换；不能替用户宣告失败。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "step_id": {"type": "string"},
                        "replacement": _PLAN_OPTION_SCHEMA,
                        "expected_version": {"type": "integer", "minimum": 1},
                    },
                    "required": ["step_id", "replacement"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "nooki_abandon_task",
                "description": "仅在用户明确永久放弃当前任务时调用；暂时休息不能调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "expected_version": {"type": "integer", "minimum": 1},
                    },
                    "required": ["task_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "nooki_list_state",
                "description": "读取当前权威任务、Step 和三档方案状态。",
                "parameters": {
                    "type": "object",
                    "properties": {"focus_task_id": {"type": "string"}},
                    "required": [],
                },
            },
        },
    ]


__all__ = ["get_goal_breakdown_tools"]

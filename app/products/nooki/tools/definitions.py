"""Nooki 目标拆解工具 schema（9 个，对应 GoalBreakdownService 的写/读方法）。"""

_PLAN_OPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {
            "type": "string",
            "enum": ["tiny", "light", "normal"],
            "description": "档位：tiny=超小步(1-2分钟零门槛)，light=轻量步(3-5分钟)，normal=普通步(8-15分钟)",
        },
        "title": {
            "type": "string",
            "description": "具体、物理、不超过15字的第一个动作，禁止模板化描述",
        },
        "description": {
            "type": "string",
            "description": "第一下具体怎么做，可选",
        },
        "estimated_minutes": {
            "type": "integer",
            "description": "预估耗时分钟数，可选",
        },
    },
    "required": ["mode", "title"],
}


def get_goal_breakdown_tools() -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": "nooki_create_task_draft",
                "description": (
                    "为用户表达出的一个具体任务/目标建一条任务草稿。"
                    "只有确认用户确实想开始某件具体的事时才调用；不确定任务名时先追问，不要调用。"
                    "同一条用户消息重复调用是幂等的，不会重复建任务。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "任务标题，用户说的具体事，如'写月报'"},
                        "raw_goal": {"type": "string", "description": "用户原话/原始诉求"},
                    },
                    "required": ["title", "raw_goal"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "nooki_create_step_options",
                "description": (
                    "为一个刚创建的任务草稿生成三档开始方式（tiny/light/normal，缺一不可）。"
                    "三档必须是三种不同难度的具体动作，不能是同一件事换种说法。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "nooki_create_task_draft 返回的 task_id"},
                        "options": {
                            "type": "array",
                            "items": _PLAN_OPTION_SCHEMA,
                            "minItems": 3,
                            "maxItems": 3,
                            "description": "必须恰好三项，mode 分别为 tiny/light/normal",
                        },
                    },
                    "required": ["task_id", "options"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "nooki_select_task_plan",
                "description": "用户选中三档方案中的一档；会把任务转为进行中，并生成一个待开始的 step。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "plan_id": {"type": "string", "description": "用户选中的方案 id"},
                    },
                    "required": ["task_id", "plan_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "nooki_start_step",
                "description": "用户表示开始做当前 step 了，把 step 标记为进行中。",
                "parameters": {
                    "type": "object",
                    "properties": {"step_id": {"type": "string"}},
                    "required": ["step_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "nooki_complete_step",
                "description": "用户表示完成了当前 step（不代表整个任务完成，任务是否随之结束由 nooki_complete_task 另行调用）。",
                "parameters": {
                    "type": "object",
                    "properties": {"step_id": {"type": "string"}},
                    "required": ["step_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "nooki_shrink_step",
                "description": (
                    "用户觉得当前进行中的 step 太难，想缩小目标。生成一个比原 step 小很多的新 step 并直接激活。"
                    "重点是让用户'先碰一下'，不是完成它：tiny=纯准备动作1分钟内，light=1-2分钟，normal=2-3分钟。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "step_id": {"type": "string", "description": "当前进行中的 step id"},
                        "new_step": _PLAN_OPTION_SCHEMA,
                    },
                    "required": ["step_id", "new_step"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "nooki_complete_task",
                "description": "用户表示整个任务都完成了，把任务标记为终态完成。",
                "parameters": {
                    "type": "object",
                    "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "nooki_abandon_task",
                "description": "用户明确表示放弃这个任务，不打算再做了。只有用户明确说放弃时才调用，不要替用户决定。",
                "parameters": {
                    "type": "object",
                    "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "nooki_list_state",
                "description": "读取用户当前聚焦任务/step 的最新状态。不确定 FOCUS_TASK 上下文是否过期时可以调用确认。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "focus_task_id": {"type": "string", "description": "可选，指定要查看的任务 id；不填则用系统默认聚焦任务"},
                    },
                    "required": [],
                },
            },
        },
    ]


__all__ = ["get_goal_breakdown_tools"]

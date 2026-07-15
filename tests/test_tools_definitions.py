from app.tools import (
    get_content_invitation_response_tools,
    get_default_tools,
    get_proactive_message_settings_tools,
    get_reminder_tools,
    get_web_search_tools,
)


def test_get_reminder_tools_returns_four_tools():
    tools = get_reminder_tools()
    names = {t["function"]["name"] for t in tools}
    assert names == {"create_reminder", "list_reminders", "cancel_reminder", "update_reminder"}


def test_each_tool_has_required_fields():
    for tool in get_reminder_tools():
        assert tool["type"] == "function"
        fn = tool["function"]
        assert "name" in fn
        assert "description" in fn
        assert "parameters" in fn
        assert fn["parameters"]["type"] == "object"


def test_create_reminder_requires_text_and_due_at():
    tools = get_reminder_tools()
    create = next(t for t in tools if t["function"]["name"] == "create_reminder")
    required = create["function"]["parameters"]["required"]
    assert "text" in required
    assert "due_at" in required


def test_reminder_description_offers_dynamic_fulfillment():
    tools = get_reminder_tools()
    create = next(t for t in tools if t["function"]["name"] == "create_reminder")
    description = create["function"]["description"]
    params = create["function"]["parameters"]["properties"]
    # 定时内容订阅走 fulfillment=dynamic；fixed 仍是默认的固定文案。
    assert "dynamic" in description
    assert "fulfillment" in params
    assert set(params["fulfillment"]["enum"]) == {"fixed", "dynamic"}
    # 多星期几周期在描述里可用。
    assert "weekly:0,2,4" in description


def test_cancel_reminder_requires_reminder_id():
    tools = get_reminder_tools()
    cancel = next(t for t in tools if t["function"]["name"] == "cancel_reminder")
    assert "reminder_id" in cancel["function"]["parameters"]["required"]


def test_get_web_search_tools_returns_schema():
    tools = get_web_search_tools()
    assert len(tools) == 1
    fn = tools[0]["function"]
    assert fn["name"] == "web_search"
    assert "query" in fn["parameters"]["required"]
    assert "count" in fn["parameters"]["properties"]


def test_get_default_tools_gates_web_search():
    disabled = {t["function"]["name"] for t in get_default_tools(web_search_enabled=False)}
    enabled = {t["function"]["name"] for t in get_default_tools(web_search_enabled=True)}
    assert "web_search" not in disabled
    assert "web_search" in enabled
    assert {"create_reminder", "list_reminders", "cancel_reminder", "update_reminder"} <= enabled


def test_content_invitation_response_tools_are_opt_in():
    tools = get_content_invitation_response_tools()
    names = {t["function"]["name"] for t in tools}
    assert names == {"send_content_invitation_titles", "record_content_invitation_feedback"}

    disabled = {t["function"]["name"] for t in get_default_tools()}
    enabled = {
        t["function"]["name"]
        for t in get_default_tools(content_invitation_response_enabled=True)
    }
    assert "send_content_invitation_titles" not in disabled
    assert "send_content_invitation_titles" in enabled


def test_proactive_settings_description_excludes_scheduled_content_subscription():
    tools = get_proactive_message_settings_tools()
    get_settings = next(
        t for t in tools if t["function"]["name"] == "get_proactive_message_settings"
    )
    update = next(
        t for t in tools if t["function"]["name"] == "update_proactive_message_settings"
    )
    description = update["function"]["description"]
    allowed_windows = update["function"]["parameters"]["properties"]["allowed_windows"]

    assert "不表示已创建定时任务" in get_settings["function"]["description"]
    assert "定时内容订阅请求" in description
    assert "不会创建例行内容任务" in description
    assert "发送策略过滤器" in allowed_windows["description"]
    assert "不能用于实现定时推送或内容订阅" in allowed_windows["description"]

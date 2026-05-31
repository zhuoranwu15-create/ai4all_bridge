from app.tools import get_default_tools, get_reminder_tools, get_web_search_tools


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

from app.tools import get_reminder_tools


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

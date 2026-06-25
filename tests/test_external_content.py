"""Tests for app/tools/external_content.py (Batch A: external evidence projection)."""
import json

import pytest

from app.tools.external_content import project_tool_result_for_llm, wrap_external_content


class TestWrapExternalContent:
    def test_contains_open_and_close_markers(self):
        result = wrap_external_content("hello", source="web_fetch")
        assert "<<<EXTERNAL_UNTRUSTED_CONTENT" in result
        assert "<<<END_EXTERNAL_UNTRUSTED_CONTENT" in result
        assert "hello" in result

    def test_source_embedded_in_marker(self):
        result = wrap_external_content("data", source="web_search")
        assert 'source="web_search"' in result

    def test_unique_ids_per_call(self):
        r1 = wrap_external_content("x", source="web_fetch")
        r2 = wrap_external_content("x", source="web_fetch")
        # Each call produces a distinct marker id
        assert r1 != r2


class TestProjectToolResultForLlm:
    def test_internal_tool_passthrough(self):
        result = {"status": "created", "reminder_id": "rem-1"}
        out = project_tool_result_for_llm("create_reminder", result)
        parsed = json.loads(out)
        assert parsed["reminder_id"] == "rem-1"
        assert "externalContent" not in parsed

    def test_web_search_gets_external_marker(self):
        result = {"status": "succeeded", "results": [{"title": "t", "url": "u", "snippet": "s"}]}
        out = project_tool_result_for_llm("web_search", result)
        parsed = json.loads(out)
        assert parsed["externalContent"]["untrusted"] is True
        assert parsed["externalContent"]["source"] == "web_search"
        assert parsed["externalContent"]["wrapped"] is True

    def test_web_fetch_gets_external_marker(self):
        result = {"status": 200, "text": "page content"}
        out = project_tool_result_for_llm("web_fetch", result)
        parsed = json.loads(out)
        assert parsed["externalContent"]["untrusted"] is True
        assert parsed["externalContent"]["source"] == "web_fetch"

    def test_already_wrapped_not_double_wrapped(self):
        result = {
            "externalContent": {"untrusted": True, "source": "web_fetch", "wrapped": True},
            "text": "content",
        }
        out = project_tool_result_for_llm("web_fetch", result)
        parsed = json.loads(out)
        # Only one externalContent key at top level
        assert isinstance(parsed["externalContent"], dict)
        assert parsed["text"] == "content"

    def test_non_dict_result_wrapped(self):
        out = project_tool_result_for_llm("create_reminder", "plain string result")
        parsed = json.loads(out)
        assert parsed["result"] == "plain string result"

    def test_internal_tool_truncated_at_max_chars(self):
        big = {"data": "x" * 10000}
        out = project_tool_result_for_llm("create_reminder", big, max_chars=100)
        assert len(out) <= 100

    def test_external_tool_truncated_gracefully(self):
        big = {"status": "succeeded", "results": [{"title": "t", "snippet": "x" * 8000}]}
        out = project_tool_result_for_llm("web_search", big, max_chars=500)
        # Should be parseable JSON and under limit
        parsed = json.loads(out)
        assert parsed["externalContent"]["wrapped"] is True

    def test_web_search_original_data_accessible(self):
        result = {"status": "succeeded", "results": [{"title": "Test", "url": "https://example.com"}]}
        out = project_tool_result_for_llm("web_search", result)
        # Within max_chars, original result keys are preserved alongside externalContent
        parsed = json.loads(out)
        assert parsed.get("status") == "succeeded" or "data" in parsed

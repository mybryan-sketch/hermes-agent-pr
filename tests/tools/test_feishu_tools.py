"""Tests for feishu_doc_tool and feishu_drive_tool — registration and schema validation."""

import importlib
import json
import unittest
from unittest.mock import Mock, patch

from tools.registry import registry

# Trigger tool discovery so feishu tools get registered
feishu_doc_tool = importlib.import_module("tools.feishu_doc_tool")
importlib.import_module("tools.feishu_drive_tool")


class TestFeishuToolRegistration(unittest.TestCase):
    """Verify feishu tools are registered and have valid schemas."""

    EXPECTED_TOOLS = {
        "feishu_doc_read": "feishu_doc",
        "feishu_drive_list_comments": "feishu_drive",
        "feishu_drive_list_comment_replies": "feishu_drive",
        "feishu_drive_reply_comment": "feishu_drive",
        "feishu_drive_add_comment": "feishu_drive",
    }

    def test_all_tools_registered(self):
        for tool_name, toolset in self.EXPECTED_TOOLS.items():
            entry = registry.get_entry(tool_name)
            self.assertIsNotNone(entry, f"{tool_name} not registered")
            self.assertEqual(entry.toolset, toolset)

    def test_schemas_have_required_fields(self):
        for tool_name in self.EXPECTED_TOOLS:
            entry = registry.get_entry(tool_name)
            schema = entry.schema
            self.assertIn("name", schema)
            self.assertEqual(schema["name"], tool_name)
            self.assertIn("description", schema)
            self.assertIn("parameters", schema)
            self.assertIn("type", schema["parameters"])
            self.assertEqual(schema["parameters"]["type"], "object")

    def test_handlers_are_callable(self):
        for tool_name in self.EXPECTED_TOOLS:
            entry = registry.get_entry(tool_name)
            self.assertTrue(callable(entry.handler))

    def test_doc_read_schema_params(self):
        entry = registry.get_entry("feishu_doc_read")
        props = entry.schema["parameters"].get("properties", {})
        self.assertIn("doc_token", props)
        self.assertIn("file_type", props)

    def test_doc_read_uses_fallback_client_in_chat_context(self):
        feishu_doc_tool.set_client(None)
        fake_client = object()

        with (
            patch.object(feishu_doc_tool, "_get_fallback_client", return_value=fake_client) as fallback,
            patch.object(
                feishu_doc_tool,
                "_request_json",
                return_value=(0, "ok", {"content": "hello from feishu"}),
            ) as request_json,
        ):
            result = json.loads(feishu_doc_tool._handle_feishu_doc_read({"doc_token": "DOCX_TOKEN_123"}))

        self.assertTrue(result["success"])
        self.assertEqual(result["content"], "hello from feishu")
        fallback.assert_called_once_with()
        request_json.assert_called_once_with(
            fake_client,
            "GET",
            feishu_doc_tool._RAW_CONTENT_URI,
            paths={"document_id": "DOCX_TOKEN_123"},
        )

    def test_doc_read_resolves_wiki_url_before_reading(self):
        feishu_doc_tool.set_client(Mock())

        def fake_request(_client, _method, uri, paths=None, queries=None):
            if uri == feishu_doc_tool._WIKI_GET_NODE_URI:
                self.assertEqual(queries, [("token", "WIKI_TOKEN_123")])
                return 0, "ok", {"node": {"obj_type": "docx", "obj_token": "DOCX_TOKEN_456"}}
            self.assertEqual(paths, {"document_id": "DOCX_TOKEN_456"})
            return 0, "ok", {"content": "resolved wiki body"}

        with patch.object(feishu_doc_tool, "_request_json", side_effect=fake_request):
            result = json.loads(
                feishu_doc_tool._handle_feishu_doc_read(
                    {"doc_token": "https://bjrrtx.feishu.cn/wiki/WIKI_TOKEN_123?from=auth_notice"}
                )
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["content"], "resolved wiki body")
        self.assertEqual(result["resolved_token"], "DOCX_TOKEN_456")
        self.assertTrue(result["resolved_from_wiki"])

    def test_doc_read_retries_raw_token_as_wiki_after_docx_failure(self):
        feishu_doc_tool.set_client(Mock())
        calls = []

        def fake_request(_client, _method, uri, paths=None, queries=None):
            calls.append((uri, paths, queries))
            if uri == feishu_doc_tool._RAW_CONTENT_URI and paths == {"document_id": "WIKI_TOKEN_123"}:
                return 999, "doc not found", {}
            if uri == feishu_doc_tool._WIKI_GET_NODE_URI:
                return 0, "ok", {"node": {"obj_type": "docx", "obj_token": "DOCX_TOKEN_456"}}
            if uri == feishu_doc_tool._RAW_CONTENT_URI and paths == {"document_id": "DOCX_TOKEN_456"}:
                return 0, "ok", {"content": "retried wiki body"}
            return 500, "unexpected", {}

        with patch.object(feishu_doc_tool, "_request_json", side_effect=fake_request):
            result = json.loads(feishu_doc_tool._handle_feishu_doc_read({"doc_token": "WIKI_TOKEN_123"}))

        self.assertTrue(result["success"])
        self.assertEqual(result["content"], "retried wiki body")
        self.assertEqual(result["wiki_token"], "WIKI_TOKEN_123")
        self.assertEqual(
            calls,
            [
                (feishu_doc_tool._RAW_CONTENT_URI, {"document_id": "WIKI_TOKEN_123"}, None),
                (feishu_doc_tool._WIKI_GET_NODE_URI, None, [("token", "WIKI_TOKEN_123")]),
                (feishu_doc_tool._RAW_CONTENT_URI, {"document_id": "DOCX_TOKEN_456"}, None),
            ],
        )

    def test_drive_tools_require_file_token(self):
        for tool_name in self.EXPECTED_TOOLS:
            if tool_name == "feishu_doc_read":
                continue
            entry = registry.get_entry(tool_name)
            props = entry.schema["parameters"].get("properties", {})
            self.assertIn("file_token", props, f"{tool_name} missing file_token param")
            self.assertIn("file_type", props, f"{tool_name} missing file_type param")


if __name__ == "__main__":
    unittest.main()

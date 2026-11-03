import unittest
import json
import asyncio
from unittest.mock import patch, MagicMock
from app import (
    parse_tool_definitions,
    build_tool_system_prompt,
    prepare_messages_for_tools,
    extract_tool_calls,
    normalize_upstream_messages,
    build_upstream_payload,
    app,
)
from fastapi.testclient import TestClient

client = TestClient(app)

class TestToolEmulation(unittest.TestCase):
    def setUp(self):
        self.sample_tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get current weather in city",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location": {"type": "string", "description": "City name"}
                        },
                        "required": ["location"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "calculate",
                    "description": "Calculate math expression",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "expr": {"type": "string"}
                        },
                        "required": ["expr"]
                    }
                }
            }
        ]

    def test_parse_tool_definitions(self):
        tool_defs, valid_names = parse_tool_definitions(self.sample_tools)
        self.assertEqual(valid_names, ["get_weather", "calculate"])
        self.assertEqual(len(tool_defs), 2)

        # Legacy functions support
        legacy_functions = [{"name": "search", "description": "web search"}]
        tool_defs2, valid_names2 = parse_tool_definitions(None, functions=legacy_functions)
        self.assertEqual(valid_names2, ["search"])

    def test_build_tool_system_prompt(self):
        tool_defs, _ = parse_tool_definitions(self.sample_tools)
        prompt = build_tool_system_prompt(tool_defs, tool_choice="auto")
        self.assertIn("## Tools", prompt)
        self.assertIn("get_weather", prompt)
        self.assertIn("```json", prompt)

        # Required choice
        req_prompt = build_tool_system_prompt(tool_defs, tool_choice="required")
        self.assertIn("REQUIRED to call at least one", req_prompt)

        # Specific function choice
        fn_prompt = build_tool_system_prompt(tool_defs, tool_choice={"type": "function", "function": {"name": "calculate"}})
        self.assertIn("REQUIRED to call the function 'calculate'", fn_prompt)

    def test_prepare_messages_for_tools(self):
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What's the weather in Tokyo?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": "{\"location\":\"Tokyo\"}"}
                    }
                ]
            },
            {"role": "tool", "tool_call_id": "call_123", "name": "get_weather", "content": "{\"temp\": 22}"}
        ]
        tool_prompt = "TOOL_PROMPT_MOCK"
        transformed = prepare_messages_for_tools(messages, tool_prompt=tool_prompt)
        self.assertEqual(len(transformed), 5)
        self.assertEqual(transformed[-1]["role"], "system")
        self.assertEqual(transformed[-1]["content"], "TOOL_PROMPT_MOCK")

    def test_normalize_upstream_messages(self):
        messages = [
            {"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": "Write a python script."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "bash", "arguments": "{\"cmd\": \"ls\"}"}
                    }
                ]
            },
            {"role": "tool", "tool_call_id": "call_abc", "content": "file1.txt\nfile2.txt"},
            {"role": "system", "content": "Additional tool schema"}
        ]
        norm = normalize_upstream_messages(messages)

        # 1. No system role allowed in upstream
        roles = [m["role"] for m in norm]
        self.assertNotIn("system", roles)
        self.assertNotIn("tool", roles)

        # 2. System prompt prepended to the first user message
        first_user = norm[0]
        self.assertEqual(first_user["role"], "user")
        self.assertIn("[System Instructions]", first_user["content"])
        self.assertIn("You are a coding agent.", first_user["content"])
        self.assertIn("Additional tool schema", first_user["content"])
        self.assertIn("Write a python script.", first_user["content"])

        # 3. Assistant tool calls formatted as JSON block for consistent in-context learning
        self.assertIn('"name": "bash"', norm[1]["content"])

        # 4. Tool result flattened to user
        self.assertEqual(norm[2]["role"], "user")
        self.assertIn("[Tool Execution Result for `call_abc`]:", norm[2]["content"])

        # 5. Last message must be user
        self.assertEqual(norm[-1]["role"], "user")

    def test_build_upstream_payload_message_id_binding(self):
        messages = [
            {"role": "system", "content": "sys prompt"},
            {"role": "user", "content": "hello"}
        ]
        payload = build_upstream_payload(messages, reasoning_effort="high", web_search=True)

        self.assertEqual(payload["reasoningEffort"], "high")
        self.assertTrue(payload["webSearchEnabled"])
        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["role"], "user")

        # messageId must strictly equal the id of the last message in messages array
        last_id = payload["messages"][-1]["id"]
        self.assertEqual(payload["messageId"], last_id)

    def test_extract_tool_calls_markdown(self):
        text = """I will check the weather for you.
```json
{
  "name": "get_weather",
  "arguments": {
    "location": "Tokyo"
  }
}
```
"""
        tool_calls, remaining = extract_tool_calls(text, ["get_weather", "calculate"])
        self.assertIsNotNone(tool_calls)
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["function"]["name"], "get_weather")
        args = json.loads(tool_calls[0]["function"]["arguments"])
        self.assertEqual(args.get("location"), "Tokyo")
        self.assertEqual(remaining, "I will check the weather for you.")

    def test_extract_tool_calls_raw_json(self):
        text = '{"name": "calculate", "arguments": {"expr": "12 * 8"}}'
        tool_calls, remaining = extract_tool_calls(text, ["get_weather", "calculate"])
        self.assertIsNotNone(tool_calls)
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["function"]["name"], "calculate")
        self.assertEqual(remaining, "")

    def test_extract_tool_calls_multiple(self):
        text = """```json
[
  {"name": "get_weather", "arguments": {"location": "Beijing"}},
  {"name": "get_weather", "arguments": {"location": "Shanghai"}}
]
```"""
        tool_calls, remaining = extract_tool_calls(text, ["get_weather"])
        self.assertIsNotNone(tool_calls)
        self.assertEqual(len(tool_calls), 2)
        self.assertEqual(tool_calls[0]["function"]["name"], "get_weather")
        self.assertEqual(tool_calls[1]["function"]["name"], "get_weather")

    def test_extract_tool_calls_user_reported_case(self):
        # Exact reproduction of user issue: natural calling text + prefix
        text = """✅ `todo` 正常（#1-#6 已创建）。

开始任务 #1，标记为进行中：
Calling tool `todo` with arguments: {"action":"update","id":1,"status":"in_progress","activeForm":"测试基础工具"}"""

        tool_calls, remaining = extract_tool_calls(text, ["todo", "bash"])
        self.assertIsNotNone(tool_calls)
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["function"]["name"], "todo")
        args = json.loads(tool_calls[0]["function"]["arguments"])
        self.assertEqual(args["action"], "update")
        self.assertEqual(args["id"], 1)
        self.assertEqual(args["status"], "in_progress")
        self.assertIn("✅ `todo` 正常", remaining)
        self.assertIn("开始任务 #1，标记为进行中：", remaining)

    def test_extract_tool_calls_action_format(self):
        text = """Action: bash
Action Input: {"command": "ls -la"}"""
        tool_calls, remaining = extract_tool_calls(text, ["bash"])
        self.assertIsNotNone(tool_calls)
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["function"]["name"], "bash")
        args = json.loads(tool_calls[0]["function"]["arguments"])
        self.assertEqual(args["command"], "ls -la")

    def test_extract_tool_calls_multiple_independent_blocks(self):
        text = """准备查看文件列表：
```json
{"name": "bash", "arguments": {"command": "ls"}}
```
同时查看当前路径：
```json
{"name": "bash", "arguments": {"command": "pwd"}}
```
操作准备完毕。"""
        tool_calls, remaining = extract_tool_calls(text, ["bash"])
        self.assertIsNotNone(tool_calls)
        self.assertEqual(len(tool_calls), 2)
        self.assertEqual(json.loads(tool_calls[0]["function"]["arguments"])["command"], "ls")
        self.assertEqual(json.loads(tool_calls[1]["function"]["arguments"])["command"], "pwd")
        self.assertIn("准备查看文件列表：", remaining)
        self.assertIn("同时查看当前路径：", remaining)
        self.assertIn("操作准备完毕。", remaining)

    def test_compact_tool_definitions(self):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": f"tool_{i}",
                    "description": "Very long description " * 20,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            f"arg_{j}": {"type": "string", "description": "arg desc " * 20}
                            for j in range(5)
                        }
                    }
                }
            }
            for i in range(70)
        ]
        tool_defs, valid_names = parse_tool_definitions(tools)
        prompt = build_tool_system_prompt(tool_defs)
        self.assertLess(len(prompt), 35000)

    def test_extract_tool_calls_no_match(self):
        text = "Hello! Tokyo is a great city. No tools needed."
        tool_calls, remaining = extract_tool_calls(text, ["get_weather"])
        self.assertIsNone(tool_calls)
        self.assertEqual(remaining, text)

    def test_extract_tool_calls_unknown_function(self):
        text = '```json\n{"name": "unknown_tool", "arguments": {}}\n```'
        tool_calls, remaining = extract_tool_calls(text, ["get_weather"])
        self.assertIsNone(tool_calls)


class TestChatCompletionEndpointWithTools(unittest.TestCase):
    @patch("app.engine.chat_events")
    def test_chat_completions_non_stream_tool_call(self, mock_chat_events):
        async def fake_events(*args, **kwargs):
            yield ("reasoning", "Thinking about what tool to use...")
            yield ("text", '```json\n{"name": "get_weather", "arguments": {"location": "Paris"}}\n```')

        mock_chat_events.side_effect = fake_events

        req_payload = {
            "model": "mercury-3",
            "messages": [{"role": "user", "content": "What is the weather in Paris?"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {"type": "object", "properties": {"location": {"type": "string"}}}
                    }
                }
            ],
            "stream": False
        }

        response = client.post("/v1/chat/completions", json=req_payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        choice = data["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertIn("tool_calls", choice["message"])
        self.assertEqual(choice["message"]["tool_calls"][0]["function"]["name"], "get_weather")
        self.assertIn("reasoning_content", choice["message"])
        self.assertEqual(choice["message"]["reasoning_content"], "Thinking about what tool to use...")

    @patch("app.engine.chat_events")
    def test_chat_completions_stream_tool_call(self, mock_chat_events):
        async def fake_events(*args, **kwargs):
            yield ("reasoning", "Thinking...")
            yield ("text", '```json\n{"name": "get_weather", "arguments": {"location": "London"}}\n```')

        mock_chat_events.side_effect = fake_events

        req_payload = {
            "model": "mercury-3",
            "messages": [{"role": "user", "content": "What is the weather in London?"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {"type": "object", "properties": {"location": {"type": "string"}}}
                    }
                }
            ],
            "stream": True
        }

        response = client.post("/v1/chat/completions", json=req_payload)
        self.assertEqual(response.status_code, 200)
        chunks = []
        for line in response.iter_lines():
            if line.startswith("data: ") and line != "data: [DONE]":
                chunks.append(json.loads(line[6:]))

        # Verify stream chunk formats
        self.assertTrue(any("reasoning_content" in c["choices"][0]["delta"] for c in chunks))
        tool_call_chunks = [c for c in chunks if "tool_calls" in c["choices"][0]["delta"]]
        self.assertTrue(len(tool_call_chunks) > 0)
        self.assertEqual(tool_call_chunks[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"], "get_weather")
        end_chunks = [c for c in chunks if c["choices"][0].get("finish_reason") == "tool_calls"]
        self.assertEqual(len(end_chunks), 1)

    @patch("app.engine.chat_events")
    def test_chat_completions_normal_chat_without_tools(self, mock_chat_events):
        async def fake_events(*args, **kwargs):
            yield ("text", "Hello there!")

        mock_chat_events.side_effect = fake_events

        req_payload = {
            "model": "mercury-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": False
        }

        response = client.post("/v1/chat/completions", json=req_payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["choices"][0]["finish_reason"], "stop")
        self.assertEqual(data["choices"][0]["message"]["content"], "Hello there!")
        self.assertNotIn("tool_calls", data["choices"][0]["message"])


if __name__ == "__main__":
    unittest.main()

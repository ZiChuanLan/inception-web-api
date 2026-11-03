import unittest
import json
import uuid
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import app, compact_tool_definitions

client = TestClient(app)

def generate_agent_tools():
    """Generates a realistic suite of 55 tools matching Pi Coding Agent + OpenViking."""
    base_tools = [
        {
            "type": "function",
            "function": {
                "name": "todo",
                "description": "Manage a task list for tracking multi-step progress. Actions: create, update, list, get, delete, clear.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["create", "update", "list", "get", "delete", "clear"]},
                        "id": {"type": "integer", "description": "Task ID"},
                        "subject": {"type": "string", "description": "Short task title"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"]},
                        "activeForm": {"type": "string", "description": "Present-continuous spinner label"},
                        "tasks": {"type": "array", "description": "Batch create tasks"}
                    },
                    "required": ["action"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": "Execute bash commands in the current working directory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Shell command to execute"},
                        "timeout": {"type": "number", "description": "Timeout in seconds"}
                    },
                    "required": ["command"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "Read file contents with optional offset and limit.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to the file to read"},
                        "offset": {"type": "integer", "description": "Line number to start reading from"},
                        "limit": {"type": "integer", "description": "Maximum number of lines to read"}
                    },
                    "required": ["path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "edit",
                "description": "Edit a single file using exact text replacement.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to file"},
                        "edits": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "oldText": {"type": "string"},
                                    "newText": {"type": "string"}
                                },
                                "required": ["oldText", "newText"]
                            }
                        }
                    },
                    "required": ["path", "edits"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "lens_diagnostics",
                "description": "Query pi-lens diagnostics from LSP probe.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "paths": {"type": "array", "items": {"type": "string"}},
                        "scope": {"type": "string", "enum": ["paths", "workspace"]},
                        "source": {"type": "string", "enum": ["lsp", "session"]}
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "openviking_search",
                "description": "Retrieval with optional session context from OpenViking long term memory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Semantic query"},
                        "limit": {"type": "integer"}
                    },
                    "required": ["query"]
                }
            }
        }
    ]

    # Fill up to 55 realistic tools to simulate full Pi environment
    for i in range(1, 50):
        base_tools.append({
            "type": "function",
            "function": {
                "name": f"extra_tool_{i}",
                "description": f"Extended utility tool {i} for specific subsystem operations.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string"},
                        "options": {"type": "object"}
                    },
                    "required": ["target"]
                }
            }
        })

    return base_tools


class TestFullAgentWorkflow(unittest.TestCase):
    def setUp(self):
        self.tools = generate_agent_tools()

    def test_schema_compaction_on_full_suite(self):
        """Verifies that 55 tools are compacted without error and remain under budget."""
        raw_defs = [t["function"] for t in self.tools]
        compacted = compact_tool_definitions(raw_defs)
        self.assertLessEqual(len(compacted), 50)
        json_str = json.dumps(compacted, ensure_ascii=False, separators=(',', ':'))
        self.assertLess(len(json_str), 30000)

    @patch("app.engine.chat_events")
    def test_multi_turn_agent_loop_streaming(self, mock_chat_events):
        """
        Simulates a complete 5-turn Agent Loop using streaming (stream=True):
        Round 1: Calling tool `todo` with arguments: {...} (Natural format)
        Round 2: Markdown block ````json {"name": "read", ...} ````
        Round 3: Concurrent tools [bash, lens_diagnostics]
        Round 4: Bare json {"name": "edit", ...}
        Round 5: Final text answer (finish_reason='stop')
        """
        responses_pool = [
            # Round 1: Natural calling syntax (user-reported case)
            [
                ("reasoning", "I should create a task first.\n"),
                ("text", "✅ 准备就绪，开始创建任务。\nCalling tool `todo` with arguments: {\"action\": \"create\", \"tasks\": [{\"subject\": \"Check system\"}]}")
            ],
            # Round 2: Standard markdown code block
            [
                ("reasoning", "Now let me inspect app.py.\n"),
                ("text", "Task created. Now reading the file.\n```json\n{\"name\": \"read\", \"arguments\": {\"path\": \"app.py\", \"limit\": 50}}\n```")
            ],
            # Round 3: Multi-tool concurrent calling
            [
                ("reasoning", "Running git check and diagnostics concurrently.\n"),
                ("text", "```json\n[\n  {\"name\": \"bash\", \"arguments\": {\"command\": \"git status\"}},\n  {\"name\": \"lens_diagnostics\", \"arguments\": {\"paths\": [\"app.py\"]}}\n]\n```")
            ],
            # Round 4: Bare JSON object with conversational lead
            [
                ("reasoning", "Applying the final edit.\n"),
                ("text", "I will now edit the file to fix the issue: {\"name\": \"edit\", \"arguments\": {\"path\": \"app.py\", \"edits\": [{\"oldText\": \"A\", \"newText\": \"B\"}]}}")
            ],
            # Round 5: Final completion (no tools)
            [
                ("reasoning", "All actions complete.\n"),
                ("text", "All 4 tasks have been successfully executed! The repository is clean and passing all tests.")
            ]
        ]

        turn = 0
        def get_turn_events(*args, **kwargs):
            nonlocal turn
            events = responses_pool[turn]
            turn += 1
            async def gen():
                for k, v in events:
                    yield (k, v)
            return gen()

        mock_chat_events.side_effect = get_turn_events

        # Initial Agent Message
        messages = [
            {"role": "system", "content": "You are Pi, an autonomous coding agent harness."},
            {"role": "user", "content": "Please inspect app.py, check diagnostics, and apply the fix."}
        ]

        executed_tools = []

        # Run 5 turns of the Agent Loop
        for round_idx in range(5):
            req_body = {
                "model": "mercury-3",
                "messages": messages,
                "tools": self.tools,
                "tool_choice": "auto",
                "stream": True
            }

            resp = client.post("/v1/chat/completions", json=req_body)
            self.assertEqual(resp.status_code, 200)

            # Consume SSE stream
            chunks = []
            for line in resp.iter_lines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    chunks.append(json.loads(line[6:]))

            # Extract tool calls and finish reason from chunks
            tool_calls = []
            finish_reason = None
            assistant_content = []

            for c in chunks:
                choice = c["choices"][0]
                delta = choice.get("delta", {})
                if "content" in delta and delta["content"]:
                    assistant_content.append(delta["content"])
                if "tool_calls" in delta and delta["tool_calls"]:
                    tool_calls.extend(delta["tool_calls"])
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]

            if finish_reason == "tool_calls":
                self.assertTrue(len(tool_calls) > 0)
                # Append assistant message with tool calls
                formatted_tc = []
                for tc in tool_calls:
                    fname = tc["function"]["name"]
                    fargs = tc["function"]["arguments"]
                    call_id = tc.get("id") or ("call_" + uuid.uuid4().hex[:12])
                    formatted_tc.append({
                        "id": call_id,
                        "type": "function",
                        "function": {"name": fname, "arguments": fargs}
                    })
                    executed_tools.append(fname)

                messages.append({
                    "role": "assistant",
                    "content": "".join(assistant_content),
                    "tool_calls": formatted_tc
                })

                # Client executes each tool and appends role="tool"
                for tc in formatted_tc:
                    mock_tool_result = f'{{"status": "ok", "tool": "{tc["function"]["name"]}", "executed": true}}'
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tc["function"]["name"],
                        "content": mock_tool_result
                    })
            else:
                self.assertEqual(finish_reason, "stop")
                self.assertIn("All 4 tasks have been successfully executed", "".join(assistant_content))
                break

        # Verify all tools across all turns were captured and executed in order
        self.assertEqual(executed_tools, ["todo", "read", "bash", "lens_diagnostics", "edit"])

    @patch("app.engine.chat_events")
    def test_multi_turn_agent_loop_non_streaming(self, mock_chat_events):
        """Verifies the same 5-turn cycle under non-streaming mode (stream=False)."""
        responses_pool = [
            [("text", "Calling tool `bash` with arguments: {\"command\": \"pwd\"}")],
            [("text", "```json\n{\"name\": \"todo\", \"arguments\": {\"action\": \"list\"}}\n```")],
            [("text", "Done! Finished everything.")]
        ]

        turn = 0
        def get_turn_events(*args, **kwargs):
            nonlocal turn
            events = responses_pool[turn]
            turn += 1
            async def gen():
                for k, v in events:
                    yield (k, v)
            return gen()

        mock_chat_events.side_effect = get_turn_events

        messages = [{"role": "user", "content": "Run commands."}]

        # Turn 1: bash
        r1 = client.post("/v1/chat/completions", json={
            "model": "mercury-3",
            "messages": messages,
            "tools": self.tools,
            "stream": False
        })
        d1 = r1.json()["choices"][0]
        self.assertEqual(d1["finish_reason"], "tool_calls")
        self.assertEqual(d1["message"]["tool_calls"][0]["function"]["name"], "bash")

        messages.append(d1["message"])
        messages.append({"role": "tool", "tool_call_id": d1["message"]["tool_calls"][0]["id"], "content": "/home/app"})

        # Turn 2: todo
        r2 = client.post("/v1/chat/completions", json={
            "model": "mercury-3",
            "messages": messages,
            "tools": self.tools,
            "stream": False
        })
        d2 = r2.json()["choices"][0]
        self.assertEqual(d2["finish_reason"], "tool_calls")
        self.assertEqual(d2["message"]["tool_calls"][0]["function"]["name"], "todo")

        messages.append(d2["message"])
        messages.append({"role": "tool", "tool_call_id": d2["message"]["tool_calls"][0]["id"], "content": "[]"})

        # Turn 3: finish
        r3 = client.post("/v1/chat/completions", json={
            "model": "mercury-3",
            "messages": messages,
            "tools": self.tools,
            "stream": False
        })
        d3 = r3.json()["choices"][0]
        self.assertEqual(d3["finish_reason"], "stop")
        self.assertIn("Done! Finished everything.", d3["message"]["content"])


if __name__ == "__main__":
    unittest.main()

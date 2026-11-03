"""
Inception Mercury Web-to-API Service (Headless Browser Bridge Edition)
Exposes an OpenAI-compatible /v1/chat/completions API.

Features:
- Playwright headless Chromium bridge: real browser network stack satisfies Vercel
  Security Checkpoint & Turnstile by native in-page fetch (works from ANY IP,
  including datacenter/VPS IPs)
- Full 4-level reasoning_effort support: instant, low, medium, high
- Full OpenAI / DeepSeek R1 reasoning_content stream & non-stream support
- Live web search citations support
- Auto-refreshing session token pool (browser engine)
- Optional curl_cffi zero-browser fallback engine (ENGINE=curl)
"""

import os
import sys
import re
import time
import uuid
import json
import asyncio
import threading
from typing import List, Dict, Any, Optional, Tuple
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn

try:
    PORT = int(os.getenv("PORT", "8011"))
except ValueError:
    PORT = 8011

HOST = os.getenv("HOST", "0.0.0.0")
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None
API_KEY = os.getenv("API_KEY", "").strip() or None
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "mercury-3")
ENGINE = os.getenv("ENGINE", "browser").strip().lower()
CHROME_PATH = os.getenv("CHROME_PATH", "").strip() or None

app = FastAPI(
    title="Inception Mercury Web-to-API",
    description="Headless browser bridge with OpenAI-compatible API for Inception Mercury 3",
    version="2.0.0"
)

REASONING_LEVELS = {"instant", "low", "medium", "high"}

def normalize_reasoning_effort(val: Optional[str]) -> str:
    if not val:
        return "medium"
    val = str(val).lower().strip()
    if val in ("instant", "none", "off", "0"):
        return "instant"
    if val in ("low", "1"):
        return "low"
    if val in ("medium", "2"):
        return "medium"
    if val in ("high", "max", "3"):
        return "high"
    return "medium"


def current_timestamp() -> int:
    try:
        return int(time.time())
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# Upstream Payload Normalization (Vercel AI SDK compatibility)
# ---------------------------------------------------------------------------
def normalize_upstream_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Normalizes OpenAI messages array strictly for Inception Web API (/api/chat):
    - Upstream only supports 'user' and 'assistant' roles (Vercel AI SDK useChat).
    - 'system' and 'developer' roles are extracted and prepended to the first 'user' message.
    - 'tool' / 'function' roles are flattened to 'user' messages.
    - 'assistant' tool_calls are converted to readable text.
    - Guarantees the last message has role='user'.
    """
    system_parts = []
    cleaned_messages = []

    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        content = m.get("content", "")
        tool_calls = m.get("tool_calls")

        # Handle system/developer roles
        if role in ("system", "developer"):
            if content:
                system_parts.append(str(content).strip())
            continue

        # Handle tool/function roles
        elif role in ("tool", "function"):
            tool_id = m.get("tool_call_id") or m.get("name") or "tool"
            cleaned_messages.append({
                "role": "user",
                "content": f"[Tool Execution Result for `{tool_id}`]:\n{content}"
            })
            continue

        # Handle assistant tool_calls: render as strict json markdown block matching our protocol
        elif role == "assistant" and tool_calls:
            calls_blocks = []
            for tc in tool_calls:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                fname = fn.get("name", "unknown")
                fargs = fn.get("arguments", "{}")
                # Format as json code block for consistent in-context learning
                calls_blocks.append(f"```json\n{{\"name\": \"{fname}\", \"arguments\": {fargs}}}\n```")
            summary_str = "\n".join(calls_blocks)
            if content:
                content = f"{content}\n{summary_str}"
            else:
                content = summary_str

        # Standard assistant or user
        cleaned_messages.append({"role": role if role in ("user", "assistant") else "user", "content": content})

    # Prepend all collected system instructions to the first user message
    if system_parts:
        merged_system = "\n\n".join(system_parts)
        system_prefix = f"[System Instructions]\n{merged_system}\n\n"
        first_user_idx = next((i for i, msg in enumerate(cleaned_messages) if msg["role"] == "user"), None)
        if first_user_idx is not None:
            cleaned_messages[first_user_idx]["content"] = system_prefix + str(cleaned_messages[first_user_idx]["content"])
        else:
            # If no user message was present, create one with the system prompt
            cleaned_messages.insert(0, {"role": "user", "content": merged_system})

    # Ensure there is at least one message and the last message is 'user'
    if not cleaned_messages:
        cleaned_messages.append({"role": "user", "content": "Hello"})
    elif cleaned_messages[-1]["role"] != "user":
        cleaned_messages.append({"role": "user", "content": "Please continue."})

    return cleaned_messages


def build_upstream_payload(
    messages: List[Dict[str, Any]],
    reasoning_effort: str = "medium",
    web_search: bool = False,
    chat_id: Optional[str] = None
) -> Dict[str, Any]:
    """Constructs a strict, error-free payload matching Vercel AI SDK useChat protocol."""
    norm_messages = normalize_upstream_messages(messages)
    chat_id = chat_id or ("chat_" + uuid.uuid4().hex[:12])

    formatted_messages = []
    for i, m in enumerate(norm_messages):
        mid = f"msg_{i}_{uuid.uuid4().hex[:8]}"
        role = m["role"]
        content = m.get("content", "")
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    parts.append({"type": "text", "text": p.get("text", "")})
        else:
            parts = [{"type": "text", "text": str(content)}]
        formatted_messages.append({"id": mid, "role": role, "parts": parts})

    last_msg_id = formatted_messages[-1]["id"]
    return {
        "id": chat_id,
        "messageId": last_msg_id,  # Strictly equals the id of the last message
        "trigger": "submit-message",
        "messages": formatted_messages,
        "reasoningEffort": reasoning_effort,
        "webSearchEnabled": web_search,
        "voiceMode": False,
        "timezone": "Asia/Shanghai",
        "language": "zh-CN",
    }


# ---------------------------------------------------------------------------
# Engine 1: Playwright headless browser bridge (default)
# ---------------------------------------------------------------------------
class BrowserBridgeEngine:
    """Runs a persistent headless Chromium in a dedicated background event loop,
    accepts chat requests via run_coroutine_threadsafe, returns SSE yield coroutine."""

    def __init__(self, proxy: Optional[str] = None, chrome_path: Optional[str] = None):
        self.proxy = proxy
        self.chrome_path = chrome_path
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._ready = asyncio.Event()
        self._page = None
        self._browser = None
        self._playwright = None
        self.token = None
        self.token_expire = 0.0
        self._token_lock = asyncio.Lock()
        self.active_queues = {}
        self.failed = False

    # ---- lifecycle ----------------------------------------------------------
    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="pw-bridge-loop")
            self._thread.start()

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._bootstrap())
        except Exception as e:
            import traceback
            print(f"[!] Browser engine bootstrap error: {e}")
            traceback.print_exc()
            self.failed = True
        if not self.failed:
            self.loop.run_forever()
        else:
            self.loop.close()

    async def _bootstrap(self):
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-gpu",
            "--window-size=1280,800",
            "--disable-dev-shm-usage",
        ]
        launch_kwargs = {"headless": True, "args": launch_args}
        if self.proxy:
            launch_kwargs["proxy"] = {"server": self.proxy}
        if self.chrome_path and os.path.exists(self.chrome_path):
            launch_kwargs["executable_path"] = self.chrome_path
        else:
            # fallback to an installed chrome executable
            for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                      r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                      "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"):
                if os.path.exists(p):
                    launch_kwargs["executable_path"] = p
                    break

        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        self._context = await self._browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 800},
        )
        self._page = await self._context.new_page()

        async def on_chunk(req_id, text):
            q = self.active_queues.get(req_id)
            if q:
                await q.put(("chunk", text))

        async def on_done(req_id, status, error_msg):
            q = self.active_queues.get(req_id)
            if q:
                await q.put(("done", status, error_msg))

        await self._page.expose_function("py_on_chunk", on_chunk)
        await self._page.expose_function("py_on_done", on_done)

        await self._page.goto("https://chat.inceptionlabs.ai", timeout=30000)
        cleared = False
        for _ in range(20):
            await asyncio.sleep(1)
            try:
                t = await self._page.title()
            except Exception:
                t = ""
            if "Vercel" not in t and ("Chat" in t or "Inception" in t):
                cleared = True
                break
        if not cleared:
            raise RuntimeError("Vercel Security Checkpoint not cleared in browser")

        await self._page.evaluate("""() => {
            window.inceptionSendChat = function(reqId, payload, token) {
                fetch('/api/chat', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'x-session-token': token,
                        'Origin': 'https://chat.inceptionlabs.ai',
                        'Referer': 'https://chat.inceptionlabs.ai/'
                    },
                    body: JSON.stringify(payload)
                }).then(async (r) => {
                    if (!r.ok) {
                        const err = await r.text();
                        await window.py_on_done(reqId, r.status, err);
                        return;
                    }
                    const reader = r.body.getReader();
                    const decoder = new TextDecoder();
                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;
                        await window.py_on_chunk(reqId, decoder.decode(value, { stream: true }));
                    }
                    await window.py_on_done(reqId, 200, '');
                }).catch(async (e) => {
                    await window.py_on_done(reqId, 500, e.toString());
                });
            };
        }""")
        self._ready.set()
        print("[Bridge] Browser engine ready: Vercel checkpoint cleared, in-page fetch injected")

    # ---- public API (called from FastAPI thread) ---------------------------
    def _submit(self, coro):
        if not self.loop or self.failed:
            raise RuntimeError("Browser bridge is not running")
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut

    async def get_token(self, force_refresh=False):
        fut = self._submit(self._get_token_inner(force_refresh))
        return await asyncio.wrap_future(fut)

    async def _get_token_inner(self, force_refresh=False):
        async with self._token_lock:
            now = self.loop.time()
            if not self.token or now > self.token_expire or force_refresh:
                res = await self._page.evaluate("""async () => {
                    try {
                        const r = await fetch('/api/session');
                        return { status: r.status, data: await r.json() };
                    } catch (e) {
                        return { err: e.toString() };
                    }
                }""")
                if res.get("status") == 200 and res.get("data", {}).get("ok"):
                    self.token = res["data"]["token"]
                    self.token_expire = now + 600
                    print(f"[Bridge] Fresh session token acquired: {self.token[:18]}...")
                else:
                    raise RuntimeError(f"Failed to fetch session token: {res}")
            return self.token

    async def chat_events(self, messages, reasoning_effort="medium", web_search=False):
        """Async generator yielding (kind, value). Runs the fetch in the browser loop."""
        if not self.loop or self.failed:
            raise RuntimeError("Browser bridge not running")

        req_id = "req_" + uuid.uuid4().hex[:12]
        queue = asyncio.Queue()
        self.active_queues[req_id] = queue

        payload = build_upstream_payload(
            messages,
            reasoning_effort=reasoning_effort,
            web_search=web_search,
            chat_id="chat_" + req_id,
        )

        token = await self.get_token()

        try:
            await asyncio.wrap_future(
                self._submit(
                    self._page.evaluate("""([reqId, payload, token]) => {
                        window.inceptionSendChat(reqId, payload, token);
                    }""", [req_id, payload, token])
                )
            )
        except Exception as e:
            self.active_queues.pop(req_id, None)
            raise RuntimeError(f"Failed to dispatch chat in browser: {e}")

        try:
            buffer = ""
            while True:
                # read from local queue; the browser loop writes into it
                item = await queue.get()
                kind = item[0]
                if kind == "chunk":
                    buffer += item[1]
                    while "\n\n" in buffer:
                        event_block, buffer = buffer.split("\n\n", 1)
                        for line in event_block.split("\n"):
                            line = line.strip()
                            if line.startswith("data:"):
                                raw_data = line[5:].strip()
                                if raw_data == "[DONE]":
                                    return
                                try:
                                    ev = json.loads(raw_data)
                                    ev_type = ev.get("type")
                                    if ev_type == "text-delta":
                                        yield ("text", ev.get("delta", ""))
                                    elif ev_type == "reasoning-delta":
                                        yield ("reasoning", ev.get("delta", ""))
                                    elif ev_type == "source-url":
                                        url = ev.get("url", "")
                                        title = ev.get("title", "")
                                        if url and url != "__searching__":
                                            yield ("citation", {"url": url, "title": title})
                                except Exception:
                                    pass
                elif kind == "done":
                    status = item[1]
                    err = item[2]
                    if status != 200:
                        raise RuntimeError(f"Inception Chat returned HTTP {status}: {err}")
                    if buffer.strip():
                        for line in buffer.split("\n"):
                            line = line.strip()
                            if line.startswith("data:"):
                                raw_data = line[5:].strip()
                                if raw_data != "[DONE]":
                                    try:
                                        ev = json.loads(raw_data)
                                        ev_type = ev.get("type")
                                        if ev_type == "text-delta":
                                            yield ("text", ev.get("delta", ""))
                                        elif ev_type == "reasoning-delta":
                                            yield ("reasoning", ev.get("delta", ""))
                                        elif ev_type == "source-url":
                                            url = ev.get("url", "")
                                            title = ev.get("title", "")
                                            if url and url != "__searching__":
                                                yield ("citation", {"url": url, "title": title})
                                    except Exception:
                                        pass
                    break
        finally:
            self.active_queues.pop(req_id, None)


# ---------------------------------------------------------------------------
# Engine 2: curl_cffi zero-browser fallback
# ---------------------------------------------------------------------------
class CurlEngine:
    def __init__(self, proxy: Optional[str] = None):
        from curl_cffi import requests as cr
        self.requests = cr
        self.proxy = proxy
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self.token = None
        self.token_expire_time = 0.0
        self.session = cr.Session(impersonate="chrome120", proxies=self.proxies)

    def get_token(self) -> str:
        now = time.time()
        if not self.token or now > self.token_expire_time:
            r = self.session.get(
                "https://chat.inceptionlabs.ai/api/session",
                timeout=15,
                headers={
                    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/120.0.0.0 Safari/537.36"),
                    "Accept": "*/*",
                    "Referer": "https://chat.inceptionlabs.ai/",
                },
            )
            if r.status_code != 200:
                raise RuntimeError(f"Failed to fetch session: HTTP {r.status_code} - {r.text}")
            data = r.json()
            self.token = data["token"]
            self.token_expire_time = now + 600
        return self.token

    async def chat_events(self, messages, reasoning_effort="medium", web_search=False):
        token = self.get_token()
        chat_id = "chat_" + uuid.uuid4().hex[:12]

        payload = build_upstream_payload(
            messages,
            reasoning_effort=reasoning_effort,
            web_search=web_search,
            chat_id=chat_id,
        )

        headers = {
            "Content-Type": "application/json",
            "x-session-token": token,
            "Origin": "https://chat.inceptionlabs.ai",
            "Referer": "https://chat.inceptionlabs.ai/",
        }

        r = self.session.post(
            "https://chat.inceptionlabs.ai/api/chat",
            headers=headers,
            json=payload,
            stream=True,
            timeout=120,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Inception Chat returned HTTP {r.status_code}: {r.text}")

        for line in r.iter_lines():
            if line:
                decoded = line.decode("utf-8", errors="ignore")
                if decoded.startswith("data: "):
                    raw_data = decoded[6:].strip()
                    if raw_data == "[DONE]":
                        break
                    try:
                        ev = json.loads(raw_data)
                        ev_type = ev.get("type")
                        if ev_type == "text-delta":
                            yield ("text", ev.get("delta", ""))
                        elif ev_type == "reasoning-delta":
                            yield ("reasoning", ev.get("delta", ""))
                        elif ev_type == "source-url":
                            url = ev.get("url", "")
                            title = ev.get("title", "")
                            if url and url != "__searching__":
                                yield ("citation", {"url": url, "title": title})
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# Engine selection
# ---------------------------------------------------------------------------
if ENGINE == "browser":
    print("[*] Engine: Playwright headless browser bridge")
    engine = BrowserBridgeEngine(proxy=PROXY_URL, chrome_path=CHROME_PATH)
    engine.start()
elif ENGINE == "curl":
    print("[*] Engine: curl_cffi zero-browser (may fail on datacenter IPs)")
    engine = CurlEngine(proxy=PROXY_URL)
else:
    raise SystemExit(f"Unknown ENGINE={ENGINE}; use browser or curl")


def verify_auth(auth_header: Optional[str] = Header(None)):
    if API_KEY:
        if not auth_header or not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or invalid Bearer token")
        if auth_header.split(" ")[1] != API_KEY:
            raise HTTPException(status_code=403, detail="Invalid API key")


@app.on_event("startup")
async def startup():
    if ENGINE == "browser":
        for _ in range(30):
            if engine._ready.is_set() or engine.failed:
                break
            await asyncio.sleep(1)
        if engine.failed:
            print("[!] Browser engine failed to start; check playwright install")
        else:
            print(f"[*] Browser engine ready: {engine._ready.is_set()}")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "inception-web-api",
        "engine": ENGINE,
        "proxy": PROXY_URL is not None,
        "browser_ready": engine._ready.is_set() if ENGINE == "browser" else True,
    }


# ---------------------------------------------------------------------------
# Tool Emulation Helpers (Function Calling virtualization)
# ---------------------------------------------------------------------------
def parse_tool_definitions(
    tools: Optional[List[Dict[str, Any]]],
    functions: Optional[List[Dict[str, Any]]] = None
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Normalizes OpenAI tools/functions definitions into a unified format."""
    tool_defs = []
    valid_names = []

    if tools:
        for t in tools:
            if not isinstance(t, dict):
                continue
            if t.get("type") == "function" and "function" in t:
                fn = t["function"]
                if isinstance(fn, dict):
                    tool_defs.append(fn)
                    if "name" in fn and isinstance(fn["name"], str):
                        valid_names.append(fn["name"])
            elif "name" in t and isinstance(t["name"], str):
                tool_defs.append(t)
                valid_names.append(t["name"])
    elif functions:
        for fn in functions:
            if isinstance(fn, dict) and "name" in fn and isinstance(fn["name"], str):
                tool_defs.append(fn)
                valid_names.append(fn["name"])

    return tool_defs, valid_names


def extract_balanced_json(s: str, start_index: int = 0) -> Optional[Tuple[str, int, int]]:
    """
    Finds the first balanced {...} or [...] starting at or after start_index.
    Handles quotes and escaping correctly.
    Returns: (json_str, start_pos, end_pos) or None
    """
    first_brace = -1
    open_char = None
    close_char = None

    for i in range(start_index, len(s)):
        if s[i] == '{':
            first_brace = i
            open_char = '{'
            close_char = '}'
            break
        elif s[i] == '[':
            first_brace = i
            open_char = '['
            close_char = ']'
            break

    if first_brace == -1:
        return None

    count = 0
    in_string = False
    escape = False

    for i in range(first_brace, len(s)):
        ch = s[i]
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if not in_string:
            if ch == open_char:
                count += 1
            elif ch == close_char:
                count -= 1
                if count == 0:
                    return s[first_brace:i+1], first_brace, i+1

    return None


def compact_tool_definitions(tool_defs: List[Dict[str, Any]], max_tools: int = 50) -> List[Dict[str, Any]]:
    """
    Compacts tool definitions to keep prompt size manageable:
    - Truncates verbose descriptions (> 100 chars).
    - Compacts nested property definitions (> 50 chars).
    - Caps tool count to prevent prompt explosion in multi-tool agent environments.
    """
    compacted = []
    for td in tool_defs[:max_tools]:
        c = {"name": td.get("name", "")}
        desc = td.get("description", "")
        if desc:
            c["description"] = (desc[:100] + "...") if len(desc) > 100 else desc

        params = td.get("parameters")
        if isinstance(params, dict):
            clean_params = {"type": params.get("type", "object")}
            if "properties" in params and isinstance(params["properties"], dict):
                clean_props = {}
                for k, v in params["properties"].items():
                    if isinstance(v, dict):
                        p_desc = v.get("description", "")
                        clean_props[k] = {"type": v.get("type", "string")}
                        if p_desc:
                            clean_props[k]["description"] = (p_desc[:50] + "...") if len(p_desc) > 50 else p_desc
                        if "enum" in v:
                            clean_props[k]["enum"] = v["enum"]
                    else:
                        clean_props[k] = v
                clean_params["properties"] = clean_props
            if "required" in params:
                clean_params["required"] = params["required"]
            c["parameters"] = clean_params
        compacted.append(c)
    return compacted


def build_tool_system_prompt(tool_defs: List[Dict[str, Any]], tool_choice: Any = "auto") -> str:
    """Builds a compact, unambiguous system prompt instructing the model how to call tools."""
    compacted_defs = compact_tool_definitions(tool_defs)
    tools_json = json.dumps(compacted_defs, ensure_ascii=False, separators=(',', ':'))
    prompt = [
        "## Tools",
        f"You have access to the following functions:\n```json\n{tools_json}\n```",
        "",
        "## Tool Call Protocol",
        "If you decide to invoke any function, you MUST reply with a ```json code block containing:",
        "```json",
        '{"name": "function_name", "arguments": {"param": "value"}}',
        "```",
        "CRITICAL RULES:",
        "1. To invoke a tool, output the ```json ... ``` code block. Do NOT merely say 'Calling tool...' as plain text without emitting the executable call.",
        '2. The "arguments" MUST be a valid JSON dictionary conforming to the parameters schema.',
        "3. If no function needs to be called, answer normally with plain natural language. Do NOT output a tool call JSON.",
    ]

    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        target = tool_choice.get("function", {}).get("name")
        if target:
            prompt.append(f"4. You are REQUIRED to call the function '{target}'.")
    elif tool_choice == "required":
        prompt.append("4. You are REQUIRED to call at least one of the available functions.")

    return "\n".join(prompt)


def prepare_messages_for_tools(
    messages: List[Dict[str, Any]],
    tool_prompt: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Appends tool_prompt to messages as a system message if provided."""
    copied = list(messages)
    if tool_prompt:
        copied.append({"role": "system", "content": tool_prompt})
    return copied


def extract_tool_calls(
    text: str,
    valid_tool_names: List[str]
) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """Extracts tool calls from model output with multi-pattern tolerance:
    1. Natural calling syntax (e.g. Calling tool `todo` with arguments: {...})
    2. Action / Tool Call syntax (e.g. Tool Call: todo Arguments: {...})
    3. Markdown ```json ... ``` code blocks
    4. Raw balanced JSON blocks containing {"name": "..."}
    Returns: (tool_calls_list or None, remaining_text)
    """
    if not text or not valid_tool_names:
        return None, text

    valid_names_lower = {name.lower(): name for name in valid_tool_names}

    # -----------------------------------------------------------------------
    # Pattern 1: Natural Calling syntax (e.g. Calling tool `todo` with arguments: {...})
    # -----------------------------------------------------------------------
    calling_patterns = [
        re.compile(r"Calling\s+tool\s+[`'\"]?([a-zA-Z0-9_\-]+)[`'\"]?\s+with\s+arguments:\s*", re.IGNORECASE),
        re.compile(r"(?:Tool\s+Call|Action):\s*[`'\"]?([a-zA-Z0-9_\-]+)[`'\"]?\s*(?:Arguments|Action\s+Input):\s*", re.IGNORECASE),
        re.compile(r"Calling\s+(?:tool\s+)?[`'\"]?([a-zA-Z0-9_\-]+)[`'\"]?\s*\(?\s*arguments:\s*", re.IGNORECASE),
    ]

    for pat in calling_patterns:
        m = pat.search(text)
        if m:
            raw_tool_name = m.group(1).strip()
            matched_name = valid_names_lower.get(raw_tool_name.lower())
            if matched_name:
                balanced = extract_balanced_json(text, m.end())
                if balanced:
                    args_str, b_start, b_end = balanced
                    try:
                        parsed_args = json.loads(args_str)
                        final_args = json.dumps(parsed_args, ensure_ascii=False) if isinstance(parsed_args, dict) else args_str
                    except Exception:
                        final_args = args_str

                    call_id = "call_" + uuid.uuid4().hex[:16]
                    tool_calls = [{
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": matched_name,
                            "arguments": final_args,
                        }
                    }]
                    # Keep conversational prefix before "Calling tool..." as remaining text
                    remaining = (text[:m.start()] + text[b_end:]).strip()
                    return tool_calls, remaining

    # -----------------------------------------------------------------------
    # Pattern 2: Markdown code blocks (```json ... ``` or ``` ... ```)
    # -----------------------------------------------------------------------
    code_block_pattern = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)
    for match in code_block_pattern.finditer(text):
        snippet = match.group(1).strip()
        try:
            parsed = json.loads(snippet)
        except Exception:
            continue

        items = parsed if isinstance(parsed, list) else [parsed]
        tool_calls = []

        for item in items:
            if isinstance(item, dict) and "name" in item:
                raw_name = str(item["name"]).strip()
                matched_name = valid_names_lower.get(raw_name.lower())
                if matched_name:
                    args = item.get("arguments", {})
                    if isinstance(args, (dict, list)):
                        args_str = json.dumps(args, ensure_ascii=False)
                    elif isinstance(args, str):
                        try:
                            json.loads(args)
                            args_str = args
                        except Exception:
                            args_str = json.dumps(args, ensure_ascii=False)
                    else:
                        args_str = json.dumps(str(args), ensure_ascii=False)

                    call_id = "call_" + uuid.uuid4().hex[:16]
                    tool_calls.append({
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": matched_name,
                            "arguments": args_str,
                        }
                    })

        if tool_calls:
            start, end = match.span()
            remaining = (text[:start] + text[end:]).strip()
            return tool_calls, remaining

    # -----------------------------------------------------------------------
    # Pattern 3: Raw balanced JSON blocks containing {"name": ...}
    # -----------------------------------------------------------------------
    pos = 0
    while pos < len(text):
        balanced = extract_balanced_json(text, pos)
        if not balanced:
            break
        json_str, b_start, b_end = balanced
        pos = b_end
        try:
            parsed = json.loads(json_str)
        except Exception:
            continue

        items = parsed if isinstance(parsed, list) else [parsed]
        tool_calls = []

        for item in items:
            if isinstance(item, dict) and "name" in item:
                raw_name = str(item["name"]).strip()
                matched_name = valid_names_lower.get(raw_name.lower())
                if matched_name:
                    args = item.get("arguments", {})
                    if isinstance(args, (dict, list)):
                        args_str = json.dumps(args, ensure_ascii=False)
                    elif isinstance(args, str):
                        try:
                            json.loads(args)
                            args_str = args
                        except Exception:
                            args_str = json.dumps(args, ensure_ascii=False)
                    else:
                        args_str = json.dumps(str(args), ensure_ascii=False)

                    call_id = "call_" + uuid.uuid4().hex[:16]
                    tool_calls.append({
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": matched_name,
                            "arguments": args_str,
                        }
                    })

        if tool_calls:
            remaining = (text[:b_start] + text[b_end:]).strip()
            return tool_calls, remaining

    return None, text


@app.get("/v1/models")
async def list_models(request: Request):
    auth = request.headers.get("authorization")
    verify_auth(auth)
    return {
        "object": "list",
        "data": [
            {"id": "mercury-3", "object": "model", "created": current_timestamp(),
             "owned_by": "inceptionlabs", "description": "Mercury 3 Diffusion LLM (Default: medium reasoning)"},
            {"id": "mercury-3-instant", "object": "model", "created": current_timestamp(),
             "owned_by": "inceptionlabs", "description": "Mercury 3 with instant response (zero reasoning overhead)"},
            {"id": "mercury-3-high", "object": "model", "created": current_timestamp(),
             "owned_by": "inceptionlabs", "description": "Mercury 3 with high reasoning effort (deep thinking)"},
            {"id": "mercury-3-search", "object": "model", "created": current_timestamp(),
             "owned_by": "inceptionlabs", "description": "Mercury 3 with live web search enabled"},
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    auth = request.headers.get("authorization")
    verify_auth(auth)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    messages = body.get("messages", [])
    model = body.get("model", DEFAULT_MODEL)
    stream = body.get("stream", False)

    tools = body.get("tools")
    functions = body.get("functions")
    tool_choice = body.get("tool_choice", "auto")

    # Tool Emulation detection
    tool_defs, valid_tool_names = parse_tool_definitions(tools, functions)
    has_tools = bool(tool_defs) and tool_choice != "none"
    tool_prompt = build_tool_system_prompt(tool_defs, tool_choice) if has_tools else None
    effective_messages = prepare_messages_for_tools(messages, tool_prompt=tool_prompt)

    requested_effort = body.get("reasoning_effort") or body.get("reasoning")
    if not requested_effort:
        if "instant" in model:
            requested_effort = "instant"
        elif "high" in model:
            requested_effort = "high"
        elif "low" in model:
            requested_effort = "low"
    reasoning_effort = normalize_reasoning_effort(requested_effort)
    web_search = bool(body.get("web_search") or ("search" in model))

    if not messages:
        raise HTTPException(status_code=400, detail="messages array is required")

    req_id = "chatcmpl-" + uuid.uuid4().hex[:16]
    created = current_timestamp()

    # Generator for raw real-time streaming when no tools are requested
    async def real_time_stream_generator():
        try:
            async for kind, val in engine.chat_events(
                effective_messages,
                reasoning_effort=reasoning_effort,
                web_search=web_search,
            ):
                if kind == "text":
                    chunk = {
                        "id": req_id, "object": "chat.completion.chunk",
                        "created": created, "model": model,
                        "choices": [{"index": 0, "delta": {"content": val}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                elif kind == "reasoning":
                    chunk = {
                        "id": req_id, "object": "chat.completion.chunk",
                        "created": created, "model": model,
                        "choices": [{"index": 0, "delta": {"reasoning_content": val}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                elif kind == "citation":
                    chunk = {
                        "id": req_id, "object": "chat.completion.chunk",
                        "created": created, "model": model,
                        "choices": [{"index": 0, "delta": {"citation": val}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        except Exception as e:
            err_chunk = {"error": str(e)}
            yield f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n"
            return

        end_chunk = {
            "id": req_id, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(end_chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    # Generator for tool-emulated streaming
    async def tool_stream_generator():
        full_text = []
        full_reasoning = []
        citations = []
        try:
            async for kind, val in engine.chat_events(
                effective_messages,
                reasoning_effort=reasoning_effort,
                web_search=web_search,
            ):
                if kind == "text":
                    full_text.append(val)
                elif kind == "reasoning":
                    full_reasoning.append(val)
                elif kind == "citation":
                    citations.append(val)
        except Exception as e:
            err_chunk = {"error": str(e)}
            yield f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n"
            return

        # Check for tool call in output
        merged_text = "".join(full_text)
        tool_calls, remaining_content = extract_tool_calls(merged_text, valid_tool_names)

        # 1. Output reasoning content first if present
        if full_reasoning:
            merged_reasoning = "".join(full_reasoning)
            chunk = {
                "id": req_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": merged_reasoning}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        else:
            role_chunk = {
                "id": req_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(role_chunk, ensure_ascii=False)}\n\n"

        # 2. Output citations if present
        for cit in citations:
            chunk = {
                "id": req_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"citation": cit}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

        if tool_calls:
            if remaining_content:
                chunk = {
                    "id": req_id, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {"content": remaining_content}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

            delta_tool_calls = [
                {
                    "index": idx,
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    }
                }
                for idx, tc in enumerate(tool_calls)
            ]
            tc_chunk = {
                "id": req_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"tool_calls": delta_tool_calls}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(tc_chunk, ensure_ascii=False)}\n\n"

            end_chunk = {
                "id": req_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            }
            yield f"data: {json.dumps(end_chunk, ensure_ascii=False)}\n\n"
        else:
            # Normal plain text output - stream in chunks
            step = 30
            for i in range(0, len(merged_text), step):
                sub_text = merged_text[i:i + step]
                chunk = {
                    "id": req_id, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {"content": sub_text}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

            end_chunk = {
                "id": req_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(end_chunk, ensure_ascii=False)}\n\n"

        yield "data: [DONE]\n\n"

    if stream:
        if has_tools:
            return StreamingResponse(tool_stream_generator(), media_type="text/event-stream")
        else:
            first_chunk = {
                "id": req_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
            }
            async def combined_generator():
                yield f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n"
                async for item in real_time_stream_generator():
                    yield item
            return StreamingResponse(combined_generator(), media_type="text/event-stream")

    else:
        full_text = []
        full_reasoning = []
        citations = []
        try:
            async for kind, val in engine.chat_events(
                effective_messages,
                reasoning_effort=reasoning_effort,
                web_search=web_search,
            ):
                if kind == "text":
                    full_text.append(val)
                elif kind == "reasoning":
                    full_reasoning.append(val)
                elif kind == "citation":
                    citations.append(val)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Upstream chat error: {str(e)}")

        merged_text = "".join(full_text)
        tool_calls = None
        finish_reason = "stop"

        if has_tools:
            tool_calls, remaining_content = extract_tool_calls(merged_text, valid_tool_names)
            if tool_calls:
                finish_reason = "tool_calls"
                msg_payload = {
                    "role": "assistant",
                    "content": remaining_content or None,
                    "tool_calls": tool_calls,
                }
            else:
                msg_payload = {"role": "assistant", "content": merged_text}
        else:
            msg_payload = {"role": "assistant", "content": merged_text}

        if full_reasoning:
            msg_payload["reasoning_content"] = "".join(full_reasoning)
        if citations:
            msg_payload["citations"] = citations

        return {
            "id": req_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": msg_payload,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": len(str(messages)),
                "completion_tokens": len(merged_text),
                "total_tokens": len(str(messages)) + len(merged_text),
            },
        }


if __name__ == "__main__":
    print(f"[*] Starting Inception Web-to-API on {HOST}:{PORT}")
    print(f"[*] Engine: {ENGINE}")
    print(f"[*] Proxy: {PROXY_URL or 'Direct Connection'}")
    print(f"[*] API Key Protection: {'Enabled' if API_KEY else 'Disabled (Open)'}")
    uvicorn.run(app, host=HOST, port=PORT)

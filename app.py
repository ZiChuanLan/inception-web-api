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
        except Exception:
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
                        window.py_on_done(reqId, r.status, err);
                        return;
                    }
                    const reader = r.body.getReader();
                    const decoder = new TextDecoder();
                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;
                        window.py_on_chunk(reqId, decoder.decode(value, { stream: true }));
                    }
                    window.py_on_done(reqId, 200, '');
                }).catch(e => {
                    window.py_on_done(reqId, 500, e.toString());
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

        formatted_messages = []
        for i, m in enumerate(messages):
            mid = f"msg_{i}_{uuid.uuid4().hex[:8]}"
            role = m.get("role", "user")
            content = m.get("content", "")
            if isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        parts.append({"type": "text", "text": p.get("text", "")})
            else:
                parts = [{"type": "text", "text": str(content)}]
            formatted_messages.append({"id": mid, "role": role, "parts": parts})

        payload = {
            "id": "chat_" + req_id,
            "messageId": "msg_" + req_id,
            "trigger": "submit-message",
            "messages": formatted_messages,
            "reasoningEffort": reasoning_effort,
            "webSearchEnabled": web_search,
            "voiceMode": False,
            "timezone": "Asia/Shanghai",
            "language": "zh-CN",
        }

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
                            if line.startswith("data: "):
                                raw_data = line[6:].strip()
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

    def chat_events(self, messages, reasoning_effort="medium", web_search=False):
        token = self.get_token()
        chat_id = "chat_" + uuid.uuid4().hex[:12]
        msg_id = "msg_" + uuid.uuid4().hex[:12]

        formatted_messages = []
        for i, m in enumerate(messages):
            mid = f"msg_{i}_{uuid.uuid4().hex[:8]}"
            role = m.get("role", "user")
            content = m.get("content", "")
            if isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        parts.append({"type": "text", "text": p.get("text", "")})
            else:
                parts = [{"type": "text", "text": str(content)}]
            formatted_messages.append({"id": mid, "role": role, "parts": parts})

        payload = {
            "id": chat_id,
            "messageId": msg_id,
            "trigger": "submit-message",
            "messages": formatted_messages,
            "reasoningEffort": reasoning_effort,
            "webSearchEnabled": web_search,
            "voiceMode": False,
            "timezone": "Asia/Shanghai",
            "language": "zh-CN",
        }

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

    async def event_generator():
        try:
            async for kind, val in engine.chat_events(
                messages,
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

    if stream:
        first_chunk = {
            "id": req_id, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
        }
        async def combined_generator():
            yield f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n"
            async for item in event_generator():
                yield item
        return StreamingResponse(combined_generator(), media_type="text/event-stream")

    else:
        full_text = []
        full_reasoning = []
        citations = []
        try:
            async for kind, val in engine.chat_events(
                messages,
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

        msg_payload = {"role": "assistant", "content": "".join(full_text)}
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
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": len(str(messages)),
                "completion_tokens": len("".join(full_text)),
                "total_tokens": len(str(messages)) + len("".join(full_text)),
            },
        }


if __name__ == "__main__":
    print(f"[*] Starting Inception Web-to-API on {HOST}:{PORT}")
    print(f"[*] Engine: {ENGINE}")
    print(f"[*] Proxy: {PROXY_URL or 'Direct Connection'}")
    print(f"[*] API Key Protection: {'Enabled' if API_KEY else 'Disabled (Open)'}")
    uvicorn.run(app, host=HOST, port=PORT)

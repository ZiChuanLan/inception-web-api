"""
Inception Mercury Web-to-API Service (Headless / Zero-Browser Edition)
Exposes an OpenAI-compatible /v1/chat/completions API.
Features:
- Pure Python + curl_cffi TLS impersonation (ZERO browser, ZERO Turnstile)
- Full 4-level reasoning_effort support: instant, low, medium, high
- Full OpenAI / DeepSeek R1 reasoning_content stream & non-stream support
- Live web search citations support
- Auto-refreshing session pool
"""

import os
import sys
import time
import uuid
import json
import asyncio
from typing import List, Dict, Any, Optional, Tuple
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import StreamingResponse, JSONResponse
from curl_cffi import requests
import uvicorn

try:
    PORT = int(os.getenv("PORT", "8011"))
except ValueError:
    PORT = 8011

HOST = os.getenv("HOST", "0.0.0.0")
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None
API_KEY = os.getenv("API_KEY", "").strip() or None
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "mercury-3")

app = FastAPI(
    title="Inception Mercury Web-to-API",
    description="Zero-browser OpenAI-compatible API bridge with full reasoning & web search for Inception Mercury 3",
    version="1.1.0"
)

def current_timestamp() -> int:
    try:
        return int(time.time())
    except (ValueError, TypeError):
        return 0

# Supported reasoning levels from frontend reversal: ['instant', 'low', 'medium', 'high']
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

class InceptionSessionPool:
    def __init__(self, proxy: Optional[str] = PROXY_URL):
        self.proxy = proxy
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self.token: Optional[str] = None
        self.token_expire_time: float = 0
        self.session = requests.Session(impersonate="chrome120", proxies=self.proxies)

    def get_token(self) -> str:
        now = time.time()
        # Session token expires every ~13 minutes on server; refresh every 10 minutes
        if not self.token or now > self.token_expire_time:
            try:
                r = self.session.get(
                    "https://chat.inceptionlabs.ai/api/session",
                    timeout=15,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "*/*",
                        "Referer": "https://chat.inceptionlabs.ai/"
                    }
                )
                if r.status_code != 200:
                    raise RuntimeError(f"Failed to fetch session: HTTP {r.status_code} - {r.text}")
                data = r.json()
                self.token = data["token"]
                self.token_expire_time = now + 600
            except Exception as e:
                self.session = requests.Session(impersonate="chrome120", proxies=self.proxies)
                raise RuntimeError(f"Failed to acquire session token: {e}")
        return self.token

    def chat_events(
        self,
        messages: List[Dict[str, Any]],
        reasoning_effort: str = "medium",
        web_search: bool = False
    ):
        """
        Yields tuple of (event_type, data_dict):
        ('reasoning', delta_text)
        ('text', delta_text)
        ('citation', {url, title})
        """
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

            formatted_messages.append({
                "id": mid,
                "role": role,
                "parts": parts
            })

        payload = {
            "id": chat_id,
            "messageId": msg_id,
            "trigger": "submit-message",
            "messages": formatted_messages,
            "reasoningEffort": reasoning_effort,
            "webSearchEnabled": web_search,
            "voiceMode": False,
            "timezone": "Asia/Shanghai",
            "language": "zh-CN"
        }

        headers = {
            "Content-Type": "application/json",
            "x-session-token": token,
            "Origin": "https://chat.inceptionlabs.ai",
            "Referer": "https://chat.inceptionlabs.ai/"
        }

        r = self.session.post(
            "https://chat.inceptionlabs.ai/api/chat",
            headers=headers,
            json=payload,
            stream=True,
            timeout=120
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

pool = InceptionSessionPool()

def verify_auth(auth_header: Optional[str] = Header(None)):
    if API_KEY:
        if not auth_header or not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or invalid Bearer token")
        token = auth_header.split(" ")[1]
        if token != API_KEY:
            raise HTTPException(status_code=403, detail="Invalid API key")

@app.get("/health")
async def health():
    return {"status": "ok", "service": "inception-web-api", "proxy": PROXY_URL is not None}

@app.get("/v1/models")
async def list_models(request: Request):
    auth = request.headers.get("authorization")
    verify_auth(auth)
    return {
        "object": "list",
        "data": [
            {
                "id": "mercury-3",
                "object": "model",
                "created": current_timestamp(),
                "owned_by": "inceptionlabs",
                "description": "Mercury 3 Diffusion LLM (Default: medium reasoning)"
            },
            {
                "id": "mercury-3-instant",
                "object": "model",
                "created": current_timestamp(),
                "owned_by": "inceptionlabs",
                "description": "Mercury 3 with instant response (zero reasoning overhead)"
            },
            {
                "id": "mercury-3-high",
                "object": "model",
                "created": current_timestamp(),
                "owned_by": "inceptionlabs",
                "description": "Mercury 3 with high reasoning effort (deep thinking)"
            },
            {
                "id": "mercury-3-search",
                "object": "model",
                "created": current_timestamp(),
                "owned_by": "inceptionlabs",
                "description": "Mercury 3 with live web search enabled"
            }
        ]
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

    # Determine reasoning effort from body or model suffix
    requested_effort = body.get("reasoning_effort") or body.get("reasoning")
    if not requested_effort:
        if "instant" in model:
            requested_effort = "instant"
        elif "high" in model:
            requested_effort = "high"
        elif "low" in model:
            requested_effort = "low"
    reasoning_effort = normalize_reasoning_effort(requested_effort)

    # Determine web search from body or model name
    web_search = bool(body.get("web_search") or ("search" in model))

    if not messages:
        raise HTTPException(status_code=400, detail="messages array is required")

    req_id = "chatcmpl-" + uuid.uuid4().hex[:16]
    created = current_timestamp()

    if stream:
        async def event_generator():
            loop = asyncio.get_event_loop()
            queue = asyncio.Queue()

            def produce():
                try:
                    for kind, val in pool.chat_events(
                        messages,
                        reasoning_effort=reasoning_effort,
                        web_search=web_search
                    ):
                        loop.call_soon_threadsafe(queue.put_nowait, (kind, val))
                    loop.call_soon_threadsafe(queue.put_nowait, ("done", None))
                except Exception as e:
                    loop.call_soon_threadsafe(queue.put_nowait, ("error", str(e)))

            asyncio.create_task(asyncio.to_thread(produce))

            first_chunk = {
                "id": req_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": ""},
                    "finish_reason": None
                }]
            }
            yield f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n"

            while True:
                kind, val = await queue.get()
                if kind == "text":
                    chunk = {
                        "id": req_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {"content": val},
                            "finish_reason": None
                        }]
                    }
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                elif kind == "reasoning":
                    chunk = {
                        "id": req_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {"reasoning_content": val},
                            "finish_reason": None
                        }]
                    }
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                elif kind == "citation":
                    chunk = {
                        "id": req_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {"citation": val},
                            "finish_reason": None
                        }]
                    }
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                elif kind == "done":
                    end_chunk = {
                        "id": req_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop"
                        }]
                    }
                    yield f"data: {json.dumps(end_chunk, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    break
                elif kind == "error":
                    err_chunk = {"error": val}
                    yield f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n"
                    break

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    else:
        def collect():
            full_text = []
            full_reasoning = []
            citations = []
            for kind, val in pool.chat_events(
                messages,
                reasoning_effort=reasoning_effort,
                web_search=web_search
            ):
                if kind == "text":
                    full_text.append(val)
                elif kind == "reasoning":
                    full_reasoning.append(val)
                elif kind == "citation":
                    citations.append(val)
            return "".join(full_text), "".join(full_reasoning), citations

        try:
            response_content, response_reasoning, response_citations = await asyncio.to_thread(collect)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Upstream chat error: {str(e)}")

        msg_payload = {
            "role": "assistant",
            "content": response_content
        }
        if response_reasoning:
            msg_payload["reasoning_content"] = response_reasoning
        if response_citations:
            msg_payload["citations"] = response_citations

        return {
            "id": req_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": msg_payload,
                    "finish_reason": "stop"
                }
            ],
            "usage": {
                "prompt_tokens": len(str(messages)),
                "completion_tokens": len(response_content),
                "total_tokens": len(str(messages)) + len(response_content)
            }
        }

if __name__ == "__main__":
    print(f"[*] Starting Inception Web-to-API on {HOST}:{PORT}")
    print(f"[*] Proxy: {PROXY_URL or 'Direct Connection'}")
    print(f"[*] API Key Protection: {'Enabled' if API_KEY else 'Disabled (Open)'}")
    uvicorn.run(app, host=HOST, port=PORT)

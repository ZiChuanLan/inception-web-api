import unittest
import asyncio
import time
import json
from unittest.mock import patch, AsyncMock
from httpx import AsyncClient, ASGITransport
from app import app, CurlEngine, POOL_SIZE

class TestConcurrencyAndPooling(unittest.IsolatedAsyncioTestCase):
    async def test_curl_engine_token_pool_round_robin(self):
        """Verifies that the Token Pool acquires multiple tokens and distributes them via round-robin."""
        engine = CurlEngine(pool_size=3)
        mock_tokens = [
            ("tok_aaa_111", time.time() + 600),
            ("tok_bbb_222", time.time() + 600),
            ("tok_ccc_333", time.time() + 600),
        ]
        call_idx = 0

        async def fake_fetch():
            nonlocal call_idx
            res = mock_tokens[call_idx % len(mock_tokens)]
            call_idx += 1
            return res

        engine._fetch_single_token = fake_fetch

        # Fetch 6 times, should cycle through tokens
        collected = []
        for _ in range(6):
            tok = await engine.get_token()
            collected.append(tok)

        self.assertEqual(len(engine.token_pool), 3)
        # Should cycle in round-robin order
        self.assertEqual(collected[0], collected[3])
        self.assertEqual(collected[1], collected[4])
        self.assertEqual(collected[2], collected[5])

    async def test_high_concurrency_non_blocking_streaming(self):
        """
        Simulates 10 concurrent requests executing simultaneously without event loop blocking.
        Each request has a simulated upstream network latency of 50ms.
        If blocking occurred, 10 * 50ms = 500ms+. With async non-blocking, total time ~ 50-100ms.
        """
        async def mock_events(messages, **kwargs):
            # Simulate upstream streaming latency
            await asyncio.sleep(0.05)
            yield ("text", f"Echo from req")
            await asyncio.sleep(0.05)
            yield ("text", " done")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            with patch("app.engine.chat_events", side_effect=mock_events):
                start_time = time.time()

                async def single_client(idx: int):
                    resp = await ac.post("/v1/chat/completions", json={
                        "model": "mercury-3",
                        "messages": [{"role": "user", "content": f"Hello {idx}"}],
                        "stream": True
                    })
                    self.assertEqual(resp.status_code, 200)
                    chunks = []
                    async for line in resp.aiter_lines():
                        if line.startswith("data: ") and line != "data: [DONE]":
                            chunks.append(json.loads(line[6:]))
                    return chunks

                # Launch 10 requests concurrently
                results = await asyncio.gather(*(single_client(i) for i in range(10)))

                elapsed = time.time() - start_time
                self.assertEqual(len(results), 10)
                for res in results:
                    self.assertTrue(len(res) > 0)

                # Total elapsed time should be well below sequential 1000ms
                print(f"[*] 10 Concurrent streaming requests finished in: {elapsed:.3f}s")
                self.assertLess(elapsed, 0.5)

    async def test_health_endpoint_reports_pool_status(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/health")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertIn("pool_size", data)
            self.assertIn("active_tokens", data)
            self.assertEqual(data["pool_size"], POOL_SIZE)


if __name__ == "__main__":
    unittest.main()

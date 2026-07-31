"""Dependency-light self-test for the Telegram mock."""

from __future__ import annotations

import asyncio

import httpx

from loadtest.telegram_mock import app


TOKEN = "999999:LOAD_TEST_TOKEN"


async def main() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://telegram-mock"
    ) as client:
        assert (await client.get("/__mock__/health")).status_code == 200
        await client.post(
            "/__mock__/config",
            json={
                "enforce_limits": False,
                "base_latency_ms": 0,
                "jitter_ms": 0,
            },
        )
        await client.post("/__mock__/reset")
        me = await client.post(f"/bot{TOKEN}/getMe", data={})
        assert me.status_code == 200 and me.json()["result"]["is_bot"]
        sent = await client.post(
            f"/bot{TOKEN}/sendMessage",
            data={"chat_id": "1001", "text": "test"},
        )
        assert sent.status_code == 200
        member = await client.post(
            f"/bot{TOKEN}/getChatMember",
            data={"chat_id": "@loadtest", "user_id": "1001"},
        )
        assert member.json()["result"]["status"] == "member"

        await client.post(
            "/__mock__/config",
            json={
                "enforce_limits": True,
                "global_messages_per_second": 0.01,
                "global_burst": 1,
                "private_messages_per_second": 100,
                "private_burst": 100,
            },
        )
        await client.post("/__mock__/reset")
        first = await client.post(
            f"/bot{TOKEN}/sendMessage",
            data={"chat_id": "1001", "text": "first"},
        )
        second = await client.post(
            f"/bot{TOKEN}/sendMessage",
            data={"chat_id": "1002", "text": "second"},
        )
        assert first.status_code == 200 and second.status_code == 429
        stats = (await client.get("/__mock__/stats")).json()
        assert stats["requests"]["rate_limited"] == 1

        await client.post(
            "/__mock__/config",
            json={"enforce_limits": False, "base_latency_ms": 0, "jitter_ms": 0},
        )
        await client.post("/__mock__/reset")
        pushed = await client.post(
            "/__mock__/push",
            json={
                "message": {
                    "message_id": 1,
                    "date": 1,
                    "chat": {"id": 42, "type": "private"},
                    "from": {"id": 42, "is_bot": False, "first_name": "A"},
                    "text": "👎",
                }
            },
        )
        assert pushed.status_code == 200 and pushed.json()["count"] == 1
        polled = await client.post(
            f"/bot{TOKEN}/getUpdates",
            data={"timeout": "0", "limit": "10"},
        )
        body = polled.json()["result"]
        assert len(body) == 1 and "update_id" in body[0]
        confirmed = await client.post(
            f"/bot{TOKEN}/getUpdates",
            data={"offset": str(body[0]["update_id"] + 1), "timeout": "0"},
        )
        assert confirmed.json()["result"] == []
    print("telegram_mock self-test: OK")


if __name__ == "__main__":
    asyncio.run(main())

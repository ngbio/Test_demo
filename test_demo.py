"""Run against a live FastAPI + Redis instance; leaves 10 orders for screenshots."""
import argparse
import asyncio
import json
from collections import Counter

import httpx


async def reset(client):
    response = await client.post("/demo/reset")
    response.raise_for_status()
    assert response.json()["redisStock"] == 10


async def get_token(client, user):
    response = await client.post("/flash-sale/token", json={
        "userId": user, "eventId": 1, "productId": 1,
    })
    response.raise_for_status()
    return response.json()["purchaseToken"]


async def buy(client, user, token):
    return await client.post("/flash-sale/purchase", json={
        "userId": user, "eventId": 1, "productId": 1, "purchaseToken": token,
    })


async def run_demo(base_url):
    async with httpx.AsyncClient(base_url=base_url, timeout=60,
                                limits=httpx.Limits(max_connections=120)) as client:
        await reset(client)
        print("\n=== RATE LIMIT TEST ===")
        token = await get_token(client, 1)
        for i in range(1, 11):
            response = await buy(client, 1, token)
            code = response.json()["code"]
            print(f"Request {i}: {response.status_code} {code}")
            assert code == ("SUCCESS" if i == 1 else
                            "TOKEN_ALREADY_USED" if i <= 3 else "RATE_LIMIT_EXCEEDED")
            if i > 3:
                assert response.status_code == 429

        await reset(client)
        print("\n=== TOKEN REPLAY TEST ===")
        token = await get_token(client, 2)
        for label, expected in [("First request", "SUCCESS"),
                                ("Second request", "TOKEN_ALREADY_USED")]:
            response = await buy(client, 2, token)
            print(f"{label}: {response.status_code} {response.json()['code']}")
            assert response.json()["code"] == expected

        await reset(client)
        print("\n=== ONE USER ONE PRODUCT TEST ===")
        for i in range(1, 4):
            token = await get_token(client, 3)
            response = await buy(client, 3, token)
            print(f"Token {i}: {response.status_code} {response.json()['code']}")
            assert response.json()["code"] == ("SUCCESS" if i == 1 else "ALREADY_PURCHASED")

        await reset(client)
        print("\n=== CONCURRENT STOCK TEST ===")
        users = list(range(1000, 1100))
        tokens = await asyncio.gather(*(get_token(client, u) for u in users))
        # Prepare every token before releasing the purchase requests together.
        start = asyncio.Event()

        async def contender(user, token):
            await start.wait()
            return await buy(client, user, token)

        tasks = [asyncio.create_task(contender(u, t)) for u, t in zip(users, tokens)]
        start.set()
        responses = await asyncio.gather(*tasks)
        counts = Counter(r.json().get("code", "UNKNOWN") for r in responses)
        status_response = await client.get("/flash-sale/status")
        status_response.raise_for_status()
        status = status_response.json()
        valid = (counts == {"SUCCESS": 10, "SOLD_OUT": 90}
                 and status == {"redisStock": 0, "databaseStock": 0, "orderCount": 10})
        print("Initial stock: 10\nTotal requests: 100")
        print(f"Success: {counts['SUCCESS']}\nFailed/Sold out: {counts['SOLD_OUT']}")
        print(f"Final Redis stock: {status['redisStock']}")
        print(f"Final Database stock: {status['databaseStock']}")
        print(f"Orders created: {status['orderCount']}")
        print(f"Overselling: {'NO' if valid else 'CHECK FAILED'}")
        print("Response counts:", dict(counts))
        print("\nGET /flash-sale/status\n" + json.dumps(status, indent=2))
        assert valid, "Stock/order invariant or response counts failed"
        assert all(r.status_code == (200 if r.json()["code"] == "SUCCESS" else 409)
                   for r in responses)
        print("\nALL 4 DEMO TESTS PASSED")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    asyncio.run(run_demo(args.base_url))

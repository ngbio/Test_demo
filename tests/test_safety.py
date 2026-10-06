"""Regression tests use an isolated SQLite file + fakeredis running real Lua.

These validate logic, NOT a substitute for test_demo.py against real Redis.
"""
import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fakeredis.aioredis import FakeRedis

from app.database import Database, PurchaseRejected
from app.main import LUA, PREFIX, STOCK_KEY, create_app


async def with_app(tmp_path, scenario):
    redis = FakeRedis(decode_responses=True)
    app = create_app(redis, tmp_path / "test.db")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://test") as client:
            assert (await client.post("/demo/reset")).status_code == 200
            await scenario(client, redis, app)


async def token(client, user=1):
    response = await client.post("/flash-sale/token", json={"userId": user, "eventId": 1, "productId": 1})
    assert response.status_code == 200
    return response.json()["purchaseToken"]


async def buy(client, nonce, user=1, event=1, product=1):
    return await client.post("/flash-sale/purchase", json={
        "userId": user, "eventId": event, "productId": product, "purchaseToken": nonce,
    })


def test_concurrent_100_users(tmp_path):
    async def scenario(client, redis, app):
        tokens = await asyncio.gather(*(token(client, i) for i in range(1, 101)))
        responses = await asyncio.gather(*(buy(client, t, i) for i, t in enumerate(tokens, 1)))
        codes = [r.json()["code"] for r in responses]
        assert codes.count("SUCCESS") == 10
        assert codes.count("SOLD_OUT") == 90
        assert (await client.get("/flash-sale/status")).json() == {
            "redisStock": 0, "databaseStock": 0, "orderCount": 10,
        }
    asyncio.run(with_app(tmp_path, scenario))


def test_replay_duplicate_and_rate_limit(tmp_path):
    async def scenario(client, redis, app):
        nonce = await token(client)
        assert (await buy(client, nonce)).json()["code"] == "SUCCESS"
        assert (await buy(client, nonce)).json()["code"] == "TOKEN_ALREADY_USED"
        assert (await buy(client, await token(client))).json()["code"] == "ALREADY_PURCHASED"
        limited = await buy(client, nonce)
        assert limited.status_code == 429
        assert limited.json() == {"code": "RATE_LIMIT_EXCEEDED"}
        assert 1 <= int(limited.headers["Retry-After"]) <= 5
        # Expire the sliding-window entries without sleeping in the test suite.
        await redis.zadd(PREFIX + "rate:1", {"old": 0})
        members = await redis.zrange(PREFIX + "rate:1", 0, -1)
        for member in members:
            await redis.zadd(PREFIX + "rate:1", {member: 0})
        assert (await buy(client, nonce)).json()["code"] == "TOKEN_ALREADY_USED"
    asyncio.run(with_app(tmp_path, scenario))


def test_invalid_expired_and_wrong_owner(tmp_path):
    async def scenario(client, redis, app):
        nonce = await token(client)
        assert (await buy(client, nonce, 2)).status_code == 403
        assert (await buy(client, "x" * 43, 3)).json()["code"] == "TOKEN_NOT_FOUND"
        assert (await buy(client, nonce, event=2)).status_code == 404
        # Server-held event/product bindings are checked by Lua as well.
        result = await redis.eval(LUA["reserve"], 3, PREFIX + "token:" + nonce,
                                  PREFIX + "user:1:1:1", STOCK_KEY, 1, 2, 1, nonce)
        assert result == ["TOKEN_MISMATCH"]
        await redis.hset(PREFIX + "token:" + nonce, "expires", 0)
        assert (await buy(client, nonce)).json()["code"] == "TOKEN_EXPIRED"
        assert int(await redis.get(STOCK_KEY)) == 10
    asyncio.run(with_app(tmp_path, scenario))


def test_same_user_concurrent_and_reset(tmp_path):
    async def scenario(client, redis, app):
        tokens = [await token(client) for _ in range(3)]
        responses = await asyncio.gather(*(buy(client, t) for t in tokens))
        codes = [r.json()["code"] for r in responses]
        assert codes.count("SUCCESS") == 1
        assert all(c in {"SUCCESS", "ALREADY_PROCESSING", "ALREADY_PURCHASED"} for c in codes)
        app.state.active += 1
        assert (await client.post("/demo/reset")).json()["code"] == "DEMO_BUSY"
        app.state.active -= 1
        assert (await client.post("/demo/reset")).status_code == 200
        assert (await buy(client, tokens[0])).json()["code"] == "TOKEN_NOT_FOUND"
        assert (await client.get("/flash-sale/status")).json()["orderCount"] == 0
    asyncio.run(with_app(tmp_path, scenario))


def test_database_failure_compensation(tmp_path, monkeypatch):
    async def scenario(client, redis, app):
        nonce = await token(client)
        # Trigger an actual DB error AFTER decrement, proving transaction rollback.
        with app.state.db.connect() as conn:
            conn.execute("CREATE TRIGGER reject_order BEFORE INSERT ON orders "
                         "BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        result = await buy(client, nonce)
        assert result.json()["code"] == "DATABASE_ERROR"
        assert (await client.get("/flash-sale/status")).json() == {
            "redisStock": 10, "databaseStock": 10, "orderCount": 0,
        }
        assert await redis.get(PREFIX + "user:1:1:1") is None
        assert (await buy(client, nonce)).json()["code"] == "TOKEN_ALREADY_USED"
        # A second compensation cannot create inventory.
        assert await redis.eval(LUA["finish"], 2, PREFIX + "user:1:1:1", STOCK_KEY,
                                nonce, "ROLLBACK") == 0
        assert int(await redis.get(STOCK_KEY)) == 10
        with app.state.db.connect() as conn:
            conn.execute("DROP TRIGGER reject_order")
        assert (await buy(client, await token(client))).json()["code"] == "SUCCESS"
    asyncio.run(with_app(tmp_path, scenario))


def test_database_sold_out_and_duplicate_backstop(tmp_path):
    async def scenario(client, redis, app):
        with app.state.db.connect() as conn, conn:
            conn.execute("UPDATE products SET stock=0")
        assert (await buy(client, await token(client))).json()["code"] == "SOLD_OUT"
        assert int(await redis.get(STOCK_KEY)) == 10  # Reservation restored, not reconciled.
        assert await redis.get(PREFIX + "user:1:1:1") is None
        await client.post("/demo/reset")
        assert (await buy(client, await token(client))).json()["code"] == "SUCCESS"
        await redis.delete(PREFIX + "user:1:1:1")  # Simulate lost Redis guard.
        assert (await buy(client, await token(client))).json()["code"] == "ALREADY_PURCHASED"
        assert (await client.get("/flash-sale/status")).json() == {
            "redisStock": 9, "databaseStock": 9, "orderCount": 1,
        }
    asyncio.run(with_app(tmp_path, scenario))


def test_sqlite_concurrency_without_redis(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    db.reset()

    def attempt(user):
        try:
            db.purchase(user, 1, 1, str(user))
            return "SUCCESS"
        except PurchaseRejected as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(attempt, range(1, 101)))
    assert results.count("SUCCESS") == 10
    assert results.count("SOLD_OUT") == 90
    assert db.status() == {"databaseStock": 0, "orderCount": 10}


def test_sqlite_unique_constraint(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    db.reset()
    db.purchase(1, 1, 1, "first")
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect() as conn, conn:
            conn.execute("INSERT INTO orders(user_id,product_id,event_id,status,request_id) "
                         "VALUES(1,1,1,'SUCCESS','different')")
    assert db.status() == {"databaseStock": 9, "orderCount": 1}

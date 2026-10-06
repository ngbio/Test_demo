import logging
import math
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.concurrency import run_in_threadpool

from app.database import Database, PurchaseRejected

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "flashsale:"
STOCK_KEY = PREFIX + "stock:1:1"
logger = logging.getLogger("uvicorn.error")
LUA = {name: (ROOT / "app" / "lua" / f"{name}.lua").read_text(encoding="utf-8")
       for name in ("rate_limit", "create_token", "reserve", "finish")}


class APIError(Exception):
    def __init__(self, code, status=409, headers=None):
        self.code, self.status, self.headers = code, status, headers


class TokenRequest(BaseModel):
    userId: int = Field(gt=0, strict=True)
    eventId: int = Field(gt=0, strict=True)
    productId: int = Field(gt=0, strict=True)


class PurchaseRequest(TokenRequest):
    purchaseToken: str = Field(min_length=20, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


def create_app(redis_client=None, database_path=None):
    db = Database(database_path or os.getenv("DATABASE_PATH", str(ROOT / "data" / "demo.db")))
    client = redis_client if redis_client is not None else Redis.from_url(
        os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"), decode_responses=True,
        socket_connect_timeout=3, socket_timeout=5,
    )

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(db.initialize)
        try:
            await client.ping()
        except RedisError:
            logger.warning("Redis unavailable. Start Redis, then POST /demo/reset.")
        yield
        await client.aclose()

    app = FastAPI(title="Flash Sale: Anti-bot & No Overselling Demo", lifespan=lifespan)
    app.state.db = db
    app.state.redis = client
    app.state.active = 0
    app.state.resetting = False

    @app.exception_handler(APIError)
    async def api_error(request, exc):
        return JSONResponse({"code": exc.code}, status_code=exc.status, headers=exc.headers)

    @app.exception_handler(RedisError)
    async def redis_error(request, exc):
        logger.error("[REDIS_UNAVAILABLE] %s", type(exc).__name__)
        return JSONResponse({"code": "REDIS_UNAVAILABLE"}, status_code=503)

    async def demo_request():
        # Single ASGI worker only. No await between checking/resetting counters.
        # This excludes reset while requests run, WITHOUT serializing purchases.
        if app.state.resetting:
            raise APIError("RESET_IN_PROGRESS", 503)
        app.state.active += 1
        try:
            yield
        finally:
            app.state.active -= 1

    def check_sale(body):
        # One event/product keeps DB inventory aligned with the Redis stock key.
        if body.eventId != 1 or body.productId != 1:
            raise APIError("SALE_NOT_FOUND", 404)

    @app.post("/demo/reset", tags=["Demo only"])
    async def reset():
        if os.getenv("DEMO_MODE", "true").lower() != "true":
            raise APIError("DEMO_DISABLED", 403)
        if app.state.active or app.state.resetting:
            raise APIError("DEMO_BUSY")
        app.state.resetting = True
        try:
            await client.ping()
            # Invalidate stock first; a partial reset must fail closed.
            await client.delete(STOCK_KEY)
            await run_in_threadpool(db.reset)
            keys = [key async for key in client.scan_iter(match=PREFIX + "*")]
            if keys:
                await client.delete(*keys)
            await client.set(STOCK_KEY, 10)
            logger.info("[RESET] event=1 product=1 stock=10")
            return {"code": "RESET_SUCCESS", "redisStock": 10,
                    "databaseStock": 10, "orderCount": 0}
        finally:
            app.state.resetting = False

    @app.post("/flash-sale/token", dependencies=[Depends(demo_request)])
    async def token(body: TokenRequest):
        check_sale(body)
        if not await client.exists(STOCK_KEY):
            raise APIError("DEMO_NOT_INITIALIZED", 503)
        # Opaque cryptographically random token is also the unique request nonce.
        while True:
            nonce = secrets.token_urlsafe(32)
            created = await client.eval(LUA["create_token"], 1, PREFIX + "token:" + nonce,
                                        body.userId, body.eventId, body.productId)
            if created:
                break
        logger.info("[TOKEN_CREATED] user=%s event=1 product=1 expiresIn=60", body.userId)
        return {"purchaseToken": nonce, "expiresIn": 60}

    @app.post("/flash-sale/purchase", dependencies=[Depends(demo_request)])
    async def purchase(body: PurchaseRequest):
        logger.info("[REQUEST] user=%s product=%s", body.userId, body.productId)
        wait_ms = await client.eval(LUA["rate_limit"], 1, PREFIX + f"rate:{body.userId}", uuid4().hex)
        if wait_ms:
            logger.info("[RATE_LIMITED] user=%s", body.userId)
            raise APIError("RATE_LIMIT_EXCEEDED", 429,
                           {"Retry-After": str(math.ceil(wait_ms / 1000))})
        check_sale(body)
        nonce = body.purchaseToken
        user_key = PREFIX + f"user:{body.eventId}:{body.productId}:{body.userId}"
        result = await client.eval(LUA["reserve"], 3, PREFIX + "token:" + nonce,
                                   user_key, STOCK_KEY, body.userId, body.eventId, body.productId, nonce)
        code = result[0]
        if code != "RESERVED":
            logger.info("[%s] user=%s", code, body.userId)
            status = {"TOKEN_NOT_FOUND": 400, "TOKEN_MISMATCH": 403,
                      "TOKEN_EXPIRED": 410, "DEMO_NOT_INITIALIZED": 503}.get(code, 409)
            raise APIError(code, status)
        logger.info("[TOKEN_USED] user=%s", body.userId)
        logger.info("[RESERVED] user=%s product=1 remainingStock=%s", body.userId, result[1])
        try:
            order_id = await run_in_threadpool(db.purchase, body.userId, body.eventId,
                                              body.productId, nonce)
        except (PurchaseRejected, sqlite3.Error) as exc:
            code = exc.code if isinstance(exc, PurchaseRejected) else "DATABASE_ERROR"
            mode = "DUPLICATE" if code == "ALREADY_PURCHASED" else "ROLLBACK"
            await client.eval(LUA["finish"], 2, user_key, STOCK_KEY, nonce, mode)
            logger.warning("[ROLLBACK] user=%s reason=%s", body.userId, code)
            raise APIError(code, 503 if code == "DATABASE_ERROR" else 409) from exc
        # DB has committed: NEVER compensate stock if final Redis marking fails.
        # The retained nonce guard still blocks another purchase until demo reset.
        try:
            finalized = await client.eval(LUA["finish"], 2, user_key, STOCK_KEY, nonce, "COMMIT")
            if not finalized:
                logger.error("[RECONCILIATION_REQUIRED] orderId=%s", order_id)
        except RedisError:
            logger.exception("[RECONCILIATION_REQUIRED] orderId=%s", order_id)
        logger.info("[ORDER_CREATED] user=%s orderId=%s", body.userId, order_id)
        return {"code": "SUCCESS", "orderId": order_id}

    @app.get("/flash-sale/status", dependencies=[Depends(demo_request)])
    async def status():
        stock = await client.get(STOCK_KEY)
        if stock is None:
            raise APIError("DEMO_NOT_INITIALIZED", 503)
        return {"redisStock": int(stock), **(await run_in_threadpool(db.status))}

    return app


app = create_app()

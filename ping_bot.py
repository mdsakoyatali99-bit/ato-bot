"""
ping_bot.py  (Bot B) - standalone keep-alive pinger for Render.

* Makes an HTTP GET to the /health URL(s) in TARGET_URLS at a RANDOM interval
  (default 240-480 s) so a Render free Web Service never reaches its 15-minute idle timeout.
* Exposes its own tiny FastAPI /health endpoint, because a Render *Web Service* must bind $PORT.
* The interval is read from Firebase (/config/heartbeat {min,max}, editable from the Master Bot
  panel) when FIREBASE_DB_URL + credentials are set; otherwise PING_MIN / PING_MAX env vars are used.

Env:
  TARGET_URLS   comma-separated base URLs, e.g. https://reaction-bot-a.onrender.com
  PING_MIN      default 240 (seconds)      PING_MAX  default 480
  PORT          set by Render
  FIREBASE_DB_URL / FIREBASE_CREDENTIALS_JSON   (optional, for dynamic interval)
"""
import asyncio
import logging
import os
import random
import time

import uvicorn
from aiohttp import ClientSession, ClientTimeout
from fastapi import FastAPI

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ping_bot")

TARGETS = [u.strip().rstrip("/") for u in os.getenv("TARGET_URLS", "").split(",") if u.strip()]
PING_MIN = float(os.getenv("PING_MIN", "240"))
PING_MAX = float(os.getenv("PING_MAX", "480"))
PORT = int(os.getenv("PORT", "10001"))

# Optional Firebase for a dynamic interval
fb = None
if os.getenv("FIREBASE_DB_URL"):
    try:
        import firebase_config as fb  # type: ignore
        fb.init()
        log.info("Firebase connected - interval is read from /config/heartbeat")
    except Exception as e:  # missing creds etc. -> fall back to env values
        log.warning("Firebase unavailable (%s) - using PING_MIN/PING_MAX", e)
        fb = None

app = FastAPI(title="ping-bot-b")
state = {"last_ping": None, "pings": 0}


@app.get("/")
@app.get("/health")
async def health():
    return {"status": "ok", "service": "bot-b", "pings": state["pings"], "last_ping": state["last_ping"], "time": int(time.time())}


async def get_interval() -> tuple[float, float]:
    lo, hi = PING_MIN, PING_MAX
    if fb:
        try:
            cfg = await fb.get("/config/heartbeat") or {}
            lo, hi = float(cfg.get("min", lo)), float(cfg.get("max", hi))
        except Exception as e:
            log.warning("could not read interval from Firebase: %s", e)
    if lo > hi:
        lo, hi = hi, lo
    return max(lo, 30.0), max(hi, 30.0)


async def ping_once(session: ClientSession, base: str) -> None:
    url = base if base.endswith("/health") else f"{base}/health"
    for attempt in (1, 2):  # one retry: a sleeping free instance can need ~1 min to wake
        try:
            async with session.get(url) as r:
                log.info("ping %s -> %s", url, r.status)
                return
        except Exception as e:
            log.warning("ping %s failed (attempt %d): %s", url, attempt, e)
            await asyncio.sleep(10)


async def ping_loop() -> None:
    if not TARGETS:
        log.error("TARGET_URLS is empty - nothing to ping. Set it to your Bot A URL.")
        return
    await asyncio.sleep(random.uniform(5, 20))  # small startup jitter
    async with ClientSession(timeout=ClientTimeout(total=90)) as session:
        while True:
            for base in TARGETS:
                await ping_once(session, base)
            state["pings"] += 1
            state["last_ping"] = int(time.time())
            lo, hi = await get_interval()
            delay = random.uniform(lo, hi)  # ALWAYS randomised
            log.info("next ping in %.0f s", delay)
            await asyncio.sleep(delay)


async def main() -> None:
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning"))
    server.install_signal_handlers = lambda: None
    await asyncio.gather(server.serve(), ping_loop())


if __name__ == "__main__":
    asyncio.run(main())

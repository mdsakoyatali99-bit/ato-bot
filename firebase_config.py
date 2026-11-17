"""
firebase_config.py
Thin async wrapper around the Firebase Realtime Database (firebase_admin SDK).

Credentials (first match wins):
  1. FIREBASE_CREDENTIALS_JSON  -> the full service-account JSON as a string (recommended on Render)
  2. FIREBASE_CREDENTIALS_FILE  -> path to a JSON file (default: firebase_credentials.json)
Also required: FIREBASE_DB_URL, e.g. https://<project-id>-default-rtdb.firebaseio.com
"""
import asyncio
import json
import os

import firebase_admin
from firebase_admin import credentials, db

DEFAULTS = {
    "config": {
        "welcome_message": "👋 <b>Welcome!</b>\nThis bot is up and running.",
        "heartbeat": {"min": 240, "max": 480},
        "reaction": {"userbot_delay_min": 6, "userbot_delay_max": 20},
    }
}


def init() -> None:
    """Initialise firebase_admin once and seed default config if missing."""
    if firebase_admin._apps:
        return
    raw = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if raw:
        cred = credentials.Certificate(json.loads(raw))
    else:
        cred = credentials.Certificate(os.getenv("FIREBASE_CREDENTIALS_FILE", "firebase_credentials.json"))
    firebase_admin.initialize_app(cred, {"databaseURL": os.environ["FIREBASE_DB_URL"]})

    # Seed defaults only where nothing exists yet
    if db.reference("/config/welcome_message").get() is None:
        db.reference("/config/welcome_message").set(DEFAULTS["config"]["welcome_message"])
    if db.reference("/config/heartbeat").get() is None:
        db.reference("/config/heartbeat").set(DEFAULTS["config"]["heartbeat"])
    if db.reference("/config/reaction").get() is None:
        db.reference("/config/reaction").set(DEFAULTS["config"]["reaction"])


# ---- async helpers (the SDK is blocking, so run it in a worker thread) ----
async def get(path: str):
    return await asyncio.to_thread(lambda: db.reference(path).get())


async def set(path: str, value) -> None:  # noqa: A001
    await asyncio.to_thread(lambda: db.reference(path).set(value))


async def update(path: str, value: dict) -> None:
    await asyncio.to_thread(lambda: db.reference(path).update(value))


async def push(path: str, value) -> str:
    ref = await asyncio.to_thread(lambda: db.reference(path).push(value))
    return ref.key


async def delete(path: str) -> None:
    await asyncio.to_thread(lambda: db.reference(path).delete())

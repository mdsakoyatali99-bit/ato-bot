"""
main.py - Telegram Auto Reaction Bot system
  * Master bot  (aiogram)  : owner-only inline control panel, welcome msg, buttons, broadcast, bot/session mgmt, heartbeat
  * Admin bots  (Bot API)  : react to new posts in target groups with random emojis
  * User bots   (Telethon) : read the emojis admin bots used, then react with one of them
  * FastAPI /health        : HTTP endpoint pinged by ping_bot.py (Bot B) so Render never idles out
All configuration lives in Firebase Realtime Database (see firebase_config.py).
"""
import asyncio
import logging
import os
import random
import re
import time
from typing import Dict, Set

import uvicorn
from aiohttp import ClientSession, ClientTimeout
from fastapi import FastAPI
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, ReactionTypeEmoji
from aiogram.utils.keyboard import InlineKeyboardBuilder
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import ReactionEmoji

try:  # load variables from a local .env file if present
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import firebase_config as fb

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("reactor")

# ----------------------------------------------------------------------------
# Environment
# ----------------------------------------------------------------------------
MASTER_BOT_TOKEN = os.environ["MASTER_BOT_TOKEN"]
OWNER_TELEGRAM_ID = int(os.environ["OWNER_TELEGRAM_ID"])
API_ID = int(os.environ["TELEGRAM_API_ID"])          # from https://my.telegram.org
API_HASH = os.environ["TELEGRAM_API_HASH"]
PARTNER_URL = os.getenv("PARTNER_URL")               # optional: Bot B URL, so A also pings B (mutual keep-alive)
PORT = int(os.getenv("PORT", "10000"))

# Standard Bot-API reaction emojis (positive subset)
EMOJI_POOL = ["👍", "❤", "🔥", "🥰", "👏", "😁", "🎉", "🤩", "💯", "⚡", "🏆", "👌", "😍", "🤗", "🙏", "😎", "🤝", "🫡", "💘", "🐳"]

UNAUTHORIZED = "⚠️ You are not authorized to use this master bot!"


# ============================================================================
# 1. Master bot: FSM states, keyboards, handlers
# ============================================================================
class WelcomeSet(StatesGroup):
    text = State()


class BtnAdd(StatesGroup):
    label = State()
    url = State()


class Broadcast(StatesGroup):
    content = State()


class AddAdmin(StatesGroup):
    token = State()


class AddUser(StatesGroup):
    session = State()


class AssignGroups(StatesGroup):
    ids = State()


class HeartbeatSet(StatesGroup):
    rng = State()


class OwnerOnly(BaseMiddleware):
    """Blocks everything except the owner on the panel router."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and user.id == OWNER_TELEGRAM_ID:
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer(UNAUTHORIZED, show_alert=True)
        elif isinstance(event, Message) and (event.text or "").startswith("/"):
            await event.answer(UNAUTHORIZED)
        return None


public = Router()   # /start for everybody
panel = Router()    # owner-only
panel.message.outer_middleware(OwnerOnly())
panel.callback_query.outer_middleware(OwnerOnly())


def main_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📝 Welcome Message", callback_data="wel:menu")
    b.button(text="🔘 Custom Buttons", callback_data="btn:menu")
    b.button(text="📢 Broadcast", callback_data="bc:start")
    b.button(text="🤖 Admin Bots", callback_data="ab:menu")
    b.button(text="👤 User Bots", callback_data="ub:menu")
    b.button(text="💓 Heartbeat", callback_data="hb:menu")
    b.button(text="📊 Stats", callback_data="stats")
    b.adjust(2, 1, 2, 2)
    return b.as_markup()


def back_kb(target="menu:main") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Back", callback_data=target)
    return b.as_markup()


async def welcome_markup() -> InlineKeyboardMarkup | None:
    buttons = await fb.get("/config/buttons") or {}
    if not buttons:
        return None
    b = InlineKeyboardBuilder()
    for item in buttons.values():
        b.button(text=item["label"], url=item["url"])
    b.adjust(1)
    return b.as_markup()


async def edit(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None):
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass  # "message is not modified"
    await cb.answer()


# ---------------- public /start ----------------
@public.message(CommandStart())
async def cmd_start(message: Message):
    uid = str(message.from_user.id)
    if await fb.get(f"/users/{uid}") is None:
        await fb.set(f"/users/{uid}", {"name": message.from_user.full_name, "joined": int(time.time())})
    text = await fb.get("/config/welcome_message") or "Welcome!"
    await message.answer(text, reply_markup=await welcome_markup())


# ---------------- panel ----------------
@panel.message(Command("panel"))
async def cmd_panel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🎛 <b>Master Control Panel</b>", reply_markup=main_kb())


@panel.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled.", reply_markup=main_kb())


@panel.callback_query(F.data == "menu:main")
async def cb_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(cb, "🎛 <b>Master Control Panel</b>", main_kb())


@panel.callback_query(F.data == "stats")
async def cb_stats(cb: CallbackQuery):
    users = await fb.get("/users") or {}
    ab = await fb.get("/bots/admin_bots") or {}
    ub = await fb.get("/bots/user_bots") or {}
    await edit(cb, f"📊 <b>Stats</b>\n👥 Users: {len(users)}\n🤖 Admin bots: {len(ab)}\n👤 User bots: {len(ub)}", back_kb())


# ---------------- welcome message ----------------
@panel.callback_query(F.data == "wel:menu")
async def cb_wel(cb: CallbackQuery, state: FSMContext):
    cur = await fb.get("/config/welcome_message") or "(none)"
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Edit", callback_data="wel:edit")
    b.button(text="⬅️ Back", callback_data="menu:main")
    b.adjust(1)
    await edit(cb, f"📝 <b>Current welcome message</b>:\n\n{cur}", b.as_markup())


@panel.callback_query(F.data == "wel:edit")
async def cb_wel_edit(cb: CallbackQuery, state: FSMContext):
    await state.set_state(WelcomeSet.text)
    await edit(cb, "Send the new welcome message (HTML formatting is kept). /cancel to abort.")


@panel.message(WelcomeSet.text)
async def got_welcome(message: Message, state: FSMContext):
    await fb.set("/config/welcome_message", message.html_text)
    await state.clear()
    await message.answer("✅ Welcome message updated.", reply_markup=main_kb())


# ---------------- custom buttons ----------------
@panel.callback_query(F.data == "btn:menu")
async def cb_btn_menu(cb: CallbackQuery):
    buttons = await fb.get("/config/buttons") or {}
    b = InlineKeyboardBuilder()
    for key, item in buttons.items():
        b.button(text=f"🗑 {item['label']}", callback_data=f"btn:del:{key}")
    b.button(text="➕ Add button", callback_data="btn:add")
    b.button(text="⬅️ Back", callback_data="menu:main")
    b.adjust(1)
    await edit(cb, "🔘 <b>Welcome buttons</b> (tap one to delete):", b.as_markup())


@panel.callback_query(F.data.startswith("btn:del:"))
async def cb_btn_del(cb: CallbackQuery):
    await fb.delete(f"/config/buttons/{cb.data.split(':', 2)[2]}")
    await cb_btn_menu(cb)


@panel.callback_query(F.data == "btn:add")
async def cb_btn_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BtnAdd.label)
    await edit(cb, "Send the button <b>label</b>:")


@panel.message(BtnAdd.label)
async def got_btn_label(message: Message, state: FSMContext):
    await state.update_data(label=message.text.strip()[:60])
    await state.set_state(BtnAdd.url)
    await message.answer("Now send the button <b>URL</b> (https://… or tg://…):")


@panel.message(BtnAdd.url)
async def got_btn_url(message: Message, state: FSMContext):
    url = (message.text or "").strip()
    if not re.match(r"^(https?|tg)://", url):
        return await message.answer("❌ Invalid URL, try again.")
    data = await state.get_data()
    await fb.push("/config/buttons", {"label": data["label"], "url": url})
    await state.clear()
    await message.answer("✅ Button added.", reply_markup=main_kb())


# ---------------- broadcast ----------------
@panel.callback_query(F.data == "bc:start")
async def cb_bc(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.content)
    await edit(cb, "📢 Send the message to broadcast (text, photo, video… anything). /cancel to abort.")


@panel.message(Broadcast.content)
async def got_bc(message: Message, state: FSMContext):
    await state.update_data(chat=message.chat.id, mid=message.message_id)
    b = InlineKeyboardBuilder()
    b.button(text="✅ Send to everyone", callback_data="bc:go")
    b.button(text="❌ Cancel", callback_data="menu:main")
    await message.answer("Send this to ALL users?", reply_markup=b.as_markup())


@panel.callback_query(F.data == "bc:go")
async def cb_bc_go(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    if not data:
        return await cb.answer("Nothing to send.", show_alert=True)
    await edit(cb, "📤 Broadcast started…")
    asyncio.create_task(run_broadcast(bot, data["chat"], data["mid"]))


async def run_broadcast(bot: Bot, from_chat: int, mid: int):
    users = await fb.get("/users") or {}
    ok = fail = 0
    for uid in users:
        try:
            await bot.copy_message(int(uid), from_chat, mid)
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.copy_message(int(uid), from_chat, mid)
                ok += 1
            except Exception:
                fail += 1
        except TelegramForbiddenError:
            fail += 1
            await fb.delete(f"/users/{uid}")  # user blocked the bot
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)  # ~20 msg/s, under Telegram's limit
    await bot.send_message(OWNER_TELEGRAM_ID, f"📢 Broadcast finished.\n✅ {ok} delivered\n❌ {fail} failed")


# ---------------- admin bots / user bots (generic) ----------------
KINDS = {"ab": ("/bots/admin_bots", "🤖 Admin Bots"), "ub": ("/bots/user_bots", "👤 User Bots")}


@panel.callback_query(F.data.regexp(r"^(ab|ub):menu$"))
async def cb_bots_menu(cb: CallbackQuery):
    kind = cb.data.split(":")[0]
    path, title = KINDS[kind]
    items = await fb.get(path) or {}
    b = InlineKeyboardBuilder()
    for key, cfg in items.items():
        n = len(cfg.get("groups") or {})
        b.button(text=f"{cfg.get('name', key)} · {n} grp", callback_data=f"{kind}:grp:{key}")
        b.button(text="🗑", callback_data=f"{kind}:del:{key}")
    b.button(text="➕ Add", callback_data=f"{kind}:add")
    b.button(text="⬅️ Back", callback_data="menu:main")
    sizes = [2] * len(items) + [1, 1]
    b.adjust(*sizes)
    await edit(cb, f"{title}\nTap a name to assign target groups, 🗑 to remove.", b.as_markup())


@panel.callback_query(F.data.regexp(r"^(ab|ub):del:"))
async def cb_bot_del(cb: CallbackQuery):
    kind, _, key = cb.data.split(":", 2)
    await fb.delete(f"{KINDS[kind][0]}/{key}")
    await cb_bots_menu(cb)


@panel.callback_query(F.data == "ab:add")
async def cb_ab_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddAdmin.token)
    await edit(cb, "Send the <b>bot token</b> (the message is deleted right after). Make the bot admin in the target groups.")


@panel.message(AddAdmin.token)
async def got_ab_token(message: Message, state: FSMContext):
    token = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    try:
        probe = Bot(token)
        me = await probe.get_me()
        await probe.session.close()
    except Exception:
        return await message.answer("❌ Invalid token, try again or /cancel.")
    await fb.update(f"/bots/admin_bots/{me.id}", {"token": token, "name": f"@{me.username}"})
    await state.clear()
    await message.answer(f"✅ Added @{me.username}. Now assign its groups from the menu.", reply_markup=main_kb())


@panel.callback_query(F.data == "ub:add")
async def cb_ub_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddUser.session)
    await edit(cb, "Send the Telethon <b>session string</b> (deleted right after).")


@panel.message(AddUser.session)
async def got_ub_session(message: Message, state: FSMContext):
    session = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    client = TelegramClient(StringSession(session), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError("not authorized")
        me = await client.get_me()
    except Exception:
        return await message.answer("❌ Invalid/expired session string, try again or /cancel.")
    finally:
        await client.disconnect()
    await fb.update(f"/bots/user_bots/{me.id}", {"session": session, "name": me.first_name or str(me.id)})
    await state.clear()
    await message.answer(f"✅ Added user bot {me.first_name}. Assign its groups from the menu.", reply_markup=main_kb())


@panel.callback_query(F.data.regexp(r"^(ab|ub):grp:"))
async def cb_grp(cb: CallbackQuery, state: FSMContext):
    kind, _, key = cb.data.split(":", 2)
    cfg = await fb.get(f"{KINDS[kind][0]}/{key}") or {}
    cur = ", ".join((cfg.get("groups") or {}).keys()) or "none"
    await state.set_state(AssignGroups.ids)
    await state.update_data(kind=kind, key=key)
    await edit(cb, f"Current groups: <code>{cur}</code>\n\nSend group/channel IDs separated by spaces "
                   f"(e.g. <code>-1001234567890 -1009876543210</code>) — this REPLACES the list. Send <code>clear</code> to empty it.")


@panel.message(AssignGroups.ids)
async def got_groups(message: Message, state: FSMContext):
    data = await state.get_data()
    path = f"{KINDS[data['kind']][0]}/{data['key']}/groups"
    text = (message.text or "").strip().lower()
    ids = [] if text == "clear" else re.findall(r"-?\d+", text)
    if ids:
        await fb.set(path, {i: True for i in ids})
    else:
        await fb.delete(path)
    await state.clear()
    await message.answer(f"✅ Groups saved ({len(ids)}).", reply_markup=main_kb())


# ---------------- heartbeat ----------------
@panel.callback_query(F.data == "hb:menu")
async def cb_hb(cb: CallbackQuery):
    cfg = await fb.get("/config/heartbeat") or {}
    b = InlineKeyboardBuilder()
    b.button(text="⏱ Set range", callback_data="hb:set")
    b.button(text="⬅️ Back", callback_data="menu:main")
    b.adjust(1)
    await edit(cb, f"💓 <b>Ping-bot interval</b> (used by ping_bot.py)\nRandom delay: {cfg.get('min', 240)}–{cfg.get('max', 480)} seconds", b.as_markup())


@panel.callback_query(F.data == "hb:set")
async def cb_hb_set(cb: CallbackQuery, state: FSMContext):
    await state.set_state(HeartbeatSet.rng)
    await edit(cb, "Send <code>min max</code> in seconds, e.g. <code>240 480</code> (30 ≤ min ≤ max ≤ 840 so it stays under Render's 15-min sleep).")


@panel.message(HeartbeatSet.rng)
async def got_hb(message: Message, state: FSMContext):
    nums = re.findall(r"\d+", message.text or "")
    if len(nums) != 2 or not (30 <= int(nums[0]) <= int(nums[1]) <= 840):
        return await message.answer("❌ Use two numbers: 30 ≤ min ≤ max ≤ 840.")
    await fb.set("/config/heartbeat", {"min": int(nums[0]), "max": int(nums[1])})
    await state.clear()
    await message.answer("✅ Ping interval saved (ping_bot applies it from its next cycle).", reply_markup=main_kb())


# ============================================================================
# 2. Reaction engine (admin bots + userbots), hot-reloaded from Firebase
# ============================================================================
def _group_set(cfg: dict) -> Set[int]:
    return {int(k) for k in (cfg.get("groups") or {})}


class ReactionManager:
    def __init__(self):
        self.admin_tasks: Dict[str, asyncio.Task] = {}
        self.admin_tokens: Dict[str, str] = {}
        self.admin_groups: Dict[str, Set[int]] = {}
        self.user_clients: Dict[str, tuple] = {}   # key -> (client, session)
        self.user_groups: Dict[str, Set[int]] = {}
        self.used: Dict[tuple, Set[str]] = {}      # (chat, msg) -> emojis already used by admin bots

    # ---- reconcile loop: start/stop workers to match Firebase ----
    async def run(self):
        while True:
            try:
                await self.sync()
            except Exception:
                log.exception("sync failed")
            await asyncio.sleep(20)

    async def sync(self):
        admins = await fb.get("/bots/admin_bots") or {}
        users = await fb.get("/bots/user_bots") or {}

        # admin bots
        for key, cfg in admins.items():
            self.admin_groups[key] = _group_set(cfg)
            if key not in self.admin_tasks or self.admin_tokens.get(key) != cfg["token"]:
                if key in self.admin_tasks:
                    self.admin_tasks[key].cancel()
                self.admin_tokens[key] = cfg["token"]
                self.admin_tasks[key] = asyncio.create_task(self._admin_loop(key, cfg["token"]))
        for key in list(self.admin_tasks):
            if key not in admins:
                self.admin_tasks.pop(key).cancel()
                self.admin_groups.pop(key, None)

        # user bots
        for key, cfg in users.items():
            self.user_groups[key] = _group_set(cfg)
            cur = self.user_clients.get(key)
            if cur and cur[1] == cfg["session"]:
                continue
            if cur:
                await cur[0].disconnect()
            await self._start_userbot(key, cfg["session"])
        for key in list(self.user_clients):
            if key not in users:
                await self.user_clients.pop(key)[0].disconnect()
                self.user_groups.pop(key, None)

    # ---- admin bots (Bot API long polling) ----
    async def _admin_loop(self, key: str, token: str):
        bot = Bot(token)
        offset = None
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            while True:
                try:
                    updates = await bot.get_updates(offset=offset, timeout=30, allowed_updates=["message", "channel_post"])
                    for u in updates:
                        offset = u.update_id + 1
                        m = u.message or u.channel_post
                        if m and m.chat.id in self.admin_groups.get(key, set()):
                            asyncio.create_task(self._admin_react(bot, m.chat.id, m.message_id))
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("admin bot %s poll error: %s", key, e)
                    await asyncio.sleep(5)
        finally:
            await bot.session.close()

    async def _admin_react(self, bot: Bot, chat_id: int, msg_id: int):
        await asyncio.sleep(random.uniform(1.0, 3.0))  # natural delay / FloodWait avoidance
        used = self.used.setdefault((chat_id, msg_id), set())
        choices = [e for e in EMOJI_POOL if e not in used] or EMOJI_POOL
        emoji = random.choice(choices)
        used.add(emoji)
        if len(self.used) > 500:                       # keep memory bounded
            self.used.pop(next(iter(self.used)))
        try:
            await bot.set_message_reaction(chat_id, msg_id, reaction=[ReactionTypeEmoji(emoji=emoji)])
        except TelegramRetryAfter as e:
            log.warning("admin FloodWait %ss", e.retry_after)
        except Exception as e:
            log.warning("admin react failed: %s", e)

    # ---- userbots (Telethon) ----
    async def _start_userbot(self, key: str, session: str):
        client = TelegramClient(StringSession(session), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            log.error("userbot %s: session not authorised", key)
            await client.disconnect()
            return
        await client.get_dialogs()  # fills the entity cache so peers resolve

        @client.on(events.NewMessage(incoming=True))
        async def handler(event, _key=key, _client=client):
            if event.chat_id in self.user_groups.get(_key, set()):
                asyncio.create_task(self._user_react(_client, event.chat_id, event.id))

        self.user_clients[key] = (client, session)
        log.info("userbot %s started", key)

    @staticmethod
    def _emojis_on(msg) -> list:
        out = []
        if msg and getattr(msg, "reactions", None):
            for r in msg.reactions.results:
                if isinstance(r.reaction, ReactionEmoji):
                    out.append(r.reaction.emoticon)
        return out

    async def _user_react(self, client: TelegramClient, chat_id: int, msg_id: int):
        cfg = await fb.get("/config/reaction") or {}
        lo, hi = float(cfg.get("userbot_delay_min", 6)), float(cfg.get("userbot_delay_max", 20))
        await asyncio.sleep(random.uniform(lo, max(lo, hi)))   # staggered: admin bots go first
        try:
            emojis = []
            for _ in range(3):                                   # wait for admin reactions to land
                msg = await client.get_messages(chat_id, ids=msg_id)
                emojis = self._emojis_on(msg)
                if emojis:
                    break
                await asyncio.sleep(random.uniform(3, 6))
            if not emojis:
                return
            peer = await client.get_input_entity(chat_id)
            await client(SendReactionRequest(peer=peer, msg_id=msg_id, reaction=[ReactionEmoji(emoticon=random.choice(emojis))]))
        except FloodWaitError as e:
            log.warning("userbot FloodWait %ss - skipping", e.seconds)
        except Exception as e:
            log.warning("userbot react failed: %s", e)


# ============================================================================
# 3. FastAPI keep-alive endpoint (+ optional partner ping)
# ============================================================================
app = FastAPI(title="reaction-bot-a")


@app.get("/")
@app.get("/health")
async def health():
    return {"status": "ok", "service": "bot-a", "time": int(time.time())}


async def partner_ping():
    """Optional: ping Bot B (PARTNER_URL) at a random interval so the two services keep each other awake."""
    if not PARTNER_URL:
        return
    while True:
        cfg = await fb.get("/config/heartbeat") or {}
        lo, hi = float(cfg.get("min", 240)), float(cfg.get("max", 480))
        if lo > hi:
            lo, hi = hi, lo
        await asyncio.sleep(random.uniform(lo, hi))
        try:
            async with ClientSession(timeout=ClientTimeout(total=90)) as s:
                await s.get(PARTNER_URL.rstrip("/") + "/health")
        except Exception as e:
            log.warning("partner ping failed: %s", e)


# ============================================================================
# 4. Entrypoint
# ============================================================================
async def main():
    fb.init()
    master = Bot(MASTER_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(public)
    dp.include_router(panel)

    manager = ReactionManager()
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning"))
    server.install_signal_handlers = lambda: None   # let asyncio/aiogram own the signals
    tasks = [asyncio.create_task(server.serve()), asyncio.create_task(manager.run()), asyncio.create_task(partner_ping())]

    await master.delete_webhook(drop_pending_updates=True)
    log.info("Master bot polling…")
    try:
        await dp.start_polling(master)
    finally:
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    asyncio.run(main())

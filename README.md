# Telegram Auto Reaction Bot (Bot A) + Keep-Alive Ping Bot (Bot B)

```
telegram-reaction-bot/
├── main.py              Bot A: master panel, broadcast, admin/user-bot reactions, FastAPI /health
├── ping_bot.py          Bot B: standalone random-interval HTTP pinger (+ its own /health)
├── firebase_config.py   Firebase Realtime Database helpers (used by both)
├── requirements.txt
├── Procfile             (only for local/Heroku-style runners; Render uses the Start Command)
├── .env.example         copy to .env for local runs
└── .gitignore
```

## 1. Firebase setup
1. Firebase Console → create project → **Build → Realtime Database → Create database**.
2. Copy the database URL (`https://<project>-default-rtdb.firebaseio.com`) → `FIREBASE_DB_URL`.
3. **Project settings → Service accounts → Generate new private key**.
   - Local: save as `firebase_credentials.json` next to `main.py`.
   - Render: paste the whole JSON into the env var `FIREBASE_CREDENTIALS_JSON`. Never commit it.

## 2. Telegram setup
- `MASTER_BOT_TOKEN` from @BotFather; `OWNER_TELEGRAM_ID` = your numeric ID (@userinfobot).
- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` from https://my.telegram.org.
- Make each admin bot an **admin** in the target groups; add userbot accounts as **members**.
- Session string:
  ```python
  from telethon.sync import TelegramClient
  from telethon.sessions import StringSession
  with TelegramClient(StringSession(), API_ID, API_HASH) as c:
      print(c.session.save())
  ```
- Open the master bot → `/panel` → add admin-bot tokens, session strings, and group IDs (`-100…`). Changes apply within ~20 s.

## 3. Run locally
```
pip install -r requirements.txt
cp .env.example .env      # fill in values
python main.py            # Bot A  (serves http://localhost:10000/health)
TARGET_URLS=http://localhost:10000 PORT=10001 python ping_bot.py   # Bot B (optional)
```

## 4. Deploy on Render (two Web Services, same repo)
Push the repo to GitHub (without credentials), then create **two** services: **New → Web Service**, Free plan, same repo.

### Service A: `reaction-bot-a`
- Build command: `pip install -r requirements.txt`
- Start command: `python main.py`
- Health Check Path: `/health`
- Environment variables:
  | Key | Value |
  |---|---|
  | `MASTER_BOT_TOKEN` | master bot token |
  | `OWNER_TELEGRAM_ID` | your numeric ID |
  | `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | from my.telegram.org |
  | `FIREBASE_DB_URL` | database URL |
  | `FIREBASE_CREDENTIALS_JSON` | full service-account JSON |
  | `PARTNER_URL` | (optional, after B exists) Bot B URL, so A pings B too |

### Service B: `reaction-bot-b-pinger`
- Build command: `pip install -r requirements.txt`
- Start command: `python ping_bot.py`
- Health Check Path: `/health`
- Environment variables:
  | Key | Value |
  |---|---|
  | `TARGET_URLS` | Bot A URL, e.g. `https://reaction-bot-a.onrender.com` (comma-separate for several) |
  | `PING_MIN` / `PING_MAX` | optional, default `240` / `480` |
  | `FIREBASE_DB_URL`, `FIREBASE_CREDENTIALS_JSON` | optional: lets the interval be edited from the master panel (💓 Heartbeat) |

Order: deploy A first, copy its URL into B's `TARGET_URLS`, deploy B, then (optionally) put B's URL into A's `PARTNER_URL`.

## 5. Things to know
- Render's idle timer resets only on **inbound HTTP**. Telegram long-polling doesn't count, which is why Bot B pings `/health` every 240–480 s (random). Interval = Firebase `/config/heartbeat {min,max}` if configured, else env values.
- Bot B itself is also a free Web Service and can sleep. Setting `PARTNER_URL` on A makes the two keep each other awake. For a bulletproof setup, also add a free external pinger (e.g. UptimeRobot on A's `/health`).
- Free instances have monthly hour limits and restart sometimes; all state is in Firebase so restarts are safe.
- Telegram bots can't see other bots' messages, so Bot B is HTTP-only by design.
- Bots can set one reaction per message, from Telegram's standard emoji set (`EMOJI_POOL` in `main.py`).
- Keep volumes modest: aggressive userbot reactions risk FloodWait or account limits.
- Bot tokens and session strings are stored in plain text in your database: keep access locked down.

## 6. Firebase schema
```
/config/welcome_message
/config/buttons/<id>        {label, url}
/config/heartbeat           {min, max}            # seconds (ping_bot interval)
/config/reaction            {userbot_delay_min, userbot_delay_max}
/users/<telegram_id>        {name, joined}
/bots/admin_bots/<bot_id>   {token, name, groups:{<chat_id>:true}}
/bots/user_bots/<user_id>   {session, name, groups:{<chat_id>:true}}
```

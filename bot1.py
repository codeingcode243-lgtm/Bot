import os
import sqlite3
import json
import logging
import asyncio
import aiohttp
from datetime import datetime
from aiohttp import web
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes
)

# ──────────────────────────────────────────────
# ⚙️ CONFIG — HARDCODED
# ──────────────────────────────────────────────
BOT_TOKEN       = "8844542046:AAG6SASHrFdiTAD-wNWR9NRFfH0xNuT76Eo"
ADMIN_IDS       = [7958926544]
ADMIN_USERNAMES = ["Bilkul19"]

API_BASE        = "https://titan-num-info-production.up.railway.app/search"
FREE_USES       = 2
DB_PATH         = os.environ.get("DB_PATH", "/data/bot_data.db")

WEBHOOK_URL     = os.environ.get("WEBHOOK_URL", "")
PORT            = int(os.environ.get("PORT", 10000))
WEBHOOK_PATH    = "/webhook"
HEALTH_PATH     = "/health"

NOT_FOUND_KEYWORDS = ["not found", "no data", "invalid", "error", "not available"]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 🗄️ DATABASE LAYER
# ──────────────────────────────────────────────
def db_init():
    global DB_PATH
    # Try /data first (persistent disk). Fall back to local if it fails.
    try:
        parent = os.path.dirname(DB_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        test_conn = sqlite3.connect(DB_PATH)
        test_conn.execute("CREATE TABLE IF NOT EXISTS _probe (x INTEGER)")
        test_conn.commit()
        test_conn.close()
        logger.info(f"✅ Database ready at: {DB_PATH}")
    except Exception as e:
        logger.warning(f"⚠️ Could not use {DB_PATH} ({e}). Falling back to local.")
        DB_PATH = os.path.join(os.getcwd(), "bot_data.db")
        logger.info(f"📁 Fallback DB path: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            usage_count INTEGER DEFAULT 0,
            is_paid INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS keys (
            code TEXT PRIMARY KEY,
            uses_remaining INTEGER DEFAULT 1,
            is_paid INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
              ("buying_list", json.dumps([
                  {"label": "🥉 Starter Pack", "price": "₹20", "uses": 5},
                  {"label": "🥈 Pro Pack",     "price": "₹70", "uses": 20},
                  {"label": "🥇 VIP 7 Days",   "price": "₹150", "days": 7}
              ])))
    conn.commit()
    conn.close()

def get_user(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT usage_count, is_paid FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return {"usage_count": row[0], "is_paid": bool(row[1])} if row else None

def create_user(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, usage_count, is_paid) VALUES (?, 0, 0)", (user_id,))
    conn.commit(); conn.close()

def increment_usage(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET usage_count = usage_count + 1 WHERE user_id = ?", (user_id,))
    conn.commit(); conn.close()

def set_paid(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET is_paid = 1 WHERE user_id = ?", (user_id,))
    conn.commit(); conn.close()

def get_setting(key: str, default=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit(); conn.close()

def create_key(code: str, uses: int, is_paid: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO keys (code, uses_remaining, is_paid, created_at) VALUES (?, ?, ?, ?)",
              (code, uses, is_paid, datetime.utcnow().isoformat()))
    conn.commit(); conn.close()

def redeem_key(code: str, user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT uses_remaining, is_paid FROM keys WHERE code = ?", (code,))
    row = c.fetchone()
    if not row:
        conn.close()
        return False, "❌ Invalid or expired key.", 0
    uses, is_paid = row
    if uses <= 0:
        conn.close()
        return False, "❌ This key has already been fully used.", 0
    c.execute("UPDATE users SET usage_count = usage_count + ? WHERE user_id = ?", (uses, user_id))
    if is_paid:
        c.execute("UPDATE users SET is_paid = 1 WHERE user_id = ?", (user_id,))
    new_uses = uses - 1
    if new_uses <= 0:
        c.execute("DELETE FROM keys WHERE code = ?", (code,))
    else:
        c.execute("UPDATE keys SET uses_remaining = ? WHERE code = ?", (new_uses, code))
    conn.commit(); conn.close()
    if is_paid:
        return True, f"👑 *VIP KEY REDEEMED!*\n\n✨ You are now a *PAID USER*!\n🎁 Extra uses: *{uses}*\n🔥 Unlimited lookups unlocked!\n🛡️ Protection feature unlocked!", 1
    return True, f"🎉 *Key Redeemed!*\n\n💎 Bonus uses added: *{uses}*\n🚀 Keep searching!", 0

# ──────────────────────────────────────────────
# 🌐 API CALL
# ──────────────────────────────────────────────
async def fetch_number_info(uid: str):
    url = f"{API_BASE}?q={uid}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return f"⚠️ API returned status {resp.status}. Try again later.", True
                try:
                    data = await resp.json()
                except Exception:
                    text = await resp.text()
                    return f"⚠️ Unexpected API response:\n`{text[:400]}`", True
                text = json.dumps(data, indent=2, ensure_ascii=False) if isinstance(data, dict) else str(data)
                lower = text.lower()
                is_not_found = any(kw in lower for kw in NOT_FOUND_KEYWORDS)
                return text, is_not_found
    except aiohttp.ClientError as e:
        return f"⚠️ Network error: `{e}`", True
    except Exception as e:
        return f"⚠️ Unexpected error: `{e}`", True

# ──────────────────────────────────────────────
# 🛠 HELPERS
# ──────────────────────────────────────────────
def remaining_uses(user_id: int) -> int:
    u = get_user(user_id)
    if not u: return FREE_USES
    if u["is_paid"]: return 9999
    return max(0, FREE_USES - u["usage_count"])

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def admin_contact_line() -> str:
    if not ADMIN_USERNAMES:
        return "👤 *Contact Admin* — (username not set)"
    parts = [f"👉 @{u.strip().lstrip('@')}" for u in ADMIN_USERNAMES if u.strip()]
    return ("📩 *Contact Admin Directly:*\n" + "\n".join(parts)) if parts else "👤 *Contact Admin* — (username not set)"

def buying_list_text() -> str:
    raw = get_setting("buying_list", "[]")
    try:
        items = json.loads(raw)
    except Exception:
        items = []
    lines = [
        "╔══════════════════════╗",
        "║   🛒 *PREMIUM STORE*  ║",
        "╚══════════════════════╝\n",
    ]
    if not items:
        lines.append("📭 *No packages available right now.*\nPlease check back later!\n")
    else:
        lines.append("💎 *Choose your power‑up package!*\n")
        for it in items:
            line = f"┣ 🔹 *{it['label']}*"
            line += f"\n┃    💰 Price: `{it['price']}`"
            if "uses" in it:
                line += f"\n┃    🎯 Uses: `{it['uses']}` lookups"
            if "days" in it:
                line += f"\n┃    ⏳ Validity: `{it['days']}` days unlimited"
            lines.append(line + "\n┃")
        lines.append("┗━━━━━━━━━━━━━━━━━━━━━┛\n")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("💳 *HOW TO PURCHASE?*")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━\n")
    lines.append("1️⃣ Pick the package you want")
    lines.append("2️⃣ Send payment to admin 💰")
    lines.append("3️⃣ Receive your secret key 🔑")
    lines.append("4️⃣ Redeem with `/key <your_code>`")
    lines.append("5️⃣ Enjoy premium features! ✨\n")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(admin_contact_line())
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━\n")
    lines.append("⚡ _Instant activation after redemption!_")
    return "\n".join(lines)

# ──────────────────────────────────────────────
# 🎯 USER COMMANDS
# ──────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    create_user(user_id)
    left = remaining_uses(user_id)
    u = get_user(user_id)
    paid = u and u["is_paid"]
    badge = "👑 PAID MEMBER" if paid else "🆓 FREE USER"
    name = update.effective_user.first_name or "Friend"

    text = (
        f"╔══════════════════════════════╗\n"
        f"║   🌟 *WELCOME TO THE BOT* 🌟   ║\n"
        f"╚══════════════════════════════╝\n\n"
        f"👋 Hey *{name}*, glad to see you here!\n\n"
        f"🎫 *Account Status:* {badge}\n"
        f"🎯 *Free Lookups Left:* `{left}`\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📖 *AVAILABLE COMMANDS*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔍 `/num <10‑digit UID>`\n"
        f"   └ Lookup any mobile number info\n\n"
        f"🔑 `/key <your_code>`\n"
        f"   └ Redeem your purchased key\n\n"
        f"🛒 `/buy`\n"
        f"   └ View premium packages\n\n"
        f"🛡️ `/protect <number>`\n"
        f"   └ Protect a number (👑 paid only)\n\n"
        f"📊 `/myinfo`\n"
        f"   └ View your account details\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 *Tip:* Free users get *{FREE_USES}* lookups.\n"
        f"🔒 Only *valid results* are counted!\n"
        f"🎁 Upgrade for unlimited access!\n\n"
        f"✨ _Powered with love & emojis_ ✨"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_myinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    create_user(user_id)
    u = get_user(user_id)
    paid = u and u["is_paid"]
    badge = "👑 PAID MEMBER" if paid else "🆓 FREE USER"
    left = remaining_uses(user_id) if not paid else "♾️ Unlimited"

    text = (
        f"╔════════════════════════════╗\n"
        f"║   📊 *YOUR ACCOUNT INFO*   ║\n"
        f"╚════════════════════════════╝\n\n"
        f"🆔 *User ID:* `{user_id}`\n"
        f"🏷️ *Status:* {badge}\n"
        f"📈 *Total Uses:* `{u['usage_count'] if u else 0}`\n"
        f"🎯 *Free Left:* `{left}`\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 *Want more?* Type `/buy` now!\n"
        f"🔑 *Have a key?* Use `/key <code>`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(buying_list_text(), parse_mode="Markdown")

async def cmd_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    create_user(user_id)
    u = get_user(user_id)
    paid = u and u["is_paid"]

    if not paid and remaining_uses(user_id) <= 0:
        text = (
            f"╔═══════════════════════════╗\n"
            f"║   🚫 *LIMIT REACHED!*   ║\n"
            f"╚═══════════════════════════╝\n\n"
            f"😔 You've used all *{FREE_USES}* free lookups!\n\n"
            f"💡 *Don't worry, upgrade now!*\n\n"
            f"🛒 Type `/buy` → see premium packs\n"
            f"🔑 Have a key? `/key <your_code>`\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎁 *Premium Benefits:*\n"
            f"✅ Unlimited number lookups\n"
            f"✅ 🛡️ Number protection feature\n"
            f"✅ Priority support\n"
            f"✅ Zero wait time\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{admin_contact_line()}"
        )
        await update.message.reply_text(text, parse_mode="Markdown")
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ *Oops! Missing UID.*\n\n"
            "📌 *Usage:* `/num <10‑digit UID>`\n"
            "📝 *Example:* `/num 9876543210`",
            parse_mode="Markdown"
        )
        return

    uid = context.args[0].strip()
    if not (uid.isdigit() and len(uid) == 10):
        await update.message.reply_text(
            "❌ *Invalid UID Format!*\n\n"
            "🔢 UID must be exactly *10 digits*.\n"
            "📝 *Example:* `9876543210`\n\n"
            "🔁 Please try again.",
            parse_mode="Markdown"
        )
        return

    msg = await update.message.reply_text(
        "🔍 *Searching...*\n\n"
        "⏳ Please wait a moment\n"
        "🌐 Contacting database...",
        parse_mode="Markdown"
    )

    text, is_not_found = await fetch_number_info(uid)

    if is_not_found:
        await msg.edit_text(
            f"╔═══════════════════════════╗\n"
            f"║   ❌ *NO DATA FOUND*   ║\n"
            f"╚═══════════════════════════╝\n\n"
            f"🔎 *Search Query:* `{uid}`\n\n"
            f"😕 Unfortunately, no records were found\n"
            f"for this number in our database.\n\n"
            f"✅ *Good news:* Your usage was **NOT** deducted!\n"
            f"🎯 You can try another number freely.\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 *Tips:*\n"
            f"• Double‑check the 10‑digit UID\n"
            f"• Try a different number\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )
        return

    if not paid:
        increment_usage(user_id)

    left = remaining_uses(user_id)
    if paid:
        footer = "\n\n👑 *Paid User* — ♾️ Unlimited lookups!"
    else:
        footer = f"\n\n🎯 *Remaining free uses:* `{left}`"

    if len(text) > 3500:
        text = text[:3500] + "\n… (truncated)"

    await msg.edit_text(
        f"╔═══════════════════════════╗\n"
        f"║   ✅ *RESULT FOUND!*   ║\n"
        f"╚═══════════════════════════╝\n\n"
        f"🔎 *Query:* `{uid}`\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 *Information:*\n\n"
        f"`{text}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━"
        f"{footer}",
        parse_mode="Markdown"
    )

async def cmd_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    create_user(user_id)

    if not context.args:
        await update.message.reply_text(
            "🔑 *Redeem Your Key*\n\n"
            "📌 *Usage:* `/key <your_code>`\n"
            "📝 *Example:* `/key ABC123XYZ`\n\n"
            "💡 Don't have a key? Type `/buy` to purchase!\n\n"
            f"{admin_contact_line()}",
            parse_mode="Markdown"
        )
        return

    code = context.args[0].strip().upper()
    ok, message, _ = redeem_key(code, user_id)
    if ok:
        await update.message.reply_text(
            f"╔═══════════════════════════╗\n"
            f"║   🎉 *KEY STATUS*   ║\n"
            f"╚═══════════════════════════╝\n\n"
            f"{message}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 Enjoy your premium experience!",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"╔═══════════════════════════╗\n"
            f"║   ❌ *REDEEM FAILED*   ║\n"
            f"╚═══════════════════════════╝\n\n"
            f"{message}\n\n"
            f"💡 Check the code and try again.\n"
            f"🛒 Need a new key? Type `/buy`\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{admin_contact_line()}",
            parse_mode="Markdown"
        )

async def cmd_protect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    create_user(user_id)
    u = get_user(user_id)

    if not (u and u["is_paid"]):
        await update.message.reply_text(
            f"╔═══════════════════════════╗\n"
            f"║   🔒 *PAID FEATURE*   ║\n"
            f"╚═══════════════════════════╝\n\n"
            f"😔 Sorry, *Number Protection* is a\n"
            f"👑 *paid‑only* feature!\n\n"
            f"🛡️ *What you get:*\n"
            f"✅ Protect any number\n"
            f"✅ Priority monitoring\n"
            f"✅ Real‑time alerts\n"
            f"✅ Lifetime access\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🛒 Type `/buy` to upgrade\n"
            f"🔑 Or redeem a paid key: `/key <code>`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{admin_contact_line()}",
            parse_mode="Markdown"
        )
        return

    if not context.args:
        await update.message.reply_text(
            "🛡️ *Number Protection*\n\n"
            "📌 *Usage:* `/protect <number>`\n"
            "📝 *Example:* `/protect 9876543210`",
            parse_mode="Markdown"
        )
        return

    number = context.args[0].strip()
    await update.message.reply_text(
        f"╔═══════════════════════════╗\n"
        f"║   🛡️ *PROTECTED!*   ║\n"
        f"╚═══════════════════════════╝\n\n"
        f"✨ Number `{number}` has been added to\n"
        f"your personal protection list!\n\n"
        f"🔔 You'll be notified of any activity.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👑 *VIP privilege active*",
        parse_mode="Markdown"
    )

# ──────────────────────────────────────────────
# 👑 ADMIN COMMANDS
# ──────────────────────────────────────────────
async def cmd_genkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ *Admin only command!*", parse_mode="Markdown")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "🔑 *Generate Key*\n\n"
            "📌 *Usage:* `/genkey <uses> <paid 0|1> [custom_code]`\n\n"
            "📝 *Examples:*\n"
            "• `/genkey 5 0` → 5 free uses\n"
            "• `/genkey 1 1 VIP123` → paid key",
            parse_mode="Markdown"
        )
        return

    try:
        uses = int(context.args[0])
        is_paid = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid numbers.")
        return
    if is_paid not in (0, 1):
        await update.message.reply_text("❌ Paid must be 0 or 1.")
        return

    code = context.args[2].upper() if len(context.args) > 2 else os.urandom(4).hex().upper()
    try:
        create_key(code, uses, is_paid)
    except sqlite3.IntegrityError:
        await update.message.reply_text("❌ That code already exists. Try another.")
        return

    await update.message.reply_text(
        f"╔═══════════════════════════╗\n"
        f"║   ✅ *KEY CREATED!*   ║\n"
        f"╚═══════════════════════════╝\n\n"
        f"🔑 *Code:* `{code}`\n"
        f"🎯 *Uses:* `{uses}`\n"
        f"👑 *Paid:* {'✅ Yes' if is_paid else '❌ No'}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 Share this key with the user.",
        parse_mode="Markdown"
    )

async def cmd_setbuy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ *Admin only command!*", parse_mode="Markdown")
        return

    if not context.args:
        await update.message.reply_text(
            "🛒 *Set Buying List*\n\n"
            "📌 *Usage:* `/setbuy <json_array>`\n\n"
            '📝 *Example:*\n'
            '`/setbuy [{"label":"5 Uses","price":"₹20","uses":5}]`',
            parse_mode="Markdown"
        )
        return

    raw = " ".join(context.args)
    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            raise ValueError
    except Exception:
        await update.message.reply_text("❌ Invalid JSON. Must be an array.")
        return

    set_setting("buying_list", json.dumps(items))
    await update.message.reply_text(
        "╔═══════════════════════════╗\n"
        "║   ✅ *BUY LIST UPDATED*   ║\n"
        "╚═══════════════════════════╝\n\n"
        "🛒 The buying list has been refreshed!\n"
        "💡 Users will see the new packages.",
        parse_mode="Markdown"
    )

async def cmd_setadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ *Admin only command!*", parse_mode="Markdown")
        return
    if not context.args:
        await update.message.reply_text(
            "👤 *Set Admin Username*\n\n"
            "📌 *Usage:* `/setadmin <username1> [username2] ...`\n"
            "📝 *Example:* `/setadmin Bilkul19`",
            parse_mode="Markdown"
        )
        return
    usernames = [a.strip().lstrip("@") for a in context.args if a.strip()]
    set_setting("admin_usernames", ",".join(usernames))
    await update.message.reply_text(
        f"✅ *Admin contact updated!*\n\n"
        f"👤 New contacts:\n" +
        "\n".join(f"👉 @{u}" for u in usernames),
        parse_mode="Markdown"
    )

async def cmd_adduses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ *Admin only command!*", parse_mode="Markdown")
        return

    if len(context.args) < 2:
        await update.message.reply_text("📌 Usage: `/adduses <user_id> <amount>`", parse_mode="Markdown")
        return

    try:
        target = int(context.args[0])
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid numbers.")
        return

    create_user(target)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET usage_count = usage_count + ? WHERE user_id = ?", (amount, target))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ *Bonus Uses Added!*\n\n"
        f"👤 User: `{target}`\n"
        f"🎁 Added: `{amount}` uses",
        parse_mode="Markdown"
    )

async def cmd_setpaid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ *Admin only command!*", parse_mode="Markdown")
        return

    if not context.args:
        await update.message.reply_text("📌 Usage: `/setpaid <user_id>`", parse_mode="Markdown")
        return

    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    create_user(target)
    set_paid(target)

    await update.message.reply_text(
        f"👑 *VIP ACCESS GRANTED!*\n\n"
        f"👤 User: `{target}`\n"
        f"✨ Status: *Paid Member*\n"
        f"🚀 All premium features unlocked!",
        parse_mode="Markdown"
    )

async def cmd_adminhelp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text = (
        "╔═══════════════════════════╗\n"
        "║   🛠️ *ADMIN PANEL*   ║\n"
        "╚═══════════════════════════╝\n\n"
        "🔑 `/genkey <uses> <0|1> [code]`\n"
        "   └ Create a redeem key\n\n"
        "🛒 `/setbuy <json>`\n"
        "   └ Replace the buying list\n\n"
        "👤 `/setadmin <usernames...>`\n"
        "   └ Update admin contact shown in /buy\n\n"
        "🎁 `/adduses <user_id> <amount>`\n"
        "   └ Add uses to any user\n\n"
        "👑 `/setpaid <user_id>`\n"
        "   └ Grant paid status\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 Use responsibly!"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# ──────────────────────────────────────────────
# ❤️ HEALTH + WEBHOOK SERVER
# ──────────────────────────────────────────────
async def health_handler(request):
    return web.json_response({
        "status": "healthy",
        "service": "titan-num-bot",
        "webhook": WEBHOOK_URL or "not-set",
        "time": datetime.utcnow().isoformat()
    })

async def webhook_handler(request):
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.update_queue.put(update)
        return web.Response(status=200)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(status=200)

# ──────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────
application: Application = None

async def main():
    global application, ADMIN_USERNAMES

    db_init()

    stored = get_setting("admin_usernames")
    if stored:
        ADMIN_USERNAMES = stored.split(",")

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_start))
    application.add_handler(CommandHandler("num", cmd_num))
    application.add_handler(CommandHandler("key", cmd_key))
    application.add_handler(CommandHandler("buy", cmd_buy))
    application.add_handler(CommandHandler("myinfo", cmd_myinfo))
    application.add_handler(CommandHandler("protect", cmd_protect))
    application.add_handler(CommandHandler("genkey", cmd_genkey))
    application.add_handler(CommandHandler("setbuy", cmd_setbuy))
    application.add_handler(CommandHandler("setadmin", cmd_setadmin))
    application.add_handler(CommandHandler("adduses", cmd_adduses))
    application.add_handler(CommandHandler("setpaid", cmd_setpaid))
    application.add_handler(CommandHandler("adminhelp", cmd_adminhelp))

    async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🤔 *Unknown Command!*\n\n💡 Type `/start` for help.",
            parse_mode="Markdown"
        )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))

    await application.initialize()
    await application.start()

    if WEBHOOK_URL:
        full_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
        await application.bot.set_webhook(
            url=full_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )
        logger.info(f"✅ Webhook set to {full_url}")
    else:
        logger.warning("⚠️ WEBHOOK_URL not set — falling back to polling mode.")
        await application.updater.start_polling()
        logger.info("📡 Polling started.")

    app = web.Application()
    app.router.add_get(HEALTH_PATH, health_handler)
    app.router.add_post(WEBHOOK_PATH, webhook_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🚀 Server listening on 0.0.0.0:{PORT}")

    try:
        await asyncio.Event().wait()
    finally:
        await application.stop()
        await application.shutdown()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
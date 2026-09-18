import os
import json
import logging
import asyncio
import random
import string
import aiohttp
from datetime import datetime
from aiohttp import web
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes
)
from motor.motor_asyncio import AsyncIOMotorClient
import certifi

# ──────────────────────────────────────────────
# ⚙️ CONFIG
# ──────────────────────────────────────────────
BOT_TOKEN       = "8844542046:AAG6SASHrFdiTAD-wNWR9NRFfH0xNuT76Eo"
ADMIN_IDS       = [7958926544]
ADMIN_USERNAMES = ["Bilkul19"]

# ✅ NEW API
API_BASE        = "https://numinfotitan.vercel.app/search"
API_KEY         = "TITANKENG"

FREE_USES       = 2

MONGO_URI       = os.environ.get(
    "MONGO_URI",
    "mongodb+srv://tejasmrt123_db_user:i5pS1UTDzsabSmf6@cluster0.7adzohs.mongodb.net/?appName=Cluster0"
)
DB_NAME         = "titan_bot"

WEBHOOK_URL     = os.environ.get("WEBHOOK_URL", "")
PORT            = int(os.environ.get("PORT", 10000))
WEBHOOK_PATH    = "/webhook"
HEALTH_PATH     = "/health"

NOT_FOUND_KEYWORDS = ["not found", "no data", "invalid", "error", "not available"]

START_TIME = datetime.utcnow()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 🗄️ MONGODB LAYER
# ──────────────────────────────────────────────
mongo_client = None
db = None
users_col = None
keys_col = None
settings_col = None
protections_col = None


async def db_init():
    global mongo_client, db, users_col, keys_col, settings_col, protections_col
    try:
        mongo_client = AsyncIOMotorClient(
            MONGO_URI,
            serverSelectionTimeoutMS=10000,
            tlsCAFile=certifi.where()
        )
        await mongo_client.admin.command("ping")
        db = mongo_client[DB_NAME]
        users_col = db["users"]
        keys_col = db["keys"]
        settings_col = db["settings"]
        protections_col = db["protections"]

        await users_col.create_index("user_id", unique=True)
        await keys_col.create_index("code", unique=True)
        await protections_col.create_index([("user_id", 1), ("number", 1)], unique=True)
        await protections_col.create_index("number")

        existing = await settings_col.find_one({"_id": "buying_list"})
        if not existing:
            await settings_col.insert_one({
                "_id": "buying_list",
                "items": [
                    {"label": "🥉 Starter Pack", "price": "₹20", "uses": 5},
                    {"label": "🥈 Pro Pack",     "price": "₹70", "uses": 20},
                    {"label": "🥇 VIP 7 Days",   "price": "₹150", "days": 7}
                ]
            })

        logger.info(f"✅ MongoDB connected → db={DB_NAME}")
    except Exception as e:
        logger.error(f"❌ MongoDB connection failed: {e}")
        raise


# ── USERS ──
async def get_user(user_id: int):
    doc = await users_col.find_one({"user_id": user_id})
    if doc:
        return {"usage_count": doc.get("usage_count", 0), "is_paid": bool(doc.get("is_paid", 0))}
    return None


async def create_user(user_id: int):
    await users_col.update_one(
        {"user_id": user_id},
        {"$setOnInsert": {"user_id": user_id, "usage_count": 0, "is_paid": 0}},
        upsert=True
    )


async def increment_usage(user_id: int):
    await users_col.update_one({"user_id": user_id}, {"$inc": {"usage_count": 1}})


async def set_paid(user_id: int):
    await users_col.update_one({"user_id": user_id}, {"$set": {"is_paid": 1}}, upsert=True)


async def add_uses(user_id: int, amount: int):
    await users_col.update_one(
        {"user_id": user_id},
        {"$inc": {"usage_count": -amount}},
        upsert=True
    )


# ── SETTINGS ──
async def get_setting(key: str, default=None):
    doc = await settings_col.find_one({"_id": key})
    if not doc:
        return default
    return doc.get("value", default)


async def get_buying_list():
    doc = await settings_col.find_one({"_id": "buying_list"})
    if doc and "items" in doc:
        return doc["items"]
    return []


async def set_buying_list(items):
    await settings_col.update_one(
        {"_id": "buying_list"},
        {"$set": {"items": items}},
        upsert=True
    )


# ── KEYS ──
async def create_key(code: str, uses: int, is_paid: int):
    existing = await keys_col.find_one({"code": code})
    if existing:
        raise ValueError(f"Key {code} already exists")
    await keys_col.insert_one({
        "code": code,
        "uses_remaining": uses,
        "is_paid": is_paid,
        "created_at": datetime.utcnow().isoformat()
    })


async def redeem_key(code: str, user_id: int):
    doc = await keys_col.find_one({"code": code})
    if not doc:
        return False, "❌ Invalid or expired key.", 0
    uses = doc.get("uses_remaining", 0)
    is_paid = doc.get("is_paid", 0)
    if uses <= 0:
        await keys_col.delete_one({"code": code})
        return False, "❌ This key has already been used.", 0

    await keys_col.delete_one({"code": code})

    await create_user(user_id)
    await users_col.update_one(
        {"user_id": user_id},
        {"$inc": {"usage_count": -uses}}
    )
    if is_paid:
        await users_col.update_one({"user_id": user_id}, {"$set": {"is_paid": 1}})

    logger.info(f"✅ Key {code} redeemed by {user_id} | +{uses} uses | paid={is_paid}")

    if is_paid:
        return True, f"👑 *VIP KEY REDEEMED!*\n\n✨ You are now a *PAID USER*!\n🎁 Extra uses: *{uses}*\n🔥 Unlimited lookups unlocked!\n🛡️ Protection feature unlocked!", 1
    return True, f"🎉 *Key Redeemed!*\n\n💎 Bonus uses added: *{uses}*\n🚀 Keep searching!", 0


async def list_keys():
    cursor = keys_col.find({}).sort("created_at", -1)
    return await cursor.to_list(length=200)


# ── PROTECTIONS ──
async def protect_number(user_id: int, number: str):
    existing = await protections_col.find_one({"user_id": user_id, "number": number})
    if existing:
        return False, f"⚠️ Number `{number}` is already protected."
    await protections_col.insert_one({
        "user_id": user_id,
        "number": number,
        "protected_at": datetime.utcnow().isoformat()
    })
    return True, f"🛡️ Number `{number}` is now protected!"


async def unprotect_number(user_id: int, number: str):
    result = await protections_col.delete_one({"user_id": user_id, "number": number})
    if result.deleted_count == 0:
        return False, f"❌ Number `{number}` is not in your protection list."
    return True, f"🗑️ Number `{number}` removed from protection."


async def get_protected_numbers(user_id: int):
    cursor = protections_col.find({"user_id": user_id}).sort("protected_at", -1)
    return await cursor.to_list(length=100)


async def count_protected(user_id: int) -> int:
    return await protections_col.count_documents({"user_id": user_id})


async def get_all_protections():
    cursor = protections_col.find({}).sort("protected_at", -1)
    return await cursor.to_list(length=500)


async def get_protections_for_number(number: str):
    cursor = protections_col.find({"number": number})
    return await cursor.to_list(length=100)


# ──────────────────────────────────────────────
# 🌐 API CALL — NEW ENDPOINT
# ──────────────────────────────────────────────
async def fetch_number_info(uid: str):
    url = f"{API_BASE}?key={API_KEY}&num={uid}"
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

                # Format response — pretty JSON for dicts
                if isinstance(data, dict):
                    text = json.dumps(data, indent=2, ensure_ascii=False)
                else:
                    text = str(data)

                # Detect not-found / error responses
                lower = text.lower()
                is_not_found = any(kw in lower for kw in NOT_FOUND_KEYWORDS)

                if isinstance(data, dict):
                    if data.get("status") in ("error", "failed", False):
                        is_not_found = True
                    if data.get("success") is False:
                        is_not_found = True
                    if not data:
                        is_not_found = True

                return text, is_not_found
    except aiohttp.ClientError as e:
        return f"⚠️ Network error: `{e}`", True
    except Exception as e:
        return f"⚠️ Unexpected error: `{e}`", True


# ──────────────────────────────────────────────
# 🛠 HELPERS
# ──────────────────────────────────────────────
async def remaining_uses(user_id: int) -> int:
    u = await get_user(user_id)
    if not u:
        return FREE_USES
    if u["is_paid"]:
        return 9999
    return max(0, FREE_USES - u["usage_count"])


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def admin_contact_line() -> str:
    if not ADMIN_USERNAMES:
        return "👤 *Contact Admin* — (username not set)"
    parts = [f"👉 @{u.strip().lstrip('@')}" for u in ADMIN_USERNAMES if u.strip()]
    return ("📩 *Contact Admin Directly:*\n" + "\n".join(parts)) if parts else "👤 *Contact Admin* — (username not set)"


async def buying_list_text() -> str:
    items = await get_buying_list()
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


def generate_key_code(length: int = 8) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


# ──────────────────────────────────────────────
# 🎯 USER COMMANDS
# ──────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await create_user(user_id)
    left = await remaining_uses(user_id)
    u = await get_user(user_id)
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
        f"🔎 `/myprotections`\n"
        f"   └ View protected numbers (👑 paid only)\n\n"
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
    await create_user(user_id)
    u = await get_user(user_id)
    paid = u and u["is_paid"]
    badge = "👑 PAID MEMBER" if paid else "🆓 FREE USER"
    left = await remaining_uses(user_id) if not paid else "♾️ Unlimited"
    prot_count = await count_protected(user_id)

    text = (
        f"╔════════════════════════════╗\n"
        f"║   📊 *YOUR ACCOUNT INFO*   ║\n"
        f"╚════════════════════════════╝\n\n"
        f"🆔 *User ID:* `{user_id}`\n"
        f"🏷️ *Status:* {badge}\n"
        f"📈 *Total Uses:* `{u['usage_count'] if u else 0}`\n"
        f"🎯 *Free Left:* `{left}`\n"
        f"🛡️ *Protected:* `{prot_count}` numbers\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 *Want more?* Type `/buy` now!\n"
        f"🔑 *Have a key?* Use `/key <code>`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await buying_list_text(), parse_mode="Markdown")


async def cmd_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await create_user(user_id)
    u = await get_user(user_id)
    paid = u and u["is_paid"]

    if not paid and await remaining_uses(user_id) <= 0:
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

    # 🛡️ CHECK IF NUMBER IS PROTECTED
    try:
        protectors = await get_protections_for_number(uid)
    except Exception as e:
        logger.error(f"❌ Protection check error: {e}")
        protectors = []

    if protectors:
        protector_ids = [p["user_id"] for p in protectors]
        is_requester_protector = user_id in protector_ids

        if not is_requester_protector and not is_admin(user_id):
            await update.message.reply_text(
                f"╔═══════════════════════════╗\n"
                f"║   🛡️ *PROTECTED NUMBER*   ║\n"
                f"╚═══════════════════════════╝\n\n"
                f"🔒 The number `{uid}` is *protected*\n"
                f"by a VIP user and cannot be looked up.\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 *Why?*\n"
                f"This number's owner has enabled\n"
                f"privacy protection via our VIP plan.\n\n"
                f"👑 *Want to protect your number too?*\n"
                f"Type `/buy` to see our premium plans!\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{admin_contact_line()}",
                parse_mode="Markdown"
            )

            for p in protectors:
                try:
                    protector_id = p["user_id"]
                    if protector_id == user_id:
                        continue
                    requester_name = update.effective_user.first_name or "Someone"
                    requester_username = update.effective_user.username
                    requester_tag = f"@{requester_username}" if requester_username else f"`{user_id}`"

                    await context.bot.send_message(
                        chat_id=protector_id,
                        text=(
                            f"🔔 *PROTECTION ALERT!*\n\n"
                            f"Someone just tried to look up your\n"
                            f"protected number `{uid}`!\n\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"👤 *Who:* {requester_name} ({requester_tag})\n"
                            f"🆔 *User ID:* `{user_id}`\n"
                            f"📱 *Number:* `{uid}`\n"
                            f"🕐 *Time:* `{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}`\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🛡️ Your protection is working!\n"
                            f"✅ Lookup was *blocked*."
                        ),
                        parse_mode="Markdown"
                    )
                    logger.info(f"🔔 Notified protector {protector_id} about lookup attempt by {user_id}")
                except Exception as e:
                    logger.warning(f"⚠️ Could not notify protector {p['user_id']}: {e}")
            return

    # ✅ NORMAL LOOKUP
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
        await increment_usage(user_id)

    left = await remaining_uses(user_id)
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
    await create_user(user_id)

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
    ok, message, _ = await redeem_key(code, user_id)
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
    await create_user(user_id)
    u = await get_user(user_id)

    if not (u and u["is_paid"]):
        await update.message.reply_text(
            f"╔═══════════════════════════╗\n"
            f"║   🔒 *PAID FEATURE*   ║\n"
            f"╚═══════════════════════════╝\n\n"
            f"😔 Sorry, *Number Protection* is a\n"
            f"👑 *paid‑only* feature!\n\n"
            f"🛡️ *What you get:*\n"
            f"✅ Protect unlimited numbers\n"
            f"✅ Block lookups by others\n"
            f"✅ Get notified when someone tries\n"
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
        count = await count_protected(user_id)
        await update.message.reply_text(
            f"🛡️ *Number Protection*\n\n"
            f"📊 *Currently protecting:* `{count}` numbers\n\n"
            f"📌 *Usage:* `/protect <number>`\n"
            f"📝 *Example:* `/protect 9876543210`\n\n"
            f"🔎 `/myprotections` — view all\n"
            f"🗑️ `/unprotect <number>` — remove",
            parse_mode="Markdown"
        )
        return

    number = context.args[0].strip()
    if not number.isdigit() or not (7 <= len(number) <= 15):
        await update.message.reply_text(
            "❌ *Invalid Number!*\n\n"
            "🔢 Number must be only digits\n"
            "📏 Length: 7 to 15 digits\n\n"
            "📝 *Example:* `/protect 9876543210`",
            parse_mode="Markdown"
        )
        return

    ok, message = await protect_number(user_id, number)

    if ok:
        count = await count_protected(user_id)
        await update.message.reply_text(
            f"╔═══════════════════════════╗\n"
            f"║   🛡️ *PROTECTED!*   ║\n"
            f"╚═══════════════════════════╝\n\n"
            f"✨ Number `{number}` has been added to\n"
            f"your personal protection list!\n\n"
            f"📊 *Total protected:* `{count}` numbers\n\n"
            f"🔔 You'll be notified when someone tries\n"
            f"to look up this number.\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 *VIP privilege active*\n\n"
            f"🔎 `/myprotections` — view all\n"
            f"🗑️ `/unprotect {number}` — remove",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(message, parse_mode="Markdown")


async def cmd_myprotections(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await create_user(user_id)
    u = await get_user(user_id)

    if not (u and u["is_paid"]):
        await update.message.reply_text("🔒 *Paid‑only feature!* Type `/buy`.", parse_mode="Markdown")
        return

    numbers = await get_protected_numbers(user_id)

    if not numbers:
        await update.message.reply_text(
            f"╔═══════════════════════════╗\n"
            f"║   📭 *NO PROTECTIONS*   ║\n"
            f"╚═══════════════════════════╝\n\n"
            f"😕 You haven't protected any numbers yet.\n\n"
            f"💡 Add one with:\n"
            f"`/protect <number>`\n\n"
            f"📝 *Example:* `/protect 9876543210`",
            parse_mode="Markdown"
        )
        return

    lines = [
        "╔═══════════════════════════╗",
        "║   🛡️ *YOUR PROTECTIONS*   ║",
        "╚═══════════════════════════╝\n"
    ]
    for i, p in enumerate(numbers, 1):
        lines.append(f"{i}. 📱 `{p['number']}`")
    lines.append(f"\n📊 *Total:* `{len(numbers)}` numbers")
    lines.append("\n🗑️ Remove one with `/unprotect <number>`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_unprotect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await create_user(user_id)
    u = await get_user(user_id)

    if not (u and u["is_paid"]):
        await update.message.reply_text("🔒 *Paid‑only feature!* Type `/buy`.", parse_mode="Markdown")
        return

    if not context.args:
        await update.message.reply_text(
            "🗑️ *Unprotect a Number*\n\n"
            "📌 *Usage:* `/unprotect <number>`\n"
            "📝 *Example:* `/unprotect 9876543210`",
            parse_mode="Markdown"
        )
        return

    number = context.args[0].strip()
    ok, message = await unprotect_number(user_id, number)
    count = await count_protected(user_id)

    if ok:
        await update.message.reply_text(
            f"🗑️ *UNPROTECTED*\n\n"
            f"{message}\n\n"
            f"📊 *Remaining:* `{count}` numbers",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(message, parse_mode="Markdown")


# ──────────────────────────────────────────────
# 👑 ADMIN COMMANDS
# ──────────────────────────────────────────────
async def cmd_genkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"🔑 /genkey called by {user_id} | args={context.args}")

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
        await update.message.reply_text("❌ Invalid numbers.", parse_mode="Markdown")
        return
    if uses <= 0:
        await update.message.reply_text("❌ Uses must be greater than 0.")
        return
    if is_paid not in (0, 1):
        await update.message.reply_text("❌ Paid must be 0 or 1.")
        return

    code = context.args[2].upper() if len(context.args) > 2 else generate_key_code()

    try:
        await create_key(code, uses, is_paid)
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}", parse_mode="Markdown")
        return
    except Exception as e:
        logger.error(f"❌ Key creation error: {e}")
        await update.message.reply_text(f"❌ Failed to create key: `{e}`", parse_mode="Markdown")
        return

    await update.message.reply_text(
        f"╔═══════════════════════════╗\n"
        f"║   ✅ *KEY CREATED!*   ║\n"
        f"╚═══════════════════════════╝\n\n"
        f"🔑 *Code:* `{code}`\n"
        f"🎯 *Uses:* `{uses}`\n"
        f"👑 *Paid:* {'✅ Yes' if is_paid else '❌ No'}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 Send this code to the user.\n"
        f"📋 They redeem with `/key {code}`",
        parse_mode="Markdown"
    )


async def cmd_keylist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ *Admin only command!*", parse_mode="Markdown")
        return

    keys = await list_keys()
    if not keys:
        await update.message.reply_text("📭 *No active keys.*\n\n💡 Create one with `/genkey`.", parse_mode="Markdown")
        return

    lines = [
        "╔═══════════════════════════╗",
        "║   🔑 *ACTIVE KEYS*   ║",
        "╚═══════════════════════════╝\n"
    ]
    for k in keys[:50]:
        badge = "👑" if k.get("is_paid") else "🆓"
        lines.append(f"{badge} `{k['code']}` — {k.get('uses_remaining', 0)} uses")
    lines.append(f"\n📊 *Total:* {len(keys)} keys")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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

    await set_buying_list(items)
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
    await settings_col.update_one(
        {"_id": "admin_usernames"},
        {"$set": {"value": ",".join(usernames)}},
        upsert=True
    )
    global ADMIN_USERNAMES
    ADMIN_USERNAMES = usernames
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

    await create_user(target)
    await add_uses(target, amount)

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

    await create_user(target)
    await set_paid(target)

    await update.message.reply_text(
        f"👑 *VIP ACCESS GRANTED!*\n\n"
        f"👤 User: `{target}`\n"
        f"✨ Status: *Paid Member*\n"
        f"🚀 All premium features unlocked!",
        parse_mode="Markdown"
    )


async def cmd_allprotections(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ *Admin only command!*", parse_mode="Markdown")
        return

    protections = await get_all_protections()

    if not protections:
        await update.message.reply_text("📭 *No protections yet.*", parse_mode="Markdown")
        return

    lines = [
        "╔═══════════════════════════╗",
        "║   🛡️ *ALL PROTECTIONS*   ║",
        "╚═══════════════════════════╝\n"
    ]
    by_user = {}
    for p in protections:
        by_user.setdefault(p["user_id"], []).append(p["number"])

    for uid, numbers in by_user.items():
        lines.append(f"👤 `{uid}` — {len(numbers)} numbers")
        for n in numbers[:5]:
            lines.append(f"   └ `{n}`")
        if len(numbers) > 5:
            lines.append(f"   └ _…and {len(numbers) - 5} more_")
        lines.append("")

    lines.append(f"📊 *Total:* `{len(protections)}` protections")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n… (truncated)"

    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_adminhelp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text = (
        "╔═══════════════════════════╗\n"
        "║   🛠️ *ADMIN PANEL*   ║\n"
        "╚═══════════════════════════╝\n\n"
        "🔑 `/genkey <uses> <0|1> [code]`\n"
        "   └ Create a redeem key\n\n"
        "📋 `/keylist`\n"
        "   └ View all active keys\n\n"
        "🛒 `/setbuy <json>`\n"
        "   └ Replace the buying list\n\n"
        "👤 `/setadmin <usernames...>`\n"
        "   └ Update admin contact shown in /buy\n\n"
        "🎁 `/adduses <user_id> <amount>`\n"
        "   └ Add uses to any user\n\n"
        "👑 `/setpaid <user_id>`\n"
        "   └ Grant paid status\n\n"
        "🛡️ `/allprotections`\n"
        "   └ View all user protections\n\n"
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
        "db": "mongodb",
        "api": API_BASE,
        "webhook": WEBHOOK_URL or "not-set",
        "uptime_min": round((datetime.utcnow() - START_TIME).total_seconds() / 60, 1),
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
# 🔄 SELF-PING
# ──────────────────────────────────────────────
async def self_ping_loop():
    if not WEBHOOK_URL:
        logger.warning("⚠️ WEBHOOK_URL not set — self-ping disabled.")
        return

    ping_url = f"{WEBHOOK_URL}{HEALTH_PATH}"
    logger.info(f"🔄 Self-ping enabled → {ping_url}")
    logger.info("⏱️  Interval: every 5 minutes (300s)")

    await asyncio.sleep(30)

    ping_count = 0
    fail_count = 0

    while True:
        ping_count += 1
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(ping_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    uptime = (datetime.utcnow() - START_TIME).total_seconds() / 60
                    logger.info(
                        f"💓 Self-ping #{ping_count} OK "
                        f"(status {resp.status}, uptime {uptime:.1f} min)"
                    )
                    fail_count = 0
        except asyncio.TimeoutError:
            fail_count += 1
            logger.warning(f"⏱️ Self-ping #{ping_count} timed out (fail #{fail_count})")
        except aiohttp.ClientError as e:
            fail_count += 1
            logger.warning(f"⚠️ Self-ping #{ping_count} network error: {e} (fail #{fail_count})")
        except Exception as e:
            fail_count += 1
            logger.warning(f"⚠️ Self-ping #{ping_count} failed: {e} (fail #{fail_count})")

        if fail_count >= 3:
            logger.error(f"🚨 Self-ping failed {fail_count} times in a row!")

        await asyncio.sleep(300)


# ──────────────────────────────────────────────
# 🚀 MAIN
# ──────────────────────────────────────────────
application: Application = None


async def main():
    global application, ADMIN_USERNAMES

    await db_init()

    stored = await get_setting("admin_usernames")
    if stored:
        ADMIN_USERNAMES = stored.split(",")
        logger.info(f"👤 Loaded admin usernames from DB: {ADMIN_USERNAMES}")

    application = Application.builder().token(BOT_TOKEN).build()

    # User commands
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_start))
    application.add_handler(CommandHandler("num", cmd_num))
    application.add_handler(CommandHandler("key", cmd_key))
    application.add_handler(CommandHandler("buy", cmd_buy))
    application.add_handler(CommandHandler("myinfo", cmd_myinfo))
    application.add_handler(CommandHandler("protect", cmd_protect))
    application.add_handler(CommandHandler("myprotections", cmd_myprotections))
    application.add_handler(CommandHandler("unprotect", cmd_unprotect))

    # Admin commands
    application.add_handler(CommandHandler("genkey", cmd_genkey))
    application.add_handler(CommandHandler("keylist", cmd_keylist))
    application.add_handler(CommandHandler("setbuy", cmd_setbuy))
    application.add_handler(CommandHandler("setadmin", cmd_setadmin))
    application.add_handler(CommandHandler("adduses", cmd_adduses))
    application.add_handler(CommandHandler("setpaid", cmd_setpaid))
    application.add_handler(CommandHandler("allprotections", cmd_allprotections))
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
        logger.warning("⚠️ WEBHOOK_URL not set — falling back to polling.")
        await application.updater.start_polling()

    app = web.Application()
    app.router.add_get(HEALTH_PATH, health_handler)
    app.router.add_post(WEBHOOK_PATH, webhook_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🚀 Server listening on 0.0.0.0:{PORT}")

    ping_task = asyncio.create_task(self_ping_loop())

    try:
        await asyncio.Event().wait()
    finally:
        ping_task.cancel()
        try:
            await ping_task
        except asyncio.CancelledError:
            pass
        await application.stop()
        await application.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
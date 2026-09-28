import asyncio
import csv
import io
import logging
import re
import os
import secrets
import shutil
import sqlite3
from datetime import datetime, timezone
from urllib.parse import quote_plus

import qrcode
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile
)

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8916701290:AAHPixl7djvo8sI1OwE73xNHZhgYNEH5v7I")
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "6863389453").split(",")
    if x.strip().isdigit()
}
DB_FILE = os.getenv("DB_FILE", "bot.db")

# Default payment settings. Change these from the admin panel.
DEFAULT_UPI_ID = os.getenv("UPI_ID", "adityagupta0018@axl")
DEFAULT_UPI_NAME = os.getenv("UPI_NAME", "ADITYA GUPTA")
DEFAULT_COIN_COST = 10

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()

# Security-code request state.
security_wait_tasks = set()

# ============================================================
# DATABASE
# ============================================================
db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row
cur = db.cursor()

cur.executescript("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    coins INTEGER DEFAULT 0,
    joined_at TEXT
);

CREATE TABLE IF NOT EXISTS packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coins INTEGER NOT NULL,
    price INTEGER NOT NULL,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    package_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT DEFAULT 'PENDING',
    screenshot_file_id TEXT,
    created_at TEXT,
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS generated_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS security_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    status TEXT DEFAULT 'PENDING',
    created_at TEXT NOT NULL,
    completed_at TEXT,
    result_code TEXT
);

CREATE TABLE IF NOT EXISTS admin_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    details TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS coin_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    action TEXT NOT NULL,
    admin_id INTEGER,
    created_at TEXT
);
""")
db.commit()

# Safe migration for older databases
try:
    cur.execute("ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")
    db.commit()
except sqlite3.OperationalError:
    pass

def get_setting(key, default=None):
    row = cur.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def set_setting(key, value):
    cur.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value))
    )
    db.commit()

set_setting("upi_id", get_setting("upi_id", DEFAULT_UPI_ID))
set_setting("upi_name", get_setting("upi_name", DEFAULT_UPI_NAME))
set_setting("code_cost", get_setting("code_cost", DEFAULT_COIN_COST))
set_setting("maintenance_mode", get_setting("maintenance_mode", "0"))
set_setting("channel_links", get_setting("channel_links", "Not configured yet."))
set_setting("force_subscribe", get_setting("force_subscribe", "0"))
set_setting("support_link", get_setting("support_link", "@CURRENTTTTTTTT"))
set_setting("welcome_message", get_setting("welcome_message", "🔐 <b>SECURITY VAULT</b> ✦"))
if cur.execute("SELECT COUNT(*) AS c FROM packages").fetchone()["c"] == 0:
    cur.execute("INSERT INTO packages(coins,price) VALUES(10,89)")
    cur.execute("INSERT INTO packages(coins,price) VALUES(50,399)")
    cur.execute("INSERT INTO packages(coins,price) VALUES(100,699)")
    db.commit()

def now():
    return datetime.now(timezone.utc).isoformat()

def is_admin(user_id):
    return user_id in ADMIN_IDS

def register_user(user: Message):
    u = user.from_user
    exists = cur.execute("SELECT user_id FROM users WHERE user_id=?", (u.id,)).fetchone()
    if not exists:
        cur.execute(
            "INSERT INTO users(user_id,username,first_name,coins,joined_at) VALUES(?,?,?,?,?)",
            (u.id, u.username or "", u.first_name or "", 0, now())
        )
    else:
        cur.execute(
            "UPDATE users SET username=?, first_name=? WHERE user_id=?",
            (u.username or "", u.first_name or "", u.id)
        )
    db.commit()

def maintenance_enabled():
    return get_setting("maintenance_mode", "0") == "1"


def user_allowed(user_id):
    if is_admin(user_id):
        return True
    row = cur.execute("SELECT banned FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row and row["banned"]:
        return False
    return not maintenance_enabled()


def log_admin(admin_id, action, details=""):
    cur.execute("INSERT INTO admin_logs(admin_id,action,details,created_at) VALUES(?,?,?,?)", (admin_id, action, details, now()))
    db.commit()


def record_coin_change(user_id, amount, action, admin_id=None):
    cur.execute(
        "INSERT INTO coin_history(user_id,amount,action,admin_id,created_at) VALUES(?,?,?,?,?)",
        (user_id, amount, action, admin_id, now())
    )
    db.commit()


def force_subscribe_enabled():
    return get_setting("force_subscribe", "0") == "1"

def extract_channel_usernames(text):
    found = []
    for m in re.findall(r"@([A-Za-z0-9_]{5,32})|t\.me/([A-Za-z0-9_]{5,32})", text or ""):
        username = m[0] or m[1]
        if username and username.lower() not in {x.lower() for x in found}:
            found.append(username)
    return found

async def user_has_required_subscription(user_id):
    if not force_subscribe_enabled():
        return True
    channels = extract_channel_usernames(get_setting("channel_links", ""))
    if not channels:
        return True

    for username in channels:
        try:
            member = await bot.get_chat_member(f"@{username}", user_id)
            status = getattr(member, "status", "")
            status = getattr(status, "value", status) or ""
            status = str(status).lower()

            if status in {"member", "administrator", "creator", "owner"}:
                continue
            if status == "restricted" and bool(getattr(member, "is_member", False)):
                continue

            return False
        except Exception:
            return False

    return True

def force_subscribe_keyboard():
    channels = extract_channel_usernames(get_setting("channel_links", ""))
    rows = []
    for username in channels:
        rows.append([InlineKeyboardButton(text=f"📢 JOIN @{username}", url=f"https://t.me/{username}", style="success")])
    rows.append([InlineKeyboardButton(text="✅ I JOINED — CHECK", callback_data="check_subscription", style="success")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def require_subscription(target):
    user_id = target.from_user.id
    if await user_has_required_subscription(user_id):
        return True
    text = (
        "🔒 <b>JOIN REQUIRED</b>\n\n"
        "Please join our required channel(s) before using FF SECURITY BOT.\n\n"
        "1️⃣ Tap <b>JOIN</b>\n"
        "2️⃣ Join the channel\n"
        "3️⃣ Tap <b>I JOINED — CHECK</b>\n\n"
        "⚡ After verification, you can continue using the bot."
    )
    if isinstance(target, CallbackQuery):
        await target.message.answer(text, reply_markup=force_subscribe_keyboard())
    else:
        await target.answer(text, reply_markup=force_subscribe_keyboard())
    return False

def main_keyboard(user_id=None):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔎 FIND SECURITY CODE",
            callback_data="find_code",
            style="danger"
        )],
        [
            InlineKeyboardButton(
                text="🪙 MY COINS",
                callback_data="my_coins",
                style="success"
            ),
            InlineKeyboardButton(
                text="💰 BUY COINS",
                callback_data="buy_coins",
                style="success"
            ),
        ],
        [
            InlineKeyboardButton(
                text="📦 MY ORDERS",
                callback_data="orders",
                style="success"
            ),
            InlineKeyboardButton(
                text="🧾 HISTORY",
                callback_data="history",
                style="success"
            ),
        ],
        [
            InlineKeyboardButton(
                text="📢 CHANNELS",
                callback_data="channels",
                style="success"
            ),
        ],
        [
            InlineKeyboardButton(
                text="✅ VERIFICATION",
                callback_data="verification",
                style="success"
            ),
            InlineKeyboardButton(
                text="🛟 SUPPORT",
                callback_data="support",
                style="success"
            ),
        ],
        [
            InlineKeyboardButton(
                text="ℹ️ ABOUT",
                callback_data="about",
                style="success"
            ),
        ],
        [
            InlineKeyboardButton(
                text="🏠 MAIN MENU",
                callback_data="main_menu",
                style="success"
            ),
        ],
    ])

    if user_id is not None and is_admin(user_id):
        keyboard.inline_keyboard.append([InlineKeyboardButton(
            text="👑 ADMIN PANEL",
            callback_data="admin_panel",
            style="primary"
        )])
    return keyboard

def admin_keyboard():
    def btn(text, callback_data, style="success"):
        return InlineKeyboardButton(text=text, callback_data=callback_data, style=style)

    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("📊 DASHBOARD", "adm_dashboard"), btn("👤 USERS", "adm_users")],
        [btn("👥 USER MANAGEMENT", "adm_user_management"), btn("🔎 SEARCH USER", "adm_user_search")],
        [btn("👤 USER DETAILS", "adm_user_details"), btn("🚫 BAN USER", "adm_user_ban", "danger")],
        [btn("🔓 UNBAN USER", "adm_user_unban"), btn("🪙 USER COINS", "adm_user_coins")],
        [btn("📜 USER HISTORY", "adm_user_history"), btn("🗑️ DELETE USER", "adm_user_delete", "danger")],
        [btn("🪙 COINS", "adm_coins"), btn("🪙 COIN HISTORY", "adm_coin_history")],
        [btn("💳 PAYMENT SETTINGS", "adm_payment"), btn("📦 PENDING PAYMENTS", "adm_pending")],
        [btn("📜 PAYMENT HISTORY", "adm_payhistory"), btn("➕ ADD PACKAGE", "adm_addpkg")],
        [btn("📦 PACKAGES", "adm_packages"), btn("📢 BROADCAST", "adm_broadcast")],
        [btn("🛠️ SUPPORT", "adm_support"), btn("📊 STATISTICS", "adm_statistics")],
        [btn("💰 REVENUE", "adm_revenue"), btn("🔐 CODE HISTORY", "adm_code_history")],
        [btn("👑 ADMIN LOGS", "adm_admin_logs"), btn("⚙️ BOT SETTINGS", "adm_settings")],
        [btn("🔧 MAINTENANCE MODE", "adm_maintenance"), btn("📢 CHANNEL SETTINGS", "adm_channels")],
        [btn("📝 EDIT WELCOME", "adm_welcome"), btn("💾 DATABASE BACKUP", "adm_backup")],
        [btn("📤 EXPORT USERS", "adm_export_users"), btn("🔄 REFRESH PANEL", "admin_panel")],
        [btn("🏠 USER MENU", "main_menu")]
    ])

def user_management_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 SEARCH USER", callback_data="adm_user_search")],
        [InlineKeyboardButton(text="👤 USER DETAILS", callback_data="adm_user_details")],
        [InlineKeyboardButton(text="🚫 BAN USER", callback_data="adm_user_ban")],
        [InlineKeyboardButton(text="🔓 UNBAN USER", callback_data="adm_user_unban")],
        [InlineKeyboardButton(text="🪙 USER COINS", callback_data="adm_user_coins")],
        [InlineKeyboardButton(text="📜 USER HISTORY", callback_data="adm_user_history")],
        [InlineKeyboardButton(text="🗑️ DELETE USER", callback_data="adm_user_delete")],
        [InlineKeyboardButton(text="🏠 ADMIN PANEL", callback_data="admin_panel")]
    ])


def packages_keyboard():
    rows = []
    packages = cur.execute(
        "SELECT * FROM packages WHERE active=1 ORDER BY coins"
    ).fetchall()
    for p in packages:
        rows.append([InlineKeyboardButton(
            text=f"🪙 {p['coins']} COINS — ₹{p['price']}",
            callback_data=f"pkg:{p['id']}",
            style="success"
        )])
    rows.append([InlineKeyboardButton(text="🏠 MAIN MENU", callback_data="main_menu", style="success")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_payment_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ SET UPI ID", callback_data="adm_setupi")],
        [InlineKeyboardButton(text="✏️ SET UPI NAME", callback_data="adm_setupiname")],
        [InlineKeyboardButton(text="➕ ADD PACKAGE", callback_data="adm_addpkg")],
        [InlineKeyboardButton(text="📦 VIEW PACKAGES", callback_data="adm_packages")],
        [InlineKeyboardButton(text="🏠 ADMIN PANEL", callback_data="admin_panel")]
    ])

async def show_main(message: Message):
    row = cur.execute("SELECT * FROM users WHERE user_id=?", (message.from_user.id,)).fetchone()
    balance = row["coins"] if row else 0
    first_name = row["first_name"] if row else (message.from_user.first_name or "User")
    joined = row["joined_at"][:10] if row and row["joined_at"] else "-"
    text = (
        "👋 <b>WELCOME TO FF SECURITY CODE BOT!</b> 🔐\n\n"
        "🔹 Get your security code easily and quickly.\n"
        "💎 Use the buttons below to access all features.\n"
        "✅ Stay tuned for latest updates and offers!\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✨ <b>YOUR ACCOUNT</b> ✦\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Name: <b>{first_name}</b>\n"
        f"🆔 User ID: <code>{message.from_user.id}</code>\n"
        f"🪙 Coins: <b>{balance}</b>\n"
        f"📅 Joined: <b>{joined}</b>\n\n"
        "✨ <b>Choose an option below:</b> 👇"
    )
    await message.answer(text, reply_markup=main_keyboard(message.from_user.id))

@dp.message(CommandStart())
async def start(message: Message):
    register_user(message)
    if not await require_subscription(message):
        return
    if not is_admin(message.from_user.id):
        row = cur.execute("SELECT banned FROM users WHERE user_id=?", (message.from_user.id,)).fetchone()
        if row and row["banned"]:
            await message.answer("🚫 <b>You are banned from using this bot.</b>")
            return
        if maintenance_enabled():
            await message.answer("🔧 <b>BOT UNDER MAINTENANCE</b>\n\nPlease try again later.")
            return
    await show_main(message)


async def hide_user_keyboard(call: CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

@dp.callback_query(F.data == "main_menu")
async def main_menu(call: CallbackQuery):
    await hide_user_keyboard(call)
    register_user(call.message)
    row = cur.execute("SELECT * FROM users WHERE user_id=?", (call.from_user.id,)).fetchone()
    balance = row["coins"] if row else 0
    first_name = row["first_name"] if row else (call.from_user.first_name or "User")
    joined = row["joined_at"][:10] if row and row["joined_at"] else "-"
    text = (
        "👋 <b>WELCOME TO FF SECURITY CODE BOT!</b> 🔐\n\n"
        "🔹 Get your security code easily and quickly.\n"
        "💎 Use the buttons below to access all features.\n"
        "✅ Stay tuned for latest updates and offers!\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✨ <b>YOUR ACCOUNT</b> ✦\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Name: <b>{first_name}</b>\n"
        f"🆔 User ID: <code>{call.from_user.id}</code>\n"
        f"🪙 Coins: <b>{balance}</b>\n"
        f"📅 Joined: <b>{joined}</b>\n\n"
        "✨ <b>Choose an option below:</b> 👇"
    )
    await call.message.answer(text, reply_markup=main_keyboard(call.from_user.id))
    await call.answer()

@dp.callback_query(F.data == "profile")
async def profile(call: CallbackQuery):
    await hide_user_keyboard(call)
    if not user_allowed(call.from_user.id):
        await call.answer("🔧 Bot is under maintenance. Please try again later.", show_alert=True)
        return

    row = cur.execute("SELECT * FROM users WHERE user_id=?", (call.from_user.id,)).fetchone()
    if not row:
        await call.message.answer("❌ Profile not found. Please use /start.")
    else:
        username = f"@{row['username']}" if row["username"] else "Not set"
        await call.message.answer(
            "👤 <b>MY PROFILE</b>\n\n"
            f"👤 Name: <b>{row['first_name']}</b>\n"
            f"🔗 Username: <b>{username}</b>\n"
            f"🆔 Telegram ID: <code>{row['user_id']}</code>\n"
            f"🪙 Coins: <b>{row['coins']}</b>\n"
            f"📅 Joined: <code>{row['joined_at']}</code>"
        )
    await call.answer()

@dp.callback_query(F.data == "my_coins")
async def my_coins(call: CallbackQuery):
    await hide_user_keyboard(call)
    if not user_allowed(call.from_user.id):
        await call.answer("🔧 Bot is under maintenance. Please try again later.", show_alert=True)
        return

    row = cur.execute("SELECT coins FROM users WHERE user_id=?", (call.from_user.id,)).fetchone()
    await call.message.answer(
        f"🪙 <b>MY COINS</b> ✦\n\n"
        f"💰 Current Balance: <b>{row['coins'] if row else 0} COINS</b>\n"
        f"🔐 Security Code Cost: <b>{int(get_setting('code_cost', DEFAULT_COIN_COST))} COIN</b>\n\n"
        "💡 Buy more coins anytime from 💰 <b>BUY COINS</b>."
    )
    await call.answer()

@dp.callback_query(F.data == "find_code")
async def find_code(call: CallbackQuery):
    if not await require_subscription(call):
        await call.answer()
        return
    await hide_user_keyboard(call)
    if not user_allowed(call.from_user.id):
        await call.answer("🔧 Bot is under maintenance. Please try again later.", show_alert=True)
        return

    row = cur.execute("SELECT coins FROM users WHERE user_id=?", (call.from_user.id,)).fetchone()
    balance = row["coins"] if row else 0
    code_cost = int(get_setting("code_cost", DEFAULT_COIN_COST))

    if balance < code_cost:
        insufficient_text = (
            "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
            "   🌑 <b>INSUFFICIENT COINS</b> ✦\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"💰 Your Balance: <b>{balance} COINS</b>\n"
            f"🔐 Required: <b>{code_cost} COIN</b>\n\n"
            "👉 Please use 💰 <b>BUY COINS</b> to request more coins."
        )
        await call.message.answer(insufficient_text)
        await call.answer()
        return

    cur.execute("UPDATE users SET coins=coins-? WHERE user_id=?", (code_cost, call.from_user.id))
    record_coin_change(call.from_user.id, -code_cost, "CODE_PURCHASE")
    db.commit()

    set_setting(f"pending_security_message:{call.from_user.id}", "1")

    await call.message.answer(
        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "   🔐 SECURITY CODE REQUEST\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "🪙 10 COIN deducted successfully.\n"
        f"💰 Balance after request: {balance - code_cost} COINS\n\n"
        "📩 𝗦𝗘𝗡𝗗 𝗬𝗢𝗨𝗥 𝗔𝗖𝗖𝗘𝗦𝗦 𝗧𝗢𝗞𝗘𝗡 𝗛𝗘𝗥𝗘\n\n"
        "➡️ Your message will be forwarded to the admin for verification.\n\n"
        "⏳ After verification, the admin will send your Security Code here.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🛡️ FF SECURITY BOT\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    await call.answer()


@dp.message(F.text, F.from_user.id.in_(ADMIN_IDS))
async def admin_text_input(message: Message):
    if not is_admin(message.from_user.id):
        return

    if get_setting(f"pending_security_message:{message.from_user.id}"):
        await _handle_security_request_message(message)
        return

    key = f"admin_wait_{message.from_user.id}"
    action = get_setting(key)
    if not action:
        return

    if await handle_extra_admin_text(message, action):
        return

    if action.startswith("send_security_code:"):
        request_id = int(action.split(":", 1)[1])
        row = cur.execute("SELECT * FROM security_requests WHERE id=?", (request_id,)).fetchone()
        if not row or row["status"] != "PENDING":
            set_setting(key, "")
            await message.answer("❌ This security request is no longer pending.")
            return
        code = message.text.strip()
        if not code:
            await message.answer("❌ Please send a valid security code.")
            return
        cur.execute(
            "UPDATE security_requests SET status='COMPLETED', completed_at=?, result_code=NULL WHERE id=?",
            (now(), request_id)
        )
        db.commit()
        set_setting(key, "")
        await bot.send_message(
            row["user_id"],
            "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
            "   🔐 <b>SECURITY CODE READY</b>\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"🧾 Request ID: <b>#{request_id}</b>\n\n"
            f"🔑 <b>Security Code:</b> <code>{code}</code>\n\n"
            "✅ Your request has been completed by the admin.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🛡️ <b>FF SECURITY BOT</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )
        log_admin(message.from_user.id, "SEND_SECURITY_CODE", f"request={request_id}, user={row['user_id']}")
        await message.answer(f"✅ Security code sent to user <code>{row['user_id']}</code> for request <b>#{request_id}</b>.")
        return

    if action == "user_search":
        query = message.text.strip()
        set_setting(key, "")
        if query.startswith("@"):
            username = query[1:]
            rows = cur.execute("SELECT * FROM users WHERE LOWER(username)=LOWER(?) ORDER BY joined_at DESC LIMIT 10", (username,)).fetchall()
        elif query.isdigit():
            rows = cur.execute("SELECT * FROM users WHERE user_id=?", (int(query),)).fetchall()
        else:
            rows = cur.execute("SELECT * FROM users WHERE LOWER(username) LIKE LOWER(?) OR LOWER(first_name) LIKE LOWER(?) ORDER BY joined_at DESC LIMIT 10", (f"%{query}%", f"%{query}%")).fetchall()
        if not rows:
            await message.answer("❌ No users found.", reply_markup=user_management_keyboard())
            return
        for row in rows:
            await message.answer(format_user(row), reply_markup=user_action_keyboard(row['user_id'], row['banned']))
    elif action == "code_cost":
        if not message.text.strip().isdigit() or int(message.text.strip()) < 1:
            await message.answer("❌ Enter a valid positive number.")
            return
        set_setting("code_cost", int(message.text.strip()))
        set_setting(key, "")
        await message.answer(f"✅ Code cost updated to <b>{message.text.strip()} COIN</b>")
    elif action == "support_link":
        support = message.text.strip()
        set_setting("support_link", support)
        set_setting(key, "")
        await message.answer(f"✅ Support updated to <code>{support}</code>")
    elif action.startswith("package_price:"):
        raw = message.text.strip()
        if not raw.isdigit() or int(raw) < 1:
            await message.answer("❌ Enter a valid positive price. Example: <code>149</code>")
            return
        pid = int(action.split(":", 1)[1])
        p = cur.execute("SELECT coins FROM packages WHERE id=?", (pid,)).fetchone()
        if not p:
            set_setting(key, "")
            await message.answer("❌ Package not found.")
            return
        cur.execute("UPDATE packages SET price=? WHERE id=?", (int(raw), pid))
        db.commit()
        set_setting(key, "")
        await message.answer(
            f"✅ Price updated: <b>{p['coins']} COINS = ₹{int(raw)}</b>"
        )
    elif action == "upi_id":
        set_setting("upi_id", message.text.strip())
        set_setting(key, "")
        await message.answer(f"✅ UPI ID updated to <code>{message.text.strip()}</code>")
    elif action == "upi_name":
        set_setting("upi_name", message.text.strip())
        set_setting(key, "")
        await message.answer("✅ UPI name updated.")
    elif action == "package":
        parts = message.text.split()
        if len(parts) != 2 or not all(x.isdigit() for x in parts):
            await message.answer("❌ Format: COINS PRICE\nExample: 250 1499")
            return
        cur.execute("INSERT INTO packages(coins,price) VALUES(?,?)", (int(parts[0]), int(parts[1])))
        db.commit()
        set_setting(key, "")
        await message.answer("✅ Package added.")
    elif action == "broadcast":
        set_setting(key, "")
        users = cur.execute("SELECT user_id FROM users").fetchall()
        sent = 0
        for u in users:
            try:
                await bot.send_message(u["user_id"], message.text)
                sent += 1
            except Exception:
                pass
        await message.answer(f"📢 Broadcast finished. Sent to {sent} users.")


# ---------------- EXTRA ADMIN FEATURES ----------------
@dp.callback_query(F.data.in_({"adm_user_details", "adm_user_ban", "adm_user_unban", "adm_user_coins", "adm_user_history", "adm_user_delete"}))
async def admin_user_action_prompt(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    mapping = {
        "adm_user_details": ("user_details", "👤 Send the Telegram user ID for details."),
        "adm_user_ban": ("user_ban", "🚫 Send the Telegram user ID to ban."),
        "adm_user_unban": ("user_unban", "🔓 Send the Telegram user ID to unban."),
        "adm_user_coins": ("user_coins", "🪙 Send the Telegram user ID to manage coins."),
        "adm_user_history": ("user_history", "📜 Send the Telegram user ID to view history."),
        "adm_user_delete": ("user_delete", "🗑️ Send the Telegram user ID to delete."),
    }
    action, prompt = mapping[call.data]
    set_setting(f"admin_wait_{call.from_user.id}", action)
    await call.message.answer(prompt)
    await call.answer()

@dp.callback_query(F.data == "adm_revenue")
async def adm_revenue(call: CallbackQuery):
    if not is_admin(call.from_user.id): await call.answer("Admin only.", show_alert=True); return
    total=cur.execute("SELECT COALESCE(SUM(amount),0) c FROM payments WHERE status='APPROVED'").fetchone()["c"]
    today=datetime.now(timezone.utc).date().isoformat()
    today_rev=cur.execute("SELECT COALESCE(SUM(amount),0) c FROM payments WHERE status='APPROVED' AND substr(reviewed_at,1,10)=?",(today,)).fetchone()["c"]
    count=cur.execute("SELECT COUNT(*) c FROM payments WHERE status='APPROVED'").fetchone()["c"]
    await call.message.answer(f"💰 <b>REVENUE</b>\\n\\n💵 Total approved: <b>₹{total}</b>\\n📅 Today: <b>₹{today_rev}</b>\\n🧾 Approved orders: <b>{count}</b>")
    await call.answer()

@dp.callback_query(F.data == "adm_code_history")
async def adm_code_history(call: CallbackQuery):
    if not is_admin(call.from_user.id): await call.answer("Admin only.", show_alert=True); return
    rows=cur.execute("SELECT id,user_id,code,created_at FROM generated_codes ORDER BY id DESC LIMIT 40").fetchall()
    text="🔐 <b>CODE HISTORY</b>\\n\\n"+("\\n".join(f"#{r['id']} — User <code>{r['user_id']}</code> — <code>{r['code']}</code> — {r['created_at']}" for r in rows) or "No generated codes.")
    await call.message.answer(text); await call.answer()

@dp.callback_query(F.data == "adm_admin_logs")
async def adm_admin_logs(call: CallbackQuery):
    if not is_admin(call.from_user.id): await call.answer("Admin only.", show_alert=True); return
    rows=cur.execute("SELECT * FROM admin_logs ORDER BY id DESC LIMIT 40").fetchall()
    text="👑 <b>ADMIN LOGS</b>\\n\\n"+("\\n".join(f"#{r['id']} — Admin <code>{r['admin_id']}</code> — {r['action']} — {r['details'] or '-'}" for r in rows) or "No admin logs yet.")
    await call.message.answer(text); await call.answer()

@dp.callback_query(F.data == "adm_channels")
async def adm_channels(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    status = "ON 🟢" if force_subscribe_enabled() else "OFF 🔴"
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ EDIT CHANNELS",callback_data="adm_setchannels")],
        [InlineKeyboardButton(text=f"🔒 FORCE SUBSCRIBE: {status}",callback_data="adm_toggle_force")],
        [InlineKeyboardButton(text="🏠 ADMIN PANEL",callback_data="admin_panel")]
    ])
    await call.message.answer(
        f"📢 <b>FORCE SUBSCRIBE SETTINGS</b>\n\nStatus: <b>{status}</b>\n\nRequired channels:\n{get_setting('channel_links','Not configured yet.')}",
        reply_markup=kb
    )
    await call.answer()

@dp.callback_query(F.data == "adm_toggle_force")
async def adm_toggle_force(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    new_value = "0" if force_subscribe_enabled() else "1"
    set_setting("force_subscribe", new_value)
    status = "ENABLED 🟢" if new_value == "1" else "DISABLED 🔴"
    await call.message.answer(f"🔒 <b>Force Subscribe {status}</b>")
    await call.answer("Updated.")

@dp.callback_query(F.data == "adm_setchannels")
async def adm_setchannels(call: CallbackQuery):
    if not is_admin(call.from_user.id): await call.answer("Admin only.", show_alert=True); return
    set_setting(f"admin_wait_{call.from_user.id}","channel_links")
    await call.message.answer("📢 Send your channel links/usernames. Multiple lines are allowed."); await call.answer()

@dp.callback_query(F.data == "adm_welcome")
async def adm_welcome(call: CallbackQuery):
    if not is_admin(call.from_user.id): await call.answer("Admin only.", show_alert=True); return
    set_setting(f"admin_wait_{call.from_user.id}","welcome_message")
    await call.message.answer(f"📝 <b>EDIT WELCOME</b>\\n\\nCurrent:\n{get_setting('welcome_message','🔐 <b>SECURITY VAULT</b> ✦')}\\n\\nSend the new text. HTML formatting is supported."); await call.answer()

@dp.callback_query(F.data == "adm_backup")
async def adm_backup(call: CallbackQuery):
    if not is_admin(call.from_user.id): await call.answer("Admin only.", show_alert=True); return
    db.commit()
    if not os.path.exists(DB_FILE): await call.answer("Database not found.",show_alert=True); return
    name=f"bot_backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.db"; path=os.path.join(os.path.dirname(os.path.abspath(DB_FILE)) or ".",name)
    shutil.copy2(DB_FILE,path); log_admin(call.from_user.id,"DATABASE_BACKUP",name)
    try:
        with open(path,"rb") as f: await call.message.answer_document(BufferedInputFile(f.read(),filename=name),caption="💾 Database backup created.")
    finally:
        try: os.remove(path)
        except OSError: pass
    await call.answer("Backup sent.")

@dp.callback_query(F.data == "adm_export_users")
async def adm_export_users(call: CallbackQuery):
    if not is_admin(call.from_user.id): await call.answer("Admin only.", show_alert=True); return
    rows=cur.execute("SELECT user_id,username,first_name,coins,banned,joined_at FROM users ORDER BY joined_at DESC").fetchall()
    out=io.StringIO(); w=csv.writer(out); w.writerow(["user_id","username","first_name","coins","banned","joined_at"])
    for r in rows: w.writerow([r["user_id"],r["username"],r["first_name"],r["coins"],r["banned"],r["joined_at"]])
    log_admin(call.from_user.id,"EXPORT_USERS",f"{len(rows)} users")
    await call.message.answer_document(BufferedInputFile(out.getvalue().encode(),filename="users_export.csv"),caption=f"📤 Exported {len(rows)} users."); await call.answer("Export sent.")

async def handle_extra_admin_text(message: Message, action: str):
    if action == "channel_links":
        set_setting("channel_links",message.text.strip()); set_setting(f"admin_wait_{message.from_user.id}",""); log_admin(message.from_user.id,"CHANNEL_SETTINGS","Updated channel links")
        await message.answer("✅ Channel settings updated.",reply_markup=admin_keyboard()); return True
    if action == "welcome_message":
        set_setting("welcome_message",message.text.strip()); set_setting(f"admin_wait_{message.from_user.id}",""); log_admin(message.from_user.id,"EDIT_WELCOME","Welcome message updated")
        await message.answer("✅ Welcome message updated.",reply_markup=admin_keyboard()); return True
    if action not in {"user_details","user_ban","user_unban","user_coins","user_history","user_delete"}: return False
    if not message.text.strip().isdigit(): await message.answer("❌ Please send a numeric Telegram user ID."); return True
    uid=int(message.text.strip()); row=cur.execute("SELECT * FROM users WHERE user_id=?",(uid,)).fetchone(); set_setting(f"admin_wait_{message.from_user.id}","")
    if not row: await message.answer("❌ User not found."); return True
    if action=="user_details": await message.answer(format_user(row),reply_markup=user_action_keyboard(uid,row["banned"]))
    elif action=="user_ban":
        if uid in ADMIN_IDS: await message.answer("❌ You cannot ban an admin."); return True
        cur.execute("UPDATE users SET banned=1 WHERE user_id=?",(uid,)); db.commit(); log_admin(message.from_user.id,"BAN_USER",str(uid)); await message.answer(f"🚫 User <code>{uid}</code> banned.")
    elif action=="user_unban":
        cur.execute("UPDATE users SET banned=0 WHERE user_id=?",(uid,)); db.commit(); log_admin(message.from_user.id,"UNBAN_USER",str(uid)); await message.answer(f"🔓 User <code>{uid}</code> unbanned.")
    elif action=="user_coins": await message.answer(f"🪙 User <code>{uid}</code> balance: <b>{row['coins']} COINS</b>\\n\\nUse /addcoins {uid} AMOUNT or /removecoins {uid} AMOUNT")
    elif action=="user_history":
        coins=cur.execute("SELECT amount,action,created_at FROM coin_history WHERE user_id=? ORDER BY id DESC LIMIT 15",(uid,)).fetchall(); payments=cur.execute("SELECT id,amount,status,created_at FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 10",(uid,)).fetchall(); codes=cur.execute("SELECT code,created_at FROM generated_codes WHERE user_id=? ORDER BY id DESC LIMIT 10",(uid,)).fetchall()
        text=f"📜 <b>USER HISTORY</b>\\nUser: <code>{uid}</code>\\n\\n🪙 Coins:\\n"+("\\n".join(f"{x['amount']} — {x['action']} — {x['created_at']}" for x in coins) or "No coin history.")+"\\n\\n💳 Payments:\\n"+("\\n".join(f"#{x['id']} — ₹{x['amount']} — {x['status']}" for x in payments) or "No payments.")+"\\n\\n🔐 Codes:\\n"+("\\n".join(f"{x['code']} — {x['created_at']}" for x in codes) or "No codes.")
        await message.answer(text)
    elif action=="user_delete":
        if uid in ADMIN_IDS: await message.answer("❌ You cannot delete an admin."); return True
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚠️ YES, DELETE",callback_data=f"adm_user_delete:{uid}"),InlineKeyboardButton(text="❌ CANCEL",callback_data="adm_user_management")]])
        await message.answer(f"⚠️ Delete user <code>{uid}</code> and their stored history?",reply_markup=kb)
    return True


async def _handle_security_request_message(message: Message):
    uid = message.from_user.id
    pending_key = f"pending_security_message:{uid}"

    if not get_setting(pending_key):
        return False

    set_setting(pending_key, "")

    cur.execute(
        "INSERT INTO security_requests(user_id,status,created_at) VALUES(?,?,?)",
        (uid, "PENDING", now())
    )
    request_id = cur.lastrowid
    db.commit()

    user = message.from_user
    admin_text = (
        "🔐 <b>NEW SECURITY CODE REQUEST</b>\n\n"
        f"🧾 Request ID: <b>#{request_id}</b>\n"
        f"👤 User: <b>{user.full_name}</b>\n"
        f"🆔 Telegram ID: <code>{uid}</code>\n\n"
        "📩 <b>User message is forwarded above.</b>\n"
        "🔎 Please verify the message, then send the Security Code using the button below."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 SEND SECURITY CODE", callback_data=f"adm_send_code:{request_id}")],
        [InlineKeyboardButton(text="❌ CLOSE REQUEST", callback_data=f"adm_close_code:{request_id}")]
    ])

    forwarded_to = 0
    for admin_id in ADMIN_IDS:
        try:
            await bot.copy_message(
                chat_id=admin_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
            await bot.send_message(admin_id, admin_text, reply_markup=kb)
            forwarded_to += 1
        except Exception:
            logging.exception("Security request forwarding failed for admin %s", admin_id)

    if forwarded_to == 0:
        set_setting(pending_key, "1")
        await message.answer("❌ <b>MESSAGE COULD NOT BE FORWARDED</b>\n\nPlease try again.")
        return True

    await message.answer(
        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "   ⏳ <b>MESSAGE FORWARDED</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"🧾 Request ID: <b>#{request_id}</b>\n"
        "✅ Your message has been forwarded to the admin.\n"
        "🔎 Admin will verify it and send the Security Code here.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🛡️ <b>FF SECURITY BOT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    return True


@dp.message(~F.photo)
async def security_request_user_message(message: Message):
    await _handle_security_request_message(message)


@dp.callback_query(F.data.startswith("adm_send_code:"))
async def adm_send_security_code(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True)
        return
    request_id = int(call.data.split(":")[1])
    row = cur.execute("SELECT * FROM security_requests WHERE id=?", (request_id,)).fetchone()
    if not row or row["status"] != "PENDING":
        await call.answer("Request is no longer pending.", show_alert=True)
        return
    set_setting(f"admin_wait_{call.from_user.id}", f"send_security_code:{request_id}")
    await call.message.answer(
        f"🔐 <b>SEND SECURITY CODE</b>\n\n"
        f"Request: <b>#{request_id}</b>\n"
        f"User ID: <code>{row['user_id']}</code>\n\n"
        "✏️ Send the security code now. It will be delivered to the user."
    )
    await call.answer()


@dp.callback_query(F.data.startswith("adm_close_code:"))
async def adm_close_security_code(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True)
        return
    request_id = int(call.data.split(":")[1])
    row = cur.execute("SELECT user_id,status FROM security_requests WHERE id=?", (request_id,)).fetchone()
    if not row or row["status"] != "PENDING":
        await call.answer("Request is already closed.", show_alert=True)
        return
    cur.execute("UPDATE security_requests SET status='CLOSED', completed_at=? WHERE id=?", (now(), request_id))
    db.commit()
    await bot.send_message(row["user_id"], f"❌ Security request <b>#{request_id}</b> was closed by an admin.")
    await call.message.answer(f"✅ Request <b>#{request_id}</b> closed.")
    await call.answer("Closed.")


@dp.callback_query(F.data == "buy_coins")
async def buy_coins(call: CallbackQuery):
    await hide_user_keyboard(call)
    if not user_allowed(call.from_user.id):
        await call.answer("🔧 Bot is under maintenance. Please try again later.", show_alert=True)
        return

    await call.message.answer("🪙 <b>SELECT COIN PACKAGE</b>", reply_markup=packages_keyboard())
    await call.answer()

@dp.callback_query(F.data.startswith("pkg:"))
async def select_package(call: CallbackQuery):
    await hide_user_keyboard(call)
    if not user_allowed(call.from_user.id):
        await call.answer("🔧 Bot is under maintenance. Please try again later.", show_alert=True)
        return

    pid = int(call.data.split(":")[1])
    p = cur.execute("SELECT * FROM packages WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        await call.answer("Package unavailable.", show_alert=True)
        return

    upi = get_setting("upi_id")
    name = get_setting("upi_name")
    upi_url = f"upi://pay?pa={quote_plus(upi)}&pn={quote_plus(name)}&am={p['price']}&cu=INR"

    img = qrcode.make(upi_url)
    from io import BytesIO
    buf = BytesIO()
    img.save(buf, format="PNG")
    qr = BufferedInputFile(buf.getvalue(), filename="upi_qr.png")

    cur.execute(
        "INSERT INTO payments(user_id,package_id,amount,status,created_at) VALUES(?,?,?,?,?)",
        (call.from_user.id, p["id"], p["price"], "WAITING_PAYMENT", now())
    )
    payment_id = cur.lastrowid
    db.commit()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 SEND PAYMENT SCREENSHOT", callback_data=f"upload:{payment_id}")],
        [InlineKeyboardButton(text="🔄 SELECT NEW PACKAGE", callback_data="buy_coins")],
        [InlineKeyboardButton(text="🏠 MAIN MENU", callback_data="main_menu")]
    ])

    caption = (
        "🇮🇳 <b>COIN PAYMENT — UPI (INDIA)</b>\n\n"
        f"🪙 Package: <b>{p['coins']} COINS</b>\n"
        f"💵 Amount: <b>₹{p['price']}</b>\n\n"
        f"📱 UPI ID: <code>{upi}</code>\n\n"
        "📲 Scan the QR above and complete the payment.\n\n"
        "⚠️ After payment, tap <b>SEND PAYMENT SCREENSHOT</b> "
        "and upload a clear screenshot of the successful payment.\n\n"
        f"🧾 Request ID: <b>#{payment_id}</b>"
    )
    await call.message.answer_photo(qr, caption=caption, reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data.startswith("upload:"))
async def upload_request(call: CallbackQuery):
    await hide_user_keyboard(call)
    payment_id = int(call.data.split(":")[1])
    row = cur.execute(
        "SELECT * FROM payments WHERE id=? AND user_id=?", (payment_id, call.from_user.id)
    ).fetchone()
    if not row:
        await call.answer("Payment request not found.", show_alert=True)
        return
    set_setting(f"upload_wait_{call.from_user.id}", payment_id)
    await call.message.answer(
        f"📸 Please send the payment screenshot now.\n\n🧾 Request ID: #{payment_id}"
    )
    await call.answer()

@dp.message(F.photo)
async def receive_screenshot(message: Message):
    register_user(message)
    key = f"upload_wait_{message.from_user.id}"
    payment_id = get_setting(key)
    if not payment_id:
        return
    payment_id = int(payment_id)
    p = cur.execute("""
        SELECT payments.*, packages.coins AS pkg_coins
        FROM payments JOIN packages ON payments.package_id=packages.id
        WHERE payments.id=? AND payments.user_id=?
    """, (payment_id, message.from_user.id)).fetchone()
    if not p:
        set_setting(key, "")
        return
    file_id = message.photo[-1].file_id
    cur.execute(
        "UPDATE payments SET status='PENDING', screenshot_file_id=? WHERE id=?",
        (file_id, payment_id)
    )
    db.commit()
    set_setting(key, "")

    await message.answer(
        f"✅ Payment screenshot submitted.\n🧾 Request ID: #{payment_id}\n"
        "⏳ Waiting for admin approval."
    )

    user = message.from_user
    admin_text = (
        "💳 <b>NEW PAYMENT REQUEST</b>\n\n"
        f"🧾 Request ID: <b>#{payment_id}</b>\n"
        f"👤 User: {user.full_name}\n"
        f"🆔 Telegram ID: <code>{user.id}</code>\n"
        f"🪙 Package: <b>{p['pkg_coins']} COINS</b>\n"
        f"💰 Amount: <b>₹{p['amount']}</b>\n\n"
        "Review the screenshot and choose an action."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ APPROVE", callback_data=f"approve:{payment_id}"),
            InlineKeyboardButton(text="❌ REJECT", callback_data=f"reject:{payment_id}")
        ]
    ])
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_photo(admin_id, file_id, caption=admin_text, reply_markup=kb)
        except Exception as e:
            logging.warning("Admin notification failed for %s: %s", admin_id, e)

@dp.callback_query(F.data.startswith("approve:"))
async def approve(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True)
        return
    payment_id = int(call.data.split(":")[1])
    p = cur.execute("""
        SELECT payments.*, packages.coins AS pkg_coins
        FROM payments JOIN packages ON payments.package_id=packages.id
        WHERE payments.id=?
    """, (payment_id,)).fetchone()
    if not p or p["status"] != "PENDING":
        await call.answer("Already processed or not found.", show_alert=True)
        return

    cur.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (p["pkg_coins"], p["user_id"]))
    record_coin_change(p["user_id"], p["pkg_coins"], f"PAYMENT_APPROVED_#{payment_id}", call.from_user.id)
    cur.execute(
        "UPDATE payments SET status='APPROVED', reviewed_at=? WHERE id=?",
        (now(), payment_id)
    )
    db.commit()

    await call.message.edit_caption(
        caption=f"✅ <b>PAYMENT APPROVED</b>\n\n🧾 Request ID: #{payment_id}\n"
                f"🪙 Added: <b>{p['pkg_coins']} COINS</b>"
    )
    try:
        await bot.send_message(
            p["user_id"],
            f"✅ <b>Payment approved!</b>\n\n"
            f"🧾 Request ID: #{payment_id}\n"
            f"🪙 Added: <b>{p['pkg_coins']} COINS</b>\n\n"
            "💼 Coins have been added to your wallet."
        )
    except Exception:
        pass
    await call.answer("Approved.")

@dp.callback_query(F.data.startswith("reject:"))
async def reject(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True)
        return
    payment_id = int(call.data.split(":")[1])
    p = cur.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    if not p or p["status"] != "PENDING":
        await call.answer("Already processed or not found.", show_alert=True)
        return
    cur.execute(
        "UPDATE payments SET status='REJECTED', reviewed_at=? WHERE id=?",
        (now(), payment_id)
    )
    db.commit()
    await call.message.edit_caption(
        caption=f"❌ <b>PAYMENT REJECTED</b>\n\n🧾 Request ID: #{payment_id}"
    )
    try:
        await bot.send_message(
            p["user_id"],
            f"❌ <b>Payment rejected.</b>\n\n🧾 Request ID: #{payment_id}\n"
            "Please contact support if you believe this was a mistake."
        )
    except Exception:
        pass
    await call.answer("Rejected.")

@dp.callback_query(F.data == "check_subscription")
async def check_subscription(call: CallbackQuery):
    if await user_has_required_subscription(call.from_user.id):
        try:
            await call.message.delete()
        except Exception:
            try:
                await call.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        await call.answer("✅ Subscription verified!")
        await show_main(call.message)
    else:
        await call.message.answer("❌ <b>Not joined yet.</b>\n\nPlease join the required channel and try again.", reply_markup=force_subscribe_keyboard())
        await call.answer()

@dp.callback_query(F.data == "verification")
async def verification(call: CallbackQuery):
    await hide_user_keyboard(call)
    if not user_allowed(call.from_user.id):
        await call.answer("🔧 Bot is under maintenance. Please try again later.", show_alert=True)
        return

    await call.message.answer(
        "🧾 <b>VERIFICATION INFO</b>\n\n"
        "Payments are reviewed manually by the bot administrator.\n"
        "Security codes are generated by this bot's own system."
    )
    await call.answer()

@dp.callback_query(F.data == "channels")
async def channels(call: CallbackQuery):
    await hide_user_keyboard(call)
    if not user_allowed(call.from_user.id):
        await call.answer("🔧 Bot is under maintenance. Please try again later.", show_alert=True)
        return

    await call.message.answer(f"📢 <b>CHANNELS</b>\n\n{get_setting('channel_links', 'Not configured yet.')}")
    await call.answer()

@dp.callback_query(F.data == "orders")
async def orders(call: CallbackQuery):
    await hide_user_keyboard(call)
    if not user_allowed(call.from_user.id):
        await call.answer("🔧 Bot is under maintenance. Please try again later.", show_alert=True)
        return

    rows = cur.execute(
        "SELECT id,amount,status,created_at FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 10",
        (call.from_user.id,)
    ).fetchall()
    if not rows:
        await call.message.answer("📦 No orders yet.")
    else:
        text = "📦 <b>YOUR ORDERS</b>\n\n" + "\n".join(
            f"#{r['id']} — ₹{r['amount']} — {r['status']}" for r in rows
        )
        await call.message.answer(text)
    await call.answer()

@dp.callback_query(F.data == "history")
async def history(call: CallbackQuery):
    await hide_user_keyboard(call)
    if not user_allowed(call.from_user.id):
        await call.answer("🔧 Bot is under maintenance. Please try again later.", show_alert=True)
        return

    rows = cur.execute(
        "SELECT id,amount,status,created_at FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 20",
        (call.from_user.id,)
    ).fetchall()
    text = "🧾 <b>ORDER HISTORY</b>\n\n"
    text += "\n".join(f"#{r['id']} — ₹{r['amount']} — {r['status']}" for r in rows) if rows else "No history."
    await call.message.answer(text)
    await call.answer()

@dp.callback_query(F.data == "about")
async def about(call: CallbackQuery):
    await hide_user_keyboard(call)
    if not user_allowed(call.from_user.id):
        await call.answer("🔧 Bot is under maintenance. Please try again later.", show_alert=True)
        return

    await call.message.answer("ℹ️ <b>ABOUT</b>\n\nFIF Security Code Bot\nCoin-based access to codes generated by this bot.")
    await call.answer()

@dp.callback_query(F.data == "support")
async def support(call: CallbackQuery):
    await hide_user_keyboard(call)
    if not user_allowed(call.from_user.id):
        await call.answer("🔧 Bot is under maintenance. Please try again later.", show_alert=True)
        return

    support = get_setting("support_link", "@CURRENTTTTTTTT")
    support_text = f"""╭━━━━━━━━━━━━━━━━━━━━━━╮
   🛠️ <b>SUPPORT CENTER</b>   ✦
╰━━━━━━━━━━━━━━━━━━━━━━╯

💬 <b>Need Help?</b>

⚡ If you're facing any issue with verification,
feel free to contact the owner.

👑 Owner: {support}

🔔 Our support team will assist you."""
    await call.message.answer(support_text)
    await call.answer()

# ---------------- ADMIN ----------------
@dp.message(Command("admin"))
async def admin_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Admin only.")
        return
    log_admin(message.from_user.id, "OPEN_ADMIN", "Admin panel opened")
    await message.answer("👑 <b>ADMIN PANEL</b>", reply_markup=admin_keyboard())

@dp.callback_query(F.data == "admin_panel")
async def admin_panel(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True)
        return
    log_admin(call.from_user.id, "REFRESH_PANEL", "Admin panel refreshed")
    await call.message.answer("👑 <b>ADMIN PANEL</b>", reply_markup=admin_keyboard())
    await call.answer()

@dp.callback_query(F.data == "adm_dashboard")
async def adm_dashboard(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    users = cur.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    coins = cur.execute("SELECT COALESCE(SUM(coins),0) c FROM users").fetchone()["c"]
    pending = cur.execute("SELECT COUNT(*) c FROM payments WHERE status='PENDING'").fetchone()["c"]
    approved = cur.execute("SELECT COUNT(*) c FROM payments WHERE status='APPROVED'").fetchone()["c"]
    await call.message.answer(
        f"📊 <b>DASHBOARD</b>\n\n👤 Users: <b>{users}</b>\n"
        f"🪙 Coins in accounts: <b>{coins}</b>\n"
        f"⏳ Pending payments: <b>{pending}</b>\n"
        f"✅ Approved payments: <b>{approved}</b>"
    )
    await call.answer()

@dp.callback_query(F.data == "adm_users")
async def adm_users(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    users = cur.execute("SELECT * FROM users ORDER BY joined_at DESC LIMIT 20").fetchall()
    text = "👤 <b>RECENT USERS</b>\n\n"
    text += "\n".join(
        f"{u['user_id']} — @{u['username'] or '-'} — {u['coins']} coins"
        f" — {'🚫 BANNED' if u['banned'] else '✅ ACTIVE'}" for u in users
    ) or "No users."
    await call.message.answer(text, reply_markup=user_management_keyboard())
    await call.answer()

@dp.callback_query(F.data == "adm_user_management")
async def adm_user_management(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    await call.message.answer(
        "👥 <b>USER MANAGEMENT</b>\n\nChoose an action below.\n"
        "You can search by Telegram ID or username.",
        reply_markup=user_management_keyboard()
    )
    await call.answer()


def user_action_keyboard(user_id: int, banned: int = 0):
    toggle_text = "🔓 UNBAN USER" if banned else "🚫 BAN USER"
    toggle_action = "unban" if banned else "ban"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 DETAILS", callback_data=f"adm_user_view:{user_id}")],
        [InlineKeyboardButton(text=toggle_text, callback_data=f"adm_user_{toggle_action}:{user_id}")],
        [InlineKeyboardButton(text="🪙 COINS", callback_data=f"adm_user_coinview:{user_id}")],
        [InlineKeyboardButton(text="📜 HISTORY", callback_data=f"adm_user_histview:{user_id}")],
        [InlineKeyboardButton(text="🗑️ DELETE", callback_data=f"adm_user_delconfirm:{user_id}")],
        [InlineKeyboardButton(text="👥 USER MANAGEMENT", callback_data="adm_user_management")]
    ])


def format_user(row):
    username = f"@{row['username']}" if row['username'] else "Not set"
    status = "🚫 BANNED" if row['banned'] else "✅ ACTIVE"
    return (
        "👤 <b>USER DETAILS</b>\n\n"
        f"🆔 ID: <code>{row['user_id']}</code>\n"
        f"👤 Name: <b>{row['first_name'] or '-'} </b>\n"
        f"🔗 Username: <b>{username}</b>\n"
        f"🪙 Coins: <b>{row['coins']}</b>\n"
        f"📅 Joined: <code>{row['joined_at'] or '-'}</code>\n"
        f"📌 Status: <b>{status}</b>"
    )

@dp.callback_query(F.data.startswith("adm_user_view:"))
async def adm_user_view(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    uid = int(call.data.split(":", 1)[1])
    row = cur.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row:
        await call.message.answer("❌ User not found.")
    else:
        await call.message.answer(format_user(row), reply_markup=user_action_keyboard(uid, row['banned']))
    await call.answer()

@dp.callback_query(F.data == "adm_user_search")
async def adm_user_search(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    set_setting(f"admin_wait_{call.from_user.id}", "user_search")
    await call.message.answer("🔎 Send the Telegram user ID or username to search.\nExample: <code>123456789</code> or <code>@username</code>")
    await call.answer()

@dp.callback_query(F.data.startswith("adm_user_ban:"))
async def adm_user_ban(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    uid = int(call.data.split(":", 1)[1])
    if uid in ADMIN_IDS:
        await call.answer("You cannot ban an admin.", show_alert=True); return
    cur.execute("UPDATE users SET banned=1 WHERE user_id=?", (uid,)); db.commit()
    row = cur.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    await call.message.answer("🚫 <b>User banned.</b>", reply_markup=user_action_keyboard(uid, row['banned']) if row else user_management_keyboard())
    await call.answer("User banned.")

@dp.callback_query(F.data.startswith("adm_user_unban:"))
async def adm_user_unban(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    uid = int(call.data.split(":", 1)[1])
    cur.execute("UPDATE users SET banned=0 WHERE user_id=?", (uid,)); db.commit()
    row = cur.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    await call.message.answer("🔓 <b>User unbanned.</b>", reply_markup=user_action_keyboard(uid, row['banned']) if row else user_management_keyboard())
    await call.answer("User unbanned.")

@dp.callback_query(F.data.startswith("adm_user_coinview:"))
async def adm_user_coinview(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    uid = int(call.data.split(":", 1)[1])
    row = cur.execute("SELECT coins FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row:
        await call.message.answer("❌ User not found.")
    else:
        await call.message.answer(
            f"🪙 <b>USER COINS</b>\n\nUser: <code>{uid}</code>\nBalance: <b>{row['coins']} COINS</b>\n\n"
            f"Use <code>/addcoins {uid} AMOUNT</code> or <code>/removecoins {uid} AMOUNT</code>"
        )
    await call.answer()

@dp.callback_query(F.data.startswith("adm_user_histview:"))
async def adm_user_histview(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    uid = int(call.data.split(":", 1)[1])
    user = cur.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone()
    if not user:
        await call.message.answer("❌ User not found.")
    else:
        coins = cur.execute("SELECT amount,action,created_at FROM coin_history WHERE user_id=? ORDER BY id DESC LIMIT 15", (uid,)).fetchall()
        payments = cur.execute("SELECT id,amount,status,created_at FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
        codes = cur.execute("SELECT code,created_at FROM generated_codes WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
        text = f"📜 <b>USER HISTORY</b>\nUser: <code>{uid}</code>\n\n🪙 <b>Coin History</b>\n"
        text += "\n".join(f"{'+' if x['amount'] > 0 else ''}{x['amount']} — {x['action']} — {x['created_at']}" for x in coins) or "No coin history."
        text += "\n\n💳 <b>Payments</b>\n"
        text += "\n".join(f"#{x['id']} — ₹{x['amount']} — {x['status']}" for x in payments) or "No payments."
        text += "\n\n🔐 <b>Codes</b>\n"
        text += "\n".join(f"{x['code']} — {x['created_at']}" for x in codes) or "No codes."
        await call.message.answer(text)
    await call.answer()

@dp.callback_query(F.data.startswith("adm_user_delconfirm:"))
async def adm_user_delconfirm(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    uid = int(call.data.split(":", 1)[1])
    if uid in ADMIN_IDS:
        await call.answer("You cannot delete an admin.", show_alert=True); return
    exists = cur.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone()
    if not exists:
        await call.message.answer("❌ User not found.")
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚠️ YES, DELETE", callback_data=f"adm_user_delete:{uid}"),
             InlineKeyboardButton(text="❌ CANCEL", callback_data="adm_user_management")]
        ])
        await call.message.answer(f"⚠️ Delete user <code>{uid}</code> and their stored history? This cannot be undone.", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data.startswith("adm_user_delete:"))
async def adm_user_delete(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    uid = int(call.data.split(":", 1)[1])
    if uid in ADMIN_IDS:
        await call.answer("You cannot delete an admin.", show_alert=True); return
    cur.execute("DELETE FROM coin_history WHERE user_id=?", (uid,))
    cur.execute("DELETE FROM generated_codes WHERE user_id=?", (uid,))
    cur.execute("DELETE FROM payments WHERE user_id=?", (uid,))
    cur.execute("DELETE FROM users WHERE user_id=?", (uid,))
    db.commit()
    await call.message.answer(f"🗑️ User <code>{uid}</code> deleted.", reply_markup=user_management_keyboard())
    await call.answer("User deleted.")

@dp.callback_query(F.data == "adm_coins")
async def adm_coins(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    await call.message.answer(
        "🪙 Coin management commands:\n\n"
        "<code>/addcoins USER_ID AMOUNT</code>\n"
        "<code>/removecoins USER_ID AMOUNT</code>"
    )
    await call.answer()

@dp.message(Command("addcoins"))
async def addcoins(message: Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Usage: /addcoins USER_ID AMOUNT"); return
    uid, amount = int(parts[1]), int(parts[2])
    if not cur.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone():
        await message.answer("❌ User not found."); return
    cur.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (amount, uid))
    record_coin_change(uid, amount, "ADMIN_ADD", message.from_user.id)
    log_admin(message.from_user.id,"ADD_COINS",f"{uid} +{amount}")
    await message.answer(f"✅ Added {amount} coins to {uid}.")

@dp.message(Command("removecoins"))
async def removecoins(message: Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Usage: /removecoins USER_ID AMOUNT"); return
    uid, amount = int(parts[1]), int(parts[2])
    if not cur.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone():
        await message.answer("❌ User not found."); return
    cur.execute("UPDATE users SET coins=MAX(coins-?,0) WHERE user_id=?", (amount, uid))
    record_coin_change(uid, -amount, "ADMIN_REMOVE", message.from_user.id)
    log_admin(message.from_user.id,"REMOVE_COINS",f"{uid} -{amount}")
    await message.answer(f"✅ Removed {amount} coins from {uid}.")

@dp.callback_query(F.data == "adm_payment")
async def adm_payment(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    await call.message.answer(
        f"💳 <b>PAYMENT SETTINGS</b>\n\n"
        f"UPI ID: <code>{get_setting('upi_id')}</code>\n"
        f"UPI Name: <b>{get_setting('upi_name')}</b>",
        reply_markup=admin_payment_keyboard()
    )
    await call.answer()

@dp.callback_query(F.data == "adm_setupi")
async def adm_setupi(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    set_setting(f"admin_wait_{call.from_user.id}", "upi_id")
    await call.message.answer("✏️ Send the new UPI ID now.\nExample: <code>name@upi</code>")
    await call.answer()

@dp.callback_query(F.data == "adm_setupiname")
async def adm_setupiname(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    set_setting(f"admin_wait_{call.from_user.id}", "upi_name")
    await call.message.answer("✏️ Send the new UPI display name now.")
    await call.answer()

@dp.callback_query(F.data == "adm_packages")
async def adm_packages(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    rows = cur.execute("SELECT * FROM packages ORDER BY coins").fetchall()
    text = "📦 <b>PACKAGES</b>\n\n" + "\n".join(
        f"ID {p['id']}: {p['coins']} coins — ₹{p['price']} — {'ON' if p['active'] else 'OFF'}"
        for p in rows
    )
    await call.message.answer(text)
    await call.answer()

@dp.callback_query(F.data == "adm_addpkg")
async def adm_addpkg(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    set_setting(f"admin_wait_{call.from_user.id}", "package")
    await call.message.answer("✏️ Send package as: <code>COINS PRICE</code>\nExample: <code>250 1499</code>")
    await call.answer()

@dp.callback_query(F.data == "adm_pending")
async def adm_pending(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    rows = cur.execute("""
        SELECT payments.id, payments.user_id, payments.amount, packages.coins
        FROM payments JOIN packages ON payments.package_id=packages.id
        WHERE payments.status='PENDING' ORDER BY payments.id DESC
    """).fetchall()
    if not rows:
        await call.message.answer("⏳ No pending payments.")
    else:
        await call.message.answer(
            "⏳ <b>PENDING PAYMENTS</b>\n\n" +
            "\n".join(f"#{r['id']} — user {r['user_id']} — {r['coins']} coins — ₹{r['amount']}" for r in rows)
        )
    await call.answer()

@dp.callback_query(F.data == "adm_payhistory")
async def adm_payhistory(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    rows = cur.execute("""
        SELECT id,user_id,amount,status FROM payments ORDER BY id DESC LIMIT 30
    """).fetchall()
    await call.message.answer(
        "📜 <b>PAYMENT HISTORY</b>\n\n" +
        ("\n".join(f"#{r['id']} — {r['user_id']} — ₹{r['amount']} — {r['status']}" for r in rows) or "No payments.")
    )
    await call.answer()

@dp.callback_query(F.data == "adm_coin_history")
async def adm_coin_history(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    rows = cur.execute("SELECT * FROM coin_history ORDER BY id DESC LIMIT 30").fetchall()
    text = "🪙 <b>COIN HISTORY</b>\n\n"
    text += "\n".join(
        f"#{r['id']} — User <code>{r['user_id']}</code> — "
        f"<b>{'+' if r['amount'] > 0 else ''}{r['amount']}</b> — {r['action']}"
        for r in rows
    ) or "No coin history yet."
    await call.message.answer(text)
    await call.answer()


@dp.callback_query(F.data == "adm_statistics")
async def adm_statistics(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    users = cur.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    payments = cur.execute("SELECT COUNT(*) c FROM payments").fetchone()["c"]
    approved = cur.execute("SELECT COUNT(*) c FROM payments WHERE status='APPROVED'").fetchone()["c"]
    pending = cur.execute("SELECT COUNT(*) c FROM payments WHERE status='PENDING'").fetchone()["c"]
    rejected = cur.execute("SELECT COUNT(*) c FROM payments WHERE status='REJECTED'").fetchone()["c"]
    revenue = cur.execute("SELECT COALESCE(SUM(amount),0) c FROM payments WHERE status='APPROVED'").fetchone()["c"]
    codes = cur.execute("SELECT COUNT(*) c FROM generated_codes").fetchone()["c"]
    coins = cur.execute("SELECT COALESCE(SUM(coins),0) c FROM users").fetchone()["c"]
    await call.message.answer(
        "📊 <b>BOT STATISTICS</b>\n\n"
        f"👤 Users: <b>{users}</b>\n"
        f"🪙 Coins in accounts: <b>{coins}</b>\n"
        f"🔐 Codes generated: <b>{codes}</b>\n\n"
        f"💳 Total payments: <b>{payments}</b>\n"
        f"✅ Approved: <b>{approved}</b>\n"
        f"⏳ Pending: <b>{pending}</b>\n"
        f"❌ Rejected: <b>{rejected}</b>\n"
        f"💰 Approved revenue: <b>₹{revenue}</b>"
    )
    await call.answer()


@dp.callback_query(F.data == "adm_settings")
async def adm_settings(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    cost = get_setting("code_cost", DEFAULT_COIN_COST)
    maintenance = "ON 🔴" if maintenance_enabled() else "OFF 🟢"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ SET CODE COST", callback_data="adm_setcost")],
        [InlineKeyboardButton(text="💰 CHANGE COIN PACKAGE PRICE", callback_data="adm_coin_price")],
        [InlineKeyboardButton(text="🛠️ CHANGE SUPPORT", callback_data="adm_setsupport")],
        [InlineKeyboardButton(text="✏️ SET UPI ID", callback_data="adm_setupi")],
        [InlineKeyboardButton(text="✏️ SET UPI NAME", callback_data="adm_setupiname")],
        [InlineKeyboardButton(text="🔧 MAINTENANCE MODE", callback_data="adm_maintenance")],
        [InlineKeyboardButton(text="🏠 ADMIN PANEL", callback_data="admin_panel")]
    ])
    await call.message.answer(
        f"⚙️ <b>BOT SETTINGS</b>\n\n🔒 Code cost: <b>{cost} COIN</b>\n"
        f"🔧 Maintenance: <b>{maintenance}</b>\n"
        f"💳 UPI ID: <code>{get_setting('upi_id')}</code>\n"
        f"👤 UPI Name: <b>{get_setting('upi_name')}</b>", reply_markup=kb
    )
    await call.answer()


@dp.callback_query(F.data == "adm_setcost")
async def adm_setcost(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    set_setting(f"admin_wait_{call.from_user.id}", "code_cost")
    await call.message.answer("✏️ Send the new code cost in coins. Example: <code>10</code>")
    await call.answer()



@dp.callback_query(F.data == "adm_setsupport")
async def adm_setsupport(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True)
        return
    set_setting(f"admin_wait_{call.from_user.id}", "support_link")
    await call.message.answer(
        "🛠️ <b>CHANGE SUPPORT</b>\n\n"
        "Send the new support username or link.\n"
        "Example: <code>@your_support</code>"
    )
    await call.answer()


@dp.callback_query(F.data == "adm_coin_price")
async def adm_coin_price(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True)
        return
    rows = cur.execute("SELECT id,coins,price,active FROM packages ORDER BY coins").fetchall()
    if not rows:
        await call.message.answer("❌ No coin packages found.")
        await call.answer()
        return

    kb = []
    for p in rows:
        kb.append([InlineKeyboardButton(
            text=f"✏️ {p['coins']} COINS — ₹{p['price']}",
            callback_data=f"adm_price:{p['id']}"
        )])
    kb.append([InlineKeyboardButton(text="🏠 ADMIN PANEL", callback_data="admin_panel")])
    await call.message.answer(
        "💰 <b>CHANGE COIN PACKAGE PRICE</b>\n\nSelect a package:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("adm_price:"))
async def adm_price_select(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True)
        return
    pid = int(call.data.split(":")[1])
    p = cur.execute("SELECT id,coins,price FROM packages WHERE id=?", (pid,)).fetchone()
    if not p:
        await call.answer("Package not found.", show_alert=True)
        return
    set_setting(f"admin_wait_{call.from_user.id}", f"package_price:{pid}")
    await call.message.answer(
        f"✏️ Send the new price for <b>{p['coins']} COINS</b>.\n"
        f"Current price: <b>₹{p['price']}</b>\n\n"
        "Example: <code>149</code>"
    )
    await call.answer()


@dp.callback_query(F.data == "adm_maintenance")
async def adm_maintenance(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    new_value = "0" if maintenance_enabled() else "1"
    set_setting("maintenance_mode", new_value)
    status = "ENABLED 🔴" if new_value == "1" else "DISABLED 🟢"
    await call.message.answer(f"🔧 <b>MAINTENANCE MODE {status}</b>\n\nAdmins can still use the admin panel.")
    await call.answer("Maintenance updated.")


@dp.callback_query(F.data == "adm_broadcast")
async def adm_broadcast(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    set_setting(f"admin_wait_{call.from_user.id}", "broadcast")
    await call.message.answer("📢 Send the broadcast message now.")
    await call.answer()

@dp.callback_query(F.data == "adm_support")
async def adm_support(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Admin only.", show_alert=True); return
    await call.message.answer("🛠️ Support settings can be added here for your support username/channel.")
    await call.answer()



# ============================================================
# START
# ============================================================
async def main():
    global bot
    if BOT_TOKEN == "PASTE_BOT_TOKEN_HERE":
        raise RuntimeError("Set BOT_TOKEN environment variable before running.")
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    # ---- Small HTTP server so Render Web Service detects an open port ----
    from aiohttp import web

    async def health(request):
        return web.Response(text="Bot is running ✅")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info("Health server started on port %s", port)
    # ----------------------------------------------------------------------

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
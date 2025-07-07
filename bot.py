# ============================ CONFIG ============================
import os
import re
import uuid
import sqlite3
import tempfile
from functools import wraps
from yt_dlp import YoutubeDL
import requests
from bs4 import BeautifulSoup

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

TOKEN = "7848205960:AAHCYy934Nof5u7FFS23GXnnuqH1SkiOJjA"
ADMIN_ID = 5997101799
MAX_CONCURRENT_DOWNLOADS = 2
DB_PATH = "bot_data.db"

os.makedirs("download", exist_ok=True)

# ============================ DATABASE ============================
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
c = conn.cursor()
c.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE,
    username TEXT,
    quality TEXT DEFAULT 'high'
)
""")
c.execute("""
CREATE TABLE IF NOT EXISTS active_downloads (
    telegram_id INTEGER,
    count INTEGER DEFAULT 0
)
""")
conn.commit()
c.execute("""
CREATE TABLE IF NOT EXISTS history (
    user_id INTEGER,
    query TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)
""")
conn.commit()

def get_or_create_user(telegram_id, username):
    c.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
    row = c.fetchone()
    if not row:
        c.execute("INSERT INTO users (telegram_id, username) VALUES (?, ?)", (telegram_id, username))
        conn.commit()
        return c.lastrowid
    return row[0]

def set_user_quality(telegram_id, quality):
    c.execute("UPDATE users SET quality = ? WHERE telegram_id = ?", (quality, telegram_id))
    conn.commit()

def get_user_quality(telegram_id):
    c.execute("SELECT quality FROM users WHERE telegram_id = ?", (telegram_id,))
    row = c.fetchone()
    return row[0] if row else 'high'

# ============================ DECORATOR ============================
def limit_downloads(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        telegram_id = update.effective_user.id
        c.execute("SELECT count FROM active_downloads WHERE telegram_id = ?", (telegram_id,))
        row = c.fetchone()
        count = row[0] if row else 0
        if count >= MAX_CONCURRENT_DOWNLOADS:
            await update.message.reply_text("⛔️ You reached the max download limit.")
            return
        c.execute("REPLACE INTO active_downloads (telegram_id, count) VALUES (?, ?)", (telegram_id, count + 1))
        conn.commit()
        try:
            await func(update, context)
        finally:
            c.execute("UPDATE active_downloads SET count = count - 1 WHERE telegram_id = ?", (telegram_id,))
            conn.commit()
    return wrapper

# ============================ COMMANDS ============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = get_or_create_user(user.id, user.username or '')
    await update.message.reply_text(
        f"👋 Welcome! Your ID is #{user_id}.\nSend an Xvideos link or search keyword."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📌 *XVideos Downloader Bot*\n\n"
        "• Send a video link to download.\n"
        "• Send a keyword to search.\n"
        "• /settings — Set video quality\n"
        "• /start — Restart the bot\n"
        "• /help — Show this menu",
        parse_mode="Markdown"
    )

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔴 High", callback_data='quality_high')],
        [InlineKeyboardButton("🟠 Medium", callback_data='quality_medium')],
        [InlineKeyboardButton("🟢 Low", callback_data='quality_low')]
    ]
    await update.message.reply_text("Choose your default quality:", reply_markup=InlineKeyboardMarkup(keyboard))

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    quality = query.data.split('_')[1]
    set_user_quality(query.from_user.id, quality)
    await query.edit_message_text(f"✅ Default quality set to *{quality}*", parse_mode="Markdown")

# ============================ ADMIN ============================
async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    c.execute("SELECT id, telegram_id, username, quality FROM users")
    rows = c.fetchall()
    if not rows:
        await update.message.reply_text("No users found.")
        return
    msg = "📋 *User List:*\n\n"
    for row in rows:
        uid, tg_id, username, quality = row
        msg += f"#{uid} — @{username or 'N/A'} — `{tg_id}` — {quality}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    c.execute("SELECT COUNT(*) FROM users")
    user_count = c.fetchone()[0]
    c.execute("SELECT SUM(count) FROM active_downloads")
    active_downloads = c.fetchone()[0] or 0
    msg = (
        f"📊 *Bot Stats:*\n\n"
        f"👥 Total users: {user_count}\n"
        f"📥 Active downloads: {active_downloads}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# ============================ LOGIC ============================
def is_xvideos_link(text):
    return "xvideos.com" in text

@limit_downloads
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    # Save query to history table
    user_id = get_or_create_user(update.effective_user.id, update.effective_user.username or '')
    c.execute("INSERT INTO history (user_id, query) VALUES (?, ?)", (user_id, text))
    conn.commit()

    if is_xvideos_link(text):
        await handle_video_download(update, context, text)
    else:
        query = text.lower()
        await show_search_results(update, context, query, page=1)

async def handle_video_download(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    msg = await update.message.reply_text("📥 Fetching video info...")

    quality = get_user_quality(update.effective_user.id)
    ydl_opts = {
        'quiet': True,
        'skip_download': True,
        'forcejson': True,
        'format': 'best' if quality == 'high' else 'worst' if quality == 'low' else 'best[height<=480]',
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        title = info.get('title')
        duration = info.get('duration')
        filesize = info.get('filesize') or info.get('filesize_approx')
        thumb = info.get('thumbnail')
        duration_str = f"{duration // 60}:{duration % 60:02d}"

        caption = f"*{title}*\n🕒 {duration_str}\n📦 {round(filesize / 1e6, 2)} MB"
        keyboard = [[InlineKeyboardButton("⬇️ Download", callback_data=f"download|{url}")]]
        await msg.edit_text(
            caption, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )
        if thumb:
            await update.message.reply_photo(thumb)
    except Exception as e:
        await msg.edit_text(f"❌ Failed to get video: {e}")

async def download_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    index = int(query.data.split('_')[-1])
    results = context.user_data.get('search_results', [])
    if index >= len(results):
        await query.message.reply_text("❌ Could not find the selected video.")
        return
    url = results[index]['url']

    await query.message.delete()
    await context.bot.send_message(chat_id=query.message.chat_id, text="⏬ Downloading video...")

    user_id = query.from_user.id
    quality = get_user_quality(user_id)
    unique_id = f"{user_id}_{uuid.uuid4().hex[:8]}"

    ydl_opts = {
        'format': 'best' if quality == 'high' else 'worst' if quality == 'low' else 'best[height<=480]',
        'outtmpl': os.path.join("download", f"{unique_id}.%(ext)s"),
        'noplaylist': True,
        'quiet': True,
        'http_chunk_size': 1048576,
        'retries': 15,
        'fragment_retries': 15,
        'sleep_interval_requests': 2,
        'socket_timeout': 30,
        'nocheckcertificate': True,
        'geo_bypass': True,
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url)
            path = ydl.prepare_filename(info)
            file_size = os.path.getsize(path)

        if file_size >= 2 * 1024 * 1024 * 1024:
            await context.bot.send_message(chat_id=user_id, text="⚠️ File too large for Telegram (2GB limit).")
        else:
            await context.bot.send_video(chat_id=user_id, video=open(path, 'rb'), caption=info.get('title'))
            await context.bot.send_message(chat_id=user_id, text="✅ Sent successfully.")
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        await context.bot.send_message(chat_id=user_id, text=f"❌ Error downloading: {e}")

# ============================ SEARCH ============================
def search_xvideos(query, page=1):
    headers = {"User-Agent": "Mozilla/5.0"}
    url = f"https://www.xvideos.com/?k={query.replace(' ', '+')}&p={page-1}"
    res = requests.get(url, headers=headers)
    soup = BeautifulSoup(res.content, 'html.parser')
    results = []

    for vid in soup.select("div.thumb-block"):
        a = vid.select_one("a")
        title = a.get("title")
        href = "https://www.xvideos.com" + a.get("href")
        duration = vid.select_one(".duration")
        thumb = vid.select_one("img")

        results.append({
            "title": title,
            "url": href,
            "duration": duration.text.strip() if duration else "??:??",
            "thumb": thumb.get("data-src") if thumb and thumb.has_attr("data-src") else None
        })

    return results

async def show_search_results(update, context, query, page):
    results = search_xvideos(query, page)
    context.user_data['search_results'] = results

    if not results:
        await update.message.reply_text("❌ No results found.")
        return

    context.user_data['search_query'] = query
    context.user_data['search_page'] = page

    for item in results[:5]:
        buttons = [[InlineKeyboardButton("⬇ Download", callback_data=f"search_dl_{results.index(item)}")]]
        caption = f"*{item['title']}*\n🕒 {item['duration']}"
        if item['thumb']:
            await update.message.reply_photo(item['thumb'], caption=caption, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
        else:
            await update.message.reply_text(caption, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("⬅ Prev", callback_data="search_prev"))
    nav_buttons.append(InlineKeyboardButton("➡ Next", callback_data="search_next"))
    await update.message.reply_text("🔍 More results:", reply_markup=InlineKeyboardMarkup([nav_buttons]))

async def search_pagination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    current_page = context.user_data.get('search_page', 1)
    keyword = context.user_data.get('search_query', '')

    if not keyword:
        await query.message.reply_text("⚠️ No active search.")
        return

    new_page = current_page + 1 if data == "search_next" else max(1, current_page - 1)
    context.user_data['search_page'] = new_page

    await query.message.delete()
    await show_search_results(update, context, keyword, new_page)

async def show_user_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /history <user_id>")
        return
    try:
        target_id = int(context.args[0])
    except:
        await update.message.reply_text("❌ Invalid ID.")
        return

    c.execute("SELECT query, timestamp FROM history WHERE user_id = ? ORDER BY timestamp DESC", (target_id,))
    rows = c.fetchall()
    if not rows:
        await update.message.reply_text("No history found.")
        return

    from telegram.constants import MAX_MESSAGE_LENGTH

    msg = f"🕓 History for user #{target_id}:\n\n"
    messages = []
    for q, t in rows:
        line = f"• `{q}`\n   _({t})_\n"
        if len(msg + line) > MAX_MESSAGE_LENGTH:
            messages.append(msg)
            msg = ""
        msg += line

    if msg:
        messages.append(msg)

    for part in messages:
        await update.message.reply_text(part, parse_mode="Markdown")



# ============================ MAIN ============================
# ============================ MAIN ============================
def main():
    app = ApplicationBuilder().token(TOKEN).concurrent_updates(True).build()  # 🔧 DISABLE old Updater

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("users", list_users))
    app.add_handler(CommandHandler("stats", show_stats))
    app.add_handler(CommandHandler("history", show_user_history))

    app.add_handler(CallbackQueryHandler(settings_callback, pattern="^quality_"))
    app.add_handler(CallbackQueryHandler(download_button_callback, pattern="^search_dl_"))
    app.add_handler(CallbackQueryHandler(search_pagination_callback, pattern="^search_"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()


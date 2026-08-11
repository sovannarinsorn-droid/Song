
import os
import sqlite3
import threading
import time

import telebot
from telebot import types
import yt_dlp

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
ADMIN_ID = 8266854899
MAX_RESULTS = 5
BROADCAST_INTERVAL_SECONDS = 6 * 60 * 60

DATA_DIR = os.environ.get("DATA_DIR", "data")
DB_PATH = os.environ.get("DB_PATH", os.path.join(DATA_DIR, "bot.db"))
DOWNLOAD_DIR = os.path.join(DATA_DIR, "downloads")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

bot = telebot.TeleBot(BOT_TOKEN)

telebot.apihelper.READ_TIMEOUT = 40
telebot.apihelper.CONNECT_TIMEOUT = 15

search_cache = {}


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            query TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            title TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ads (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            file_id TEXT,
            caption TEXT,
            button_text TEXT,
            button_url TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    return conn


def db_log_user(chat_id, username):
    """រក្សាទុក user ថ្មី។ ត្រឡប់ True បើ user នេះជា user ថ្មី (ដំបូងគេចូល)"""
    conn = db_connect()
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO users (chat_id, username) VALUES (?, ?)",
            (chat_id, username or ""),
        )
        is_new = cur.rowcount > 0
    conn.close()
    return is_new


def db_all_user_ids():
    conn = db_connect()
    rows = conn.execute("SELECT chat_id FROM users").fetchall()
    conn.close()
    return [r[0] for r in rows]


def db_set_ad(file_id, caption, button_text, button_url):
    conn = db_connect()
    with conn:
        conn.execute(
            """INSERT INTO ads (id, file_id, caption, button_text, button_url, updated_at)
               VALUES (1, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET
                   file_id=excluded.file_id, caption=excluded.caption,
                   button_text=excluded.button_text, button_url=excluded.button_url,
                   updated_at=CURRENT_TIMESTAMP""",
            (file_id, caption, button_text, button_url),
        )
    conn.close()


def db_get_ad():
    conn = db_connect()
    row = conn.execute(
        "SELECT file_id, caption, button_text, button_url FROM ads WHERE id = 1"
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"file_id": row[0], "caption": row[1], "button_text": row[2], "button_url": row[3]}


def db_log_search(chat_id, query):
    conn = db_connect()
    with conn:
        conn.execute(
            "INSERT INTO searches (chat_id, query) VALUES (?, ?)",
            (chat_id, query),
        )
    conn.close()


def db_log_download(chat_id, title):
    conn = db_connect()
    with conn:
        conn.execute(
            "INSERT INTO downloads (chat_id, title) VALUES (?, ?)",
            (chat_id, title),
        )
    conn.close()


def db_stats():
    conn = db_connect()
    cur = conn.cursor()
    users = cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    searches = cur.execute("SELECT COUNT(*) FROM searches").fetchone()[0]
    downloads = cur.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
    conn.close()
    return users, searches, downloads


def format_duration(seconds):
    if not seconds:
        return "??:??"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def search_youtube(query, max_results=MAX_RESULTS):
    """ស្វែងរកចម្រៀងតាមចំណងជើងនៅលើ YouTube (metadata ប៉ុណ្ណោះ, មិនទាញយក)"""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "default_search": f"ytsearch{max_results}",
        "noplaylist": True,
        "js_runtimes": {"node": {}},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(query, download=False)
        entries = info.get("entries", []) if info else []
        results = []
        for e in entries:
            if not e:
                continue
            results.append({
                "id": e.get("id"),
                "title": e.get("title") or "Unknown",
                "duration": e.get("duration"),
                "uploader": e.get("uploader") or e.get("channel") or "",
                "url": f"https://www.youtube.com/watch?v={e.get('id')}",
            })
        return results


COOKIES_FILE = os.path.join(DATA_DIR, "cookies.txt")


def _find_cookies_file():
    """ស្វែងរកឯកសារ cookies.txt ពី DATA_DIR ឬ root folder
    កំណត់ USE_COOKIES=0 ជា env var ដើម្បីបិទ cookies ជាបណ្តោះអាសន្ន (សម្រាប់ test)"""
    if os.environ.get("USE_COOKIES", "1") == "0":
        return None
    candidates = [COOKIES_FILE, "cookies.txt"]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def download_audio(video_url, out_path_template, max_retries=4):
    """ទាញយកសំឡេង MP3 ពី YouTube (ការកំណត់សាមញ្ញ)"""
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_path_template,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])


def build_ad_markup(ad):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(text=ad["button_text"], url=ad["button_url"]))
    return markup


def send_ad_to(chat_id, ad):
    bot.send_photo(
        chat_id,
        ad["file_id"],
        caption=ad["caption"],
        reply_markup=build_ad_markup(ad),
    )


def broadcast_ad():
    """ផ្ញើពាណិជ្ជកម្មទៅអ្នកប្រើទាំងអស់ ត្រឡប់ (success, failed)"""
    ad = db_get_ad()
    if not ad or not ad.get("file_id"):
        return 0, 0
    success, failed = 0, 0
    for chat_id in db_all_user_ids():
        try:
            send_ad_to(chat_id, ad)
            success += 1
        except Exception:
            failed += 1
        time.sleep(0.05)
    return success, failed


def broadcast_scheduler_loop():
    """ផ្ញើពាណិជ្ជកម្មដោយស្វ័យប្រវត្តិរៀងរាល់ BROADCAST_INTERVAL_SECONDS"""
    while True:
        time.sleep(BROADCAST_INTERVAL_SECONDS)
        try:
            success, failed = broadcast_ad()
            if ADMIN_ID:
                bot.send_message(
                    ADMIN_ID,
                    f"📢 ការផ្សាយពាណិជ្ជកម្មស្វ័យប្រវត្តិចប់ហើយ\n✅ {success} នាក់ | ❌ {failed} នាក់",
                )
        except Exception:
            pass


@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    username = message.from_user.username if message.from_user else ""
    is_new = db_log_user(message.chat.id, username)
    bot.reply_to(
        message,
        "🎵 សួស្តី! ខ្ញុំជា Bot ស្វែងរកចម្រៀង\n\n"
        "គ្រាន់តែវាយចំណងជើងចម្រៀង ខ្ញុំនឹងស្វែងរកអោយអ្នកភ្លាមៗ!\n"
        "ឧទាហរណ៍: `ស្រឡាញ់គេម្នាក់ឯង`",
        parse_mode="Markdown",
    )
    if is_new:
        ad = db_get_ad()
        if ad and ad.get("file_id"):
            try:
                send_ad_to(message.chat.id, ad)
            except Exception:
                pass


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        return
    users, searches, downloads = db_stats()
    bot.reply_to(
        message,
        f"📊 ស្ថិតិ Bot\n"
        f"👥 អ្នកប្រើ: {users}\n"
        f"🔍 ការស្វែងរក: {searches}\n"
        f"⬇️ ការទាញយក: {downloads}",
    )


def admin_menu_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("👥 អ្នកប្រើ", callback_data="admin:users"),
        types.InlineKeyboardButton("📊 ស្ថិតិ", callback_data="admin:stats"),
    )
    markup.add(
        types.InlineKeyboardButton("🖼 កំណត់ពាណិជ្ជកម្ម", callback_data="admin:setad"),
        types.InlineKeyboardButton("👁 មើលពាណិជ្ជកម្ម", callback_data="admin:preview"),
    )
    markup.add(types.InlineKeyboardButton("📢 ផ្សាយឥឡូវនេះ", callback_data="admin:broadcast"))
    return markup


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        return
    bot.send_message(message.chat.id, "🛠 Admin Panel", reply_markup=admin_menu_markup())


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin:"))
def handle_admin_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "អ្នកមិនមែនជា admin ទេ", show_alert=True)
        return

    action = call.data.split(":", 1)[1]
    chat_id = call.message.chat.id

    if action == "users":
        users, searches, downloads = db_stats()
        ids = db_all_user_ids()
        preview = ", ".join(str(i) for i in ids[:20])
        bot.answer_callback_query(call.id)
        bot.send_message(
            chat_id,
            f"👥 សរុបអ្នកប្រើ: {users}\n\nID ថ្មីៗ (20 ដំបូង):\n{preview or '—'}",
        )

    elif action == "stats":
        users, searches, downloads = db_stats()
        bot.answer_callback_query(call.id)
        bot.send_message(
            chat_id,
            f"📊 ស្ថិតិ Bot\n👥 អ្នកប្រើ: {users}\n🔍 ការស្វែងរក: {searches}\n⬇️ ការទាញយក: {downloads}",
        )

    elif action == "setad":
        bot.answer_callback_query(call.id)
        ad_draft[chat_id] = {}
        msg = bot.send_message(chat_id, "🖼 សូមផ្ញើ *រូបភាព* សម្រាប់ពាណិជ្ជកម្ម:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, process_ad_photo)

    elif action == "preview":
        ad = db_get_ad()
        bot.answer_callback_query(call.id)
        if not ad or not ad.get("file_id"):
            bot.send_message(chat_id, "😔 មិនទាន់មានពាណិជ្ជកម្មត្រូវបានកំណត់ទេ")
        else:
            send_ad_to(chat_id, ad)

    elif action == "broadcast":
        bot.answer_callback_query(call.id, "កំពុងផ្សាយ...")
        ad = db_get_ad()
        if not ad or not ad.get("file_id"):
            bot.send_message(chat_id, "😔 មិនទាន់មានពាណិជ្ជកម្មត្រូវបានកំណត់ទេ")
            return

        def do_broadcast():
            success, failed = broadcast_ad()
            bot.send_message(chat_id, f"📢 ផ្សាយចប់ហើយ\n✅ {success} នាក់ | ❌ {failed} នាក់")

        threading.Thread(target=do_broadcast).start()


ad_draft = {}


def process_ad_photo(message):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        return
    chat_id = message.chat.id

    if message.content_type != "photo":
        msg = bot.reply_to(message, "❌ នេះមិនមែនរូបភាពទេ។ សូមផ្ញើរូបភាពម្តងទៀត:")
        bot.register_next_step_handler(msg, process_ad_photo)
        return

    ad_draft[chat_id] = {"file_id": message.photo[-1].file_id}
    msg = bot.send_message(chat_id, "✏️ សូមវាយ *អត្ថបទផ្សាយពាណិជ្ជកម្ម*:", parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_ad_caption)


def process_ad_caption(message):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        return
    chat_id = message.chat.id

    if not message.text or not message.text.strip():
        msg = bot.reply_to(message, "❌ សូមវាយអត្ថបទផ្សាយពាណិជ្ជកម្ម:")
        bot.register_next_step_handler(msg, process_ad_caption)
        return

    ad_draft.setdefault(chat_id, {})["caption"] = message.text.strip()
    msg = bot.send_message(chat_id, "🔘 សូមវាយ *ឈ្មោះប៊ូតុង* (ឧទាហរណ៍: មើលឥឡូវនេះ):", parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_ad_button_text)


def process_ad_button_text(message):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        return
    chat_id = message.chat.id

    if not message.text or not message.text.strip():
        msg = bot.reply_to(message, "❌ សូមវាយឈ្មោះប៊ូតុង:")
        bot.register_next_step_handler(msg, process_ad_button_text)
        return

    ad_draft.setdefault(chat_id, {})["button_text"] = message.text.strip()
    msg = bot.send_message(chat_id, "🔗 សូមវាយ *តំណលីង* (ត្រូវចាប់ផ្តើមដោយ http:// ឬ https://):", parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_ad_button_url)


def process_ad_button_url(message):
    if message.from_user is None or message.from_user.id != ADMIN_ID:
        return
    chat_id = message.chat.id

    button_url = (message.text or "").strip()
    if not button_url.startswith(("http://", "https://")):
        msg = bot.reply_to(message, "❌ តំណលីង (URL) ត្រូវចាប់ផ្តើមដោយ http:// ឬ https:// សូមផ្ញើម្តងទៀត:")
        bot.register_next_step_handler(msg, process_ad_button_url)
        return

    draft = ad_draft.pop(chat_id, {})
    if not draft.get("file_id") or not draft.get("caption") or not draft.get("button_text"):
        bot.send_message(chat_id, "❌ មានបញ្ហា សូមចាប់ផ្តើមម្តងទៀតតាម /admin")
        return

    db_set_ad(draft["file_id"], draft["caption"], draft["button_text"], button_url)

    bot.send_message(chat_id, "✅ បានកំណត់ពាណិជ្ជកម្មរួចរាល់!")
    send_ad_to(chat_id, db_get_ad())


@bot.message_handler(func=lambda m: m.content_type == "text" and not m.text.startswith("/"))
def handle_search(message):
    query = message.text.strip()
    if len(query) < 2:
        bot.reply_to(message, "សូមវាយចំណងជើងចម្រៀងឱ្យបានច្បាស់លាស់ 🎵")
        return

    db_log_user(message.chat.id, message.from_user.username if message.from_user else "")
    db_log_search(message.chat.id, query)

    wait_msg = bot.reply_to(message, f"🔍 កំពុងស្វែងរក \"{query}\" ...")

    try:
        results = search_youtube(query)
    except Exception as e:
        bot.edit_message_text(f"❌ ស្វែងរកមិនបានទេ: {e}", message.chat.id, wait_msg.message_id)
        return

    if not results:
        bot.edit_message_text("😔 រកមិនឃើញចម្រៀងទេ សូមសាកល្បងឈ្មោះផ្សេង", message.chat.id, wait_msg.message_id)
        return

    search_cache[message.chat.id] = results

    markup = types.InlineKeyboardMarkup(row_width=1)
    text_lines = [f"🎶 លទ្ធផលសម្រាប់ \"{query}\":\n"]
    for idx, r in enumerate(results):
        text_lines.append(f"{idx + 1}. {r['title']} — {format_duration(r['duration'])} ({r['uploader']})")
        markup.add(types.InlineKeyboardButton(
            text=f"{idx + 1}. {r['title'][:45]}",
            callback_data=f"dl:{idx}"
        ))

    bot.edit_message_text(
        "\n".join(text_lines),
        message.chat.id,
        wait_msg.message_id,
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("dl:"))
def handle_download(call):
    chat_id = call.message.chat.id
    idx = int(call.data.split(":")[1])
    results = search_cache.get(chat_id)

    if not results or idx >= len(results):
        bot.answer_callback_query(call.id, "លទ្ធផលនេះលែងមានទៀតហើយ សូមស្វែងរកម្តងទៀត", show_alert=True)
        return

    song = results[idx]
    bot.answer_callback_query(call.id, "កំពុងទាញយក...")
    status = bot.send_message(chat_id, f"⬇️ កំពុងទាញយក: {song['title']}")

    def do_download():
        out_template = os.path.join(DOWNLOAD_DIR, f"{chat_id}_{idx}.%(ext)s")
        mp3_path = os.path.join(DOWNLOAD_DIR, f"{chat_id}_{idx}.mp3")
        try:
            download_audio(song["url"], out_template)
            safe_title = "".join(c for c in song["title"] if c not in '\\/:*?"<>|').strip() or "song"
            with open(mp3_path, "rb") as f:
                bot.send_document(
                    chat_id, f,
                    visible_file_name=f"{safe_title}.mp3",
                    caption=f"🎵 {song['title']}",
                )
            bot.delete_message(chat_id, status.message_id)
            db_log_download(chat_id, song["title"])
        except Exception as e:
            bot.edit_message_text(f"❌ ទាញយកមិនបានទេ: {e}", chat_id, status.message_id)
        finally:
            if os.path.exists(mp3_path):
                os.remove(mp3_path)

    threading.Thread(target=do_download).start()


if __name__ == "__main__":
    print("🤖 Song Search Bot កំពុងដំណើរការ...")
    threading.Thread(target=broadcast_scheduler_loop, daemon=True).start()
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
        except Exception as e:
            print(f"⚠️ Polling crashed: {e}. កំពុងព្យាយាមម្តងទៀតក្នុង 5 វិនាទី...")
            time.sleep(5)

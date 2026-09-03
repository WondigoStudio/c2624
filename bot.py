from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import wave
from datetime import datetime, time as dtime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import vosk

import storage

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Moscow")
VOSK_MODEL_PATH = os.getenv("VOSK_MODEL_PATH", "/app/vosk-model")
MAP_DIR = os.path.join(os.path.dirname(__file__), os.getenv("MAP_DIR", "map"))
MAP_FLOOR_COUNT = int(os.getenv("MAP_FLOOR_COUNT", "3"))

TZ = ZoneInfo(TIMEZONE_NAME)

vosk.SetLogLevel(-1)
vosk_model = vosk.Model(VOSK_MODEL_PATH) if os.path.isdir(VOSK_MODEL_PATH) else None

CABINET_RE = re.compile(r"^\s*([A-Za-zА-Яа-яЁё]+\d+)\.(\d+)\.(\d)(\d+)\s*$")
DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})$")


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def mention_html(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{escape_html(name)}</a>'


def find_map_file(stem: str) -> str | None:
    if not os.path.isdir(MAP_DIR):
        return None
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        path = os.path.join(MAP_DIR, stem + ext)
        if os.path.isfile(path):
            return path
    return None


async def send_map_photo(update: Update, stem: str, caption: str, not_found_text: str) -> None:
    path = find_map_file(stem)
    if not path:
        await update.message.reply_text(not_found_text)
        return

    data = storage.load()
    cached_file_id = storage.get_map_file_id(data, stem)
    if cached_file_id:
        await update.message.reply_photo(photo=cached_file_id, caption=caption)
        return

    with open(path, "rb") as photo:
        sent = await update.message.reply_photo(photo=photo, caption=caption)
    if sent.photo:
        storage.set_map_file_id(data, stem, sent.photo[-1].file_id)
        storage.save(data)


async def send_all_floor_maps(update: Update) -> bool:
    data = storage.load()
    media = []
    stems = []
    open_files = []

    for floor in range(1, MAP_FLOOR_COUNT + 1):
        stem = f"floor_{floor}"
        path = find_map_file(stem)
        if not path:
            continue
        stems.append(stem)
        cached_file_id = storage.get_map_file_id(data, stem)
        if cached_file_id:
            media.append(InputMediaPhoto(media=cached_file_id, caption=f"Этаж {floor}"))
        else:
            f = open(path, "rb")
            open_files.append(f)
            media.append(InputMediaPhoto(media=f, caption=f"Этаж {floor}"))

    if not media:
        return False

    sent_messages = await update.message.reply_media_group(media=media)
    for f in open_files:
        f.close()

    for stem, msg in zip(stems, sent_messages):
        if msg.photo:
            storage.set_map_file_id(data, stem, msg.photo[-1].file_id)
    storage.save(data)
    return True


async def track_member(update: Update, data: dict) -> None:
    if not update.effective_user or not update.effective_chat:
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        return
    user = update.effective_user
    storage.register_member(data, update.effective_chat.id, user.id, user.username, user.first_name)


async def require_group(update: Update) -> bool:
    if update.effective_chat.type in ("group", "supergroup"):
        return True
    await update.message.reply_text("Эта команда работает только в группах.")
    return False


START_TEXT = (
    "Привет! Вот что я умею:\n\n"
    "/call — позвать всех в чате\n"
    "/call_deny — выйти из созыва или вернуться обратно\n"
    "/cab C1.1.323 — узнать павильон, блок, этаж и кабинет по коду\n"
    "/map — карты всех этажей, /map 3 — карта конкретного этажа\n"
    "/hb 17.03 — сохранить день рождения, /hb off — удалить\n"
    "/hb_info — список дней рождения в чате\n\n"
    "Голосовые, кружки и видео я расшифровываю автоматически."
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(START_TEXT)


async def cmd_call(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_group(update):
        return

    data = storage.load()
    await track_member(update, data)
    chat = storage.get_chat(data, update.effective_chat.id)
    storage.save(data)

    deny_list = set(chat["call_deny"])
    mentions = []
    for uid_str, info in chat["members"].items():
        uid = int(uid_str)
        if uid in deny_list:
            continue
        name = info.get("first_name") or info.get("username") or "участник"
        mentions.append(mention_html(uid, name))

    if not mentions:
        await update.message.reply_text("Пока некого звать.")
        return

    caller = update.effective_user.first_name or "Кто-то"
    text = f"📣 {caller} зовёт всех:\n" + " ".join(mentions)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_call_deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_group(update):
        return

    data = storage.load()
    await track_member(update, data)
    user_id = update.effective_user.id
    currently_denied = storage.is_call_denied(data, update.effective_chat.id, user_id)
    storage.set_call_deny(data, update.effective_chat.id, user_id, not currently_denied)
    storage.save(data)

    if currently_denied:
        await update.message.reply_text("Готово, теперь /call снова будет тебя звать.")
    else:
        await update.message.reply_text("Готово, ты больше не будешь получать упоминания по /call.")


async def cmd_cab(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /cab C1.1.323")
        return

    match = CABINET_RE.match(context.args[0])
    if not match:
        await update.message.reply_text("Не понял формат кабинета. Пример: /cab C1.1.323")
        return

    pavilion, block, floor, room = match.groups()
    info_text = f"Павильон {pavilion} Блок {block} Этаж {floor} Кабинет {room}"

    await send_map_photo(
        update,
        stem=f"floor_{floor}",
        caption=info_text,
        not_found_text=f"{info_text}\n(Карта этажа {floor} пока не загружена)",
    )


async def cmd_map(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        sent_album = await send_all_floor_maps(update)
        if sent_album:
            return
        await send_map_photo(
            update,
            stem="campus",
            caption="Карта кампуса",
            not_found_text="Карты пока не загружены.",
        )
        return

    floor_arg = context.args[0]
    if not floor_arg.isdigit():
        await update.message.reply_text("Использование: /map или /map 3")
        return

    await send_map_photo(
        update,
        stem=f"floor_{floor_arg}",
        caption=f"Этаж {floor_arg}",
        not_found_text=f"(Карта этажа {floor_arg} пока не загружена)",
    )


async def cmd_hb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = storage.load()
    if update.effective_chat.type in ("group", "supergroup"):
        await track_member(update, data)
    user = update.effective_user

    if not context.args:
        await update.message.reply_text("Использование: /hb 17.03. Чтобы удалить: /hb off")
        return

    if context.args[0].lower() == "off":
        storage.remove_birthday(data, user.id)
        storage.save(data)
        await update.message.reply_text("Твой день рождения удалён из списка.")
        return

    m = DATE_RE.match(context.args[0])
    if not m:
        await update.message.reply_text("Не понял дату. Формат: ДД.ММ, например /hb 17.03")
        return

    day, month = int(m.group(1)), int(m.group(2))
    try:
        datetime(2000, month, day)
    except ValueError:
        await update.message.reply_text("Такой даты не существует.")
        return

    date_str = f"{day:02d}.{month:02d}"
    name = user.first_name or user.username or "Без имени"
    storage.set_birthday(data, user.id, date_str, name)
    storage.save(data)

    await update.message.reply_text(f"Записал: {name} — {date_str}. Напомню за неделю 🎉")


async def cmd_hb_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = storage.load()

    if update.effective_chat.type == "private":
        info = storage.get_birthday(data, update.effective_user.id)
        if not info:
            await update.message.reply_text("У тебя пока не сохранён день рождения.")
        else:
            await update.message.reply_text(f"🎂 Твой день рождения: {info['date']}")
        return

    await track_member(update, data)
    storage.save(data)

    chat = storage.get_chat(data, update.effective_chat.id)
    member_ids = set(chat["members"].keys())
    birthdays = {uid: info for uid, info in data["users"].items() if uid in member_ids}

    if not birthdays:
        await update.message.reply_text("Пока никто в этом чате не добавил день рождения.")
        return

    def sort_key(item):
        d, m = item[1]["date"].split(".")
        return (int(m), int(d))

    lines = ["🎂 Дни рождения в чате:"]
    for uid_str, info in sorted(birthdays.items(), key=sort_key):
        lines.append(f"• {info['name']} — {info['date']}")

    await update.message.reply_text("\n".join(lines))


async def check_birthdays_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = storage.load()
    now = datetime.now(TZ)
    target = (now + timedelta(days=7)).date()
    target_str = f"{target.day:02d}.{target.month:02d}"
    current_year = str(now.year)

    for chat_id_str, chat in data["chats"].items():
        member_ids = set(chat["members"].keys())
        for uid_str, info in data["users"].items():
            if uid_str not in member_ids:
                continue
            if info["date"] != target_str:
                continue
            if chat["hb_notified"].get(uid_str) == current_year:
                continue
            await context.bot.send_message(
                chat_id=int(chat_id_str),
                text=f"🎉 Через неделю ({target_str}) день рождения у {mention_html(int(uid_str), info['name'])}!",
                parse_mode=ParseMode.HTML,
            )
            chat["hb_notified"][uid_str] = current_year

    storage.save(data)


def transcribe_with_vosk(media_path: str) -> str:
    wav_path = media_path + ".wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", media_path, "-ar", "16000", "-ac", "1", "-f", "wav", wav_path, "-loglevel", "error"],
        check=True,
    )

    wf = wave.open(wav_path, "rb")
    rec = vosk.KaldiRecognizer(vosk_model, wf.getframerate())
    parts = []
    while True:
        data = wf.readframes(4000)
        if not data:
            break
        if rec.AcceptWaveform(data):
            result = json.loads(rec.Result())
            if result.get("text"):
                parts.append(result["text"])
    final = json.loads(rec.FinalResult())
    if final.get("text"):
        parts.append(final["text"])
    wf.close()
    os.remove(wav_path)
    return " ".join(parts).strip()


async def transcribe_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not vosk_model:
        return

    message = update.message
    media = message.voice or message.video_note or message.audio or message.video
    if not media:
        return

    tg_file = await context.bot.get_file(media.file_id)
    local_dir = "/tmp/tgbot_media"
    os.makedirs(local_dir, exist_ok=True)

    if message.voice:
        ext = "ogg"
    elif message.video_note or message.video:
        ext = "mp4"
    else:
        ext = "mp3"
    local_path = os.path.join(local_dir, f"{media.file_unique_id}.{ext}")

    await tg_file.download_to_drive(local_path)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    text = transcribe_with_vosk(local_path)
    os.remove(local_path)

    if not text:
        return

    safe_text = escape_html(text)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"📝 <b>Транскрипция:</b>\n<blockquote>{safe_text}</blockquote>",
        reply_to_message_id=message.message_id,
        parse_mode=ParseMode.HTML,
    )


async def track_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.is_bot:
        return
    data = storage.load()
    await track_member(update, data)
    storage.save(data)


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def start_health_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("call", cmd_call))
    application.add_handler(CommandHandler("call_deny", cmd_call_deny))
    application.add_handler(CommandHandler("cab", cmd_cab))
    application.add_handler(CommandHandler("map", cmd_map))
    application.add_handler(CommandHandler("hb", cmd_hb))
    application.add_handler(CommandHandler("hb_info", cmd_hb_info))
    application.add_handler(
        MessageHandler(filters.VOICE | filters.VIDEO_NOTE | filters.AUDIO | filters.VIDEO, transcribe_voice)
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, track_any_message))

    if os.getenv("PORT"):
        start_health_server()

    if application.job_queue is not None:
        application.job_queue.run_daily(check_birthdays_job, time=dtime(hour=9, minute=0, tzinfo=TZ))

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

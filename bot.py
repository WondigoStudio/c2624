"""
Telegram-бот для группы: /call, /cab, /call_deny, /hb, /hb_info, /map,
+ автоматическая транскрипция голосовых сообщений и видео-кружков (ГС)
через OpenAI Whisper API.

Запуск:
    python bot.py

Переменные окружения (см. .env.example):
    TELEGRAM_BOT_TOKEN   - токен бота от @BotFather
    OPENAI_API_KEY       - ключ OpenAI для транскрипции
    TIMEZONE             - таймзона для проверки дней рождений (по умолчанию Europe/Moscow)
"""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime, time as dtime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

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

import storage

# ---------------------------------------------------------------------------
# Настройка
# ---------------------------------------------------------------------------

load_dotenv()

# Отключаем шумные (но безобидные) предупреждения huggingface_hub про symlinks на Windows
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Moscow")

# Способ транскрипции: "local" (faster-whisper, бесплатно, грузит CPU)
# или "openai" (Whisper API, платно, нужен OPENAI_API_KEY)
TRANSCRIBE_MODE = os.getenv("TRANSCRIBE_MODE", "local").lower()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "whisper-large-v3-turbo")
LOCAL_WHISPER_MODEL = os.getenv("LOCAL_WHISPER_MODEL", "small")  # tiny/base/small/medium/large-v3
VOSK_MODEL_PATH = os.getenv("VOSK_MODEL_PATH", "/app/vosk-model")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

openai_client = None
local_whisper_model = None
vosk_model = None
transcribe_model_name = None  # используется только для openai/groq режимов

if TRANSCRIBE_MODE == "openai":
    if OPENAI_API_KEY:
        from openai import OpenAI

        openai_client = OpenAI(api_key=OPENAI_API_KEY)
        transcribe_model_name = "whisper-1"
    else:
        logger.warning("TRANSCRIBE_MODE=openai, но OPENAI_API_KEY не задан — транскрипция отключена.")
elif TRANSCRIBE_MODE == "groq":
    if GROQ_API_KEY:
        from openai import OpenAI

        # У Groq API, совместимый с OpenAI — просто другой base_url и ключ
        openai_client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
        transcribe_model_name = GROQ_MODEL
        logger.info("Транскрипция через Groq (%s).", GROQ_MODEL)
    else:
        logger.warning("TRANSCRIBE_MODE=groq, но GROQ_API_KEY не задан — транскрипция отключена.")
elif TRANSCRIBE_MODE == "local":
    try:
        from faster_whisper import WhisperModel

        # compute_type="int8" — самый лёгкий и быстрый вариант для CPU
        local_whisper_model = WhisperModel(LOCAL_WHISPER_MODEL, device="cpu", compute_type="int8")
        logger.info("Локальная модель Whisper (%s) загружена.", LOCAL_WHISPER_MODEL)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось загрузить локальную модель Whisper: %s", exc)
elif TRANSCRIBE_MODE == "vosk":
    try:
        import vosk

        vosk.SetLogLevel(-1)  # выключаем шумные логи vosk в консоль
        if os.path.isdir(VOSK_MODEL_PATH):
            vosk_model = vosk.Model(VOSK_MODEL_PATH)
            logger.info("Локальная модель Vosk загружена из %s.", VOSK_MODEL_PATH)
        else:
            logger.warning(
                "TRANSCRIBE_MODE=vosk, но папка модели не найдена: %s. "
                "Транскрипция отключена.",
                VOSK_MODEL_PATH,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось загрузить модель Vosk: %s", exc)
else:
    logger.warning("Неизвестный TRANSCRIBE_MODE=%s — транскрипция отключена.", TRANSCRIBE_MODE)

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo(TIMEZONE_NAME)
except Exception:
    TZ = None

# Пример кода кабинета: C1.1.323 -> Павильон C1, Блок 1, Этаж 3, Кабинет 23
CABINET_RE = re.compile(r"^\s*([A-Za-zА-Яа-яЁё]+\d+)\.(\d+)\.(\d)(\d+)\s*$")

# Папка с картами: map/campus.jpg (весь кампус), map/floor_1.jpg, map/floor_2.jpg, ...
MAP_DIR = os.path.join(os.path.dirname(__file__), os.getenv("MAP_DIR", "map"))

# Сколько этажей пытаться найти и отправить одним альбомом по команде /map без аргументов
MAP_FLOOR_COUNT = int(os.getenv("MAP_FLOOR_COUNT", "3"))

# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def mention_html(user_id: int, name: str) -> str:
    safe_name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


def find_map_file(stem: str) -> str | None:
    """Ищет файл карты по имени без расширения, например 'floor_3' или 'campus'."""
    if not os.path.isdir(MAP_DIR):
        return None
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        path = os.path.join(MAP_DIR, stem + ext)
        if os.path.isfile(path):
            return path
    return None


MAX_MAP_PHOTO_BYTES = 3 * 1024 * 1024  # если файл больше — попробуем сжать


def get_sendable_map_path(stem: str, original_path: str) -> str:
    """Если исходный файл большой — возвращает путь к уменьшенной копии (кэшируется
    в /tmp), чтобы отправка в Telegram не упиралась в таймаут на медленном интернете."""
    try:
        if os.path.getsize(original_path) <= MAX_MAP_PHOTO_BYTES:
            return original_path
    except OSError:
        return original_path

    cache_dir = "/tmp/tgbot_map_cache"
    os.makedirs(cache_dir, exist_ok=True)
    cached_path = os.path.join(cache_dir, f"{stem}.jpg")

    # Если уже сжимали и исходник с тех пор не менялся — используем кэш
    if os.path.isfile(cached_path) and os.path.getmtime(cached_path) >= os.path.getmtime(original_path):
        return cached_path

    try:
        from PIL import Image

        img = Image.open(original_path).convert("RGB")
        img.thumbnail((1600, 1600))
        img.save(cached_path, "JPEG", quality=80, optimize=True)
        logger.info(
            "Сжал карту %s: %d КБ -> %d КБ",
            stem,
            os.path.getsize(original_path) // 1024,
            os.path.getsize(cached_path) // 1024,
        )
        return cached_path
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось сжать фото карты (%s), шлю оригинал: %s", stem, exc)
        return original_path


async def send_map_photo(update: Update, stem: str, caption: str, not_found_text: str) -> None:
    path = find_map_file(stem)
    if not path:
        await update.message.reply_text(not_found_text)
        return

    data = storage.load()
    cached_file_id = storage.get_map_file_id(data, stem)

    # 1) Если фото уже когда-то загружали в Telegram — шлём по file_id, это мгновенно
    if cached_file_id:
        try:
            await update.message.reply_photo(photo=cached_file_id, caption=caption)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось отправить по кэшированному file_id (%s): %s", stem, exc)
            # file_id мог протухнуть/файл удалили на серверах Telegram — грузим заново ниже

    # 2) Загружаем файл с диска, с несколькими попытками на случай медленного интернета
    send_path = get_sendable_map_path(stem, path)
    last_exc = None
    for attempt in range(1, 4):
        try:
            with open(send_path, "rb") as photo:
                sent = await update.message.reply_photo(
                    photo=photo,
                    caption=caption,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=60,
                    pool_timeout=60,
                )
            # кэшируем file_id самого крупного варианта фото на будущее
            if sent.photo:
                storage.set_map_file_id(data, stem, sent.photo[-1].file_id)
                storage.save(data)
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("Попытка %s отправить фото карты (%s) не удалась: %s", attempt, stem, exc)

    logger.warning("Не удалось отправить фото карты (%s) после нескольких попыток: %s", stem, last_exc)
    await update.message.reply_text(
        f"{caption}: не получилось отправить фото — слишком медленное соединение с Telegram. "
        "Попробуй ещё раз чуть позже, или сожми файл в папке map/ до размера поменьше."
    )


async def send_all_floor_maps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Отправляет все найденные карты этажей (floor_1, floor_2, ...) одним альбомом.
    Возвращает True, если хотя бы одна карта найдена и отправлена (даже если отправка
    в итоге не удалась технически — чтобы вызывающий код не пытался слать campus.jpg)."""
    data = storage.load()
    media: list[InputMediaPhoto] = []
    open_files = []
    stems: list[str] = []

    try:
        for floor in range(1, MAP_FLOOR_COUNT + 1):
            stem = f"floor_{floor}"
            path = find_map_file(stem)
            if not path:
                continue
            stems.append(stem)

            cached_file_id = storage.get_map_file_id(data, stem)
            if cached_file_id:
                media.append(InputMediaPhoto(media=cached_file_id, caption=f"Этаж {floor}"))
                continue

            send_path = get_sendable_map_path(stem, path)
            f = open(send_path, "rb")
            open_files.append(f)
            media.append(InputMediaPhoto(media=f, caption=f"Этаж {floor}"))

        if not media:
            return False

        last_exc = None
        for attempt in range(1, 4):
            try:
                sent_messages = await update.message.reply_media_group(
                    media=media,
                    read_timeout=180,
                    write_timeout=180,
                    connect_timeout=60,
                    pool_timeout=60,
                )
                # кэшируем file_id для тех этажей, что грузили с диска впервые
                for stem, msg in zip(stems, sent_messages):
                    if msg.photo:
                        storage.set_map_file_id(data, stem, msg.photo[-1].file_id)
                storage.save(data)
                return True
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning("Попытка %s отправить альбом этажей не удалась: %s", attempt, exc)
                for f in open_files:
                    try:
                        f.seek(0)
                    except Exception:  # noqa: BLE001
                        pass

        logger.warning("Не удалось отправить альбом этажей после нескольких попыток: %s", last_exc)
        await update.message.reply_text(
            "Не получилось отправить карты этажей — слишком медленное соединение с Telegram. "
            "Попробуй ещё раз чуть позже."
        )
        return True
    finally:
        for f in open_files:
            f.close()


async def track_member(update: Update, data: dict) -> None:
    """Запоминаем каждого написавшего в чат пользователя, чтобы /call мог его позвать."""
    if not update.effective_user or not update.effective_chat:
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        return
    user = update.effective_user
    storage.register_member(
        data,
        update.effective_chat.id,
        user.id,
        user.username,
        user.first_name,
    )


# ---------------------------------------------------------------------------
# /call и /call_deny
# ---------------------------------------------------------------------------


async def cmd_call(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Эта команда работает только в группах.")
        return

    data = storage.load()
    await track_member(update, data)
    chat = storage.get_chat(data, update.effective_chat.id)
    storage.save(data)

    members = chat["members"]
    deny_list = set(chat["call_deny"])

    mentions = []
    for uid_str, info in members.items():
        uid = int(uid_str)
        if uid in deny_list:
            continue
        name = info.get("first_name") or info.get("username") or "участник"
        mentions.append(mention_html(uid, name))

    if not mentions:
        await update.message.reply_text(
            "Пока некого звать — бот ещё не видел сообщений от участников, "
            "либо все отключили /call через /call_deny."
        )
        return

    caller = update.effective_user.first_name or "Кто-то"
    text = f"📣 {caller} зовёт всех:\n" + " ".join(mentions)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_call_deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Эта команда работает только в группах.")
        return

    data = storage.load()
    await track_member(update, data)
    chat = storage.get_chat(data, update.effective_chat.id)
    user_id = update.effective_user.id

    currently_denied = storage.is_call_denied(data, update.effective_chat.id, user_id)
    storage.set_call_deny(data, update.effective_chat.id, user_id, not currently_denied)
    storage.save(data)

    if currently_denied:
        await update.message.reply_text("Готово, теперь /call снова будет тебя звать.")
    else:
        await update.message.reply_text(
            "Готово, ты больше не будешь получать упоминания по /call. "
            "Чтобы включить обратно — отправь команду ещё раз."
        )


# ---------------------------------------------------------------------------
# /cab
# ---------------------------------------------------------------------------


async def cmd_cab(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Использование: /cab C1.1.323\n"
            "Формат: <Павильон>.<Блок>.<Этаж+Кабинет>"
        )
        return

    code = context.args[0]
    match = CABINET_RE.match(code)
    if not match:
        await update.message.reply_text(
            "Не понял формат кабинета 🤔\n"
            "Пример правильного формата: /cab C1.1.323"
        )
        return

    pavilion, block, floor, room = match.groups()
    info_text = f"Павильон {pavilion} Блок {block} Этаж {floor} Кабинет {room}"

    await send_map_photo(
        update,
        stem=f"floor_{floor}",
        caption=info_text,
        not_found_text=f"{info_text}\n(Карта этажа {floor} пока не загружена в папку map/)",
    )


async def cmd_map(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        # Без аргументов — сразу шлём альбом со всеми найденными этажами
        sent_album = await send_all_floor_maps(update, context)
        if sent_album:
            return
        # Если ни одной карты этажа не нашлось — пробуем общую карту кампуса
        await send_map_photo(
            update,
            stem="campus",
            caption="Карта кампуса",
            not_found_text=(
                "Карты пока не загружены. Ожидаются файлы map/floor_1.jpg, "
                "map/floor_2.jpg, ... или map/campus.jpg"
            ),
        )
        return

    floor_arg = context.args[0]
    if not floor_arg.isdigit():
        await update.message.reply_text(
            "Использование: /map — карты всех этажей, или /map 3 — карта конкретного этажа"
        )
        return

    await send_map_photo(
        update,
        stem=f"floor_{floor_arg}",
        caption=f"Этаж {floor_arg}",
        not_found_text=f"(Карта этажа {floor_arg} пока не загружена в папку map/)",
    )


# ---------------------------------------------------------------------------
# /hb и /hb_info
# ---------------------------------------------------------------------------

DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})$")


async def cmd_hb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = storage.load()
    if update.effective_chat.type in ("group", "supergroup"):
        await track_member(update, data)
    user = update.effective_user

    if not context.args:
        await update.message.reply_text(
            "Использование: /hb 17.03 — сохранить свой день рождения (17 марта).\n"
            "Можно писать и в группе, и в личку боту. Чтобы удалить: /hb off"
        )
        return

    if context.args[0].lower() == "off":
        storage.remove_birthday(data, user.id)
        storage.save(data)
        await update.message.reply_text("Твой день рождения удалён из списка.")
        return

    m = DATE_RE.match(context.args[0])
    if not m:
        await update.message.reply_text(
            "Не понял дату 🤔 Формат: ДД.ММ, например /hb 17.03"
        )
        return

    day, month = int(m.group(1)), int(m.group(2))
    try:
        # 2000 - просто "безопасный" високосный год для валидации 29 февраля
        datetime(2000, month, day)
    except ValueError:
        await update.message.reply_text("Такой даты не существует, проверь число и месяц.")
        return

    date_str = f"{day:02d}.{month:02d}"
    name = user.first_name or user.username or "Без имени"
    storage.set_birthday(data, user.id, date_str, name)
    storage.save(data)

    if update.effective_chat.type == "private":
        await update.message.reply_text(
            f"Записал: {name} — {date_str}. Напомню за неделю в тех группах, "
            f"где ты писал(а) вместе со мной 🎉"
        )
    else:
        await update.message.reply_text(
            f"Записал: {name} — {date_str}. Напомню в чате за неделю 🎉"
        )


async def cmd_hb_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = storage.load()

    if update.effective_chat.type == "private":
        info = storage.get_birthday(data, update.effective_user.id)
        if not info:
            await update.message.reply_text(
                "У тебя пока не сохранён день рождения. Используй /hb ДД.ММ"
            )
        else:
            await update.message.reply_text(f"🎂 Твой день рождения: {info['date']}")
        return

    await track_member(update, data)
    storage.save(data)

    chat = storage.get_chat(data, update.effective_chat.id)
    member_ids = set(chat["members"].keys())
    birthdays = {
        uid: info for uid, info in data["users"].items() if uid in member_ids
    }

    if not birthdays:
        await update.message.reply_text(
            "Пока никто в этом чате не добавил день рождения. "
            "Используй /hb ДД.ММ (можно и в личке боту)."
        )
        return

    def sort_key(item):
        _, info = item
        d, m = info["date"].split(".")
        return (int(m), int(d))

    lines = ["🎂 Дни рождения в чате:"]
    for uid_str, info in sorted(birthdays.items(), key=sort_key):
        lines.append(f"• {info['name']} — {info['date']}")

    await update.message.reply_text("\n".join(lines))


async def check_birthdays_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ежедневная проверка: если у кого-то ДР ровно через 7 дней — напомнить
    во всех группах, где бот видел этого пользователя."""
    data = storage.load()
    now = datetime.now(TZ) if TZ else datetime.now()
    target = (now + timedelta(days=7)).date()
    target_str = f"{target.day:02d}.{target.month:02d}"
    current_year = str(now.year)

    changed = False
    for chat_id_str, chat in data["chats"].items():
        member_ids = set(chat["members"].keys())
        for uid_str, info in data["users"].items():
            if uid_str not in member_ids:
                continue
            if info["date"] != target_str:
                continue
            if chat["hb_notified"].get(uid_str) == current_year:
                continue  # уже напоминали в этом году
            try:
                await context.bot.send_message(
                    chat_id=int(chat_id_str),
                    text=(
                        f"🎉 Через неделю ({target_str}) день рождения у "
                        f"{mention_html(int(uid_str), info['name'])}!"
                    ),
                    parse_mode=ParseMode.HTML,
                )
                chat["hb_notified"][uid_str] = current_year
                changed = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("Не удалось отправить напоминание о ДР: %s", exc)

    if changed:
        storage.save(data)


# ---------------------------------------------------------------------------
# Транскрипция голосовых / кружков (ГС)
# ---------------------------------------------------------------------------


def transcribe_with_vosk(media_path: str) -> str:
    """Конвертирует аудио/видео в 16кГц моно WAV через ffmpeg и распознаёт через Vosk."""
    import json
    import subprocess
    import wave

    wav_path = media_path + ".16k.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", media_path,
            "-ar", "16000", "-ac", "1", "-f", "wav", wav_path,
            "-loglevel", "error",
        ],
        check=True,
    )

    try:
        with wave.open(wav_path, "rb") as wf:
            rec = vosk.KaldiRecognizer(vosk_model, wf.getframerate())
            rec.SetWords(False)
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
            return " ".join(parts).strip()
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)


async def transcribe_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not openai_client and not local_whisper_model and not vosk_model:
        return  # транскрипция не настроена — тихо пропускаем

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

    try:
        await tg_file.download_to_drive(local_path)

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        if openai_client:
            with open(local_path, "rb") as audio_file:
                transcript = openai_client.audio.transcriptions.create(
                    model=transcribe_model_name,
                    file=audio_file,
                )
            text = transcript.text.strip()
        elif vosk_model:
            text = transcribe_with_vosk(local_path)
        else:
            segments, _info = local_whisper_model.transcribe(local_path)
            text = " ".join(seg.text.strip() for seg in segments).strip()
        if not text:
            return

        safe_text = (
            text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        reply_to = message.message_id
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"📝 <b>Транскрипция:</b>\n<blockquote>{safe_text}</blockquote>",
            reply_to_message_id=reply_to,
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка транскрипции: %s", exc)
    finally:
        if os.path.exists(local_path):
            os.remove(local_path)


# ---------------------------------------------------------------------------
# Общий трекер участников (на любое текстовое сообщение)
# ---------------------------------------------------------------------------


async def track_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.is_bot:
        return
    data = storage.load()
    await track_member(update, data)
    storage.save(data)


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.warning("Ошибка при обработке апдейта: %s", context.error)


class _HealthCheckHandler(BaseHTTPRequestHandler):
    """Пустой HTTP-обработчик — нужен только чтобы Render (Web Service)
    считал сервис 'живым' по открытому порту. Реальный трафик бот не получает —
    вся работа идёт через Telegram long polling."""

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):  # noqa: A002
        pass  # не засоряем логи бота HTTP-пингами


def start_health_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), _HealthCheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health-check сервер запущен на порту %s (для Render Web Service).", port)


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN в переменных окружения / .env")
    if not openai_client and not local_whisper_model and not vosk_model:
        logger.warning(
            "Транскрипция не настроена (ни OpenAI/Groq, ни локальная модель) — "
            "голосовые сообщения расшифровываться не будут."
        )

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )

    application.add_handler(CommandHandler("call", cmd_call))
    application.add_handler(CommandHandler("call_deny", cmd_call_deny))
    application.add_handler(CommandHandler("cab", cmd_cab))
    application.add_handler(CommandHandler("map", cmd_map))
    application.add_handler(CommandHandler("hb", cmd_hb))
    application.add_handler(CommandHandler("hb_info", cmd_hb_info))

    application.add_handler(
        MessageHandler(
            filters.VOICE | filters.VIDEO_NOTE | filters.AUDIO | filters.VIDEO,
            transcribe_voice,
        )
    )

    # Трекаем участников на любом обычном сообщении (после спец-хендлеров команд)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, track_any_message)
    )

    application.add_error_handler(error_handler)

    # Render Web Service требует открытый порт — запускаем фиктивный HTTP-сервер
    # в фоновом потоке. Сам бот при этом продолжает работать через long polling.
    if os.getenv("PORT"):
        start_health_server()

    if application.job_queue is not None:
        application.job_queue.run_daily(
            check_birthdays_job,
            time=dtime(hour=9, minute=0, tzinfo=TZ),
            name="daily_birthday_check",
        )
    else:
        logger.warning(
            "JobQueue недоступен — установите python-telegram-bot[job-queue], "
            "иначе напоминания о днях рождения работать не будут."
        )

    logger.info("Бот запущен.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

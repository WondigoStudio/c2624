"""
Хранилище для бота. Если задана переменная окружения DATABASE_URL — данные хранятся
в Postgres (одна строка с JSONB-колонкой, обновляется целиком при каждом save()).
Если DATABASE_URL не задана — используется старый вариант: JSON-файл на диске
(удобно для локальной разработки без БД).

Структура данных (одинаковая что в БД, что в файле):

{
  "users": {
    "<user_id>": {"date": "17.03", "name": "..."}   # дни рождения — глобально, не по чатам
  },
  "chats": {
    "<chat_id>": {
      "members": {
        "<user_id>": {"username": "...", "first_name": "..."}
      },
      "call_deny": [user_id, ...],
      "hb_notified": {"<user_id>": "YYYY"}   # чтобы не слать напоминание повторно в этом году
    }
  },
  "map_file_ids": {
    "floor_1": "<telegram file_id>"
  }
}
"""

import json
import os
import threading

DATABASE_URL = os.getenv("DATABASE_URL")

DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))
DATA_PATH = os.path.join(DATA_DIR, "data.json")
_lock = threading.Lock()

_DEFAULT_ROOT = {"chats": {}, "users": {}, "map_file_ids": {}}


def _default_chat():
    return {
        "members": {},
        "call_deny": [],
        "hb_notified": {},
    }


# ---------------------------------------------------------------------------
# Бэкенд на Postgres (используется, если задана DATABASE_URL)
# ---------------------------------------------------------------------------

if DATABASE_URL:
    import psycopg
    from psycopg.rows import dict_row

    def _get_conn():
        # autocommit=True — каждый запрос сразу фиксируется, отдельные транзакции не нужны
        return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)

    def _ensure_table():
        with _get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_state (
                    id INTEGER PRIMARY KEY,
                    data JSONB NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO bot_state (id, data)
                VALUES (1, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (json.dumps(_DEFAULT_ROOT),),
            )

    _ensure_table()

    def load():
        with _lock:
            with _get_conn() as conn:
                row = conn.execute("SELECT data FROM bot_state WHERE id = 1").fetchone()
        data = row["data"] if row else dict(_DEFAULT_ROOT)
        data.setdefault("chats", {})
        data.setdefault("users", {})
        data.setdefault("map_file_ids", {})
        return data

    def save(data):
        with _lock:
            with _get_conn() as conn:
                conn.execute(
                    "UPDATE bot_state SET data = %s WHERE id = 1",
                    (json.dumps(data),),
                )

# ---------------------------------------------------------------------------
# Бэкенд на JSON-файле (фолбэк, если DATABASE_URL не задана)
# ---------------------------------------------------------------------------

else:

    def load():
        if not os.path.exists(DATA_PATH):
            return dict(_DEFAULT_ROOT)
        with _lock:
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    data = {}
        data.setdefault("chats", {})
        data.setdefault("users", {})
        data.setdefault("map_file_ids", {})
        return data

    def save(data):
        with _lock:
            os.makedirs(os.path.dirname(DATA_PATH) or ".", exist_ok=True)
            tmp_path = DATA_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, DATA_PATH)


def get_chat(data, chat_id):
    chat_id = str(chat_id)
    if chat_id not in data["chats"]:
        data["chats"][chat_id] = _default_chat()
    else:
        # На случай если структура была создана раньше и не хватает ключей
        for key, val in _default_chat().items():
            data["chats"][chat_id].setdefault(key, val)
    return data["chats"][chat_id]


def register_member(data, chat_id, user_id, username, first_name):
    chat = get_chat(data, chat_id)
    chat["members"][str(user_id)] = {
        "username": username or "",
        "first_name": first_name or "",
    }


def set_call_deny(data, chat_id, user_id, deny: bool):
    chat = get_chat(data, chat_id)
    uid = int(user_id)
    if deny:
        if uid not in chat["call_deny"]:
            chat["call_deny"].append(uid)
    else:
        if uid in chat["call_deny"]:
            chat["call_deny"].remove(uid)


def is_call_denied(data, chat_id, user_id):
    chat = get_chat(data, chat_id)
    return int(user_id) in chat["call_deny"]


def set_birthday(data, user_id, date_str, name):
    """Дни рождения хранятся глобально по пользователю (не привязаны к чату),
    чтобы можно было задать /hb и в ЛС, и бот напоминал во всех группах,
    где видел этого пользователя."""
    data["users"][str(user_id)] = {"date": date_str, "name": name}
    # сбрасываем отметки об уведомлении во всех чатах, чтобы при смене даты сработало заново
    for chat in data["chats"].values():
        chat.get("hb_notified", {}).pop(str(user_id), None)


def remove_birthday(data, user_id):
    data["users"].pop(str(user_id), None)
    for chat in data["chats"].values():
        chat.get("hb_notified", {}).pop(str(user_id), None)


def get_map_file_id(data, stem):
    return data.get("map_file_ids", {}).get(stem)


def set_map_file_id(data, stem, file_id):
    data.setdefault("map_file_ids", {})[stem] = file_id

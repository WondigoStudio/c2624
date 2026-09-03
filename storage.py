import json
import os
import threading

DATABASE_URL = os.getenv("DATABASE_URL")
DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))
DATA_PATH = os.path.join(DATA_DIR, "data.json")
_lock = threading.Lock()

_DEFAULT_ROOT = {"chats": {}, "users": {}, "map_file_ids": {}}


def _default_chat():
    return {"members": {}, "call_deny": [], "hb_notified": {}}


def _with_defaults(data):
    data.setdefault("chats", {})
    data.setdefault("users", {})
    data.setdefault("map_file_ids", {})
    return data


if DATABASE_URL:
    import psycopg
    from psycopg.rows import dict_row

    def _get_conn():
        return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)

    with _get_conn() as _conn:
        _conn.execute("CREATE TABLE IF NOT EXISTS bot_state (id INTEGER PRIMARY KEY, data JSONB NOT NULL)")
        _conn.execute(
            "INSERT INTO bot_state (id, data) VALUES (1, %s) ON CONFLICT (id) DO NOTHING",
            (json.dumps(_DEFAULT_ROOT),),
        )

    def load():
        with _lock:
            with _get_conn() as conn:
                row = conn.execute("SELECT data FROM bot_state WHERE id = 1").fetchone()
        return _with_defaults(row["data"] if row else dict(_DEFAULT_ROOT))

    def save(data):
        with _lock:
            with _get_conn() as conn:
                conn.execute("UPDATE bot_state SET data = %s WHERE id = 1", (json.dumps(data),))

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
        return _with_defaults(data)

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
        for key, val in _default_chat().items():
            data["chats"][chat_id].setdefault(key, val)
    return data["chats"][chat_id]


def register_member(data, chat_id, user_id, username, first_name):
    chat = get_chat(data, chat_id)
    chat["members"][str(user_id)] = {"username": username or "", "first_name": first_name or ""}


def set_call_deny(data, chat_id, user_id, deny):
    chat = get_chat(data, chat_id)
    uid = int(user_id)
    if deny:
        if uid not in chat["call_deny"]:
            chat["call_deny"].append(uid)
    else:
        if uid in chat["call_deny"]:
            chat["call_deny"].remove(uid)


def is_call_denied(data, chat_id, user_id):
    return int(user_id) in get_chat(data, chat_id)["call_deny"]


def set_birthday(data, user_id, date_str, name):
    data["users"][str(user_id)] = {"date": date_str, "name": name}
    for chat in data["chats"].values():
        chat.get("hb_notified", {}).pop(str(user_id), None)


def remove_birthday(data, user_id):
    data["users"].pop(str(user_id), None)
    for chat in data["chats"].values():
        chat.get("hb_notified", {}).pop(str(user_id), None)


def get_birthday(data, user_id):
    return data["users"].get(str(user_id))


def get_map_file_id(data, stem):
    return data.get("map_file_ids", {}).get(stem)


def set_map_file_id(data, stem, file_id):
    data.setdefault("map_file_ids", {})[stem] = file_id

"""
Простое JSON-хранилище для бота.
Структура файла data.json:

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
  }
}
"""

import json
import os
import threading

DATA_PATH = os.path.join(os.path.dirname(__file__), "data.json")
_lock = threading.Lock()


def _default_chat():
    return {
        "members": {},
        "call_deny": [],
        "hb_notified": {},
    }


def load():
    if not os.path.exists(DATA_PATH):
        return {"chats": {}, "users": {}, "map_file_ids": {}}
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
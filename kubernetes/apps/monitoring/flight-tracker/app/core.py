#!/usr/bin/env python3
"""Telegram bot core: owner-only command and button routing for pluggable modules.

A module is any object with:
  name      str, used for its state file (/data/<name>.json) and log prefix
  help      list of help lines
  commands  {"track": fn(args)}   args = words after the command
  callbacks {"f": fn(parts)}      parts = callback data split on ":" without the prefix
  start()   optional, spawn background threads here
"""
import json
import os
import time
import urllib.request


def log(msg):
    print(msg, flush=True)


def parse_command(text):
    parts = (text or "").split()
    if not parts or not parts[0].startswith("/"):
        return None
    return parts[0][1:].split("@")[0].lower(), parts[1:]


class Telegram:
    def __init__(self, token, chat_id):
        self.token, self.chat_id = token, chat_id

    def _call(self, method, body, timeout=20):
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)

    def send(self, text, buttons=None):
        body = {"chat_id": self.chat_id, "text": text}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": [
                [{"text": label, "callback_data": data}] for label, data in buttons]}
        try:
            self._call("sendMessage", body)
        except (OSError, ValueError) as e:
            log(f"send failed: {type(e).__name__}")

    def answer(self, callback_id):
        try:
            self._call("answerCallbackQuery", {"callback_query_id": callback_id})
        except (OSError, ValueError) as e:
            log(f"answer failed: {type(e).__name__}")

    def updates(self, offset):
        body = {"offset": offset, "timeout": 50, "allowed_updates": ["message", "callback_query"]}
        return self._call("getUpdates", body, timeout=65)["result"]


class Ctx:
    """What a module gets from the bot: send, log, and a state file path of its own."""

    def __init__(self, name, send, state_path):
        self.name, self.send, self.state_path = name, send, state_path

    def log(self, msg):
        log(f"[{self.name}] {msg}")


class Bot:
    def __init__(self, tg, chat_id, data_dir, legacy_state=None):
        self.tg, self.chat_id, self.data_dir = tg, str(chat_id), data_dir
        self.modules, self.commands, self.callbacks = [], {}, {}
        self._core_path = os.path.join(data_dir, "core.json")
        self._legacy_state = legacy_state
        self.offset = self._load_offset()

    # -- offset --

    def _load_offset(self):
        try:
            with open(self._core_path) as f:
                return int(json.load(f)["offset"])
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError):  # JSONDecodeError is a ValueError
            log("core state unreadable, seeding offset from legacy state")
        return self._legacy_offset()

    def _legacy_offset(self):
        """First start after the split: keep the offset the old single-file bot had reached."""
        if not self._legacy_state:
            return 0
        try:
            with open(self._legacy_state) as f:
                return int(json.load(f).get("offset", 0))
        except (OSError, ValueError, AttributeError, TypeError):
            return 0

    def _save_offset(self):
        tmp = self._core_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"offset": self.offset}, f)
        os.replace(tmp, self._core_path)

    # -- registry --

    def register(self, module):
        for name in module.commands:
            if name in self.commands:
                raise ValueError(f"duplicate command: /{name}")
        for prefix in module.callbacks:
            if prefix in self.callbacks:
                raise ValueError(f"duplicate callback prefix: {prefix}")
        self.commands.update(module.commands)
        self.callbacks.update(module.callbacks)
        self.modules.append(module)

    def ctx(self, name):
        return Ctx(name, self.tg.send, os.path.join(self.data_dir, f"{name}.json"))

    def help_text(self):
        return "\n".join(line for m in self.modules for line in m.help)

    def start(self):
        for m in self.modules:
            start = getattr(m, "start", None)
            if start:
                start()

    # -- routing --

    def handle_message(self, text):
        cmd = parse_command(text)
        if cmd is None:
            return
        name, args = cmd
        handler = self.commands.get(name)
        if handler is None:
            self.tg.send(self.help_text())
        else:
            handler(args)

    def handle_callback(self, data):
        prefix, *parts = (data or "").split(":")
        handler = self.callbacks.get(prefix)
        if handler is not None:
            handler(parts)

    def dispatch(self, update):
        """Route one Telegram update. Anything not from the owner's chat is ignored."""
        msg = update.get("message")
        if msg and str(msg.get("chat", {}).get("id")) == self.chat_id:
            self.handle_message(msg.get("text"))
            return
        cq = update.get("callback_query")
        if cq and str(cq.get("from", {}).get("id")) == self.chat_id:
            self.tg.answer(cq.get("id"))
            self.handle_callback(cq.get("data"))

    def process(self, update):
        self.offset = update["update_id"] + 1
        self._save_offset()
        try:
            self.dispatch(update)
        except Exception as e:  # a bad update must not kill the bot
            log(f"update failed: {type(e).__name__}")

    # -- loop --

    def poll_once(self):
        try:
            updates = self.tg.updates(self.offset)
        except (OSError, ValueError, KeyError) as e:
            log(f"getUpdates failed: {type(e).__name__}")
            return False
        for update in updates:
            self.process(update)
        return True

    def run(self):
        while True:
            if not self.poll_once():
                time.sleep(30)

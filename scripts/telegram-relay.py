#!/usr/bin/env python3
# Managed by Ansible — do not edit manually
# Deployed to monitoring VM (.11) at /opt/scripts/telegram-relay.py
# Schedule: */5 * * * * /usr/bin/python3 /opt/scripts/telegram-relay.py >> /var/log/telegram-relay.log 2>&1
#
# Polls Gotify for unread messages and forwards them to a Telegram chat.
# Deletes successfully forwarded messages from Gotify so they are not re-sent.

import sys
import logging
import requests

GOTIFY_URL = "{{ gotify_relay_url }}"
GOTIFY_CLIENT_TOKEN = "{{ gotify_client_token }}"

TELEGRAM_BOT_TOKEN = "{{ telegram_bot_token }}"
TELEGRAM_CHAT_ID = "{{ telegram_chat_id }}"

GOTIFY_MESSAGES_URL = f"{GOTIFY_URL.rstrip('/')}/message"
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

TELEGRAM_MAX_LENGTH = 4096

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def fetch_messages() -> list[dict]:
    messages = []
    params: dict = {"limit": 100, "since": 0}

    while True:
        try:
            resp = requests.get(
                GOTIFY_MESSAGES_URL,
                headers={"X-Gotify-Key": GOTIFY_CLIENT_TOKEN},
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.error("Failed to fetch messages from Gotify: %s", exc)
            sys.exit(1)

        data = resp.json()
        page_messages = data.get("messages", [])
        messages.extend(page_messages)

        paging = data.get("paging", {})
        if len(page_messages) < params["limit"] or not paging.get("next"):
            break

        params["since"] = page_messages[-1]["id"]

    return messages


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _post_telegram(text: str, msg_id) -> bool:
    try:
        resp = requests.post(
            TELEGRAM_API_URL,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        log.error("Failed to send message id=%s to Telegram: %s", msg_id, exc)
        return False


def send_to_telegram(message: dict) -> bool:
    title = message.get("title", "").strip()
    body = message.get("message", "").strip()

    parts = []
    if title:
        parts.append(f"<b>{escape_html(title)}</b>")
    parts.append(escape_html(body))

    text = "\n".join(parts)
    msg_id = message.get("id")

    if len(text) <= TELEGRAM_MAX_LENGTH:
        return _post_telegram(text, msg_id)

    log.warning("Message id=%s exceeds %d chars (%d); splitting", msg_id, TELEGRAM_MAX_LENGTH, len(text))
    chunks = []
    while text:
        if len(text) <= TELEGRAM_MAX_LENGTH:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, TELEGRAM_MAX_LENGTH)
        if split_at == -1:
            split_at = text.rfind(" ", 0, TELEGRAM_MAX_LENGTH)
        if split_at == -1:
            split_at = TELEGRAM_MAX_LENGTH
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()

    success = True
    for i, chunk in enumerate(chunks, 1):
        log.info("Sending chunk %d/%d for message id=%s", i, len(chunks), msg_id)
        if not _post_telegram(chunk, msg_id):
            success = False
    return success


def delete_message(message_id: int) -> None:
    try:
        resp = requests.delete(
            f"{GOTIFY_MESSAGES_URL}/{message_id}",
            headers={"X-Gotify-Key": GOTIFY_CLIENT_TOKEN},
            timeout=10,
        )
        resp.raise_for_status()
        log.info("Deleted Gotify message id=%s", message_id)
    except requests.RequestException as exc:
        log.error("Failed to delete Gotify message id=%s: %s", message_id, exc)


def main() -> None:
    log.info("Starting Gotify → Telegram relay")

    messages = fetch_messages()
    if not messages:
        log.info("No messages found. Nothing to do.")
        return

    log.info("Found %d message(s) to forward", len(messages))
    sent = 0
    failed = 0

    for msg in reversed(messages):
        msg_id = msg.get("id")
        if send_to_telegram(msg):
            log.info("Forwarded message id=%s to Telegram", msg_id)
            delete_message(msg_id)
            sent += 1
        else:
            failed += 1

    log.info("Done. Sent: %d | Failed (kept in Gotify): %d", sent, failed)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Bot entrypoint: build the core, register modules, run. A new feature is one new file plus one
`bot.register(...)` line below."""
import os
from datetime import timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import alerts
import clef
import core
import flights
import freshrss
import news
import status
import triage

DEFAULT_CLEF_URL = "http://clef.ai.svc.cluster.local:8080"
DEFAULT_FRESHRSS_URL = "http://freshrss.freshrss.svc.cluster.local"


def digest_tz():
    name = os.environ.get("DIGEST_TZ", "Europe/Brussels")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        core.log(f"timezone {name} not found, digest times are UTC")
        return timezone.utc


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    key = os.environ["AERODATABOX_KEY"]
    gotify_token = os.environ["CLIENT_TOKEN"]
    gotify_host = os.environ.get("GOTIFY_HOST", "http://gotify.monitoring.svc.cluster.local")
    vm_url = os.environ.get("VM_URL", status.DEFAULT_VM_URL)
    state_path = os.environ.get("STATE_PATH", "/data/state.json")
    data_dir = os.environ.get("DATA_DIR", os.path.dirname(state_path))

    bot = core.Bot(core.Telegram(token, chat_id), chat_id, data_dir, legacy_state=state_path)
    bot.register(flights.Flights(
        bot.ctx("flights"), lambda number, day: flights.fetch_flight(number, day, key), state_path))
    clef_client = clef.Clef(os.environ.get("CLEF_URL", DEFAULT_CLEF_URL))
    bot.register(alerts.Alerts(
        bot.ctx("alerts"), lambda: alerts.fetch_messages(gotify_host, gotify_token),
        triage=triage.Triage(clef_client)))
    if os.environ.get("FRESHRSS_USER"):  # the news module needs the sealed freshrss-api-secret
        source = freshrss.FreshRSS(os.environ.get("FRESHRSS_URL", DEFAULT_FRESHRSS_URL),
                                   os.environ["FRESHRSS_USER"], os.environ["FRESHRSS_API_PASSWORD"])
        hours = tuple(int(h) for h in os.environ.get("DIGEST_HOURS", "8,18").split(","))
        bot.register(news.News(bot.ctx("news"), chat_id, source, clef_client,
                               tz=digest_tz(), digest_hours=hours))
    else:
        core.log("FRESHRSS_USER not set, news module off")
    bot.register(status.Status(bot.ctx("status"), lambda promql: status.vm_query(vm_url, promql)))
    bot.start()
    core.log("flight-tracker started")
    bot.run()


if __name__ == "__main__":
    main()

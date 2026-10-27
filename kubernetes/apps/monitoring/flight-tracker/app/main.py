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
import interests
import news
import status
import triage

DEFAULT_CLEF_URL = "http://clef.ai.svc.cluster.local:8080"
DEFAULT_FRESHRSS_URL = "http://freshrss.freshrss.svc.cluster.local"
DEFAULT_PROFILE_PATH = "/scripts/news-profile.json"


def parse_digest_hours(text):
    """Sorted unique hours (0-23) from a comma list; anything wrong falls back to 8 and 18."""
    try:
        hours = {int(part) for part in text.split(",")}
        if not hours or any(not 0 <= h <= 23 for h in hours):
            raise ValueError
    except ValueError:
        core.log(f"DIGEST_HOURS {text!r} is not a comma list of hours 0-23, using 8,18")
        return (8, 18)
    return tuple(sorted(hours))


def load_news_profile(path):
    """The compiled interest profile, or None (news stays off) with a log line when it cannot be used."""
    try:
        return interests.load_profile(path)
    except interests.ProfileError as e:
        core.log(f"news profile unusable ({e}), news module off")
        return None


def warn_unused_categories(profile, source):
    """Log profile categories that no subscribed feed uses: a renamed category silently stops matching."""
    try:
        missing = interests.missing_categories(profile, source.labels())
    except freshrss.FreshRSSError as e:
        core.log(f"could not check FreshRSS categories ({e})")
        return
    if missing:
        core.log("profile categories with no subscribed feed: " + ", ".join(missing))


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
    clef_url = os.environ.get("CLEF_URL", DEFAULT_CLEF_URL)
    clef_client = clef.Clef(clef_url)  # triage: fail fast, 15 s
    bot.register(alerts.Alerts(
        bot.ctx("alerts"), lambda: alerts.fetch_messages(gotify_host, gotify_token),
        triage=triage.Triage(clef_client)))
    fr_user = os.environ.get("FRESHRSS_USER")
    fr_password = os.environ.get("FRESHRSS_API_PASSWORD")
    if fr_user and fr_password:  # the news module needs the sealed freshrss-api-secret
        source = freshrss.FreshRSS(os.environ.get("FRESHRSS_URL", DEFAULT_FRESHRSS_URL),
                                   fr_user, fr_password)
        hours = parse_digest_hours(os.environ.get("DIGEST_HOURS", "8,18"))
        news_clef = clef.Clef(clef_url, timeout=60)  # news scores on CPU: allow longer than triage
        profile = load_news_profile(os.environ.get("NEWS_PROFILE", DEFAULT_PROFILE_PATH))
        if profile is not None:
            warn_unused_categories(profile, source)
            bot.register(news.News(bot.ctx("news"), chat_id, source, news_clef,
                                   tz=digest_tz(), digest_hours=hours, profile=profile))
    else:
        core.log("FRESHRSS_USER or FRESHRSS_API_PASSWORD missing or empty, news module off")
    bot.register(status.Status(bot.ctx("status"), lambda promql: status.vm_query(vm_url, promql)))
    bot.start()
    core.log("flight-tracker started")
    bot.run()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Bot entrypoint: build the core, register modules, run. A new feature is one new file plus one
`bot.register(...)` line below."""
import os

import alerts
import core
import flights
import status


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
    bot.register(alerts.Alerts(
        bot.ctx("alerts"), lambda: alerts.fetch_messages(gotify_host, gotify_token)))
    bot.register(status.Status(bot.ctx("status"), lambda promql: status.vm_query(vm_url, promql)))
    bot.start()
    core.log("flight-tracker started")
    bot.run()


if __name__ == "__main__":
    main()

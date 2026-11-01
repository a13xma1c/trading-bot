# -*- coding: utf-8 -*-
"""Edit settings through the form; values are applied and persisted across reloads."""

import names
import bot_helpers


def main():
    bot_helpers.open_bot()
    bot_helpers.select_view(names.nav_settings)
    waitFor(lambda: str(findObject(names.settings_scan_interval).value) != "", bot_helpers.DEFAULT_TIMEOUT)
    original_interval = str(findObject(names.settings_scan_interval).value)

    bot_helpers.set_text(names.settings_scan_interval, "77")
    bot_helpers.set_text(names.settings_webhook, "https://hooks.example.com/squish")
    clickButton(waitForObject(names.settings_save_button))
    waitFor(lambda: "saved" in str(findObject(names.settings_status).innerText).lower(), bot_helpers.DEFAULT_TIMEOUT)
    test.verify("saved" in str(findObject(names.settings_status).innerText).lower(), "settings saved")

    # Reload the page: the values come back from the server.
    bot_helpers.open_bot()
    bot_helpers.select_view(names.nav_settings)
    waitFor(lambda: str(findObject(names.settings_scan_interval).value) == "77", bot_helpers.DEFAULT_TIMEOUT)
    test.compare(str(findObject(names.settings_scan_interval).value), "77", "scan interval persisted")
    test.compare(str(findObject(names.settings_webhook).value), "https://hooks.example.com/squish", "webhook persisted")

    # Invalid input is rejected with a message.
    bot_helpers.set_text(names.settings_timezone, "Mars/Olympus")
    clickButton(waitForObject(names.settings_save_button))
    waitFor(lambda: "time zone" in str(findObject(names.settings_status).innerText), bot_helpers.DEFAULT_TIMEOUT)
    test.verify("time zone" in str(findObject(names.settings_status).innerText), "invalid time zone is reported")

    # Restore the original values.
    bot_helpers.set_text(names.settings_timezone, "America/New_York")
    bot_helpers.set_text(names.settings_scan_interval, original_interval)
    bot_helpers.set_text(names.settings_webhook, "")
    clickButton(waitForObject(names.settings_save_button))
    waitFor(lambda: "saved" in str(findObject(names.settings_status).innerText).lower(), bot_helpers.DEFAULT_TIMEOUT)

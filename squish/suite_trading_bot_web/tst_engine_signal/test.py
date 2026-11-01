# -*- coding: utf-8 -*-
"""Starting the engine produces a trade signal that is notified in the UI."""

import names
import bot_helpers


def main():
    bot_helpers.open_bot()

    clickButton(waitForObject(names.start_button))
    bot_helpers.wait_until_engine(True)
    test.compare(str(waitForObject(names.start_button_label).innerText), "Stop", "toolbar button toggles to Stop")

    # The "Always Buy" strategy fires on the first scan: signal row + snackbar notification.
    waitFor(lambda: bot_helpers.table_rows(names.signals_table) >= 1, bot_helpers.DEFAULT_TIMEOUT)
    waitFor(lambda: evalJS("[...document.querySelectorAll('#signalsTable tbody tr')].some(r => r.innerText.includes('Always Buy'))"),
            bot_helpers.DEFAULT_TIMEOUT)
    test.verify(evalJS("[...document.querySelectorAll('#signalsTable tbody tr')].some(r => r.innerText.includes('BUY') && r.innerText.includes('Always Buy'))"),
                "a BUY signal of 'Always Buy' is listed")
    snackbar = waitForObject(names.snackbar)
    test.verify("open" in str(snackbar.className), "snackbar notification is shown")
    test.verify("BUY" in str(waitForObject(names.snackbar_text).innerText), "snackbar names the signal")

    # Executed by the paper broker: positions appear.
    bot_helpers.select_view(names.nav_positions)
    waitForObject(names.account_open_positions)
    waitFor(lambda: str(findObject(names.account_open_positions).innerText) == "2", bot_helpers.DEFAULT_TIMEOUT)
    test.compare(str(findObject(names.account_open_positions).innerText), "2", "two paper positions opened")

    # Scan now forces a refresh while running.
    clickButton(waitForObject(names.scan_now_button))
    snooze(1)
    test.verify(bot_helpers.engine_running(), "engine keeps running after a forced scan")

    clickButton(waitForObject(names.start_button))
    bot_helpers.wait_until_engine(False)

    # Leave the paper account clean for the next test case.
    clickButton(waitForObject(names.reset_paper_account_button))
    clickButton(waitForObject(names.confirm_ok_button))
    waitFor(lambda: str(waitForObject(names.account_open_positions).innerText) == "0", bot_helpers.DEFAULT_TIMEOUT)
    test.compare(str(findObject(names.account_open_positions).innerText), "0", "paper account reset")

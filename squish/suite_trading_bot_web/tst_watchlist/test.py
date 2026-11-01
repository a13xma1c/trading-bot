# -*- coding: utf-8 -*-
"""Adding, selecting and removing watchlist symbols."""

import names
import bot_helpers


def main():
    bot_helpers.open_bot()
    waitFor(lambda: bot_helpers.table_text(names.watchlist_table, 0, 1) != "-", bot_helpers.DEFAULT_TIMEOUT)

    # Add a symbol with the keyboard (Enter) ...
    bot_helpers.set_text(names.watchlist_symbol_input, "nvda")
    typeText(waitForObject(names.watchlist_symbol_input), "<Return>")
    waitFor(lambda: bot_helpers.table_rows(names.watchlist_table) == 3, bot_helpers.DEFAULT_TIMEOUT)
    test.compare(bot_helpers.table_text(names.watchlist_table, 2, 0), "NVDA", "symbol is upper-cased")
    test.compare(str(waitForObject(names.watchlist_symbol_input).value), "", "input is cleared after adding")
    waitFor(lambda: bot_helpers.table_text(names.watchlist_table, 2, 1) != "-", bot_helpers.DEFAULT_TIMEOUT)
    test.verify(bot_helpers.table_text(names.watchlist_table, 2, 1) != "-", "new symbol gets a quote")
    test.compare(str(waitForObject(names.chart_title).innerText), "NVDA", "new symbol is charted")

    # ... and with the Add button; duplicates are ignored.
    bot_helpers.set_text(names.watchlist_symbol_input, "NVDA")
    clickButton(waitForObject(names.watchlist_add_button))
    snooze(0.5)
    test.compare(bot_helpers.table_rows(names.watchlist_table), 3, "duplicate symbol is not added")
    test.verify("already" in str(waitForObject(names.snackbar_text).innerText), "snackbar explains the duplicate")

    # Clicking a row charts that symbol.
    mouseClick(waitForObject(names.table_cell(names.watchlist_table, 1, 0)))
    waitFor(lambda: str(findObject(names.chart_title).innerText) == "MSFT", bot_helpers.DEFAULT_TIMEOUT)
    test.compare(str(findObject(names.chart_title).innerText), "MSFT", "chart follows the selected row")

    # Remove the added symbol again (the suite must leave the server as it found it).
    mouseClick(waitForObject({"tagName": "BUTTON", "className": "icon-btn remove-symbol", "title": "Remove NVDA", "visible": True}))
    waitFor(lambda: bot_helpers.table_rows(names.watchlist_table) == 2, bot_helpers.DEFAULT_TIMEOUT)
    test.compare(bot_helpers.table_text(names.watchlist_table, 0, 0), "AAPL")
    test.compare(bot_helpers.table_text(names.watchlist_table, 1, 0), "MSFT")

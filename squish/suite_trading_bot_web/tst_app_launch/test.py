# -*- coding: utf-8 -*-
"""The dashboard loads with the Material layout, live quotes and a candlestick chart."""

import names
import bot_helpers


def main():
    bot_helpers.open_bot()

    test.compare(str(waitForObject(names.engine_state_text).innerText), "Engine stopped")
    test.verify("API budget" in str(waitForObject(names.budget_label).innerText), "top app bar shows the API budget")
    test.verify("Market" in str(waitForObject(names.market_chip_text).innerText), "market status chip is shown")

    # Material Design 3 colour roles are applied.
    surface = evalJS("getComputedStyle(document.documentElement).getPropertyValue('--md-sys-color-surface').trim()")
    test.compare(surface, "#111418", "MD3 surface colour token")

    waitFor(lambda: bot_helpers.table_text(names.watchlist_table, 0, 1) != "-", bot_helpers.DEFAULT_TIMEOUT)
    test.compare(bot_helpers.table_rows(names.watchlist_table), 2, "watchlist has the two configured symbols")
    test.compare(bot_helpers.table_text(names.watchlist_table, 0, 0), "AAPL")
    test.compare(bot_helpers.table_text(names.watchlist_table, 1, 0), "MSFT")
    test.verify(bot_helpers.table_text(names.watchlist_table, 0, 1) != "-", "AAPL quote loaded")
    test.compare(str(waitForObject(names.chart_title).innerText), "AAPL", "first symbol is charted")
    test.verify("data as of" in str(waitForObject(names.chart_status_label).innerText), "chart status shows data time")

    state = bot_helpers.chart_state()
    test.compare(state["bars"], 200, "chart holds 200 bars")
    test.compare(state["to"] - state["from"], 150, "default view shows the last 150 bars")

    # Overlaying a strategy draws its indicators.
    selectOption(waitForObject(names.overlay_strategy_select), "MA Crossover")
    waitFor(lambda: bot_helpers.chart_state()["overlay"] == "MA Crossover", bot_helpers.DEFAULT_TIMEOUT)
    test.compare(bot_helpers.chart_state()["overlay"], "MA Crossover", "overlay selected")
    clickButton(waitForObject(names.reset_view_button))

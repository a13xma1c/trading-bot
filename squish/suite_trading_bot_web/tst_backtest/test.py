# -*- coding: utf-8 -*-
"""Run a backtest from the strategy editor and check the results dialog."""

import names
import bot_helpers


def main():
    bot_helpers.open_bot()
    bot_helpers.select_view(names.nav_strategies)
    mouseClick(waitForObject(names.list_item("MA Crossover")))
    waitFor(lambda: str(findObject(names.strategy_name_input).value) == "MA Crossover", bot_helpers.DEFAULT_TIMEOUT)

    clickButton(waitForObject(names.backtest_button))
    waitForObject(names.backtest_dialog)
    test.compare(str(waitForObject(names.backtest_symbol_select).value), "AAPL")
    clickButton(waitForObject(names.backtest_run_button))

    waitFor(lambda: bot_helpers.table_rows(names.backtest_trades_table) > 0, bot_helpers.DEFAULT_TIMEOUT)
    test.compare(bot_helpers.table_text(names.backtest_stats_table, 0, 1), "AAPL", "stats: symbol")
    test.compare(bot_helpers.table_text(names.backtest_stats_table, 1, 1), "MA Crossover", "stats: strategy")
    trades = bot_helpers.table_rows(names.backtest_trades_table)
    test.verify(trades > 0, "backtest produced %d trades" % trades)
    test.compare(int(bot_helpers.table_text(names.backtest_stats_table, 2, 1)), trades, "trade count matches the trade list")
    test.verify("trades" in str(findObject(names.backtest_status).innerText), "status summarises the run")

    clickButton(waitForObject(names.backtest_show_button))
    waitFor(lambda: len(bot_helpers.chart_state()["markers"]["buy"]) > 0, bot_helpers.DEFAULT_TIMEOUT)
    test.verify(len(bot_helpers.chart_state()["markers"]["buy"]) > 0, "trades are marked on the chart")

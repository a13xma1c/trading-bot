# -*- coding: utf-8 -*-
"""Create, validate, save, disable and delete a custom strategy through the editor."""

import names
import bot_helpers


def main():
    bot_helpers.open_bot()
    bot_helpers.select_view(names.nav_strategies)

    clickButton(waitForObject(names.new_strategy_button))
    bot_helpers.set_text(names.strategy_name_input, "Squish Breakout")
    bot_helpers.set_text(names.strategy_description_input, "20-bar breakout created by Squish")
    bot_helpers.set_text(names.strategy_symbols_input, "aapl")
    bot_helpers.set_text(names.buy_rule_input, "close > highest(high, 20) and rsi(14) < 70")
    bot_helpers.set_text(names.sell_rule_input, "close < lowest(low, 10)")
    bot_helpers.set_text(names.stop_loss_input, "2.5")

    clickButton(waitForObject(names.validate_strategy_button))
    waitFor(lambda: str(findObject(names.strategy_status_label).innerText) != "", bot_helpers.DEFAULT_TIMEOUT)
    status = str(findObject(names.strategy_status_label).innerText)
    test.verify(status.startswith("Valid"), "rule validates: %s" % status)

    clickButton(waitForObject(names.save_strategy_button))
    waitFor(lambda: "Saved" in str(findObject(names.strategy_status_label).innerText), bot_helpers.DEFAULT_TIMEOUT)
    test.verify("Saved 'Squish Breakout'" in str(findObject(names.strategy_status_label).innerText), "strategy saved")
    test.verify(object.exists(names.list_item("Squish Breakout")), "strategy listed")

    # The editor reports invalid rules instead of saving them.
    bot_helpers.set_text(names.buy_rule_input, "close > nonsense")
    clickButton(waitForObject(names.save_strategy_button))
    waitFor(lambda: "nonsense" in str(findObject(names.strategy_status_label).innerText), bot_helpers.DEFAULT_TIMEOUT)
    test.verify("nonsense" in str(findObject(names.strategy_status_label).innerText), "invalid rule is reported")
    bot_helpers.set_text(names.buy_rule_input, "close > highest(high, 20) and rsi(14) < 70")
    clickButton(waitForObject(names.save_strategy_button))
    waitFor(lambda: "Saved" in str(findObject(names.strategy_status_label).innerText), bot_helpers.DEFAULT_TIMEOUT)

    # Show it on the chart: the dashboard opens with the overlay selected.
    clickButton(waitForObject(names.show_on_chart_button))
    waitFor(lambda: bot_helpers.chart_state()["overlay"] == "Squish Breakout", bot_helpers.DEFAULT_TIMEOUT)
    test.compare(str(waitForObject(names.overlay_strategy_select).value), "Squish Breakout", "overlay select shows the strategy")

    # Delete it again (Material confirmation dialog).
    bot_helpers.select_view(names.nav_strategies)
    mouseClick(waitForObject(names.list_item("Squish Breakout")))
    clickButton(waitForObject(names.delete_strategy_button))
    clickButton(waitForObject(names.confirm_ok_button))
    waitFor(lambda: not object.exists(names.list_item("Squish Breakout")), bot_helpers.DEFAULT_TIMEOUT)
    test.verify(not object.exists(names.list_item("Squish Breakout")), "strategy removed from the list")

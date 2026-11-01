# -*- coding: utf-8 -*-
"""Scripted object map for the Trading Bot web UI (Squish for Web real names).

Every widget used by the tests carries a stable HTML ``id``, so the names never
depend on layout or text.
"""

# Top app bar
start_button = {"tagName": "BUTTON", "id": "startButton", "visible": True}
start_button_label = {"tagName": "SPAN", "id": "startButtonLabel", "visible": True}
scan_now_button = {"tagName": "BUTTON", "id": "scanNowButton", "visible": True}
engine_state_text = {"tagName": "SPAN", "id": "engineStateText", "visible": True}
market_chip_text = {"tagName": "SPAN", "id": "marketChipText", "visible": True}
budget_label = {"tagName": "SPAN", "id": "budgetLabel", "visible": True}

# Navigation rail
nav_dashboard = {"tagName": "BUTTON", "id": "navDashboard", "visible": True}
nav_strategies = {"tagName": "BUTTON", "id": "navStrategies", "visible": True}
nav_positions = {"tagName": "BUTTON", "id": "navPositions", "visible": True}
nav_settings = {"tagName": "BUTTON", "id": "navSettings", "visible": True}

# Dashboard
watchlist_symbol_input = {"tagName": "INPUT", "id": "watchlistSymbolInput", "visible": True}
watchlist_add_button = {"tagName": "BUTTON", "id": "watchlistAddButton", "visible": True}
watchlist_table = {"tagName": "TABLE", "id": "watchlistTable", "visible": True}
chart_canvas = {"tagName": "CANVAS", "id": "chartCanvas", "visible": True}
chart_title = {"tagName": "H2", "id": "chartTitle", "visible": True}
chart_status_label = {"tagName": "SPAN", "id": "chartStatusLabel"}
overlay_strategy_select = {"tagName": "SELECT", "id": "overlayStrategySelect", "visible": True}
reset_view_button = {"tagName": "BUTTON", "id": "resetViewButton", "visible": True}
refresh_button = {"tagName": "BUTTON", "id": "refreshButton", "visible": True}
signals_table = {"tagName": "TABLE", "id": "signalsTable", "visible": True}
signals_count = {"tagName": "SPAN", "id": "signalsCount"}
snackbar = {"tagName": "DIV", "id": "snackbar"}
snackbar_text = {"tagName": "SPAN", "id": "snackbarText"}

# Strategies
strategy_list = {"tagName": "DIV", "id": "strategyList", "visible": True}
new_strategy_button = {"tagName": "BUTTON", "id": "newStrategyButton", "visible": True}
duplicate_strategy_button = {"tagName": "BUTTON", "id": "duplicateStrategyButton", "visible": True}
delete_strategy_button = {"tagName": "BUTTON", "id": "deleteStrategyButton", "visible": True}
strategy_name_input = {"tagName": "INPUT", "id": "strategyNameInput", "visible": True}
strategy_description_input = {"tagName": "INPUT", "id": "strategyDescriptionInput", "visible": True}
strategy_symbols_input = {"tagName": "INPUT", "id": "strategySymbolsInput", "visible": True}
buy_rule_input = {"tagName": "TEXTAREA", "id": "buyRuleInput", "visible": True}
sell_rule_input = {"tagName": "TEXTAREA", "id": "sellRuleInput", "visible": True}
add_param_button = {"tagName": "BUTTON", "id": "addParamButton", "visible": True}
stop_loss_input = {"tagName": "INPUT", "id": "stopLossInput", "visible": True}
validate_strategy_button = {"tagName": "BUTTON", "id": "validateStrategyButton", "visible": True}
save_strategy_button = {"tagName": "BUTTON", "id": "saveStrategyButton", "visible": True}
show_on_chart_button = {"tagName": "BUTTON", "id": "showOnChartButton", "visible": True}
backtest_button = {"tagName": "BUTTON", "id": "backtestButton", "visible": True}
strategy_status_label = {"tagName": "DIV", "id": "strategyStatusLabel"}
confirm_ok_button = {"tagName": "BUTTON", "id": "confirmOkButton", "visible": True}
confirm_cancel_button = {"tagName": "BUTTON", "id": "confirmCancelButton", "visible": True}

# Backtest dialog
backtest_dialog = {"tagName": "DIALOG", "id": "backtestDialog", "visible": True}
backtest_symbol_select = {"tagName": "SELECT", "id": "backtestSymbolSelect", "visible": True}
backtest_run_button = {"tagName": "BUTTON", "id": "backtestRunButton", "visible": True}
backtest_status = {"tagName": "SPAN", "id": "backtestStatus"}
backtest_stats_table = {"tagName": "TABLE", "id": "backtestStatsTable", "visible": True}
backtest_trades_table = {"tagName": "TABLE", "id": "backtestTradesTable", "visible": True}
backtest_show_button = {"tagName": "BUTTON", "id": "backtestShowButton", "visible": True}
backtest_done_button = {"tagName": "BUTTON", "id": "backtestDoneButton", "visible": True}

# Positions
positions_table = {"tagName": "TABLE", "id": "positionsTable", "visible": True}
account_open_positions = {"tagName": "DD", "id": "accountOpenPositions", "visible": True}
reset_paper_account_button = {"tagName": "BUTTON", "id": "resetPaperAccountButton", "visible": True}

# Settings
settings_provider = {"tagName": "SELECT", "id": "settingsProvider", "visible": True}
settings_api_key = {"tagName": "INPUT", "id": "settingsApiKey", "visible": True}
settings_scan_interval = {"tagName": "INPUT", "id": "settingsScanInterval", "visible": True}
settings_webhook = {"tagName": "INPUT", "id": "settingsWebhook", "visible": True}
settings_timezone = {"tagName": "INPUT", "id": "settingsTimezone", "visible": True}
settings_market_hours_enabled = {"tagName": "INPUT", "id": "settingsMarketHoursEnabled"}
settings_save_button = {"tagName": "BUTTON", "id": "settingsSaveButton", "visible": True}
settings_status = {"tagName": "SPAN", "id": "settingsStatus"}


def table_cell(table, row, column):
    """Real name of a cell in a table body (0-based row and column)."""
    return {"tagName": "TD", "container": table, "row": row, "column": column}


def list_item(text):
    """Real name of an entry in the strategy list (its title label; clicking it selects the entry)."""
    return {"tagName": "SPAN", "className": "label-large", "container": strategy_list, "innerText": text, "visible": True}

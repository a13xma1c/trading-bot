"""End-to-end browser tests of the Material Design web UI (Playwright + headless Chromium).

A real server (uvicorn) runs on a free port against the demo data provider; the
browser drives the UI exactly like a user - typing, clicking, reading tables -
and the chart is verified by inspecting canvas pixels.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from trading_bot.config import AppConfig
from trading_bot.data import DemoProvider, TwelveDataProvider

from .conftest import TwelveDataSession
from .web_helpers import TEST_PASSWORD, TEST_USER, LiveServer, login, start_live_server, write_project

pytest.importorskip("playwright")
from playwright.sync_api import Browser, Page, expect  # noqa: E402

UP_RGB = (78, 204, 163)
DOWN_RGB = (255, 123, 114)
# Offline font fallback, and 4xx API responses that the UI handles (browsers log every failed fetch).
IGNORED_CONSOLE = ("ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED", "fonts.googleapis.com",
                   "the server responded with a status of 4")


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    live = start_live_server(write_project(tmp_path))
    yield live
    live.stop()


@pytest.fixture
def page(browser: Browser, server: LiveServer) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" and not any(k in msg.text for k in IGNORED_CONSOLE) else None)
    login(page, server.url)
    yield page
    context.close()
    assert not errors, f"browser console errors: {errors}"


def chart_state(page: Page) -> dict[str, Any]:
    return page.evaluate("window.__app.chart.getState()")


def canvas_colors(page: Page, selector: str = "#chartCanvas") -> set[tuple[int, int, int]]:
    """Distinct opaque colours painted on a canvas (sampled on a grid)."""
    return {tuple(c) for c in page.evaluate("""(sel) => {
        const canvas = document.querySelector(sel);
        const ctx = canvas.getContext('2d');
        const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
        const seen = new Set();
        for (let i = 0; i < data.length; i += 4 * 7) {
            if (data[i + 3] > 200) seen.add(`${data[i]},${data[i + 1]},${data[i + 2]}`);
        }
        return [...seen].map((s) => s.split(',').map(Number));
    }""", selector)}


def wait_for_workers(server: LiveServer) -> None:
    assert server.engine.wait_for_workers(10.0)


# ------------------------------------------------------------------ layout


def test_dashboard_renders_material_layout_and_chart(page: Page, server: LiveServer) -> None:
    expect(page).to_have_title("Trading Bot")
    # Material Design 3 colour roles are applied through CSS custom properties.
    surface = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--md-sys-color-surface').trim()")
    assert surface == "#111418"
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(17, 20, 24)"
    expect(page.locator("nav.nav-rail .nav-item")).to_have_count(4)
    expect(page.locator("#navDashboard")).to_have_class(re.compile(r"active"))
    expect(page.locator("header.top-app-bar")).to_be_visible()
    expect(page.locator("#engineStateText")).to_have_text("Engine stopped")
    expect(page.locator("#startButtonLabel")).to_have_text("Start")
    expect(page.locator("#marketChipText")).to_contain_text("Market open")
    expect(page.locator("#budgetLabel")).to_contain_text("demo")

    # Watchlist with live quotes; the first symbol is charted.
    rows = page.locator("#watchlistTable tbody tr")
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_have_class(re.compile(r"selected"))
    expect(rows.nth(0).locator("td").nth(1)).not_to_have_text("-")
    expect(page.locator("#chartTitle")).to_have_text("AAPL")
    expect(page.locator("#chartStatusLabel")).to_contain_text("data as of")

    state = chart_state(page)
    assert state["symbol"] == "AAPL" and state["bars"] == 200
    assert state["to"] - state["from"] == 150  # default view shows the last 150 bars
    colors = canvas_colors(page)
    assert UP_RGB in colors and DOWN_RGB in colors  # candles were painted

    # Clicking another watchlist row charts that symbol.
    rows.nth(1).click()
    expect(page.locator("#chartTitle")).to_have_text("MSFT")
    page.wait_for_function("window.__app.chart.getState().symbol === 'MSFT'")
    expect(rows.nth(1)).to_have_class(re.compile(r"selected"))


def test_chart_zoom_pan_and_reset(page: Page) -> None:
    canvas = page.locator("#chartCanvas")
    box = canvas.bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, -300)  # zoom in
    page.wait_for_function("window.__app.chart.getState().to - window.__app.chart.getState().from < 150")
    zoomed = chart_state(page)
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2 + 200, box["y"] + box["height"] / 2, steps=5)
    page.mouse.up()
    panned = chart_state(page)
    assert panned["from"] < zoomed["from"]  # dragged to the right => older bars
    page.click("#resetViewButton")
    state = chart_state(page)
    assert state["to"] == 200 and state["to"] - state["from"] == 150


# ----------------------------------------------------------- watchlist


def test_add_and_remove_symbol(page: Page, server: LiveServer) -> None:
    page.fill("#watchlistSymbolInput", "nvda")
    page.press("#watchlistSymbolInput", "Enter")
    rows = page.locator("#watchlistTable tbody tr")
    expect(rows).to_have_count(3)
    expect(rows.nth(2).locator("td").nth(0)).to_have_text("NVDA")
    expect(page.locator("#chartTitle")).to_have_text("NVDA")  # the new symbol is charted
    page.wait_for_function("window.__app.chart.getState().symbol === 'NVDA' && window.__app.chart.bars.length > 0")
    expect(page.locator("#watchlistSymbolInput")).to_have_value("")
    assert AppConfig.load(server.config_path).watchlist == ["AAPL", "MSFT", "NVDA"]

    page.fill("#watchlistSymbolInput", "NVDA")
    page.click("#watchlistAddButton")
    expect(page.locator("#snackbar")).to_contain_text("already in the watchlist")
    expect(rows).to_have_count(3)

    rows.nth(2).locator("button.remove-symbol").click()
    expect(rows).to_have_count(2)
    assert server.engine.watchlist == ["AAPL", "MSFT"]


# --------------------------------------------------------------- engine


def test_start_engine_shows_signal_notification(page: Page, server: LiveServer) -> None:
    page.click("#startButton")
    expect(page.locator("#engineStateText")).to_have_text("Engine running")
    expect(page.locator("#startButtonLabel")).to_have_text("Stop")
    expect(page.locator("#engineStateChip")).to_have_class(re.compile(r"\bon\b"))

    rows = page.locator("#signalsTable tbody tr")
    expect(rows.first).to_be_visible(timeout=15000)
    page.wait_for_function("[...document.querySelectorAll('#signalsTable tbody tr')].some(r => r.innerText.includes('Always Buy'))")
    row = page.locator("#signalsTable tbody tr", has_text="Always Buy").first
    expect(row.locator(".pill")).to_have_text("BUY")
    expect(row.locator("td").nth(5)).to_have_text("yes")  # executed by the paper broker
    snackbar = page.locator("#snackbar")
    expect(snackbar).to_have_class(re.compile(r"open"))
    expect(snackbar).to_contain_text("Executed: BUY")
    expect(page.locator("#signalsCount")).to_contain_text("recent")

    page.click("#navPositions")
    expect(page.locator("#positionsTable tbody tr[data-symbol]")).to_have_count(2, timeout=10000)
    expect(page.locator("#accountOpenPositions")).to_have_text("2")

    page.click("#startButton")
    expect(page.locator("#engineStateText")).to_have_text("Engine stopped")
    assert not server.engine.running


def test_scan_now_refreshes_every_symbol(page: Page, server: LiveServer) -> None:
    provider = server.engine.data.provider
    assert isinstance(provider, DemoProvider)
    before = provider.calls
    assert server.engine.stats.scans == 0
    page.click("#scanNowButton")
    # The "Refreshing..." snackbar is transient (a signal notification may replace it at once),
    # so assert on the durable effects: a completed scan and one provider request per symbol.
    page.wait_for_function("window.__app.state.status.scans >= 1")
    wait_for_workers(server)
    assert server.engine.stats.scans == 1
    assert provider.calls >= before + 2


# ------------------------------------------------------------- overlays


def test_strategy_overlay_on_chart(page: Page) -> None:
    page.select_option("#overlayStrategySelect", "MA Crossover")
    page.wait_for_function("window.__app.chart.getState().overlay === 'MA Crossover'")
    state = chart_state(page)
    assert state["panes"] == 0
    assert page.evaluate("window.__app.chart.overlay.price.map(s => s.name)") == ["sma(fast)", "sma(slow)"]
    assert page.evaluate("window.__app.chart.overlay.buy.length") > 0
    assert (255, 183, 77) in canvas_colors(page)  # first overlay colour drawn on the price pane
    page.select_option("#overlayStrategySelect", "")
    page.wait_for_function("window.__app.chart.getState().overlay === null")


# ------------------------------------------------------ strategy editor


def test_strategy_editor_flow(page: Page, server: LiveServer) -> None:
    page.click("#navStrategies")
    expect(page.locator("#strategyList .list-item")).to_have_count(2)
    expect(page.locator("#strategyNameInput")).to_have_value("MA Crossover")  # first strategy loaded

    page.click("#newStrategyButton")
    page.fill("#strategyNameInput", "Gap Up")
    page.fill("#strategyDescriptionInput", "Opens above the previous high")
    page.fill("#strategySymbolsInput", "aapl")
    page.fill("#buyRuleInput", "open > prev(high) and rsi(period) < 70")
    page.fill("#sellRuleInput", "close < prev(low)")
    page.click("#addParamButton")
    page.fill("#paramsTable .param-name", "period")
    page.fill("#paramsTable .param-value", "14")
    page.fill("#stopLossInput", "2.5")
    page.click("#validateStrategyButton")
    expect(page.locator("#strategyStatusLabel")).to_contain_text("Valid")

    page.click("#saveStrategyButton")
    expect(page.locator("#strategyStatusLabel")).to_contain_text("Saved 'Gap Up'")
    expect(page.locator("#strategyList .list-item", has_text="Gap Up")).to_be_visible()
    saved = server.engine.strategies.get("Gap Up")
    assert saved is not None and saved.symbols == ["AAPL"] and saved.params == {"period": 14.0} and saved.stop_loss_pct == 2.5

    # Invalid rule is reported instead of saved.
    page.fill("#buyRuleInput", "open > nonsense")
    page.click("#saveStrategyButton")
    expect(page.locator("#strategyStatusLabel")).to_contain_text("nonsense")
    page.fill("#buyRuleInput", "open > prev(high) and rsi(period) < 70")
    page.click("#saveStrategyButton")
    expect(page.locator("#strategyStatusLabel")).to_contain_text("Saved")

    # The enable switch in the list toggles live scanning.
    page.locator("#strategyList .list-item", has_text="Gap Up").locator("label.switch").click()
    page.wait_for_function("fetch('/api/strategies').then(r => r.json()).then(rows => rows.find(s => s.name === 'Gap Up').enabled === false)")

    # Show on chart switches to the dashboard with the overlay selected.
    page.click("#showOnChartButton")
    expect(page.locator("#view-dashboard")).to_have_class(re.compile(r"active"))
    expect(page.locator("#overlayStrategySelect")).to_have_value("Gap Up")
    page.wait_for_function("window.__app.chart.getState().overlay === 'Gap Up'")

    # Delete asks for confirmation (Material dialog).
    page.click("#navStrategies")
    page.click("#strategyList .list-item >> text=Gap Up")
    page.click("#deleteStrategyButton")
    expect(page.locator("#confirmDialog")).to_be_visible()
    page.click("#confirmOkButton")
    expect(page.locator("#strategyList .list-item")).to_have_count(2)
    assert server.engine.strategies.get("Gap Up") is None


def test_backtest_dialog(page: Page) -> None:
    page.click("#navStrategies")
    page.click("#strategyList .list-item >> text=MA Crossover")
    page.click("#backtestButton")
    dialog = page.locator("#backtestDialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#backtestSymbolSelect")).to_have_value("AAPL")
    page.click("#backtestRunButton")
    expect(page.locator("#backtestTradesTable tbody tr").first).to_be_visible(timeout=10000)
    stats = page.locator("#backtestStatsTable tbody tr")
    assert stats.count() >= 8
    expect(stats.filter(has_text="strategy")).to_contain_text("MA Crossover")
    expect(page.locator("#backtestStatus")).to_contain_text("trades")
    page.click("#backtestShowButton")
    expect(dialog).not_to_be_visible()
    expect(page.locator("#view-dashboard")).to_have_class(re.compile(r"active"))
    assert page.evaluate("window.__app.chart.getState().markers.buy.length") > 0


# ------------------------------------------------------------- settings


def test_settings_form_saves_config(page: Page, server: LiveServer) -> None:
    page.click("#navSettings")
    expect(page.locator("#settingsProvider")).to_have_value("demo")
    page.fill("#settingsApiKey", "browser-key")
    page.fill("#settingsScanInterval", "90")
    page.fill("#settingsWebhook", "https://hooks.example/web")
    page.locator("label.switch:has(#settingsMarketHoursEnabled)").click()
    page.fill("#settingsTimezone", "Europe/Berlin")
    page.click("#settingsSaveButton")
    expect(page.locator("#settingsStatus")).to_contain_text("Settings saved")
    saved = AppConfig.load(server.config_path)
    assert saved.data.api_key == "browser-key" and saved.scan_interval == 90
    assert saved.market_hours.enabled is True and saved.market_hours.timezone == "Europe/Berlin"
    assert saved.notifications.webhook_url == "https://hooks.example/web"
    expect(page.locator("#apiKeyNote")).to_contain_text("A key is stored")
    expect(page.locator("#marketChipText")).to_contain_text("Market")

    page.fill("#settingsTimezone", "Mars/Olympus")
    page.click("#settingsSaveButton")
    expect(page.locator("#settingsStatus")).to_contain_text("time zone")


# ---------------------------------------------------------------- errors


def test_provider_errors_are_visible(browser: Browser, tmp_path: Path) -> None:
    config_path = write_project(tmp_path, data={"provider": "twelvedata", "api_key": "k"})
    session = TwelveDataSession()
    live = start_live_server(config_path, provider=TwelveDataProvider("k", session=session))
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        page = context.new_page()
        login(page, live.url)
        session.mode = "auth"
        page.click("#refreshButton")
        expect(page.locator("#chartStatusLabel")).to_contain_text("ERROR", timeout=10000)
        expect(page.locator("#chartStatusLabel")).to_contain_text("apikey")
        expect(page.locator("#watchlistTable tbody tr").first.locator("td").nth(3)).to_contain_text("error")
        expect(page.locator("#snackbar")).to_contain_text("apikey")
        session.mode = "ok"
        page.click("#refreshButton")
        page.wait_for_function("!document.querySelector('#chartStatusLabel').textContent.includes('ERROR')", timeout=10000)
    finally:
        context.close()
        live.stop()


# ------------------------------------------------------------------ login


def test_login_page_flow(browser: Browser, tmp_path: Path) -> None:
    live = start_live_server(write_project(tmp_path))
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        page = context.new_page()
        page.goto(live.url)
        page.wait_for_url("**/login")  # anonymous users land on the login page
        expect(page).to_have_title(re.compile("Sign in"))
        expect(page.locator("#loginSubtitle")).to_have_text("Sign in to continue")
        expect(page.locator("#usernameInput")).to_have_value(TEST_USER)
        expect(page.locator("#confirmField")).to_be_hidden()
        assert page.evaluate("getComputedStyle(document.querySelector('#loginCard')).borderRadius") == "28px"  # MD3 extra-large shape

        page.fill("#passwordInput", "wrong password")
        page.click("#loginButton")
        expect(page.locator("#loginError")).to_have_text("Invalid username or password")
        assert "/login" in page.url

        page.fill("#passwordInput", TEST_PASSWORD)
        page.press("#passwordInput", "Enter")
        page.wait_for_url(live.url)
        page.wait_for_function("window.__app && window.__app.chart.bars.length > 0", timeout=15000)
        expect(page.locator("#userName")).to_have_text(TEST_USER)
        expect(page.locator("#userAvatar")).to_have_text("A")

        # Visiting /login while signed in goes straight back to the app.
        page.goto(live.url + "login")
        page.wait_for_url(live.url)

        # Logout returns to the login page and the session is gone.
        page.click("#logoutButton")
        page.wait_for_url("**/login")
        page.goto(live.url)
        page.wait_for_url("**/login")
    finally:
        context.close()
        live.stop()


def test_first_run_setup_page(browser: Browser, tmp_path: Path) -> None:
    live = start_live_server(write_project(tmp_path, login=False))
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        page = context.new_page()
        page.goto(live.url)
        page.wait_for_url("**/login")
        expect(page.locator("#loginSubtitle")).to_have_text("Create the administrator account")
        expect(page.locator("#confirmField")).to_be_visible()
        expect(page.locator("#loginButtonLabel")).to_have_text("Create account")
        # Optional market-data section: the key field only shows for providers that need one.
        expect(page.locator("#setupDataFields")).to_be_visible()
        expect(page.locator("#providerSelect")).to_have_value("demo")
        expect(page.locator("#apiKeyField")).to_be_hidden()
        page.select_option("#providerSelect", "alphavantage")
        expect(page.locator("#apiKeyField")).to_be_visible()
        expect(page.locator("#providerNote")).to_contain_text("25/day")
        page.select_option("#providerSelect", "demo")
        page.fill("#usernameInput", "owner")
        page.fill("#passwordInput", "first run password")
        page.fill("#confirmInput", "different")
        page.click("#loginButton")
        expect(page.locator("#loginError")).to_have_text("Passwords do not match")
        page.fill("#confirmInput", "first run password")
        page.click("#loginButton")
        page.wait_for_url(live.url)
        page.wait_for_function("window.__app && window.__app.chart.bars.length > 0", timeout=15000)
        expect(page.locator("#userName")).to_have_text("owner")
        saved = AppConfig.load(live.config_path)
        assert saved.web.username == "owner" and saved.web.password_hash.startswith("pbkdf2_sha256$")
    finally:
        context.close()
        live.stop()


def test_change_credentials_in_settings(page: Page, server: LiveServer) -> None:
    page.click("#navSettings")
    expect(page.locator("#accountUsername")).to_have_value(TEST_USER)
    page.fill("#accountUsername", "trader")
    page.fill("#accountCurrentPassword", "not the password")
    page.fill("#accountNewPassword", "brand new password")
    page.fill("#accountConfirmPassword", "brand new password")
    page.click("#accountSaveButton")
    expect(page.locator("#accountStatus")).to_contain_text("Current password is incorrect")
    page.fill("#accountCurrentPassword", TEST_PASSWORD)
    page.click("#accountSaveButton")
    expect(page.locator("#accountStatus")).to_contain_text("Credentials updated")
    expect(page.locator("#userName")).to_have_text("trader")
    saved = AppConfig.load(server.config_path)
    assert saved.web.username == "trader"
    # Still signed in with the renewed session; the app keeps working.
    page.click("#navDashboard")
    page.click("#refreshButton")
    expect(page.locator("#chartStatusLabel")).to_contain_text("data as of")


def test_missing_api_key_banner_and_demo_switch(browser: Browser, tmp_path: Path) -> None:
    """The shipped configuration (Alpha Vantage, no key) must be usable: banner + one-click demo data."""
    live = start_live_server(write_project(tmp_path, data={"provider": "alphavantage", "api_key": ""}))
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        page = context.new_page()
        page.goto(live.url)
        page.wait_for_url("**/login")
        page.fill("#usernameInput", TEST_USER)
        page.fill("#passwordInput", TEST_PASSWORD)
        page.click("#loginButton")
        page.wait_for_url(live.url)
        banner = page.locator("#apiKeyBanner")
        expect(banner).to_be_visible()
        expect(banner).to_contain_text("needs an API key")
        expect(page.locator("#chartStatusLabel")).to_contain_text("requires an API key", timeout=10000)
        expect(page.locator("#watchlistTable tbody tr").first.locator("td").nth(3)).to_have_text("error")

        page.click("#bannerUseDemoButton")
        page.wait_for_function("window.__app && window.__app.chart.bars.length > 0", timeout=15000)
        expect(banner).to_be_hidden()
        expect(page.locator("#budgetLabel")).to_contain_text("demo")
        expect(page.locator("#watchlistTable tbody tr").first.locator("td").nth(1)).not_to_have_text("-")
        assert AppConfig.load(live.config_path).data.provider == "demo"

        # "Enter API key" leads to Settings, where a key for a real provider is applied live.
        page.click("#navDashboard")
        page.click("#navSettings")
        page.select_option("#settingsProvider", "twelvedata")
        page.fill("#settingsApiKey", "entered-later")
        page.click("#settingsSaveButton")
        expect(page.locator("#settingsStatus")).to_contain_text("applied")
        expect(page.locator("#budgetLabel")).to_contain_text("twelvedata")
        expect(page.locator("#apiKeyNote")).to_contain_text("A key is stored")
        saved = AppConfig.load(live.config_path)
        assert saved.data.provider == "twelvedata" and saved.data.api_key == "entered-later"
    finally:
        context.close()
        live.stop()

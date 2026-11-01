"""End-to-end browser tests of the Material Design web UI (Playwright + headless Chromium).

A real server (uvicorn) runs on a free port against the demo data provider; the
browser drives the UI exactly like a user - typing, clicking, reading tables -
and the chart is verified by inspecting canvas pixels.
"""

from __future__ import annotations

import email
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trading_bot.broker import Fill
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
    expect(page.locator("nav.nav-rail .nav-item")).to_have_count(9)
    expect(page.locator("nav.nav-rail .nav-item .label-medium")).to_have_text(
        ["Dashboard", "Strategies", "Positions", "Trade", "Wallet", "History", "Insights", "FX", "Settings"])
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
    rows = page.locator("#watchlistTable tbody tr")
    expect(rows.nth(0)).to_contain_text("Apple Inc.")  # company names from the local directory
    expect(page.locator("#chartName")).to_have_text("Apple Inc.")

    # Typing suggests tickers with their names; pick one with the keyboard.
    page.type("#watchlistSymbolInput", "nvid")
    menu = page.locator("#watchlistSymbolInputMenu")
    expect(menu).to_be_visible()
    expect(menu.locator(".menu-item").first).to_contain_text("NVDA")
    expect(menu.locator(".menu-item").first).to_contain_text("NVIDIA Corporation")
    page.keyboard.press("ArrowDown")
    expect(menu.locator(".menu-item").first).to_have_class(re.compile(r"active"))
    page.keyboard.press("Enter")
    expect(rows).to_have_count(3)
    expect(rows.nth(2).locator("td").nth(0)).to_contain_text("NVDA")
    expect(rows.nth(2).locator("td").nth(0)).to_contain_text("NVIDIA")
    expect(menu).to_be_hidden()
    page.wait_for_function("window.__app.chart.getState().symbol === 'NVDA' && window.__app.chart.bars.length > 0")
    rows.nth(2).locator("button.remove-symbol").click()
    expect(rows).to_have_count(2)

    # Plain Enter still works for symbols the directory does not know.
    page.fill("#watchlistSymbolInput", "nvda")
    page.press("#watchlistSymbolInput", "Enter")
    expect(rows).to_have_count(3)
    expect(rows.nth(2).locator("td").nth(0)).to_contain_text("NVDA")
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
        expect(page.locator("#usernameLabel")).to_have_text("Username or e-mail")
        expect(page.locator("#confirmField")).to_be_hidden()
        expect(page.locator("#signupLink")).to_be_visible()  # self-service registration is on by default
        assert page.evaluate("getComputedStyle(document.querySelector('#loginCard')).borderRadius") == "28px"  # MD3 extra-large shape

        page.fill("#usernameInput", TEST_USER)
        page.fill("#passwordInput", "wrong password")
        page.click("#loginButton")
        expect(page.locator("#loginError")).to_have_text("Invalid username/e-mail or password")
        assert "/login" in page.url

        # Reveal-password toggle (injected next to every password field).
        toggle = page.locator("#passwordInput ~ .pw-toggle")
        expect(toggle).to_have_attribute("aria-pressed", "false")
        toggle.click()
        expect(page.locator("#passwordInput")).to_have_attribute("type", "text")
        expect(page.locator("#passwordInput")).to_have_value("wrong password")
        toggle.click()
        expect(page.locator("#passwordInput")).to_have_attribute("type", "password")

        page.fill("#passwordInput", TEST_PASSWORD)
        page.press("#passwordInput", "Enter")
        page.wait_for_url(live.url)
        page.wait_for_function("window.__app && window.__app.chart.bars.length > 0", timeout=15000)
        expect(page.locator("#userName")).to_have_text(TEST_USER)
        expect(page.locator("#userAvatar")).to_have_text("A")

        # Visiting /login while signed in goes straight back to the app.
        page.goto(live.url + "login")
        page.wait_for_url(live.url)

        # Logout returns to the login page (with the sign-up option) and the session is gone.
        page.click("#logoutButton")
        page.wait_for_url("**/login")
        expect(page.locator("#signupLink")).to_be_visible()
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

        # Requirements are checked live while typing (server rules mirrored in the browser).
        rules = page.locator("#passwordFeedback .pw-rules li")
        expect(rules).to_have_count(9)
        page.type("#passwordInput", "owner")
        expect(rules.filter(has_text="lowercase")).to_have_class(re.compile(r"passed"))
        expect(rules.filter(has_text="At least 10")).to_have_class(re.compile(r"failed"))
        expect(rules.filter(has_text="username")).to_have_class(re.compile(r"failed"))
        expect(page.locator("#passwordFeedback .pw-meter-label")).to_contain_text("requirements not met")
        page.fill("#confirmInput", "owner")
        page.click("#loginButton")  # blocked client-side with the unmet rules listed
        expect(page.locator("#loginError")).to_contain_text("Password requirements not met")
        expect(page.locator("#loginError")).to_contain_text("username")
        page.fill("#passwordInput", "First-run-pass1!")
        expect(page.locator("#passwordFeedback")).to_have_class(re.compile(r"all-passed"))
        expect(rules.filter(has_text="username")).to_have_class(re.compile(r"passed"))
        expect(page.locator("#passwordFeedback .pw-meter")).to_have_attribute("data-score", re.compile(r"[34]"))
        page.fill("#confirmInput", "different")
        expect(page.locator("#confirmField")).to_have_class(re.compile(r"invalid"))
        page.click("#loginButton")
        expect(page.locator("#loginError")).to_have_text("Passwords do not match")
        page.fill("#confirmInput", "First-run-pass1!")
        page.click("#loginButton")
        page.wait_for_url(live.url)
        page.wait_for_function("window.__app && window.__app.chart.bars.length > 0", timeout=15000)
        expect(page.locator("#userName")).to_have_text("owner")
        saved = AppConfig.load(live.config_path)
        assert saved.web.username == "owner" and saved.web.password_hash == ""  # credentials live in the user store
        admin = live.engine.storage.find_user("owner")
        assert admin is not None and admin["role"] == "admin"
    finally:
        context.close()
        live.stop()


def test_change_password_and_profile_in_settings(page: Page, server: LiveServer) -> None:
    page.click("#navSettings")
    expect(page.locator("#profileSummary")).to_contain_text(f"Signed in as {TEST_USER} (administrator)")
    # Profile: e-mail + per-user auto-trading switch.
    page.fill("#profileEmail", "admin@example.com")
    page.locator("label.switch:has(#profileAutoTrade)").click()
    page.click("#profileSaveButton")
    expect(page.locator("#profileStatus")).to_contain_text("Profile saved")
    user = server.engine.storage.find_user(TEST_USER)
    assert user is not None and user["email"] == "admin@example.com" and not user["auto_trade"]
    assert server.engine.accounts.is_enabled(TEST_USER) is False

    # Password change with live requirement feedback.
    page.fill("#accountCurrentPassword", "not the password")
    page.type("#accountNewPassword", "weak")
    rules = page.locator("#accountPasswordFeedback .pw-rules li")
    expect(rules.filter(has_text="At least 10")).to_have_class(re.compile(r"failed"))
    page.click("#accountSaveButton")
    expect(page.locator("#accountStatus")).to_contain_text("Password requirements not met")
    page.fill("#accountNewPassword", "Brand-new-pass2!")
    expect(page.locator("#accountPasswordFeedback")).to_have_class(re.compile(r"all-passed"))
    page.fill("#accountConfirmPassword", "Brand-new-pass2!")
    page.click("#accountSaveButton")
    expect(page.locator("#accountStatus")).to_contain_text("Current password is incorrect")
    page.fill("#accountCurrentPassword", TEST_PASSWORD)
    page.click("#accountSaveButton")
    expect(page.locator("#accountStatus")).to_contain_text("Password changed")
    expect(page.locator("#userName")).to_have_text(TEST_USER)
    # Still signed in with the renewed session; the app keeps working.
    page.click("#navDashboard")
    page.click("#refreshButton")
    expect(page.locator("#chartStatusLabel")).to_contain_text("data as of")
    # The new password works, and so does signing in with the e-mail address.
    page.click("#logoutButton")
    page.wait_for_url("**/login")
    page.fill("#usernameInput", "Admin@Example.com")
    page.fill("#passwordInput", "Brand-new-pass2!")
    page.click("#loginButton")
    page.wait_for_url(server.url)
    expect(page.locator("#userName")).to_have_text(TEST_USER)


def verification_link(data_dir: Path) -> str:
    """The verification URL from the newest message in the outbox folder."""
    files = sorted((data_dir / "outbox").glob("*.eml"), key=lambda f: f.stat().st_mtime)
    assert files, "no verification e-mail was written"
    message = email.message_from_bytes(files[-1].read_bytes())
    body = message.get_payload(decode=True)
    assert isinstance(body, bytes)
    match = re.search(r"https?://\S+/verify\?token=[\w-]+", body.decode())
    assert match, body
    return match.group(0)


def test_signup_with_email_verification(browser: Browser, tmp_path: Path) -> None:
    """Logout -> Create account -> verification e-mail (outbox transport) -> sign in with the e-mail address."""
    live = start_live_server(write_project(tmp_path, notifications={"desktop": False, "sound": False,
                                                                   "email": {"transport": "outbox", "sender": "bot@example.com"}}))
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        page = context.new_page()
        login(page, live.url)
        page.click("#logoutButton")
        page.wait_for_url("**/login")
        page.click("#signupLink")
        page.wait_for_url("**/signup")
        expect(page).to_have_title(re.compile("Create account"))
        expect(page.locator("#emailLabel")).to_have_text("E-mail")  # verification is on -> e-mail required
        expect(page.locator("#emailInput")).to_have_attribute("required", "")

        page.fill("#usernameInput", "bob")
        page.fill("#emailInput", "Bob@Example.com")
        rules = page.locator("#passwordFeedback .pw-rules li")
        page.type("#passwordInput", "bobbob")
        expect(rules.filter(has_text="username")).to_have_class(re.compile(r"failed"))
        page.fill("#passwordInput", "Sunny-Trail-42!")
        expect(page.locator("#passwordFeedback")).to_have_class(re.compile(r"all-passed"))
        page.locator("#passwordInput ~ .pw-toggle").click()
        expect(page.locator("#passwordInput")).to_have_attribute("type", "text")
        page.fill("#confirmInput", "Sunny-Trail-42!")
        page.click("#signupButton")
        expect(page.locator("#verifyPanel")).to_be_visible()
        expect(page.locator("#verifyText")).to_contain_text("bob@example.com")

        # Not verified yet: login is refused with a hint and a resend button.
        page.goto(live.url + "login")
        page.fill("#usernameInput", "bob")
        page.fill("#passwordInput", "Sunny-Trail-42!")
        page.click("#loginButton")
        expect(page.locator("#loginError")).to_contain_text("verify your e-mail")
        expect(page.locator("#resendButton")).to_be_visible()
        page.click("#resendButton")
        expect(page.locator("#loginNotice")).to_contain_text("new link has been sent")
        assert len(list((tmp_path / "bot_data" / "outbox").glob("*.eml"))) == 2

        # Following the link in the e-mail activates the account.
        page.goto(verification_link(tmp_path / "bot_data"))
        page.wait_for_url("**/login?verified=1")
        expect(page.locator("#loginNotice")).to_contain_text("verified")
        page.fill("#usernameInput", "bob@example.com")  # e-mail works as login identifier
        page.fill("#passwordInput", "Sunny-Trail-42!")
        page.click("#loginButton")
        page.wait_for_url(live.url)
        expect(page.locator("#userName")).to_have_text("bob")
        expect(page.locator("#userAvatar")).to_have_text("B")
        # Bob has his own paper account and is not an administrator.
        page.click("#navPositions")
        expect(page.locator("#positionsAccountName")).to_contain_text("bob")
        expect(page.locator("#accountCash")).to_have_text("50,000.00")
        page.click("#navSettings")
        expect(page.locator("#aiSettingsCard")).to_be_hidden()
        expect(page.locator("#profileSummary")).not_to_contain_text("administrator")
    finally:
        context.close()
        live.stop()


def seed_trades(server: LiveServer, account: str = TEST_USER) -> None:
    """Write a deterministic set of round-trip fills for ``account``."""
    start = datetime(2024, 3, 4, 15, 0, tzinfo=UTC)
    legs = [("AAPL", "MA Crossover", 10, 100.0, 106.0), ("MSFT", "MA Crossover", 5, 300.0, 294.0),
            ("AAPL", "Always Buy", 10, 106.0, 110.0), ("NVDA", "Always Buy", 2, 800.0, 790.0),
            ("MSFT", "MA Crossover", 5, 294.0, 301.0)]
    when = start
    for symbol, strategy, qty, entry, exit_ in legs:
        server.engine.storage.record_fill(Fill(symbol, "BUY", qty, entry, when, strategy), account)
        when += timedelta(hours=5)
        server.engine.storage.record_fill(Fill(symbol, "SELL", qty, exit_, when, strategy, (exit_ - entry) * qty), account)
        when += timedelta(hours=20)


def test_history_page_shows_trades_and_pnl_chart(page: Page, server: LiveServer) -> None:
    seed_trades(server)
    page.click("#navHistory")
    kpis = page.locator("#historyKpis .kpi")
    expect(kpis).to_have_count(7)
    expect(kpis.nth(0)).to_contain_text("+85.00")      # 60 - 30 + 40 - 20 + 35
    expect(kpis.nth(1)).to_contain_text("5")
    expect(kpis.nth(1)).to_contain_text("3 wins · 2 losses")
    expect(kpis.nth(2)).to_contain_text("60.0%")
    expect(page.locator("#historySubtitle")).to_contain_text("5 closed trades")
    rows = page.locator("#closedTradesTable tbody tr")
    expect(rows).to_have_count(5)
    expect(rows.first).to_contain_text("MSFT")          # newest first
    expect(rows.first.locator("td").nth(7)).to_have_text("+35.00")
    expect(rows.first.locator("td").nth(7)).to_have_class(re.compile(r"up"))
    expect(page.locator("#historyFillsTable tbody tr")).to_have_count(10)
    # The P&L curve is painted (the account stays above zero, so the line is green).
    colours = canvas_colors(page, "#pnlCanvas")
    assert UP_RGB in colours, colours
    assert page.evaluate("document.querySelector('#pnlCanvas').width") > 0


def test_insights_page_renders_analytics(page: Page, server: LiveServer) -> None:
    seed_trades(server)
    page.click("#navInsights")
    expect(page.locator("#insightsKpis .kpi")).to_have_count(7)
    expect(page.locator("#insightsSubtitle")).to_contain_text("5 closed trades")
    insights = page.locator("#insightList .insight")
    expect(insights.filter(has_text="Small sample")).to_have_count(1)
    expect(insights.first).to_have_class(re.compile(r"insight (good|info|warning)"))
    strategies = page.locator("#byStrategyTable tbody tr")
    expect(strategies).to_have_count(2)
    expect(strategies.first.locator("td").first).to_have_text("MA Crossover")   # sorted by P&L: 60-30+35 = 65 first
    expect(strategies.first.locator("td").nth(3)).to_have_text("+65.00")
    expect(page.locator("#bySymbolTable tbody tr")).to_have_count(3)
    expect(page.locator("#byWeekdayTable tbody tr").first).to_contain_text(re.compile(r"day"))
    # No LLM configured: the button is disabled and the note tells the admin where to configure it.
    expect(page.locator("#aiAnalyzeButton")).to_be_disabled()
    expect(page.locator("#aiNote")).to_contain_text("Settings")


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


# ------------------------------------------------- trade / wallet / fx pages


@pytest.fixture
def funded_server(tmp_path: Path) -> Iterator[LiveServer]:
    """Server with the demo FX provider, the sandbox payment provider and a realistic execution model."""
    live = start_live_server(write_project(tmp_path, fx={"provider": "demo"},
                                           execution={"slippage_model": "fixed", "slippage_bps": 10, "commission_per_order": 1.0}))
    yield live
    live.stop()


@pytest.fixture
def funded_page(browser: Browser, funded_server: LiveServer) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" and not any(k in msg.text for k in IGNORED_CONSOLE) else None)
    login(page, funded_server.url)
    yield page
    context.close()
    assert not errors, f"browser console errors: {errors}"


def test_trade_page_simulate_place_and_close(funded_page: Page, funded_server: LiveServer) -> None:
    page = funded_page
    page.click("#navTrade")
    expect(page.locator("#tradeBrokerLabel")).to_have_text("Paper account (simulated)")
    expect(page.locator("#tradePositionsTable tbody tr.empty")).to_be_visible()
    page.fill("#orderSymbolInput", "AAPL")
    page.fill("#orderQuantityInput", "15")
    page.fill("#orderStopInput", "5")
    page.click("#simulateOrderButton")
    expect(page.locator("#simulationResult")).to_be_visible()
    expect(page.locator("#simulationVerdict")).to_contain_text("BUY 15 AAPL would fill @")
    expect(page.locator("#simulationModel")).to_contain_text("fixed")
    table = page.locator("#simulationTable")
    expect(table).to_contain_text("Commission")
    expect(table).to_contain_text("1.00")
    expect(table).to_contain_text("Stop loss")
    assert funded_server.engine.broker.position("AAPL") is None  # simulation only

    # Place it (confirmation dialog), the position appears with the manual strategy tag.
    page.click("#placeOrderButton")
    expect(page.locator("#confirmDialog")).to_be_visible()
    expect(page.locator("#confirmText")).to_contain_text("BUY 15 shares of AAPL at market")
    page.click("#confirmOkButton")
    expect(page.locator("#orderStatus")).to_contain_text("BUY 15 AAPL filled @")
    rows = page.locator("#tradePositionsTable tbody tr")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("AAPL")
    expect(rows.first).to_contain_text("manual")
    position = funded_server.engine.broker.position("AAPL")
    assert position is not None and position.quantity == 15 and position.stop_loss is not None

    # A non-marketable limit order rests in the pending table and can be cancelled.
    page.select_option("#orderTypeSelect", "limit")
    page.fill("#orderSymbolInput", "MSFT")
    page.fill("#orderQuantityInput", "2")
    page.fill("#orderLimitInput", "1")
    page.click("#simulateOrderButton")
    expect(page.locator("#simulationVerdict")).to_contain_text("Would rest as a limit order")
    expect(page.locator("#simulationWarnings li").first).to_contain_text("not marketable")
    page.click("#placeOrderButton")
    page.click("#confirmOkButton")
    expect(page.locator("#orderStatus")).to_contain_text("Limit order placed")
    pending = page.locator("#tradePendingTable tbody tr")
    expect(pending).to_have_count(1)
    expect(pending.first).to_contain_text("MSFT")
    pending.first.locator("button.cancel-order").click()
    expect(page.locator("#tradePendingTable tbody tr.empty")).to_be_visible()

    # Rejections are explained inline.
    page.select_option("#orderTypeSelect", "market")
    page.fill("#orderSymbolInput", "AAPL")
    page.click("#orderSideSell", force=True)
    page.fill("#orderQuantityInput", "99")
    page.click("#simulateOrderButton")
    expect(page.locator("#simulationVerdict")).to_have_class(re.compile(r"rejected"))
    expect(page.locator("#simulationVerdict")).to_contain_text("only 15 held")

    # Close the position from the table.
    page.locator("#tradePositionsTable tbody tr").first.locator("button.close-position").click()
    expect(page.locator("#confirmTitle")).to_have_text("Close AAPL?")
    page.click("#confirmOkButton")
    expect(page.locator("#tradePositionsTable tbody tr.empty")).to_be_visible(timeout=10000)
    assert funded_server.engine.broker.position("AAPL") is None
    # Positions page shows the same data with a close button column; the ledger has both legs.
    page.click("#navWallet")
    expect(page.locator("#transactionsTable tbody tr")).to_have_count(2)
    expect(page.locator("#transactionsTable tbody tr").first.locator(".pill").first).to_have_text("sell")


def test_wallet_deposit_via_sandbox_checkout_and_withdraw(funded_page: Page, funded_server: LiveServer) -> None:
    page = funded_page
    page.click("#navWallet")
    kpis = page.locator("#walletKpis .kpi")
    expect(kpis).to_have_count(6)
    expect(kpis.nth(0)).to_contain_text("50,000.00")
    expect(page.locator("#depositProviderLabel")).to_contain_text("sandbox")
    page.fill("#depositAmountInput", "200")
    page.select_option("#depositCurrencySelect", "EUR")
    expect(page.locator("#depositQuote")).to_contain_text("≈")
    expect(page.locator("#depositQuote")).to_contain_text("USD/EUR")
    page.click("#depositButton")
    page.wait_for_url("**/pay/mock/**")
    expect(page).to_have_title(re.compile("Sandbox checkout"))
    expect(page.locator("#paySummary")).to_contain_text("€200.00")
    expect(page.locator("#payButtonLabel")).to_have_text("Pay €200.00")
    page.click("#payButton")
    page.wait_for_url(re.compile(r".*#wallet$"))
    expect(page.locator("#walletNotice")).to_contain_text("Deposit of")
    expect(page.locator("#walletNotice")).to_contain_text("received")
    cash = funded_server.engine.broker.cash()
    assert 50_200 < cash < 50_260  # 200 EUR converted at the demo rate
    expect(page.locator("#walletKpis .kpi").nth(0)).to_contain_text(f"{cash:,.2f}")
    rows = page.locator("#transactionsTable tbody tr")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("deposit")
    expect(rows.first).to_contain_text("(200.00 EUR)")
    expect(rows.first).to_contain_text("completed")

    # Cancelling on the checkout page leaves a cancelled entry and no money moves.
    page.fill("#depositAmountInput", "50")
    page.select_option("#depositCurrencySelect", "USD")
    page.click("#depositButton")
    page.wait_for_url("**/pay/mock/**")
    page.click("#cancelButton")
    page.wait_for_url(re.compile(r".*#wallet$"))
    expect(page.locator("#walletNotice")).to_contain_text("cancelled")
    assert funded_server.engine.broker.cash() == pytest.approx(cash)

    # Withdraw part of the cash.
    page.fill("#withdrawAmountInput", "1000")
    page.fill("#withdrawDestinationInput", "DE89 3704 0044 0532 0130 00")
    page.click("#withdrawButton")
    page.click("#confirmOkButton")
    expect(page.locator("#withdrawStatus")).to_contain_text("completed")
    assert funded_server.engine.broker.cash() == pytest.approx(cash - 1000)
    expect(page.locator("#transactionsTable tbody tr")).to_have_count(3)
    expect(page.locator("#transactionsTable tbody tr").first).to_contain_text("withdrawal")
    page.select_option("#txKindFilter", "deposit")
    expect(page.locator("#transactionsTable tbody tr")).to_have_count(2)
    expect(page.locator("#txExportLink")).to_have_attribute("href", re.compile(r"kind=deposit.*format=csv"))


def test_fx_page_rates_chart_and_converter(funded_page: Page) -> None:
    page = funded_page
    page.click("#navFx")
    expect(page.locator("#fxSubtitle")).to_contain_text("demo")
    rows = page.locator("#fxTable tbody tr")
    expect(rows).to_have_count(12)  # default quote currencies
    expect(rows.filter(has_text="EUR")).to_contain_text("Euro")
    expect(page.locator("#fxChartTitle")).to_have_text("USD/EUR")
    expect(page.locator("#fxChartStats")).to_contain_text("over 90 days")
    colours = canvas_colors(page, "#fxCanvas")
    assert UP_RGB in colours or DOWN_RGB in colours, colours
    page.locator("#fxTable tbody tr", has_text="JPY").click()
    expect(page.locator("#fxChartTitle")).to_have_text("USD/JPY")
    page.locator("#fxRangeGroup label", has_text="1Y").click()
    expect(page.locator("#fxChartStats")).to_contain_text("over 365 days")
    page.select_option("#fxBaseSelect", "EUR")
    expect(page.locator("#fxChartTitle")).to_have_text("EUR/USD")
    expect(page.locator("#fxTable tbody tr", has_text="USD")).to_be_visible()
    page.fill("#convertAmountInput", "250")
    page.select_option("#convertFromSelect", "EUR")
    page.select_option("#convertToSelect", "USD")
    page.click("#convertButton")
    expect(page.locator("#convertResult")).to_contain_text("250.00 EUR =")
    expect(page.locator("#convertDetail")).to_contain_text("1 EUR =")
    page.click("#convertSwapButton")
    expect(page.locator("#convertFromSelect")).to_have_value("USD")
    expect(page.locator("#convertToSelect")).to_have_value("EUR")


def test_broker_connection_card_in_settings(funded_page: Page) -> None:
    page = funded_page
    page.click("#navSettings")
    expect(page.locator("#brokerCurrent")).to_contain_text("paper account")
    expect(page.locator("#brokerCredentialFields")).to_be_hidden()
    page.select_option("#brokerProviderSelect", "alpaca")
    expect(page.locator("#brokerCredentialFields")).to_be_visible()
    expect(page.locator("#brokerProviderNote")).to_contain_text("Alpaca")
    page.click("#brokerSaveButton")  # no credentials entered
    expect(page.locator("#brokerStatus")).to_contain_text("API key and secret are required")
    page.select_option("#brokerProviderSelect", "paper")
    expect(page.locator("#brokerSaveButton")).to_have_text("Use paper account")
    # Admin-only execution / payments cards are present for the administrator.
    expect(page.locator("#executionSettingsCard")).to_be_visible()
    expect(page.locator("#settingsSlippageModel")).to_have_value("fixed")
    expect(page.locator("#settingsPaymentsProvider")).to_have_value("mock")
    expect(page.locator("#stripeFields")).to_be_hidden()
    page.select_option("#settingsPaymentsProvider", "stripe")
    expect(page.locator("#stripeFields")).to_be_visible()

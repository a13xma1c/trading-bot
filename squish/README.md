# Squish for Web GUI tests

`suite_trading_bot_web` is a [Squish for Web](https://www.qt.io/product/quality-assurance/squish)
test suite (Python scripts, scripted object map) that drives the Trading Bot
web UI end-to-end in a real browser: dashboard and chart, watchlist editing,
engine start/stop with signal notifications, the strategy editor, backtesting
and the settings form.

## Running with Squish

1. Install Squish for Web (7.x) with the Python scripting language and register
   a browser (Chrome/Firefox/Edge) in the Squish IDE or via `squishserver --config`.
2. Start the application on the suite's test data (demo data provider, no API
   key needed). Relative paths in the config are resolved next to the file, so
   copy the test data somewhere writable first:
   ```bash
   cp -r squish/suite_trading_bot_web/shared/testdata /tmp/bot-squish
   trading-bot serve --config /tmp/bot-squish/config.json --no-browser
   ```
3. Point the suite at the server (default `http://127.0.0.1:8080/`, see
   `envvars`) and run it:
   ```bash
   squishrunner --testsuite squish/suite_trading_bot_web --reportgen xml3,results.xml
   ```

The test cases clean up after themselves (added symbols, created strategies,
paper positions and settings are restored), so they can run repeatedly against
the same server.

## Object map

`shared/scripts/names.py` is a scripted object map using Squish for Web real
names (`{"tagName": "BUTTON", "id": "startButton", "visible": True}`). All
elements referenced by the tests carry a stable HTML `id`, so names never
depend on layout or text.

## Running without Squish

`tests/test_squish_web_suite.py` executes the very same scripts under pytest
through a Playwright-backed implementation of the Squish for Web API
(`tests/squish_web_shim.py`: `startBrowser`, `waitForObject`, `clickButton`,
`typeText`, `selectOption`, `evalJS`, `test.compare`, ...). It starts a server
on a pristine copy of the test data for every test case:

```bash
uv run playwright install chromium   # once
uv run pytest tests/test_squish_web_suite.py -v
```

The shim covers the subset of the API used by this suite; anything outside of
it raises so a script cannot silently pass.

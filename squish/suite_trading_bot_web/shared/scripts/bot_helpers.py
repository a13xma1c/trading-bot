# -*- coding: utf-8 -*-
"""Shared helpers for the Trading Bot Squish for Web suite.

The suite tests a *running* server (see README.md): start it on the shared test
data with ``trading-bot serve --config shared/testdata/config.json --no-browser``
and point ``TRADING_BOT_URL`` (envvars) at it.  The same scripts are executed by
``tests/test_squish_web_suite.py`` through a Playwright-backed implementation of
the Squish for Web API, so they stay verified where Squish is not installed.
"""

import os

import names

DEFAULT_TIMEOUT = 20000  # milliseconds


def open_bot():
    """Open the web UI and wait until the chart has data."""
    url = os.environ.get("TRADING_BOT_URL", "http://127.0.0.1:8080/")
    startBrowser(url)
    waitForObject(names.chart_canvas)
    waitFor(lambda: evalJS("window.__app && window.__app.chart.bars.length > 0"), DEFAULT_TIMEOUT)
    test.verify(evalJS("window.__app.chart.bars.length") > 0, "chart data loaded")


def select_view(nav_name):
    clickButton(waitForObject(nav_name))


def set_text(object_name, text):
    """Replace the contents of an input / textarea with ``text``."""
    field = waitForObject(object_name)
    mouseClick(field)
    typeText(field, "<Ctrl+a>")
    typeText(field, "<Delete>")
    if text:
        typeText(field, text)


def table_text(table_name, row, column):
    """Text of a table cell, or "" if it does not exist (yet)."""
    name = names.table_cell(table_name, row, column)
    if not object.exists(name):
        return ""
    return str(findObject(name).innerText).strip()


def table_rows(table_name):
    return int(evalJS("document.getElementById(%r).querySelectorAll('tbody tr').length" % table_name["id"]))


def engine_running():
    return "running" in str(waitForObject(names.engine_state_text).innerText)


def wait_until_engine(running):
    waitFor(lambda: engine_running() == running, DEFAULT_TIMEOUT)
    test.compare(engine_running(), running, "engine running state")


def chart_state():
    return evalJS("window.__app.chart.getState()")

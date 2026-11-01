"""Scalability smoke test: many concurrent WebSocket subscribers and HTTP users against a live server.

The full-size run happens in CI through ``docker compose -f docker-compose.ci.yml`` (see
``trading_bot/tools/loadtest.py``); this keeps a smaller version in the regular suite.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from trading_bot.tools.loadtest import LoadTestOptions, LoadTestReport, format_report, run_load_test, write_report

from .web_helpers import TEST_PASSWORD, TEST_USER, LiveServer, start_live_server, write_project


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    live = start_live_server(write_project(tmp_path))
    yield live
    live.stop()


def test_many_clients_receive_events_and_api_stays_responsive(server: LiveServer) -> None:
    options = LoadTestOptions(url=server.url, username=TEST_USER, password=TEST_PASSWORD, ws_clients=60, users=8,
                              duration=4.0, max_p95_ms=2000, max_fanout_ms=3000, min_rps=20, max_error_rate=0.0)
    report: LoadTestReport = asyncio.run(run_load_test(options))
    print(format_report(report))
    assert report.ws_connected == 60
    assert report.ws_received_scan == 60, report.failures
    assert report.fanout_ms is not None and report.fanout_ms < 3000
    assert report.errors == 0 and report.requests > 80
    assert not report.failures, report.failures
    data = report.to_dict()
    assert data["passed"] and data["websocket"]["connected"] == 60


def test_report_thresholds_are_enforced() -> None:
    report = LoadTestReport(requests=100, errors=5, duration=10.0, latencies_ms=[10.0] * 90 + [900.0] * 10,
                            ws_clients=10, ws_connected=9, ws_received_scan=8, fanout_ms=2500)
    report.check(LoadTestOptions(url="x", username="u", password="p", max_p95_ms=500, max_fanout_ms=1000, min_rps=50,
                                 max_error_rate=0.01))
    text = "\n".join(report.failures)
    assert "9/10 WebSocket clients connected" in text
    assert "8/9 WebSocket clients received" in text
    assert "fan-out latency 2500" in text
    assert "p95 latency 900.0 ms exceeds 500" in text
    assert "throughput 10.0 req/s below 50" in text
    assert "error rate 5.00% exceeds 1.00%" in text
    assert "FAIL" in format_report(report)
    assert report.to_dict()["passed"] is False


def test_report_writing_is_best_effort(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = LoadTestReport(requests=10, duration=1.0, latencies_ms=[5.0] * 10, ws_clients=1, ws_connected=1,
                            ws_received_scan=1, fanout_ms=10.0)
    target = tmp_path / "out" / "loadtest.json"
    target.parent.mkdir()
    assert write_report(report, str(target)) is True
    assert '"passed": true' in target.read_text()
    assert write_report(report, "-") is True  # stdout
    assert '"rps": 10.0' in capsys.readouterr().out
    # An unwritable location (e.g. a bind mount owned by another user) does not fail the run.
    assert write_report(report, str(tmp_path / "missing-dir" / "loadtest.json")) is False
    captured = capsys.readouterr()
    assert "could not write report" in captured.err and '"passed": true' in captured.out

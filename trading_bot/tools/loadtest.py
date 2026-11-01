"""Load / scalability test for a running Trading Bot server.

Simulates many browser sessions at once:

* ``--ws-clients`` WebSocket subscribers that stay connected for the whole run
  (the real-time fan-out path);
* ``--users`` concurrent HTTP users that keep requesting the dashboard APIs
  (status, watchlist, bars, signals, strategies);
* a forced scan in the middle of the run; the time until *every* WebSocket
  client has received the resulting ``scan`` event is the fan-out latency.

Thresholds (``--max-p95-ms``, ``--max-fanout-ms``, ``--min-rps``,
``--max-error-rate``) turn the run into a pass/fail check for CI::

    python -m trading_bot.tools.loadtest --url http://localhost:8080 \\
        --username admin --password ... --ws-clients 200 --users 20 --duration 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets

ENDPOINTS = ["api/status", "api/watchlist", "api/bars/{symbol}", "api/signals?limit=50", "api/strategies",
             "api/positions"]


@dataclass
class LoadTestOptions:
    url: str
    username: str
    password: str
    ws_clients: int = 100
    users: int = 10
    duration: float = 15.0
    symbol: str = "AAPL"
    max_p95_ms: float | None = None
    max_fanout_ms: float | None = None
    min_rps: float | None = None
    max_error_rate: float = 0.0
    connect_timeout: float = 30.0


@dataclass
class LoadTestReport:
    requests: int = 0
    errors: int = 0
    duration: float = 0.0
    latencies_ms: list[float] = field(default_factory=list)
    per_endpoint: dict[str, list[float]] = field(default_factory=dict)
    ws_clients: int = 0
    ws_connected: int = 0
    ws_received_scan: int = 0
    fanout_ms: float | None = None
    failures: list[str] = field(default_factory=list)

    @property
    def rps(self) -> float:
        return self.requests / self.duration if self.duration else 0.0

    @property
    def error_rate(self) -> float:
        return self.errors / self.requests if self.requests else 0.0

    def percentile(self, pct: float, values: list[float] | None = None) -> float:
        data = sorted(values if values is not None else self.latencies_ms)
        if not data:
            return 0.0
        index = min(len(data) - 1, max(0, round(pct / 100 * (len(data) - 1))))
        return data[index]

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests, "errors": self.errors, "error_rate": round(self.error_rate, 4),
            "duration_s": round(self.duration, 2), "rps": round(self.rps, 1),
            "latency_ms": {"p50": round(self.percentile(50), 1), "p95": round(self.percentile(95), 1),
                           "p99": round(self.percentile(99), 1),
                           "mean": round(statistics.fmean(self.latencies_ms), 1) if self.latencies_ms else 0.0},
            "per_endpoint_p95_ms": {k: round(self.percentile(95, v), 1) for k, v in sorted(self.per_endpoint.items())},
            "websocket": {"clients": self.ws_clients, "connected": self.ws_connected,
                          "received_scan_event": self.ws_received_scan, "fanout_ms": self.fanout_ms},
            "failures": self.failures,
            "passed": not self.failures,
        }

    def check(self, options: LoadTestOptions) -> None:
        if self.ws_connected < self.ws_clients:
            self.failures.append(f"only {self.ws_connected}/{self.ws_clients} WebSocket clients connected")
        if self.ws_received_scan < self.ws_connected:
            self.failures.append(f"only {self.ws_received_scan}/{self.ws_connected} WebSocket clients received the scan event")
        if options.max_fanout_ms is not None and (self.fanout_ms is None or self.fanout_ms > options.max_fanout_ms):
            self.failures.append(f"fan-out latency {self.fanout_ms} ms exceeds {options.max_fanout_ms} ms")
        if options.max_p95_ms is not None and self.percentile(95) > options.max_p95_ms:
            self.failures.append(f"p95 latency {self.percentile(95):.1f} ms exceeds {options.max_p95_ms} ms")
        if options.min_rps is not None and self.rps < options.min_rps:
            self.failures.append(f"throughput {self.rps:.1f} req/s below {options.min_rps} req/s")
        if self.error_rate > options.max_error_rate:
            self.failures.append(f"error rate {self.error_rate:.2%} exceeds {options.max_error_rate:.2%}")


async def _login(client: httpx.AsyncClient, options: LoadTestOptions) -> str:
    response = await client.post("api/auth/login", json={"username": options.username, "password": options.password})
    response.raise_for_status()
    cookie = response.cookies.get("tb_session")
    if not cookie:
        raise RuntimeError("login did not return a session cookie")
    return cookie


async def _ws_client(index: int, ws_url: str, cookie: str, ready: asyncio.Event, stop: asyncio.Event,
                     received: dict[int, float], connected: list[int], options: LoadTestOptions) -> None:
    try:
        async with websockets.connect(ws_url, additional_headers={"Cookie": f"tb_session={cookie}"},
                                      open_timeout=options.connect_timeout, max_queue=64) as socket:
            hello = json.loads(await asyncio.wait_for(socket.recv(), options.connect_timeout))
            if hello.get("type") != "hello":
                return
            connected.append(index)
            await ready.wait()
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=0.5)
                except TimeoutError:
                    continue
                if json.loads(raw).get("type") == "scan" and index not in received:
                    received[index] = time.perf_counter()
    except Exception as exc:  # noqa: BLE001 - counted as a failed client
        sys.stderr.write(f"ws client {index}: {exc}\n")


async def _http_user(client: httpx.AsyncClient, index: int, options: LoadTestOptions, report: LoadTestReport,
                     stop: asyncio.Event) -> None:
    step = index
    while not stop.is_set():
        path = ENDPOINTS[step % len(ENDPOINTS)].format(symbol=options.symbol)
        step += 1
        started = time.perf_counter()
        try:
            response = await client.get(path)
            ok = response.status_code == 200
        except httpx.HTTPError:
            ok = False
        elapsed = (time.perf_counter() - started) * 1000
        report.requests += 1
        report.latencies_ms.append(elapsed)
        report.per_endpoint.setdefault(path.split("?")[0], []).append(elapsed)
        if not ok:
            report.errors += 1


async def run_load_test(options: LoadTestOptions) -> LoadTestReport:
    base = options.url.rstrip("/") + "/"
    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "ws"
    report = LoadTestReport(ws_clients=options.ws_clients)
    limits = httpx.Limits(max_connections=options.users + 5, max_keepalive_connections=options.users + 5)
    async with httpx.AsyncClient(base_url=base, timeout=30.0, limits=limits) as client:
        cookie = await _login(client, options)
        client.cookies.set("tb_session", cookie)
        await client.get(f"api/bars/{options.symbol}")  # warm the cache like a first page load would

        ready, stop = asyncio.Event(), asyncio.Event()
        received: dict[int, float] = {}
        connected: list[int] = []
        ws_tasks = [asyncio.create_task(_ws_client(i, ws_url, cookie, ready, stop, received, connected, options))
                    for i in range(options.ws_clients)]
        deadline = time.perf_counter() + options.connect_timeout
        while len(connected) < options.ws_clients and time.perf_counter() < deadline and not all(t.done() for t in ws_tasks):
            await asyncio.sleep(0.05)
        report.ws_connected = len(connected)
        ready.set()

        started = time.perf_counter()
        http_tasks = [asyncio.create_task(_http_user(client, i, options, report, stop)) for i in range(options.users)]
        await asyncio.sleep(options.duration / 2)
        # Fan-out probe: a forced scan publishes a "scan" event to every subscriber.
        sent_at = time.perf_counter()
        await client.post("api/engine/scan")
        probe_deadline = time.perf_counter() + max(5.0, options.duration / 2)
        while len(received) < report.ws_connected and time.perf_counter() < probe_deadline:
            await asyncio.sleep(0.02)
        remaining = options.duration - (time.perf_counter() - started)
        if remaining > 0:
            await asyncio.sleep(remaining)
        stop.set()
        report.duration = time.perf_counter() - started
        await asyncio.gather(*http_tasks, return_exceptions=True)
        await asyncio.gather(*ws_tasks, return_exceptions=True)
        report.ws_received_scan = len(received)
        if received:
            report.fanout_ms = round((max(received.values()) - sent_at) * 1000, 1)
    report.check(options)
    return report


def format_report(report: LoadTestReport) -> str:
    data = report.to_dict()
    lines = [
        "Load test report",
        f"  requests: {data['requests']}  errors: {data['errors']} ({data['error_rate']:.2%})  "
        f"duration: {data['duration_s']} s  throughput: {data['rps']} req/s",
        f"  latency ms: p50 {data['latency_ms']['p50']}  p95 {data['latency_ms']['p95']}  "
        f"p99 {data['latency_ms']['p99']}  mean {data['latency_ms']['mean']}",
    ]
    for endpoint, p95 in data["per_endpoint_p95_ms"].items():
        lines.append(f"    {endpoint:<22} p95 {p95} ms")
    ws = data["websocket"]
    lines.append(f"  websocket: {ws['connected']}/{ws['clients']} connected, {ws['received_scan_event']} received the scan "
                 f"event, fan-out {ws['fanout_ms']} ms")
    lines.append("  RESULT: " + ("PASS" if data["passed"] else "FAIL - " + "; ".join(data["failures"])))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load / scalability test for a running Trading Bot server")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", required=True)
    parser.add_argument("--ws-clients", type=int, default=100)
    parser.add_argument("--users", type=int, default=10)
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--symbol", default="AAPL")
    parser.add_argument("--max-p95-ms", type=float)
    parser.add_argument("--max-fanout-ms", type=float)
    parser.add_argument("--min-rps", type=float)
    parser.add_argument("--max-error-rate", type=float, default=0.0)
    parser.add_argument("--report", help="write the JSON report to this file ('-' for stdout)")
    args = parser.parse_args(argv)
    options = LoadTestOptions(url=args.url, username=args.username, password=args.password, ws_clients=args.ws_clients,
                              users=args.users, duration=args.duration, symbol=args.symbol, max_p95_ms=args.max_p95_ms,
                              max_fanout_ms=args.max_fanout_ms, min_rps=args.min_rps, max_error_rate=args.max_error_rate)
    report = asyncio.run(run_load_test(options))
    print(format_report(report))
    if args.report:
        write_report(report, args.report)
    return 0 if not report.failures else 1


def write_report(report: LoadTestReport, path: str) -> bool:
    """Write the JSON report; ``-`` means stdout.

    A report location that cannot be written (e.g. a bind mount owned by another
    user) must not turn a passed load test into a failure: the JSON is printed to
    stdout instead and False is returned.
    """
    payload = json.dumps(report.to_dict(), indent=2)
    if path == "-":
        print(payload)
        return True
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
        return True
    except OSError as exc:
        sys.stderr.write(f"warning: could not write report to {path}: {exc}; printing it instead\n")
        print(payload)
        return False


if __name__ == "__main__":
    sys.exit(main())

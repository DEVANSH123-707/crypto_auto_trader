"""Load-test POST /webhook/tradingview and report latency percentiles.

    # against a server you started yourself
    python scripts/benchmark_webhook.py --concurrency 50 --requests 100

    # start both server modes and compare them (the "before/after" run)
    python scripts/benchmark_webhook.py --compare

``--compare`` is the interesting one. It starts the app twice - once with
``WEBHOOK_ASYNC_EXECUTION=false`` (the exchange call happens inside the
request) and once with it on (the request returns as soon as the trade is
durable) - runs the same load against each, and prints the difference.

Both modes use the simulated exchange from ``scripts/bench_app.py``, so the
comparison isolates *our* architecture rather than Binance's network latency.
No real order is ever placed.

What is measured is the **webhook acknowledgement latency**: the time for the
HTTP request to be accepted and the trade to be durably recorded. It is not the
time for the order to reach Binance - see the trade's status for that.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import subprocess
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import httpx2

DEFAULT_URL = "http://127.0.0.1:8000"
BENCH_HOST = "127.0.0.1"


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class Result:
    label: str
    concurrency: int
    latencies_ms: list[float] = field(default_factory=list)
    status_codes: Counter[int] = field(default_factory=Counter)
    errors: int = 0
    wall_seconds: float = 0.0

    @property
    def total(self) -> int:
        return len(self.latencies_ms) + self.errors

    @property
    def successes(self) -> int:
        return sum(n for code, n in self.status_codes.items() if 200 <= code < 300)

    @property
    def failures(self) -> int:
        return self.total - self.successes

    @property
    def error_rate(self) -> float:
        return (self.failures / self.total * 100) if self.total else 0.0

    @property
    def rps(self) -> float:
        return (self.total / self.wall_seconds) if self.wall_seconds else 0.0

    def pct(self, p: float) -> float:
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        index = min(int(len(ordered) * p / 100), len(ordered) - 1)
        return ordered[index]

    @property
    def mean(self) -> float:
        return statistics.fmean(self.latencies_ms) if self.latencies_ms else 0.0

    def render(self) -> str:
        codes = " ".join(f"{code}:{n}" for code, n in sorted(self.status_codes.items()))
        return (
            f"  requests      {self.total}\n"
            f"  successful    {self.successes}\n"
            f"  failed        {self.failures}  ({self.error_rate:.1f}%)\n"
            f"  status codes  {codes or '-'}"
            + (f"  transport-errors:{self.errors}" if self.errors else "")
            + "\n"
            f"  avg           {self.mean:7.1f} ms\n"
            f"  p50           {self.pct(50):7.1f} ms\n"
            f"  p95           {self.pct(95):7.1f} ms\n"
            f"  p99           {self.pct(99):7.1f} ms\n"
            f"  max           {max(self.latencies_ms, default=0.0):7.1f} ms\n"
            f"  throughput    {self.rps:7.1f} req/s\n"
            f"  wall time     {self.wall_seconds:7.2f} s"
        )


# ---------------------------------------------------------------------------
# Load generation
# ---------------------------------------------------------------------------


async def run_load(
    *,
    url: str,
    secret: str,
    requests: int,
    concurrency: int,
    label: str,
    symbol: str,
    quantity: str,
) -> Result:
    """Fire ``requests`` webhooks, at most ``concurrency`` in flight at once."""
    result = Result(label=label, concurrency=concurrency)
    semaphore = asyncio.Semaphore(concurrency)
    run_id = uuid.uuid4().hex[:8]
    lock = asyncio.Lock()

    limits = httpx2.Limits(
        max_connections=concurrency, max_keepalive_connections=concurrency
    )
    async with httpx2.AsyncClient(
        base_url=url, timeout=httpx2.Timeout(30.0), limits=limits
    ) as client:

        async def one(index: int) -> None:
            payload = {
                # Unique per request, so every one is a real new trade rather
                # than a duplicate short-circuit.
                "signal_id": f"bench-{run_id}-{index:05d}",
                "symbol": symbol,
                "action": "BUY" if index % 2 == 0 else "SELL",
                "quantity": quantity,
            }
            async with semaphore:
                started = time.perf_counter()
                try:
                    response = await client.post(
                        "/webhook/tradingview",
                        json=payload,
                        headers={"X-Webhook-Secret": secret},
                    )
                except Exception:  # noqa: BLE001 - a transport error is a result
                    async with lock:
                        result.errors += 1
                    return
                elapsed_ms = (time.perf_counter() - started) * 1000
                async with lock:
                    result.latencies_ms.append(elapsed_ms)
                    result.status_codes[response.status_code] += 1

        wall_start = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(requests)))
        result.wall_seconds = time.perf_counter() - wall_start

    return result


async def wait_for_health(url: str, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    async with httpx2.AsyncClient(base_url=url, timeout=httpx2.Timeout(5.0)) as client:
        while time.time() < deadline:
            try:
                if (await client.get("/health")).status_code == 200:
                    return True
            except Exception:  # noqa: BLE001 - still starting
                pass
            await asyncio.sleep(0.4)
    return False


# ---------------------------------------------------------------------------
# Server management (--compare)
# ---------------------------------------------------------------------------


def start_server(port: int, *, async_execution: bool, latency_ms: float):
    env = os.environ.copy()
    env["WEBHOOK_ASYNC_EXECUTION"] = "true" if async_execution else "false"
    env["BENCH_BINANCE_LATENCY_MS"] = str(latency_ms)
    env["LOG_LEVEL"] = "WARNING"  # do not benchmark the logger
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "scripts.bench_app:app",
            "--host",
            BENCH_HOST,
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def benchmark_mode(
    *,
    label: str,
    async_execution: bool,
    port: int,
    args: argparse.Namespace,
) -> dict[int, Result]:
    server = start_server(
        port, async_execution=async_execution, latency_ms=args.binance_latency_ms
    )
    url = f"http://{BENCH_HOST}:{port}"
    results: dict[int, Result] = {}
    try:
        if not await wait_for_health(url):
            raise RuntimeError(f"{label}: server did not become healthy on {url}")

        # A short warm-up so connection setup and the first-request import cost
        # do not land in the measured sample.
        await run_load(
            url=url,
            secret=args.secret,
            requests=max(5, args.concurrency_levels[0]),
            concurrency=args.concurrency_levels[0],
            label="warmup",
            symbol=args.symbol,
            quantity=args.quantity,
        )

        for concurrency in args.concurrency_levels:
            result = await run_load(
                url=url,
                secret=args.secret,
                requests=args.requests,
                concurrency=concurrency,
                label=label,
                symbol=args.symbol,
                quantity=args.quantity,
            )
            results[concurrency] = result
            print(f"\n[{label}] concurrency={concurrency}")
            print(result.render())
    finally:
        server.terminate()
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def read_secret_from_env_file() -> str | None:
    env_file = Path(".env")
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("WEBHOOK_SECRET="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("WEBHOOK_SECRET")


async def async_main(args: argparse.Namespace) -> int:
    if not args.secret:
        print("No webhook secret. Set WEBHOOK_SECRET in .env or pass --secret.")
        return 1

    if not args.compare:
        if not await wait_for_health(args.url, timeout=5):
            print(f"No server responding at {args.url}. Start one, or use --compare.")
            return 1
        result = await run_load(
            url=args.url,
            secret=args.secret,
            requests=args.requests,
            concurrency=args.concurrency,
            label="webhook",
            symbol=args.symbol,
            quantity=args.quantity,
        )
        print(f"\nPOST /webhook/tradingview  concurrency={args.concurrency}")
        print(result.render())
        return 0

    print("=" * 68)
    print("Webhook acknowledgement latency: inline vs queued exchange call")
    print(f"  requests per level : {args.requests}")
    print(f"  concurrency levels : {args.concurrency_levels}")
    print(f"  simulated exchange : {args.binance_latency_ms:.0f} ms round trip")
    print("=" * 68)

    baseline = await benchmark_mode(
        label="BEFORE (inline: request waits for the exchange)",
        async_execution=False,
        port=args.port,
        args=args,
    )
    optimised = await benchmark_mode(
        label="AFTER  (queued: request returns once the trade is durable)",
        async_execution=True,
        port=args.port + 1,
        args=args,
    )

    print("\n" + "=" * 68)
    print("SUMMARY - webhook acknowledgement latency (ms)")
    print("=" * 68)
    header = (
        f"{'conc':>5} | {'before p50':>10} {'after p50':>10} {'p50 -%':>8}"
        f" | {'before p95':>10} {'after p95':>10} {'p95 -%':>8}"
    )
    print(header)
    print("-" * len(header))
    for concurrency in args.concurrency_levels:
        before, after = baseline[concurrency], optimised[concurrency]
        p50_gain = (
            (before.pct(50) - after.pct(50)) / before.pct(50) * 100
            if before.pct(50)
            else 0.0
        )
        p95_gain = (
            (before.pct(95) - after.pct(95)) / before.pct(95) * 100
            if before.pct(95)
            else 0.0
        )
        print(
            f"{concurrency:>5} | {before.pct(50):>10.1f} {after.pct(50):>10.1f} "
            f"{p50_gain:>7.1f}% | {before.pct(95):>10.1f} {after.pct(95):>10.1f} "
            f"{p95_gain:>7.1f}%"
        )

    print("\nNote: this is the time to accept and durably record a signal.")
    print("Order execution still takes a full exchange round trip in both modes;")
    print("in queued mode it happens after the response, tracked as PENDING.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="Server to load-test.")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Start the app in both execution modes and compare them.",
    )
    parser.add_argument(
        "--concurrency-levels",
        type=int,
        nargs="+",
        default=[10, 25, 50],
        help="Concurrency levels for --compare (default: 10 25 50).",
    )
    parser.add_argument(
        "--binance-latency-ms",
        type=float,
        default=165.0,
        help=(
            "Simulated exchange round trip for --compare. The default is the "
            "median RTT measured against testnet.binance.vision."
        ),
    )
    parser.add_argument("--port", type=int, default=8801)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--quantity", default="0.001")
    parser.add_argument("--secret", default=None)
    args = parser.parse_args()

    args.secret = args.secret or read_secret_from_env_file()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    sys.exit(main())

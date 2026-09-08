"""Send N external alert signals to a running server.

    python scripts/simulate_trades.py --count 100
    python scripts/simulate_trades.py --count 250 --concurrency 20

Each signal gets a unique ``signal_id``, so every one is a real new trade
rather than a duplicate short-circuit. Use ``--duplicate-every N`` to
deliberately re-send an earlier signal and watch idempotency reject it.

Safety: this talks to whatever server is at ``--url``. That server is
testnet-only by construction, and with ``BINANCE_DRY_RUN=true`` it calls
``POST /api/v3/order/test``, which validates an order without creating one.
No real-money order is possible.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

import httpx2

DEFAULT_URL = "http://127.0.0.1:8000"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]


def read_secret() -> str | None:
    env_file = Path(".env")
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("WEBHOOK_SECRET="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("WEBHOOK_SECRET")


async def simulate(args: argparse.Namespace) -> int:
    run_id = uuid.uuid4().hex[:8]
    semaphore = asyncio.Semaphore(args.concurrency)
    statuses: Counter[int] = Counter()
    trade_statuses: Counter[str] = Counter()
    latencies: list[float] = []
    transport_errors = 0
    lock = asyncio.Lock()

    async with httpx2.AsyncClient(
        base_url=args.url, timeout=httpx2.Timeout(30.0)
    ) as client:
        try:
            health = await client.get("/health/ready")
        except Exception as exc:  # noqa: BLE001
            print(f"Cannot reach {args.url}: {type(exc).__name__}")
            print("Start the server first:  uvicorn app.main:app")
            return 1
        if health.status_code != 200:
            print(f"Server is not ready: {health.text}")
            return 1

        async def send(index: int) -> None:
            nonlocal transport_errors
            # Optionally repeat an earlier signal id to exercise idempotency.
            if args.duplicate_every and index and index % args.duplicate_every == 0:
                signal_id = f"sim-{run_id}-{index - 1:05d}"
            else:
                signal_id = f"sim-{run_id}-{index:05d}"

            payload = {
                "signal_id": signal_id,
                "symbol": args.symbol or SYMBOLS[index % len(SYMBOLS)],
                "action": "BUY" if index % 2 == 0 else "SELL",
                "quantity": args.quantity,
            }
            async with semaphore:
                started = time.perf_counter()
                try:
                    response = await client.post(
                        "/webhook/tradingview",
                        json=payload,
                        headers={"X-Webhook-Secret": args.secret},
                    )
                except Exception:  # noqa: BLE001
                    async with lock:
                        transport_errors += 1
                    return
                elapsed_ms = (time.perf_counter() - started) * 1000

            async with lock:
                latencies.append(elapsed_ms)
                statuses[response.status_code] += 1
                if response.status_code == 202:
                    trade_statuses[response.json()["trade"]["status"]] += 1

        print(f"Sending {args.count} signals to {args.url} ...")
        wall_start = time.perf_counter()
        await asyncio.gather(*(send(i) for i in range(args.count)))
        wall = time.perf_counter() - wall_start

    accepted = statuses.get(202, 0)
    duplicates = statuses.get(409, 0)
    rejected = sum(n for code, n in statuses.items() if code not in (202, 409))

    print("\n" + "=" * 56)
    print("SIMULATION SUMMARY")
    print("=" * 56)
    print(f"  signals sent        {args.count}")
    print(f"  accepted (202)      {accepted}")
    print(f"  duplicates (409)    {duplicates}")
    print(f"  other failures      {rejected + transport_errors}")
    print(f"  wall time           {wall:.2f} s  ({args.count / wall:.1f} signals/s)")
    if latencies:
        ordered = sorted(latencies)
        p95 = ordered[min(int(len(ordered) * 0.95), len(ordered) - 1)]
        print(f"  avg latency         {statistics.fmean(ordered):.1f} ms")
        print(f"  p95 latency         {p95:.1f} ms")
    if statuses:
        print(f"  status codes        {dict(sorted(statuses.items()))}")
    if trade_statuses:
        print(f"  trade status at ack {dict(trade_statuses)}")
        print(
            "\n  (In queued mode a trade is PENDING at acknowledgement and is\n"
            "   completed by the executor. Check the final states with:\n"
            "     GET /trades   or   python -m scripts.reconcile)"
        )
    return 0 if (accepted + duplicates) == args.count else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--count", type=int, default=100, help="Signals to send.")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--quantity", default="0.001")
    parser.add_argument(
        "--symbol", default=None, help="Fixed symbol (default: rotate through five)."
    )
    parser.add_argument(
        "--duplicate-every",
        type=int,
        default=0,
        metavar="N",
        help="Re-send the previous signal_id every N signals, to exercise idempotency.",
    )
    parser.add_argument("--secret", default=None)
    args = parser.parse_args()

    args.secret = args.secret or read_secret()
    if not args.secret:
        print("No webhook secret. Set WEBHOOK_SECRET in .env or pass --secret.")
        return 1

    return asyncio.run(simulate(args))


if __name__ == "__main__":
    sys.exit(main())

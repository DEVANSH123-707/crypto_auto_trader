"""ASGI app for the benchmark harness. **Not a production entry point.**

This is ``app.main:app`` with one thing replaced: the Binance client becomes a
simulated exchange with a fixed, configurable latency. That is deliberate - a
benchmark that called the real testnet would be measuring Binance's network
round trip (measured at ~164 ms median from this machine) rather than this
backend, and would be neither reproducible nor rate-limit-friendly at 100+
requests.

The substitution uses the same ``dependency_overrides`` seam the test suite
uses, so every route, service and executor code path underneath runs unchanged.

Run it via ``scripts/benchmark_webhook.py``, which sets the environment for
you, or directly:

    $env:BENCH_BINANCE_LATENCY_MS="165"
    uvicorn scripts.bench_app:app
"""

from __future__ import annotations

import os

from app.dependencies.services import get_binance_client
from app.main import app
from app.services.execution_queue import TradeExecutor, set_trade_executor
from tests.fakes import FakeBinanceClient

#: Simulated exchange round-trip time. The default matches the median RTT
#: measured against https://testnet.binance.vision from this machine.
LATENCY_MS = float(os.environ.get("BENCH_BINANCE_LATENCY_MS", "165"))

_simulated_exchange = FakeBinanceClient(latency_seconds=LATENCY_MS / 1000.0)

# The request path (used when WEBHOOK_ASYNC_EXECUTION is off).
app.dependency_overrides[get_binance_client] = lambda: _simulated_exchange

# The executor path (used when it is on). Installed before the lifespan runs,
# so the executor the app starts is this one.
set_trade_executor(TradeExecutor(binance_client_factory=lambda: _simulated_exchange))


def order_count() -> int:
    """How many orders the simulated exchange has been asked to place."""
    return _simulated_exchange.place_order_calls

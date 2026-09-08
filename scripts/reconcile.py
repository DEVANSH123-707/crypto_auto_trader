"""Reconcile every in-flight trade against Binance.

Run it whenever trades are stuck, and on a schedule in a real deployment
(Windows Task Scheduler, cron, or a systemd timer - no extra infrastructure
needed):

    python -m scripts.reconcile
    python -m scripts.reconcile --older-than 300 --limit 50

This only ever *reads* from Binance, so running it twice is harmless and it can
never create a duplicate order.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.core.logging_config import configure_logging
from app.db.database import AsyncSessionLocal
from app.services.binance_service import BinanceClient
from app.services.reconciliation_service import reconcile_pending_trades

logger = logging.getLogger("scripts.reconcile")


async def run(older_than: int, limit: int) -> int:
    db = AsyncSessionLocal()
    try:
        async with BinanceClient() as binance:
            summary = await reconcile_pending_trades(
                db, binance, older_than_seconds=older_than, limit=limit
            )
    except Exception:
        logger.exception("Reconciliation run failed")
        return 1
    finally:
        await db.close()

    print(
        f"Reconciliation complete: checked={summary['checked']} "
        f"changed={summary['changed']} unchanged={summary['unchanged']}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--older-than",
        type=int,
        default=60,
        metavar="SECONDS",
        help=(
            "Only check trades created more than this many seconds ago, so "
            "requests still legitimately in flight are left alone (default 60)."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum number of trades to check in one run (default 100).",
    )
    args = parser.parse_args()

    configure_logging()
    return asyncio.run(run(args.older_than, args.limit))


if __name__ == "__main__":
    sys.exit(main())

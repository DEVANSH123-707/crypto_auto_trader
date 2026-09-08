"""Pre-flight check: is this machine actually configured to run the project?

    python -m scripts.check_setup

Verifies, in order: configuration loads, the Binance URL is a testnet host,
PostgreSQL is reachable, the schema exists, the webhook owner account exists,
and that the Binance testnet is reachable. Every failure comes with the exact
fix.

Read-only: it never writes to the database and never places an order.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select, text

OK = "[ OK ]"
FAIL = "[FAIL]"
WARN = "[WARN]"


async def run() -> int:
    failures = 0

    # -- 1. Configuration ------------------------------------------------
    try:
        from app.core.config import settings
    except Exception as exc:  # noqa: BLE001 - this report *is* the output
        print(f"{FAIL} Configuration did not load.")
        print(f"       {exc}")
        print("       Fix: copy .env.example to .env and fill in every value.")
        return 1
    print(f"{OK} Configuration loaded (.env)")

    # -- 2. Binance safety ------------------------------------------------
    # Reaching this line already proves the testnet gate passed: Settings
    # refuses to construct for a non-testnet BINANCE_BASE_URL.
    print(f"{OK} Binance host is a testnet host: {settings.binance_host}")
    dry_run_note = (
        "validates only, creates no order"
        if settings.BINANCE_DRY_RUN
        else "places real TESTNET orders"
    )
    print(f"       Dry run: {settings.BINANCE_DRY_RUN} ({dry_run_note})")
    execution_note = (
        "queued (202, the executor places the order)"
        if settings.WEBHOOK_ASYNC_EXECUTION
        else "inline (the request waits for the exchange)"
    )
    print(f"       Webhook execution: {execution_note}")

    # -- 3. Database connectivity ----------------------------------------
    from app.db.database import engine

    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT version()"))
            version = result.scalar_one()
        print(f"{OK} PostgreSQL reachable: {str(version).split(',')[0]}")
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} Cannot connect to PostgreSQL.")
        print(f"       {type(exc).__name__}: {exc}")
        print("       Fix: start PostgreSQL and check DATABASE_URL in .env.")
        await engine.dispose()
        return 1

    # -- 4. Schema --------------------------------------------------------
    async with engine.connect() as connection:
        tables = set(
            await connection.run_sync(
                lambda sync_conn: sync_conn.dialect.get_table_names(sync_conn)
            )
        )
    missing = {"users", "trades"} - tables
    if missing:
        print(f"{FAIL} Missing table(s): {', '.join(sorted(missing))}")
        print("       Fix: alembic upgrade head")
        failures += 1
    else:
        print(f"{OK} Tables 'users' and 'trades' exist")

    # -- 5. Webhook owner account ----------------------------------------
    if not missing:
        from app.db.database import AsyncSessionLocal
        from app.db.models import User

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User.id).where(
                    User.email == settings.WEBHOOK_TRADE_OWNER_EMAIL
                )
            )
            owner_id = result.scalar_one_or_none()

        if owner_id is None:
            print(
                f"{WARN} No account for WEBHOOK_TRADE_OWNER_EMAIL "
                f"({settings.WEBHOOK_TRADE_OWNER_EMAIL})"
            )
            print("       Fix: POST /auth/register with that email address.")
            failures += 1
        else:
            print(f"{OK} Webhook trade owner exists (user id={owner_id})")

    await engine.dispose()

    # -- 6. Binance reachability -----------------------------------------
    from app.services.binance_service import BinanceClient

    try:
        async with BinanceClient() as binance:
            if not await binance.ping():
                raise RuntimeError("ping failed")
            server_time = await binance.get_server_time()
        print(f"{OK} Binance testnet reachable (server time {server_time})")
    except Exception as exc:  # noqa: BLE001
        print(f"{WARN} Could not reach the Binance testnet: {exc}")
        print("       The app still runs; trades will be recorded as FAILED.")

    print()
    if failures:
        print(f"{failures} item(s) still need attention.")
        return 1
    print("All checks passed. Start the server with:  uvicorn app.main:app --reload")
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())

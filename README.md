# Crypto Auto-Trading Backend

A single-service FastAPI backend that receives TradingView alerts over a
webhook and places the corresponding orders on the **Binance Spot testnet**.

> ### Safety
> This project is **testnet-only**. It refuses to start unless
> `BINANCE_BASE_URL` points at a Binance testnet host, and the check is
> repeated inside the Binance client immediately before every order. There is
> no environment flag to switch it off - pointing this at real-money Binance
> would require editing `ALLOWED_BINANCE_HOSTS` in `app/core/config.py`, which
> is a deliberate, reviewable code change. No real money is ever at risk.

---

## Table of contents

1. [What it does](#1-what-it-does)
2. [Architecture](#2-architecture)
3. [Folder structure](#3-folder-structure)
4. [Prerequisites](#4-prerequisites)
5. [Setup, step by step (Windows)](#5-setup-step-by-step-windows)
6. [Running the app](#6-running-the-app)
7. [Using the API](#7-using-the-api)
8. [The TradingView webhook](#8-the-tradingview-webhook)
9. [How duplicate protection works](#9-how-duplicate-protection-works)
10. [Failure and recovery](#10-failure-and-recovery)
11. [Running the tests](#11-running-the-tests)
12. [Manual Binance testnet integration test](#12-manual-binance-testnet-integration-test)
13. [Deploying to Render](#13-deploying-to-render)
14. [Interview demo](#14-interview-demo)
15. [Troubleshooting](#15-troubleshooting)

For a line-by-line explanation of *why* everything is built the way it is, see
**[PROJECT_IMPLEMENTATION_NOTES.md](PROJECT_IMPLEMENTATION_NOTES.md)**.

---

## 1. What it does

| Capability | Endpoint |
| --- | --- |
| Register an account | `POST /auth/register` |
| Log in, receive a JWT | `POST /auth/login` |
| Check the current token | `GET /auth/me` |
| List your own trades | `GET /trades` |
| Read one of your trades | `GET /trades/{trade_id}` |
| Ask Binance what really happened to a trade | `POST /trades/{trade_id}/reconcile` |
| Receive a TradingView alert | `POST /webhook/tradingview` |
| Liveness | `GET /health` |
| Readiness (checks PostgreSQL) | `GET /health/ready` |

The interesting parts are not the CRUD. They are:

* **Idempotency** - the same `signal_id` can never produce two orders, even
  under concurrent requests, because the guarantee is a PostgreSQL `UNIQUE`
  constraint rather than an application check.
* **Honest failure states** - a Binance timeout is recorded as `UNKNOWN`, not
  guessed to be a success or a failure, and is never blindly retried.
* **Recoverability** - the trade row is committed *before* Binance is called,
  so any lost response can be resolved afterwards by asking Binance about the
  client order id that was already durably stored.

---

## 2. Architecture

One FastAPI process, one PostgreSQL database, one outbound HTTP client. No
message broker, no worker pool, no microservices - none of them would earn
their complexity here.

```
                     ┌──────────────────────────────────────────────┐
   TradingView ──────▶│  POST /webhook/tradingview                   │
   (shared secret)    │      route: authenticate + log               │
                      └───────────────────┬──────────────────────────┘
                                          │
   Browser / curl ────▶ /auth/*  /trades/*│ (JWT bearer auth)
   (JWT)                                  │
                                          ▼
                      ┌──────────────────────────────────────────────┐
                      │  TradingService  (all business logic)        │
                      │   1. business validation                     │
                      │   2. INSERT trade PENDING ──► COMMIT ①       │
                      │      (the UNIQUE index on signal_id is what  │
                      │       rejects a duplicate - see §9)          │
                      │   3. enqueue trade id, return 202 Accepted   │
                      └───────┬──────────────────────────────────────┘
                              │ asyncio.Queue (in-process)
                              ▼
                      ┌──────────────────────────────────────────────┐
                      │  TradeExecutor worker                        │
                      │   4. claim atomically (conditional UPDATE)   │
                      │   5. call Binance                            │
                      │   6. map outcome ──────────► COMMIT ②        │
                      └───────┬───────────────────────────┬──────────┘
                              │                           │
                              ▼                           ▼
                   ┌────────────────────┐    ┌──────────────────────────┐
                   │  SQLAlchemy (async)│    │  BinanceClient (httpx2)  │
                   │ AsyncSession/Engine│    │  async, HMAC-SHA256      │
                   └─────────┬──────────┘    └────────────┬─────────────┘
                             │ asyncpg                    │ HTTPS
                             ▼                            ▼
                   ┌────────────────────┐    ┌──────────────────────────┐
                   │    PostgreSQL      │    │ testnet.binance.vision   │
                   │  users, trades     │    │   (TESTNET ONLY)         │
                   └────────────────────┘    └──────────────────────────┘

   Set WEBHOOK_ASYNC_EXECUTION=false to run steps 4-6 inside the request
   instead. Durability, idempotency and recovery are identical either way.

   Recovery path (read-only against Binance, safe to run any time):
       ReconciliationService  ── GET /api/v3/order?origClientOrderId=… ──▶ Binance
       driven by POST /trades/{id}/reconcile  or  python -m scripts.reconcile
```

**Layering rule:** routes never contain business logic, services never contain
HTTP status codes. Routes validate and delegate; services raise domain errors;
a central set of exception handlers turns those into JSON.

---

## 3. Folder structure

```
crypto_auto_trader/
├── app/
│   ├── main.py                     FastAPI app factory, middleware, lifespan
│   ├── core/
│   │   ├── config.py               .env -> validated Settings; testnet safety gate
│   │   ├── security.py             bcrypt hashing, JWT sign/verify, secret compare
│   │   ├── exceptions.py           domain errors + centralised exception handlers
│   │   ├── logging_config.py       log format + request-id ContextVar
│   │   ├── middleware.py           assigns X-Request-ID, logs each request
│   │   └── numbers.py              canonical Decimal formatting
│   ├── db/
│   │   ├── database.py             AsyncEngine, AsyncSessionLocal, Base, get_db
│   │   └── models.py               User and Trade ORM models, TradeStatus enum
│   ├── schemas/
│   │   ├── user.py  auth.py        registration / login / token shapes
│   │   ├── webhook.py              TradingView payload + schema validation
│   │   ├── trade.py                trade response shapes
│   │   └── health.py               health response shapes
│   ├── api/
│   │   ├── router.py               collects every route module
│   │   └── routes/
│   │       ├── auth.py  trades.py  webhook.py  health.py
│   ├── services/
│   │   ├── auth_service.py         register / authenticate / issue token
│   │   ├── trading_service.py      the signal -> trade workflow
│   │   ├── execution_queue.py      in-process trade executor (asyncio.Queue)
│   │   ├── binance_service.py      async Binance testnet client + error taxonomy
│   │   └── reconciliation_service.py   recovery for uncertain outcomes
│   └── dependencies/
│       ├── auth.py                 get_current_user (JWT -> User)
│       └── services.py             get_binance_client, get_trading_service
├── alembic/                        migrations (versions/ holds the schema history)
├── scripts/
│   ├── check_setup.py              pre-flight check: config, DB, schema, Binance
│   ├── reconcile.py                batch reconciliation entry point
│   ├── benchmark_webhook.py        load test + before/after latency comparison
│   ├── bench_app.py                benchmark-only app (simulated exchange)
│   └── simulate_trades.py          send N alert signals at a running server
├── tests/                          pytest suite (148 tests, no network access)
├── .env.example                    every variable, documented
├── requirements.txt
└── pyproject.toml                  pytest + ruff configuration
```

---

## 4. Prerequisites

| Requirement | Notes |
| --- | --- |
| **Python 3.11+** | Developed and verified on 3.14. `python --version` |
| **PostgreSQL 14+** | Download: <https://www.postgresql.org/download/windows/> |
| **A Binance testnet account** | <https://testnet.binance.vision> - log in with GitHub |
| VS Code + the Python extension | Optional but assumed below |

---

## 5. Setup, step by step (Windows)

All commands are **PowerShell**, run from the project folder.

### 5.1 Create and activate the virtual environment

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If activation is blocked with *"running scripts is disabled on this system"*:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

Your prompt should now start with `(.venv)`.

### 5.2 Install dependencies

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt -r requirements-dev.txt
```

### 5.3 Install PostgreSQL

Run the EnterpriseDB installer linked above. During installation:

* keep the default port **5432**;
* set a password for the `postgres` superuser and **write it down**;
* installing pgAdmin is optional; the commands below use `psql`.

Add `psql` to this PowerShell session if it is not on your `PATH`
(adjust the version number to match your install):

```powershell
$env:Path += ";C:\Program Files\PostgreSQL\17\bin"
psql --version
```

### 5.4 Create the database and role

```powershell
psql -U postgres
```

Then, at the `postgres=#` prompt (keep the semicolons):

```sql
CREATE ROLE crypto_user WITH LOGIN PASSWORD 'choose_a_password_here';
CREATE DATABASE crypto_trader OWNER crypto_user;
GRANT ALL PRIVILEGES ON DATABASE crypto_trader TO crypto_user;
\q
```

Optionally create a second database so tests can run against real PostgreSQL:

```sql
CREATE DATABASE crypto_trader_test OWNER crypto_user;
```

### 5.5 Get Binance testnet API keys

1. Go to <https://testnet.binance.vision> and sign in with GitHub.
2. Click **Generate HMAC_SHA256 Key**.
3. Copy the **API Key** and the **Secret Key** immediately - the secret is
   shown only once.
4. The testnet funds your account with fake balances automatically.

These are testnet-only credentials with no real value, but still treat them as
secrets: they go in `.env`, which is git-ignored.

### 5.6 Configure `.env`

A `.env` already exists with generated `JWT_SECRET` and `WEBHOOK_SECRET`
values. If you are starting from a fresh clone, create it first:

```powershell
Copy-Item .env.example .env
```

Then edit `.env` and fill in the values marked `TODO`:

| Variable | What to put |
| --- | --- |
| `DATABASE_URL` | The password you chose in step 5.4 |
| `BINANCE_API_KEY` | Your testnet API key from step 5.5 |
| `BINANCE_SECRET_KEY` | Your testnet secret key from step 5.5 |

To generate fresh secrets of your own:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Leave `BINANCE_DRY_RUN=true` for now. In dry-run mode the app calls
`POST /api/v3/order/test`, which validates an order without creating one.

### 5.7 Create the database schema

```powershell
alembic upgrade head
```

This creates `users`, `trades`, every index and constraint, and the
`alembic_version` bookkeeping table.

Verify:

```powershell
psql -U crypto_user -d crypto_trader -c "\dt"
psql -U crypto_user -d crypto_trader -c "\d trades"
```

> **Why Alembic and not `create_all()`?** Migrations are versioned, reviewable
> and reversible, and the application never reshapes the schema at runtime. To
> confirm the models and the migration agree at any time, run `alembic check`.

### 5.8 Run the pre-flight check

```powershell
python -m scripts.check_setup
```

It verifies configuration, the testnet URL, PostgreSQL connectivity, that the
tables exist, that the webhook owner account exists, and that Binance is
reachable - and prints the exact fix for anything that fails.

---

## 6. Running the app

```powershell
uvicorn app.main:app --reload
```

* API: <http://127.0.0.1:8000>
* **Swagger UI: <http://127.0.0.1:8000/docs>**
* ReDoc: <http://127.0.0.1:8000/redoc>
* Health: <http://127.0.0.1:8000/health>

From VS Code you can instead press **F5** and pick *FastAPI: uvicorn --reload*
to run with the debugger attached.

Stop the server with `Ctrl+C`.

---

## 7. Using the API

### 7.1 Register and log in

```powershell
$body = @{ email = "trader@example.com"; password = "correct-horse-battery-staple" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/auth/register -ContentType application/json -Body $body
```

> Register the address in `WEBHOOK_TRADE_OWNER_EMAIL` (default
> `trader@example.com`). Webhook-created trades are attributed to that account.

```powershell
$token = (Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/auth/login -ContentType application/json -Body $body).access_token
$headers = @{ Authorization = "Bearer $token" }
```

### 7.2 How JWT authentication works

1. `POST /auth/login` verifies your password against the stored **bcrypt** hash.
2. On success the server builds a token containing `sub` (your user id), `iat`,
   `exp` and a unique `jti`, and signs it with `JWT_SECRET` using HS256.
3. You send it back on every protected request as
   `Authorization: Bearer <token>`.
4. `get_current_user` verifies the signature and expiry, loads the user, and
   injects it into the route. Anything wrong → `401`.

The token is **signed, not encrypted**: anyone can read the payload, but
nobody can alter it without `JWT_SECRET`. Never put anything secret in a JWT.

### 7.3 Call protected endpoints

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/auth/me   -Headers $headers
Invoke-RestMethod -Uri http://127.0.0.1:8000/trades    -Headers $headers
Invoke-RestMethod -Uri http://127.0.0.1:8000/trades/1  -Headers $headers
```

In Swagger UI, click **Authorize** (top right), paste just the token value, and
every protected endpoint will send it for you.

**Authorization is enforced separately from authentication.** Ownership is part
of the SQL `WHERE` clause, so another user's rows are never even loaded.
Requesting a trade that belongs to someone else returns `404`, not `403` -
a `403` would confirm the id exists.

---

## 8. The TradingView webhook

### 8.1 Payload

```json
{
  "signal_id": "tv-btc-20260908-0930",
  "symbol": "BTCUSDT",
  "action": "BUY",
  "quantity": 0.001
}
```

| Field | Rules |
| --- | --- |
| `signal_id` | 1-64 chars, `A-Z a-z 0-9 . : _ -`. **Must be unique per alert.** |
| `symbol` | 5-20 uppercase alphanumerics, and must be in `ALLOWED_SYMBOLS` |
| `action` | `BUY` or `SELL` (lowercase is accepted and normalised) |
| `quantity` | > 0, within `MIN_ORDER_QUANTITY`..`MAX_ORDER_QUANTITY`, ≤ 12 dp |

Unknown fields are rejected, so a typo like `"quantiy"` is a loud `422` rather
than a silently wrong order size.

### 8.2 Sending the secret

Preferred - an HTTP header:

```
X-Webhook-Secret: <the value of WEBHOOK_SECRET>
```

**TradingView alerts cannot send custom headers** - the alert dialog only
accepts a URL and a message body. For that integration, put the same secret in
the JSON body instead:

```json
{
  "secret": "<the value of WEBHOOK_SECRET>",
  "signal_id": "{{timenow}}-{{ticker}}",
  "symbol": "{{ticker}}",
  "action": "{{strategy.order.action}}",
  "quantity": 0.001
}
```

Both are compared in constant time. The body field is stripped from every
response and never logged.

TradingView alert setup: **Create Alert → Notifications → Webhook URL** pointing
at `https://<your-host>/webhook/tradingview`, with the JSON above as the alert
message. Locally, expose port 8000 with a tunnel (e.g. `ngrok http 8000`)
because TradingView cannot reach `127.0.0.1`.

### 8.3 Test it manually

```powershell
$secret = (Select-String -Path .env -Pattern '^WEBHOOK_SECRET=(.*)$').Matches.Groups[1].Value
$payload = @{ signal_id = "manual-001"; symbol = "BTCUSDT"; action = "BUY"; quantity = 0.001 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/webhook/tradingview `
  -ContentType application/json -Headers @{ "X-Webhook-Secret" = $secret } -Body $payload
```

Send it a second time - you will get `409 duplicate_signal`, and no second
order is placed.

### 8.4 Response codes

| Code | Meaning |
| --- | --- |
| `202` | Signal accepted and durably recorded. Read `trade.status` for the outcome. |
| `401` | Missing or wrong webhook secret. |
| `409` | This `signal_id` was already processed. |
| `422` | Schema validation or a business rule failed. |

A signal that **Binance** rejects still returns `202` with
`trade.status = REJECTED`. The webhook did its job; returning an error would
only invite TradingView to re-send an alert guaranteed to fail identically.

---

## 9. How duplicate protection works

TradingView can and does fire the same alert twice. Three independent layers
stop that from becoming two positions:

1. **A read check.** The service looks for an existing trade with that
   `signal_id` and returns `409` if it finds one. This is only a nicety - two
   concurrent requests can both pass it, because there is no lock between the
   `SELECT` and the `INSERT`.
2. **A `UNIQUE` constraint on `trades.signal_id`.** This is the real guarantee.
   Only one `INSERT` can win; the loser gets an `IntegrityError`, which is
   caught and turned into the same `409`. It holds across concurrent requests,
   multiple worker processes, and multiple servers, because it is enforced by
   PostgreSQL itself.
3. **A deterministic Binance client order id.** `client_order_id` is
   `"cat-" + sha256(signal_id)[:28]`, sent as `newClientOrderId`. Binance
   refuses a second live order with the same id - so even if layers 1 and 2
   were somehow bypassed, the exchange would still reject the duplicate.

Layer 3 has a second job: it is the handle used for recovery, below.

---

## 10. Failure and recovery

### 10.1 The trade state machine

```
                    ┌──────────┐
                    │ PENDING  │  row committed, Binance not yet called
                    └────┬─────┘
                         │  POST /api/v3/order
        ┌────────────────┼─────────────────┬──────────────────┐
        ▼                ▼                 ▼                  ▼
   ┌─────────┐     ┌───────────┐    ┌────────────┐     ┌───────────┐
   │ FILLED  │     │ REJECTED  │    │  FAILED    │     │  UNKNOWN  │
   │ NEW     │     │ Binance   │    │ never      │     │ timeout / │
   │ PARTIAL │     │ refused   │    │ connected  │     │ HTTP 5xx  │
   └─────────┘     └───────────┘    └────────────┘     └─────┬─────┘
                                                             │ reconcile
                                                             │ (query, never retry)
                                              ┌──────────────┴──────────────┐
                                              ▼                             ▼
                                    order exists → real status     -2013 → FAILED
```

Terminal: `FILLED`, `CANCELLED`, `REJECTED`, `EXPIRED`, `FAILED`.
In flight: `PENDING`, `NEW`, `PARTIALLY_FILLED`, `UNKNOWN`.

### 10.2 What happens when Binance times out

A timeout does **not** mean the order failed - the request may have executed
while the response was lost. So the application never retries. It:

1. marks the trade `UNKNOWN` and commits that fact;
2. immediately queries `GET /api/v3/order?origClientOrderId=<client_order_id>`;
3. if the order exists → records its real status;
   if Binance answers `-2013 Order does not exist` → the submission never
   landed, so `FAILED` is now a *known fact*;
   if the query itself fails → the trade stays `UNKNOWN` for the reconciliation
   job. "We could not ask" is never recorded as "there is no order".

### 10.3 What happens when Binance succeeds but the database write fails

PostgreSQL and Binance are separate systems and **no transaction spans both**.
A database rollback cannot cancel a Binance order, and this project never
pretends otherwise. Instead it makes the situation *recoverable*:

* the `PENDING` row - including the exact `client_order_id` that will be sent -
  is committed **before** Binance is called;
* if the post-Binance commit fails, a `CRITICAL` log line records the trade id,
  signal id, client order id and Binance order id;
* the row stays in a reconcilable state, so reconciliation finds the live order
  and repairs it.

### 10.4 Running reconciliation

```powershell
# One trade, via the API (owner only)
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/trades/1/reconcile -Headers $headers

# Every in-flight trade older than 60 seconds
python -m scripts.reconcile

# Wider sweep
python -m scripts.reconcile --older-than 300 --limit 50
```

Reconciliation only *reads* from Binance, so it can never create an order and
is safe to run repeatedly. In a real deployment, schedule
`python -m scripts.reconcile` every few minutes with Windows Task Scheduler or
cron - no extra infrastructure required.

---

## 11. Running the tests

```powershell
pytest                       # everything
pytest -v                    # one line per test
pytest tests/test_trading.py # one file
pytest -k duplicate          # tests matching a name
```

**No test ever contacts Binance.** The Binance client is replaced through
FastAPI's `dependency_overrides` with `tests/fakes.py::FakeBinanceClient`,
which can simulate success, rejection, rate limiting, connection failure and
timeout. `BinanceClient` itself is tested separately against scripted responses
using `httpx2.MockTransport`, so the signing and error-mapping code is really
executed without a network.

By default the suite runs on a throwaway SQLite database so it works on a fresh
clone. To run the identical suite against **real PostgreSQL** - which also makes
the concurrency test a genuine race:

```powershell
$env:TEST_DATABASE_URL = "postgresql+asyncpg://crypto_user:your_password@localhost:5432/crypto_trader_test"
pytest
Remove-Item Env:\TEST_DATABASE_URL
```

Lint:

```powershell
ruff check .
```

---

## 12. Manual Binance testnet integration test

The automated suite never touches the network. This is how to verify the real
integration by hand.

**Step 1 - confirm connectivity and credentials.**

```powershell
python -m scripts.check_setup
```

**Step 2 - dry run (creates no order).** With `BINANCE_DRY_RUN=true` in `.env`,
send a webhook as in §8.3. The app calls `POST /api/v3/order/test`. A trade in
status `FILLED` with `binance_order_id: null` means Binance accepted the order
as valid without creating it.

**Step 3 - place a real testnet order.** Set `BINANCE_DRY_RUN=false`, restart
the server, and send a webhook with a **new** `signal_id`. You should get a
trade with a real `binance_order_id` and `status: FILLED`.

Verify it on the exchange side at <https://testnet.binance.vision> under
*Open Orders* / *Order History* - look for a client order id starting `cat-`.

**Step 4 - see a real rejection.** Send a quantity far below Binance's minimum
(e.g. `0.00001` BTC). Binance replies with a `LOT_SIZE`/`NOTIONAL` filter error
and the trade is recorded `REJECTED` with Binance's own message.

Remember: this is the testnet. The balances are fake and the orders are not
real. Never point `BINANCE_BASE_URL` at production.

---

## 12b. Benchmark and simulation

Both scripts run entirely against a **simulated exchange** or the testnet
dry-run path. No real order is possible.

```powershell
# 1. Load-test a server you started yourself
uvicorn app.main:app                                        # in one terminal
python scripts/benchmark_webhook.py --concurrency 50 --requests 100

# 2. Before/after comparison - starts the app in BOTH execution modes itself
python scripts/benchmark_webhook.py --compare
python scripts/benchmark_webhook.py --compare --concurrency-levels 50 75 100

# 3. Send 100+ alert signals at a running server
python scripts/simulate_trades.py --count 120
python scripts/simulate_trades.py --count 120 --duplicate-every 20   # + idempotency
```

`--compare` starts the app twice - once with `WEBHOOK_ASYNC_EXECUTION=false`
(the exchange call happens inside the request) and once with it on (the request
returns as soon as the trade is durable) - and prints p50/p95 for each. Both
use `scripts/bench_app.py`, which swaps the Binance client for a simulator with
a fixed latency, so the comparison measures this backend rather than Binance's
network.

**What is measured is webhook acknowledgement latency** - the time to accept a
signal and commit it - not the time for the order to reach Binance. In queued
mode the order is placed after the response, and the trade is `PENDING` until
it completes. `GET /trades` shows the final states.

---

## 13. Deploying to Render

```
GitHub repo --> Render Web Service (FastAPI, one instance)
                        |
                        +--> Render PostgreSQL
                        |
                        +--> Binance SPOT TESTNET
```

Everything below is described by [`render.yaml`](render.yaml) in the repository
root, so Render provisions both resources from the file rather than from
settings typed into a dashboard.

### 13.1 Push to GitHub

```powershell
git remote add origin https://github.com/<your-username>/crypto-auto-trader.git
git branch -M main
git push -u origin main
```

`.env` is git-ignored and must stay that way. Check before pushing:

```powershell
git status                       # .env must not be listed
git ls-files | Select-String env # only .env.example may appear
```

### 13.2 Create the Blueprint

In the Render dashboard: **New > Blueprint**, connect the repository, apply.
Render creates the web service and the PostgreSQL database, and prompts for the
three values that are deliberately not in the file:

| Prompted for | Where it comes from |
| --- | --- |
| `WEBHOOK_SECRET` | You generate it: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `BINANCE_API_KEY` | <https://testnet.binance.vision> - log in with GitHub, **Generate HMAC_SHA256 Key** |
| `BINANCE_SECRET_KEY` | Same page. Shown once; copy it immediately. |

`DATABASE_URL` is injected by Render from the database. `JWT_SECRET` is
generated by Render. Nothing secret is ever committed.

### 13.3 Build, start and health check

| Setting | Value |
| --- | --- |
| Build command | `pip install -r requirements.txt` |
| Start command | `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Health check path | `/health` |
| Python version | `3.14` (pinned by `.python-version`) |

Migrations run before the server starts, so a schema that does not match the
code fails the deploy instead of serving traffic. There is no `--reload` (a
development-only file watcher) and no `--workers` flag - see 13.6.

### 13.4 Complete the setup

The webhook attributes its trades to `WEBHOOK_TRADE_OWNER_EMAIL`, so that
account has to exist. Once, after the first deploy, at
`https://<service>.onrender.com/docs`:

```
POST /auth/register   { "email": "trader@example.com", "password": "<your choice>" }
```

Then:

| URL | What it is |
| --- | --- |
| `https://<service>.onrender.com/health` | Liveness, no dependencies |
| `https://<service>.onrender.com/health/ready` | Readiness; checks PostgreSQL, reports `binance_host` |
| `https://<service>.onrender.com/docs` | Swagger UI - the demo surface |

`BINANCE_DRY_RUN` starts at `true`: Binance validates each order and creates
nothing, and the trade is recorded `FILLED` with
`binance_status=DRY_RUN_VALIDATED`. Set it to `false` in the Render dashboard
when you want real testnet fills.

### 13.5 Free-plan limits worth knowing before a live demo

* The web service **spins down after 15 minutes** without traffic, and the next
  request takes about a minute to wake it. Open `/health` a few minutes before
  the interview so the service is warm.
* A **free PostgreSQL database expires 30 days after it is created**, and only
  one can be active per workspace. Note the date; if the demo matters after
  that, upgrade the database to a paid compute plan.
* 750 free instance hours per workspace per month, and 1 GB of database storage.

### 13.6 Why exactly one instance

The trade executor is an `asyncio.Queue` living inside this process. It is not a
distributed queue, and it is not durable by itself. Two instances would mean two
separate queues, not one shared queue, so the service runs single-instance with
a single uvicorn worker.

Correctness does not depend on the queue surviving. Every trade is committed to
PostgreSQL as `PENDING` *before* it is enqueued, and `submitted_at IS NULL`
separates "never sent" from "sent, outcome unknown". A hard kill - which the
free plan's spin-down does routinely - loses the queue's contents but no trades:
`resume_pending()` re-queues everything unsent at the next startup, and
reconciliation handles the rest. Losing the queue costs latency, not
correctness.

Making this genuinely horizontal would mean replacing `asyncio.Queue` with a
shared durable queue - a real change with real operating cost, and not one this
demo needs.

---

## 14. Interview demo

At `https://<service>.onrender.com/docs`, in order:

1. **`POST /auth/register`** - create an account (or reuse the webhook owner).
2. **`POST /auth/login`** - returns `access_token`.
3. **Authorize** (top right of Swagger) - paste the token. Every protected
   endpoint now carries it.
4. **`GET /auth/me`** - proves the JWT works. Without it the same call is `401`.
5. **`POST /webhook/tradingview`** - set the `X-Webhook-Secret` header and send:

   ```json
   { "signal_id": "demo-001", "symbol": "BTCUSDT", "action": "BUY", "quantity": "0.001" }
   ```

   Response: **`202 Accepted`**, with the trade already `PENDING`.
6. **`GET /trades`** - the trade is there, with its current status.
7. **Send the exact same body again** - **`409 Conflict`**, `duplicate_signal`.

What to say about it:

> The webhook validates the payload and durably persists the trade as `PENDING`
> in PostgreSQL, and only then returns `202 Accepted`. The Binance call happens
> afterwards, in a worker, outside the request path - so the acknowledgement
> latency is our database write, not the exchange round trip. The two are
> separate numbers and the exchange round trip has not gone anywhere; it just no
> longer sits between TradingView and its `202`.
>
> The duplicate is refused by a `UNIQUE` constraint on `signal_id`, not by an
> application-level check - so two concurrent copies of the same alert cannot
> both get through.

Worth showing if asked: `GET /health/ready` reports `binance_host`, which is
always a testnet host - the application refuses to start otherwise.

---

## 15. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `REFUSING TO START: ... is not an allowed Binance testnet host` | `BINANCE_BASE_URL` is not a testnet URL. Set `https://testnet.binance.vision`. This guard is intentional. |
| `ValidationError ... JWT_SECRET` at startup | Missing or under 32 characters. Generate one: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `... still contains a placeholder value` | You copied `.env.example` but did not replace `JWT_SECRET` / `WEBHOOK_SECRET`. |
| `Database is NOT reachable at startup` / `/health/ready` says `degraded` | PostgreSQL is not running, or `DATABASE_URL` is wrong. Start the service: `Get-Service postgresql*` then `Start-Service postgresql-x64-17`. |
| `password authentication failed for user "crypto_user"` | Wrong password in `DATABASE_URL`. If it contains `@ : / #`, percent-encode it (`@` → `%40`). |
| `No module named 'psycopg2'` / `InvalidRequestError: asyncio extension requires an async driver` | `DATABASE_URL` is auto-rewritten to `postgresql+asyncpg://`; make sure you did not pin a different driver by hand. |
| `relation "users" does not exist` | Migrations have not run. `alembic upgrade head` |
| `500 configuration_error` from the webhook | The `WEBHOOK_TRADE_OWNER_EMAIL` account is not registered. `POST /auth/register` with that email. |
| Webhook returns `401` | Wrong or missing secret. It must exactly match `WEBHOOK_SECRET`, in the `X-Webhook-Secret` header or the body's `secret` field. |
| Webhook returns `409` | Working as designed - that `signal_id` was already processed. Use a new one. |
| Trade is `REJECTED` with `-2014 API-key format invalid` | `BINANCE_API_KEY` is still the placeholder. Paste your real testnet key. |
| Trade is `REJECTED` with `-1021 Timestamp for this request...` | Your PC clock has drifted. Sync it: `w32tm /resync` (run PowerShell as administrator). |
| Trade is `REJECTED` with `Filter failure: LOT_SIZE` / `NOTIONAL` | The quantity is below Binance's minimum for that symbol. Increase it. |
| Trade is stuck in `UNKNOWN` | A timeout that could not be resolved yet. Run `python -m scripts.reconcile`. |
| Trades stay `PENDING` | The executor has not drained them yet (normal for a moment), or the process was restarted. Startup re-queues unsubmitted trades automatically; `GET /trades` shows the current state. |
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser` |
| `psql: command not found` | Add PostgreSQL to `PATH`: `$env:Path += ";C:\Program Files\PostgreSQL\17\bin"` |
| VS Code does not find the interpreter | **Ctrl+Shift+P → Python: Select Interpreter →** `.\.venv\Scripts\python.exe` |

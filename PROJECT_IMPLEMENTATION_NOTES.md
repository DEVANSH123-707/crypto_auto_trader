# Implementation Notes — a learning and interview guide

This is not API documentation. It is the explanation of *why every piece exists
and how the pieces fit together*, written so you can defend any part of this
codebase in an interview.

Read it top to bottom once. After that, §17 (the complete request flow) and
§20 (likely interview questions) are the parts worth re-reading.

---

## Contents

1. [The stack, and why each piece is here](#1-the-stack-and-why-each-piece-is-here)
2. [What every folder does](#2-what-every-folder-does)
3. [What every major file does](#3-what-every-major-file-does)
4. [How FastAPI, SQLAlchemy and PostgreSQL connect](#4-how-fastapi-sqlalchemy-and-postgresql-connect)
5. [Where PostgreSQL actually runs, and what durability means](#5-where-postgresql-actually-runs-and-what-durability-means)
6. [Sessions, `Depends()` and `get_db`](#6-sessions-depends-and-get_db)
7. [How JWT authentication works](#7-how-jwt-authentication-works)
8. [Authorization: a valid token is not enough](#8-authorization-a-valid-token-is-not-enough)
9. [Pydantic, and the two kinds of validation](#9-pydantic-and-the-two-kinds-of-validation)
10. [Where business logic lives](#10-where-business-logic-lives)
11. [How TradingView reaches the webhook](#11-how-tradingview-reaches-the-webhook)
12. [How duplicate protection works](#12-how-duplicate-protection-works)
13. [How Binance testnet is called](#13-how-binance-testnet-is-called)
14. [The three hard failure cases](#14-the-three-hard-failure-cases)
15. [The trade state machine and reconciliation](#15-the-trade-state-machine-and-reconciliation)
16. [Error handling and logging](#16-error-handling-and-logging)
17. [The complete request flow, end to end](#17-the-complete-request-flow-end-to-end)
18. [How the tests work](#18-how-the-tests-work)
19. [Design decisions and their alternatives](#19-design-decisions-and-their-alternatives)
20. [Questions you should be able to answer](#20-questions-you-should-be-able-to-answer)

---

## 1. The stack, and why each piece is here

| Technology | What it actually does here | Why not something else |
| --- | --- | --- |
| **FastAPI** | Routing, dependency injection, request/response validation, auto-generated OpenAPI docs | Flask would need extra libraries for validation and docs; Django is a much larger framework than a JSON API needs |
| **Uvicorn** | The ASGI server. FastAPI is a framework — it does not open a socket. Uvicorn does, and calls into the app | Gunicorn alone is WSGI; in production you would run Gunicorn *with* uvicorn workers |
| **PostgreSQL** | Durable storage, and the source of the uniqueness guarantee that makes idempotency real | SQLite has no real concurrency story; a document store would not give a transactional unique constraint |
| **SQLAlchemy** | Maps Python classes to tables, manages the connection pool, builds SQL, tracks changes. Used through `create_async_engine` / `AsyncSession`, so every query is awaited | Raw psycopg means hand-writing SQL and mapping rows; SQLAlchemy Core alone loses the identity map and relationships |
| **asyncpg** | The actual PostgreSQL driver — speaks the wire protocol over TCP, asynchronously | psycopg2 is synchronous and cannot back an async engine; psycopg 3's async mode refuses to run on Windows' default ProactorEventLoop, which is the loop uvicorn gets |
| **Alembic** | Versioned, reversible schema migrations | `create_all()` cannot evolve a schema that already holds data |
| **Pydantic v2** | Parses and validates untrusted JSON into typed Python objects; serialises responses | Hand-written `if "x" not in body` checks are error-prone and produce inconsistent errors |
| **pydantic-settings** | Loads `.env` into a validated `Settings` object at startup | Bare `os.getenv()` returns `None` silently and fails much later, inside a request |
| **PyJWT** | Signs and verifies the access token | Sessions would need server-side storage; this API is stateless |
| **bcrypt** | Hashes passwords — deliberately slow, salted per password | A plain SHA-256 is far too fast, which makes brute-forcing cheap |
| **httpx2** | The HTTP client for Binance. Also backs Starlette's `TestClient` | `requests` is unmaintained-ish and has no `MockTransport` equivalent as clean for tests |
| **pytest** | The test runner: fixtures, parametrisation, plain `assert` | `unittest` needs far more boilerplate for the same coverage |

**What is deliberately absent:** Redis, Celery, Kafka, RabbitMQ, Kubernetes,
microservices. Every one of them would add an operational component with no
problem to solve here. Reconciliation, the one background job, is a plain
script you can run from Task Scheduler. *Being able to explain what you left
out, and why, is worth as much in an interview as what you built.*

---

## 2. What every folder does

```
app/core/           Cross-cutting concerns that depend on nothing in the app:
                    configuration, hashing, tokens, logging, error types.
                    Everything imports from here; it imports from nobody.

app/db/             The database layer. `database.py` owns the engine and
                    session; `models.py` describes the tables.

app/schemas/        Pydantic models = the shape of the API. Requests in,
                    responses out. Never touches the database.

app/api/routes/     HTTP endpoints. Thin on purpose: parse, authenticate,
                    delegate to a service, shape the response.

app/services/       All the business logic. Pure Python + SQLAlchemy — no
                    FastAPI imports, no HTTP status codes.

app/dependencies/   Reusable `Depends()` providers: the current user, the
                    Binance client, the trading service.

alembic/            Schema migration history.

scripts/            Operational entry points that run outside a web request.

tests/              The pytest suite.
```

The dependency arrows only ever point **one way**:

```
routes ──▶ services ──▶ db / core
   │           │
   └──▶ schemas ┘
```

Nothing in `services/` imports from `routes/`. That is why the trading logic
can be tested, or driven from a script, with no web server anywhere.

---

## 3. What every major file does

### `app/main.py`
Builds the `FastAPI` object. Registers the middleware, the exception handlers
and the routers, and defines the `lifespan` (startup/shutdown) hooks. When you
run `uvicorn app.main:app`, uvicorn imports this module and looks for `app`.

Importing it also imports `app.core.config`, which is what validates the
environment — so a bad `.env` stops the process at startup, not mid-trade.

### `app/core/config.py`
Turns environment variables into a typed, validated `Settings` object.
Everything is read here and nowhere else — no `os.getenv()` is scattered
through the codebase.

It also holds the **testnet safety gate**: `ALLOWED_BINANCE_HOSTS` and a field
validator that refuses to construct `Settings` at all if `BINANCE_BASE_URL` is
not a Binance testnet host, with an extra-loud message for known production
hosts. There is deliberately no override flag.

### `app/core/security.py`
Pure cryptography, no database and no FastAPI:
* `hash_password` / `verify_password` — bcrypt, with an explicit guard against
  bcrypt's 72-byte input limit (past it, bcrypt silently truncates, which would
  let two different long passwords authenticate each other);
* `create_access_token` / `decode_access_token` — PyJWT;
* `secrets_match` — constant-time comparison for the webhook secret.

### `app/core/exceptions.py`
Defines the domain errors (`DuplicateSignalError`, `BusinessValidationError`,
`AuthenticationError`, …) and the handlers that convert them into one JSON
envelope. This is what keeps status codes out of the service layer.

### `app/core/logging_config.py` and `app/core/middleware.py`
Together they give every log line a correlation id. The middleware mints (or
reuses) a request id and stores it in a `ContextVar`; a logging `Filter`
stamps it onto every record. One webhook call is then traceable across the
route, the service, the Binance call and the status update by grepping one id.

### `app/db/database.py`
Creates the `AsyncEngine` (which owns the connection pool), the
`AsyncSessionLocal` factory, the declarative `Base`, and the `get_db`
dependency.

### `app/db/models.py`
`User` and `Trade` as SQLAlchemy 2.0 typed models, plus the `TradeStatus` and
`OrderSide` enums, the unique constraints, the foreign key, and the indexes.

### `app/schemas/*.py`
The API contract. `UserCreate` has a password; `UserRead` does not even have
the *field*, so a hash cannot leak by accident.

### `app/services/auth_service.py`
Register, authenticate, issue a token. Notably: unknown-email and wrong-password
produce the **identical** error, so login cannot be used to discover which
addresses have accounts.

### `app/services/trading_service.py`
The workflow: business validation → duplicate check → commit `PENDING` → call
Binance → map the outcome → commit. Also holds the queries behind `GET /trades`,
with ownership baked into the `WHERE` clause.

### `app/services/binance_service.py`
The only file that knows Binance's HTTP API exists. It signs requests, and —
most importantly — classifies every failure into three categories that must be
handled differently (rejected / unavailable / **uncertain**).

### `app/services/reconciliation_service.py`
The recovery path. Given a trade, asks Binance what really happened and records
the truth. Read-only against the exchange, so it can never create an order.

### `app/dependencies/auth.py`
`get_current_user`: bearer token → verified claims → `User`. Adding
`current_user: CurrentUser` to a route is what makes it protected.

### `app/dependencies/services.py`
Provides the shared `BinanceClient` and a per-request `TradingService`. This is
the seam the tests use to swap in a fake exchange.

---

## 4. How FastAPI, SQLAlchemy and PostgreSQL connect

Four layers. Be able to draw this:

```
  Your route function
        │  db.execute(select(Trade)...)
        ▼
  SQLAlchemy Session        ← unit of work: identity map, change tracking, commit
        │  borrows a connection for the transaction
        ▼
  SQLAlchemy Engine         ← owns the connection pool, compiles SQL for the dialect
        │  DBAPI calls
        ▼
  asyncpg                   ← speaks the PostgreSQL wire protocol, asynchronously
        │  TCP, usually localhost:5432
        ▼
  PostgreSQL server process ← a completely separate program
```

**FastAPI → SQLAlchemy** happens through dependency injection. The route
declares `db: AsyncSession = Depends(get_db)`; FastAPI calls `get_db`, which
creates an `AsyncSession` from `AsyncSessionLocal`, and hands it in.

**SQLAlchemy → PostgreSQL** happens through the `Engine`, created once per
process from `DATABASE_URL`:

```
postgresql+asyncpg://crypto_user:password@localhost:5432/crypto_trader
└────┬────┘ └──┬──┘  └────┬────┘ └───┬──┘ └───┬───┘ └┬─┘ └──────┬─────┘
 dialect     driver      user     password   host   port    database
```

The dialect tells SQLAlchemy which SQL flavour to emit; the driver tells it
which Python DBAPI module to import. This is why the config auto-rewrites a
bare `postgresql://` to `postgresql+asyncpg://` — the bare form defaults to
psycopg**2**, which is synchronous and cannot back an async engine at all.

**Creating the engine does not connect.** The pool opens connections lazily, on
first use. That is why importing `app.db.database` works even when PostgreSQL
is down — and why the app can start and report itself "not ready" rather than
crashing.

Two production details in the engine configuration worth knowing:
* `pool_pre_ping=True` — sends a cheap liveness check before handing out a
  pooled connection, so a connection dropped by a restart or an idle timeout is
  replaced transparently instead of surfacing as a random mid-request error.
* `connect_args={"connect_timeout": 5}` — without it, a firewall that drops
  packets rather than refusing the connection (the Windows default) makes
  startup hang forever instead of reporting a clear error.

---

## 5. Where PostgreSQL actually runs, and what durability means

PostgreSQL is **a separate operating-system process**, not part of your Python
program. On Windows it is installed as a service (`postgresql-x64-17`) that
starts with the machine and listens on TCP port 5432. Your app is just a client
connecting to it — the same way pgAdmin or `psql` is.

The database *files* live on disk under something like
`C:\Program Files\PostgreSQL\17\data`. Never edit them by hand.

**How durability actually works.** When you call `db.commit()`:

1. SQLAlchemy flushes pending changes as `INSERT`/`UPDATE` statements.
2. It sends `COMMIT` over the connection.
3. PostgreSQL writes the change to the **write-ahead log (WAL)** and `fsync`s
   it to physical disk.
4. Only then does it acknowledge the commit.

That last point is the whole guarantee: once `commit()` returns, the data
survives the process being killed, the machine losing power, or the disk cache
being lost. If the server crashes before the data pages are written, PostgreSQL
replays the WAL on restart.

**This is exactly why the `PENDING` trade is committed before Binance is
called.** "Committed" means "on disk, survives a crash". If the process dies
one millisecond after sending the order, the row — and the `client_order_id`
needed to find that order again — is still there.

Also: `db.rollback()` undoes everything since the transaction began. It cannot
undo an HTTP request that has already left the machine. Which brings us to §14.

---

## 6. Sessions, `Depends()` and `get_db`

### What a Session is

A `Session` is SQLAlchemy's **unit of work**. It:
* holds a database transaction open,
* keeps an **identity map** — ask for `User` id 5 twice, get the same object,
* **tracks changes** to loaded objects, so `trade.status = FILLED` followed by
  `commit()` produces an `UPDATE` without you writing any SQL,
* flushes pending SQL at the right moments.

A Session is **not thread-safe** and is meant to be short-lived. One per
request is the standard scope, which is exactly what `get_db` provides.

### What `Depends()` does

`Depends()` is FastAPI's dependency injection. You declare *what you need*, not
*how to build it*:

```python
def list_trades(current_user: CurrentUser, service: TradingServiceDep): ...
```

Before calling the handler, FastAPI resolves the whole dependency graph:

```
list_trades
 ├── get_current_user
 │    ├── bearer_scheme  (reads the Authorization header)
 │    └── get_db         ← resolved once, cached, shared
 └── get_trading_service
      ├── get_db         ← the SAME session object, not a second one
      └── get_binance_client
```

Three things this buys you, all of which are worth saying out loud:

1. **Caching within a request.** `get_db` appears twice in that graph but runs
   once, so the auth lookup and the trade query share one transaction.
2. **Guaranteed cleanup**, via the `yield` protocol below.
3. **Substitutability.** `app.dependency_overrides[get_binance_client] = fake`
   swaps the exchange for the entire application — which is how the tests avoid
   ever touching the network without patching a single internal.

### How `get_db` works

```python
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as db:   # closes on the way out
        try:
            yield db                        # ← the request runs here
        except Exception:
            await db.rollback()             # never leave a half-finished transaction
            raise
```

Everything before `yield` runs when the request starts. The `AsyncSession` is
injected. Everything after `yield` runs once the response has been produced —
**even if the handler raised**.

Note what `get_db` deliberately does *not* do: it does not commit. Transaction
boundaries belong to the service layer, because the trading workflow needs
*two* separate commits at precisely chosen points (§14). A `get_db` that
auto-committed at the end of the request would make that impossible.

---

## 7. How JWT authentication works

### The token

A JWT is three base64url segments joined by dots: `header.payload.signature`.

```
header    {"alg": "HS256", "typ": "JWT"}
payload   {"sub": "1", "type": "access",
           "iat": 1757000000, "exp": 1757003600,
           "jti": "9f2c..."}
signature HMAC_SHA256(base64(header) + "." + base64(payload), JWT_SECRET)
```

The claims used here:
* `sub` — the subject: the user id, as a string (RFC 7519 requires a string);
* `exp` — expiry; PyJWT rejects an expired token automatically;
* `iat` — issued at;
* `jti` — a unique token id, the hook a future revocation list would use;
* `type` — our own claim, so a different token kind (e.g. a refresh token)
  could never be accepted as an access token.

### Signed, not encrypted

Anyone can base64-decode a JWT and read the payload. What they *cannot* do is
change it: any edit invalidates the HMAC, and producing a valid one requires
`JWT_SECRET`. **Never put anything secret in a JWT.**

HS256 is symmetric — the same secret signs and verifies. That is correct here
because one service does both. If several separate services had to *verify*
tokens issued by one *issuer*, you would switch to RS256 so verifiers only
need the public key.

### The flow

```
POST /auth/login {email, password}
   │
   ├─ look the user up by email
   ├─ bcrypt.checkpw(password, stored_hash)     ← constant time
   ├─ build claims, sign with JWT_SECRET (HS256)
   └─▶ {"access_token": "eyJ…", "token_type": "bearer", "expires_in": 3600}

GET /trades   Authorization: Bearer eyJ…
   │
   └─ get_current_user
        ├─ read the Authorization header  (HTTPBearer)
        ├─ verify the signature with JWT_SECRET
        ├─ verify exp, require sub, check type == "access"
        ├─ load the User by sub
        ├─ confirm the account still exists and is active
        └─▶ the route receives a real User object
```

Every failure — missing header, wrong scheme, bad signature, expired, deleted
user — returns the same `401`, so a caller cannot probe *why* it failed.

### Why bcrypt for passwords

bcrypt is **slow on purpose** and **salted per password**:
* the salt (generated per password and stored inside the hash string) means two
  users with the same password get different hashes, which defeats rainbow
  tables;
* the cost factor (12 by default here) means a single verification takes
  ~200–300 ms, which makes offline brute-forcing of a stolen database
  enormously expensive.

That cost is also why this project's test suite takes about a minute — real
hashing, not a stub.

---

## 8. Authorization: a valid token is not enough

**Authentication** = who are you. **Authorization** = are you allowed to touch
*this*. A token proves the first, never the second.

The rule here: **ownership lives in the SQL `WHERE` clause.**

```python
select(Trade).where(Trade.id == trade_id, Trade.user_id == user.id)
```

Not "load the trade, then compare `trade.user_id`". Filtering in SQL means
another user's row is never even loaded into memory, so there is no version of
this code where a later refactor accidentally returns it.

**Why 404 and not 403 for someone else's trade.** A `403` says "this exists,
but it is not yours" — which is itself an information leak, letting an attacker
enumerate valid ids. A `404` reveals nothing. `403` is reserved for cases where
the caller already knows the resource exists.

---

## 9. Pydantic, and the two kinds of validation

### What Pydantic actually does

A Pydantic model is a **parser**, not just a checker. Given untrusted JSON it
produces a typed Python object or raises. FastAPI wires this in automatically:
declare `payload: TradingViewWebhookPayload` and FastAPI will read the body,
parse it, validate it, and return `422` with a field-by-field explanation if it
fails — before your function is ever called.

Some specifics in this project worth pointing at:

* `extra="forbid"` on the webhook payload — an unknown key is an error.
  A typo like `"quantiy": 5` becomes a loud `422` instead of a silently dropped
  field followed by a wrong-sized order.
* `quantity: Decimal`, never `float`. `0.1 + 0.2 != 0.3` in binary floating
  point; an order quantity must round-trip exactly. The column is
  `NUMERIC(28, 12)` for the same reason.
* A `field_validator` on `action` that uppercases before enum matching, because
  TradingView strategy alerts commonly emit lowercase `"buy"`.
* `exclude=True` on the `secret` field, so it can never appear in a response.
* On the way out, `from_attributes=True` lets FastAPI build a response straight
  from an ORM object — while the response model's field list acts as an
  allow-list. `UserRead` has no `password_hash` field, so a hash cannot leak
  even if someone returns the `User` object directly.

### The two layers, and why they are separate

**Layer 1 — schema validation (Pydantic, in `app/schemas/webhook.py`).**
*Is this JSON structurally a trading signal?* Required fields present, correct
types, `BUY`/`SELL` only, quantity greater than zero, symbol shaped like a
Binance pair. Always true, everywhere, for every deployment.

**Layer 2 — business validation (the service, in `TradingService.validate_signal`).**
*Should we act on this signal, here, now?* Is `BTCUSDT` in this deployment's
`ALLOWED_SYMBOLS`? Is the quantity within this deployment's configured cap?

The dividing line: **layer 1 depends only on the payload; layer 2 depends on
configuration and application state.** `DOGEUSDT` is a perfectly valid Binance
symbol — Pydantic has no business rejecting it. Whether *this* deployment is
allowed to trade it is a policy question, and policy belongs in the service.

Both return `422`, but with different error codes (`validation_failed` vs
`business_validation_failed`) so a caller can tell them apart.

---

## 10. Where business logic lives

**In `app/services/`. Never in a route.**

Look at how short the webhook route is: authenticate the caller, log the
signal, call `service.process_signal(payload)`, shape the response. That is the
entire function.

Three concrete reasons this matters:

1. **Testability.** `TradingService` can be exercised with a `Session` and a
   fake client — no HTTP, no server.
2. **Reuse.** `scripts/reconcile.py` drives the same reconciliation logic from
   the command line. If it lived in a route it would have to be re-implemented.
3. **Clear boundaries.** Services raise domain errors (`DuplicateSignalError`);
   they contain no status codes at all. If you ever put this behind gRPC or a
   CLI, the business layer would not change.

---

## 11. How TradingView reaches the webhook

```
TradingView alert fires
   │  HTTPS POST, JSON body
   ▼
Your public URL  (ngrok tunnel locally, a real host in production)
   │
   ▼
POST /webhook/tradingview
   │
   ├─ 1. authenticate: X-Webhook-Secret header, or "secret" in the body
   ├─ 2. Pydantic parses and schema-validates the body
   └─ 3. TradingService takes over
```

### Why a shared secret rather than a JWT

The caller is a **machine**, not a person. There is nobody to log in, no
browser to store a token, no way to refresh one. A pre-shared secret compared
in constant time is the right tool: simple, stateless, and rotatable by editing
`.env` and restarting.

Constant-time comparison (`hmac.compare_digest`) matters because a normal `==`
on strings returns as soon as it finds a differing byte. That timing difference
is measurable over many requests and lets an attacker recover the secret one
byte at a time.

### The header-versus-body wrinkle — a good thing to raise unprompted

The clean design is a header. **TradingView cannot send custom headers** — its
alert dialog only accepts a URL and a message body. So the endpoint accepts the
secret either way, checks the header first, and documents the body form as the
TradingView-compatible fallback.

Be honest about the trade-off if asked: a secret in the body is more likely to
end up in a log somewhere, which is exactly why the field is `exclude=True` and
never logged. In production you would also require HTTPS (so the body is
encrypted in transit) and consider IP-allowlisting TradingView's published
ranges.

### Why the webhook does not carry a user

The webhook is authenticated as an *integration*, not as a person, so there is
no user in the request. Trades it creates are attributed to the account named
by `WEBHOOK_TRADE_OWNER_EMAIL`. That keeps `trades.user_id` `NOT NULL` and lets
the ownership checks on `GET /trades` work uniformly.

The multi-tenant version — every user gets their own webhook token, and the
token identifies the account — is a small extension: add a `webhook_token`
column, look the user up by it, and drop the config setting. It was left out
deliberately as unnecessary complexity for a single-operator system.

---

## 12. How duplicate protection works

**The problem.** TradingView can fire the same alert twice — a retry, a network
blip, a user clicking twice. Two identical HTTP requests must not become two
positions.

**The wrong solution**, and the one to name before you are asked:

```python
if db.query(Trade).filter_by(signal_id=sig).first():   # ← check
    raise Duplicate
db.add(Trade(signal_id=sig))                           # ← then act
db.commit()
```

This is a classic **time-of-check to time-of-use** race. Two concurrent
requests can both run the `SELECT` before either runs the `INSERT`. Both see
nothing, both insert, and two orders go out. Nothing in the Python code can fix
this, because there is no lock between the two statements.

**The right solution: let the database decide.**

```python
signal_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
```

`UNIQUE` is enforced by PostgreSQL inside the transaction. Exactly one `INSERT`
can succeed. The loser gets an `IntegrityError`, which the service catches and
converts into the same `409 Conflict`:

```python
try:
    self.db.commit()
except IntegrityError:
    self.db.rollback()
    winner = <look up the trade that won>
    raise DuplicateSignalError(existing_trade_id=winner.id)
```

The pre-check is still there, but understand its role: it is a **nicety** that
produces a clean error in the ordinary case. The `UNIQUE` constraint is the
**guarantee**, and it holds across concurrent requests, multiple uvicorn
workers, and multiple servers, because it lives in the one place all of them
share.

**The third layer.** `client_order_id` is derived deterministically:

```python
"cat-" + sha256(signal_id).hexdigest()[:28]      # 32 chars
```

and sent to Binance as `newClientOrderId`. Binance itself rejects a second live
order with the same id — a duplicate guard that does not depend on our database
at all. A hash rather than the raw signal id because Binance caps that field at
36 characters from a restricted alphabet, while `signal_id` is caller-supplied.

Determinism is not just for deduplication: it is what makes recovery possible
(§14). The same signal always maps to the same client order id, so after a
timeout we can ask Binance about *that specific id* instead of guessing.

---

## 13. How Binance testnet is called

### The endpoints used

| Purpose | Call |
| --- | --- |
| Place an order | `POST /api/v3/order` (SIGNED) |
| Validate without creating | `POST /api/v3/order/test` (SIGNED) — used when `BINANCE_DRY_RUN=true` |
| Look an order up | `GET /api/v3/order?origClientOrderId=…` (SIGNED) — reconciliation |
| Connectivity / clock | `GET /api/v3/ping`, `GET /api/v3/time` (public) |

Base URL: `https://testnet.binance.vision`.

### How signing works

Every SIGNED request needs two things:

1. the header `X-MBX-APIKEY: <api key>`;
2. a `signature` parameter — the hex HMAC-SHA256 of the **entire query string**,
   keyed by the API secret.

```python
params = {..., "timestamp": now_ms, "recvWindow": 5000}
query  = urlencode(params)
params["signature"] = hmac.new(secret, query.encode(), sha256).hexdigest()
```

`timestamp` plus `recvWindow` is a replay defence: Binance rejects a request
whose timestamp is outside the window. This is also why a drifting PC clock
produces `-1021 Timestamp for this request is outside of the recvWindow` — a
very common first-run error, fixed with `w32tm /resync`.

The secret is used to *compute* the signature and is **never transmitted**.
There is a test asserting exactly that.

### Why the Binance service is separate from the trading service

`binance_service.py` is the only file that knows Binance's HTTP API exists. It
returns a normalised `BinanceOrderResult` or raises a typed error. The trading
service works entirely in those terms. Swap in another exchange and only one
file changes.

### The safety gate

Two independent checks, neither of which can be disabled by configuration:

1. `Settings` refuses to construct if `BINANCE_BASE_URL` is not in
   `ALLOWED_BINANCE_HOSTS` — with a louder message for known production hosts.
   The app cannot start.
2. `BinanceClient._assert_testnet()` runs again in the constructor **and** at
   the top of `place_market_order`, right next to the code that sends the order.

Reaching real-money Binance therefore requires editing a constant in the source
— a visible, reviewable code change, not an `.env` typo.

---

## 14. The three hard failure cases

This section is the heart of the project. If you can explain these three, you
can explain the whole design.

### The setup: two systems, no shared transaction

PostgreSQL and Binance are independent. There is **no transaction that spans
both**. `db.rollback()` can undo a database write; it can do nothing whatsoever
about an HTTP request that has already left the machine.

The chosen ordering follows directly:

```
INSERT trade PENDING (with client_order_id)
COMMIT ①                    ← durable BEFORE anything irreversible happens
call Binance                ← the irreversible step
map the outcome
COMMIT ②
```

Commit ① is the whole trick. Once it returns, there is a durable row carrying
the exact `client_order_id` that is about to be sent. Whatever happens next —
timeout, crash, power loss — there is a record to recover from.

### Case A — Binance explicitly rejects

Invalid symbol, insufficient balance, a `LOT_SIZE` filter failure. Binance
answers HTTP 4xx with a code and message. **No order exists and never will.**

→ status `REJECTED`, Binance's own error code and message stored, terminal.
Retrying would fail identically, so nothing retries.

The webhook still returns `201`. The signal was received and processed
correctly; the *trade* is rejected. Returning an error would invite TradingView
to re-send an alert guaranteed to fail the same way.

### Case B — Binance times out (the dangerous one)

**A timeout does not mean failure.** The request may have been received and
executed while the response was lost on the way back. Two tempting mistakes:

* *retry the order* → one signal becomes two positions;
* *mark it failed* → a real, live position is now untracked.

The code refuses to guess. It **asks**:

```python
except BinanceUncertainError:
    trade.status = UNKNOWN
    db.commit()                                # record the uncertainty itself
    reconcile_trade(db, binance, trade)        # a QUERY, never a retry
```

`reconcile_trade` calls `GET /api/v3/order?origClientOrderId=<the id we sent>`:

| Binance says | We conclude | New status |
| --- | --- | --- |
| Here is the order | It landed | its real status (`FILLED`, `NEW`, …) |
| `-2013 Order does not exist` | It never landed — now a *known fact* | `FAILED` |
| (the query itself fails) | We still do not know | stays `UNKNOWN`, retried later |

That third row is the one people get wrong. **"We could not ask" must never be
recorded as "there is no order."** There is a test for exactly that.

Note also which exceptions are treated as uncertain. A `ConnectError` means no
connection was ever established, so no order can exist — that is safely
`FAILED`. A `ReadTimeout` means the request went out and the answer was lost —
that is `UNKNOWN`. HTTP 5xx is also `UNKNOWN`, because Binance's own docs say:
*"It is important to NOT treat this as a failure operation; the execution
status is UNKNOWN and could have been a success."*

### Case C — Binance succeeds but the database write fails

The order is filled. We try to write `FILLED` and the commit fails — connection
dropped, disk full, PostgreSQL restarted.

**A rollback cannot cancel the Binance order.** Money has moved. This project
does not pretend otherwise. What it does instead:

```python
except SQLAlchemyError:
    self.db.rollback()
    logger.critical(
        "BINANCE ORDER SUCCEEDED BUT DATABASE UPDATE FAILED. "
        "trade_id=%s signal_id=%s client_order_id=%s binance_order_id=%s ...",
        ...)
    raise
```

* A `CRITICAL` log line carries every identifier needed to recover by hand.
* The row survives as `PENDING` — because of commit ① — complete with the
  `client_order_id`.
* Reconciliation finds it (it selects trades in non-terminal statuses), queries
  Binance, and repairs it automatically.

The honest summary, and a good sentence to say in an interview: *"You cannot
make two systems atomic without a distributed transaction protocol, and that is
far too much machinery here. So instead of pretending, I made every uncertain
outcome detectable and recoverable — write the intent down durably first, then
act, then reconcile."*

---

## 15. The trade state machine and reconciliation

```
PENDING ──────────────┬─────────────┬──────────────┬─────────────┐
row committed,        │             │              │             │
Binance not yet called│             │              │             │
                      ▼             ▼              ▼             ▼
              FILLED / NEW /    REJECTED       FAILED        UNKNOWN
              PARTIALLY_FILLED  Binance        never         timeout or
              CANCELLED /       refused        connected     HTTP 5xx
              EXPIRED           (terminal)     (terminal)    (in flight)
              (from Binance)                                      │
                                                                  │ reconcile
                                                    ┌─────────────┴──────────────┐
                                                    ▼                            ▼
                                        order exists → its real status   -2013 → FAILED
```

| Group | Statuses |
| --- | --- |
| **Terminal** — never change again | `FILLED`, `CANCELLED`, `REJECTED`, `EXPIRED`, `FAILED` |
| **In flight** — reconciliation re-checks these | `PENDING`, `NEW`, `PARTIALLY_FILLED`, `UNKNOWN` |

`NEW`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `EXPIRED` and `REJECTED` are
mirrored from Binance's own `status` field. (Binance spells it `CANCELED` with
one L; the mapping table handles that.) `PENDING`, `UNKNOWN` and `FAILED` are
ours, describing where *we* are rather than where the order is.

### Reconciliation

`reconcile_trade(db, binance, trade)`:

* **skips terminal trades entirely** — re-querying a settled trade could only
  overwrite a precise outcome (`REJECTED` with "insufficient balance") with a
  vaguer one, and there is nothing to learn;
* otherwise queries Binance by `origClientOrderId` and records the truth;
* leaves the trade untouched if the query itself fails.

It is **read-only against the exchange**, so it can never create an order and
is safe to run repeatedly. Three ways to drive it:

1. automatically, immediately after a timeout;
2. manually, `POST /trades/{id}/reconcile` (owner only);
3. in bulk, `python -m scripts.reconcile`, which selects in-flight trades older
   than a cutoff. The age filter stops it racing requests that are legitimately
   still waiting.

In production you would schedule (3) every few minutes — Windows Task
Scheduler or cron. No queue, no worker pool, no extra infrastructure.

---

## 16. Error handling and logging

### One envelope for every error

```json
{
  "error": {
    "code": "duplicate_signal",
    "message": "This signal has already been processed.",
    "request_id": "a3f1c2...",
    "details": [ ... ]
  }
}
```

Handlers registered in `register_exception_handlers`:

| Raised | Status | Notes |
| --- | --- | --- |
| `AuthenticationError` | 401 | plus `WWW-Authenticate: Bearer` |
| `AuthorizationError` | 403 | |
| `ResourceNotFoundError` | 404 | also used for another user's trade |
| `ConflictError` / `DuplicateSignalError` | 409 | |
| `BusinessValidationError` | 422 | |
| `RequestValidationError` | 422 | malformed JSON and Pydantic failures |
| `SQLAlchemyError` | 503 | logged in full, generic message returned |
| any other `Exception` | 500 | full traceback logged, nothing leaked |

Two subtleties worth pointing out:

* **The validation handler deliberately drops Pydantic's `input` field.** The
  raw error list echoes the submitted value — which for `/auth/register` is the
  plaintext password. Only `field`, `message` and `type` are forwarded. There
  is a test asserting the password never appears in a `422` response.
* **Database errors never reach the client.** A driver message can contain the
  connection string, table names and column values.

### Logging

Format: `timestamp LEVEL [request_id] logger: message`.

The correlation id comes from an `X-Request-ID` header if present (so a proxy
can keep one id across systems) or a fresh UUID, is stored in a `ContextVar`,
injected into every record by a logging `Filter`, and echoed back in the
response header. A `ContextVar` is isolated per asyncio task *and* per thread,
which is what makes it correct here: with async routes many requests are in
flight on one thread at the same time, and each task still sees its own id.

Real output from one webhook call:

```
INFO  [98d689…] app.api.routes.webhook: Webhook received: signal_id=smoke-001 BUY BTCUSDT qty=0.001
INFO  [98d689…] app.services.trading_service: Business validation passed for signal_id=smoke-001
INFO  [98d689…] app.services.trading_service: Trade created: id=1 signal_id=smoke-001 status=PENDING client_order_id=cat-68a5…
INFO  [98d689…] app.services.binance_service: Binance order request: /api/v3/order/test BUY BTCUSDT qty=0.001 client_order_id=cat-68a5…
WARN  [98d689…] app.services.binance_service: Binance rejected POST /api/v3/order/test: HTTP 401 code=-2014 msg=API-key format invalid.
WARN  [98d689…] app.services.trading_service: Trade 1 status PENDING -> REJECTED (-2014: API-key format invalid.)
```

One id, the whole story.

**What is never logged:** passwords, password hashes, JWTs (a token is a live
credential — only the *reason* a token was rejected is logged), the JWT secret,
the webhook secret, Binance API keys or secrets, and Binance request parameters
(a signed request's query string contains the HMAC signature, which is why the
`httpx2` logger is pinned to `WARNING`).

---

## 17. The complete request flow, end to end

A TradingView alert arriving, in full:

```
1.  HTTPS POST /webhook/tradingview
        {"signal_id":"tv-001","symbol":"BTCUSDT","action":"BUY","quantity":0.001}
        X-Webhook-Secret: <secret>

2.  RequestContextMiddleware
        assigns request_id = 4f2a…, stores it in a ContextVar
        logs: --> POST /webhook/tradingview

3.  FastAPI routing → tradingview_webhook()
        resolves dependencies first:
          get_trading_service → get_db (opens a Session)
                              → get_binance_client (shared client)

4.  Pydantic parses the body into TradingViewWebhookPayload
        types checked, action uppercased, symbol uppercased,
        quantity parsed as Decimal, unknown fields rejected
        ✗ → 422 validation_failed   (function never runs)

5.  Route: _authenticate_webhook(header_secret, body_secret)
        constant-time compare against WEBHOOK_SECRET
        ✗ → 401 authentication_failed

6.  Route logs the signal and calls service.process_signal(payload)

7.  TradingService.validate_signal()
        symbol in ALLOWED_SYMBOLS?  quantity within the cap?
        ✗ → 422 business_validation_failed   (no row, no order)

8.  TradingService._resolve_trade_owner()
        looks up WEBHOOK_TRADE_OWNER_EMAIL
        ✗ → 500 configuration_error

9.  TradingService._create_pending_trade()
        SELECT … WHERE signal_id = 'tv-001'
        found → 409 duplicate_signal
        else INSERT trade (status=PENDING,
                           client_order_id='cat-' + sha256(signal_id)[:28])
             COMMIT ①  ← durable on disk before anything irreversible
             IntegrityError (lost the race) → 409 duplicate_signal

10. TradingService._submit_to_binance()
        trade.submitted_at = now
        BinanceClient.place_market_order(...)
            build params, add timestamp + recvWindow
            sign the query string with HMAC-SHA256
            POST https://testnet.binance.vision/api/v3/order
                 X-MBX-APIKEY: <key>

11. Classify the outcome
        200          → BinanceOrderResult
        4xx + code   → BinanceRejectedError      → REJECTED
        429 / 418    → BinanceRateLimitError     → REJECTED (rate_limit:…)
        ConnectError → BinanceUnavailableError   → FAILED   (definitely no order)
        ReadTimeout  → BinanceUncertainError     → UNKNOWN  → reconcile
        5xx          → BinanceUncertainError     → UNKNOWN  → reconcile

12. Record it
        map Binance status → TradeStatus, store order id / executed qty
        COMMIT ②
        if COMMIT ② fails → CRITICAL log with every identifier;
                            the row stays recoverable by reconciliation

13. Response: 201
        {"detail":"Signal processed; trade is FILLED.","trade":{…}}
        serialised through TradeRead (no user_id, no internals,
        Decimals rendered as "0.001")

14. get_db's finally: closes the Session, returns the connection to the pool
15. Middleware logs <-- 202 (7.3ms), sets X-Request-ID on the response
```

The authenticated path is shorter:

```
GET /trades   Authorization: Bearer eyJ…
  → middleware assigns request_id
  → get_current_user: verify signature + exp → load User → inject
  → route calls service.list_trades_for_user(user, …)
  → SELECT … WHERE user_id = <caller>  ← ownership is in the query
  → TradeListResponse
```

---

## 18. How the tests work

148 tests, **none of which touch the network**.

### Substituting the exchange

```python
app.dependency_overrides[get_binance_client] = lambda: fake_binance
app.dependency_overrides[get_db] = override_get_db
```

That is the whole mechanism, and it is the payoff for building the session and
the Binance client as dependencies. Every route, service and code path runs
for real; only the two edges are swapped. Nothing in `app/` is patched, and no
application code knows the test suite exists.

`FakeBinanceClient` (`tests/fakes.py`) simulates success, rejection, rate
limiting, connection failure and timeout, and counts calls. That counter is how
the important tests prove their point:

```python
assert fake_binance.place_order_calls == 1   # a duplicate never reached Binance
assert fake_binance.get_order_calls  == 1    # a timeout asked instead of retrying
```

### Testing the real Binance client

`FakeBinanceClient` verifies how the *trading service* reacts. It says nothing
about whether `BinanceClient` itself is correct. So `tests/test_binance_service.py`
tests the real class against `httpx2.MockTransport`, which answers requests
in-process. The real signing, error classification and quantity formatting code
runs — with no network. Those tests recompute the HMAC by hand and assert the
secret never appears in the request.

### Database

Each test gets a fresh schema (`drop_all` / `create_all`). By default that is a
throwaway SQLite file, so `pytest` works on a clean clone with nothing but the
requirements installed. Setting `TEST_DATABASE_URL` runs the identical suite
against PostgreSQL — which is what makes the concurrency test a genuine race
rather than a smoke test.

Note the one place where import order is load-bearing: `conftest.py` populates
`os.environ` **before** importing anything from `app`, because
`app.core.config` validates the environment at import time.

### Coverage

| Area | Examples |
| --- | --- |
| Registration | success, duplicate, case-insensitive email, invalid payloads, password never echoed in errors |
| Login | success, wrong password, unknown email indistinguishable from wrong password |
| JWT | valid, missing, malformed, expired, forged signature, wrong scheme, deleted user |
| Authorization | cannot read or reconcile another user's trade, response field allow-list |
| Schema validation | malformed JSON, missing fields, wrong types, bad action/symbol/quantity, unknown fields |
| Business validation | symbol not allowlisted, quantity over/under the caps, no row created |
| Trading | filled, partial fill, rejection, rate limit, connection failure |
| Timeout | becomes UNKNOWN, never retries, resolves to FILLED, resolves to FAILED |
| Duplicates | 409, never reaches Binance, one row, failed trades still block resends, **4 concurrent identical signals** |
| Database | persistence, relationships both ways, unique `signal_id` and `client_order_id`, FK rejection, cascade delete |
| Binance client | signing, secret never sent, error classification, `-2013` handling, dry-run endpoint |
| Safety | production URLs refused at config *and* client level, placeholder secrets refused |

---

## 19. Design decisions and their alternatives

Every one of these is a fair interview question. Know the trade-off, not just
the choice.

| Decision | Why | The alternative, and why not |
| --- | --- | --- |
| **Async routes + async SQLAlchemy + async HTTP** | Every route is `async def`, every query is awaited through `AsyncSession`/asyncpg, and the Binance client is `httpx2.AsyncClient`. A request waiting on I/O yields the loop instead of holding a thread, which is what lets one worker carry 50+ concurrent webhooks | Sync `def` routes run in FastAPI's threadpool, which defaults to 40 workers — 50 concurrent requests start queueing before any of them touches the database. The trap to avoid is the middle ground: `async def` wrapping blocking I/O, which stalls the whole loop. That is why bcrypt (CPU-bound) is explicitly pushed to `asyncio.to_thread` |
| **Exchange call off the request path** | The webhook commits the PENDING trade and hands execution to an in-process `asyncio.Queue` worker pool, then returns 202. The exchange round trip (measured: ~164 ms median to testnet.binance.vision) no longer sits inside the acknowledgement | Waiting inline makes the caller pay Binance's latency for no benefit — TradingView only needs to know the signal was accepted. Durability is unchanged because the row is committed *before* it is queued |
| **Claim a trade with a conditional UPDATE** | Several workers can hold the same trade id, so ownership is taken with `UPDATE ... WHERE id=? AND status='PENDING' AND submitted_at IS NULL` and the winner is whoever changed a row | "Load it, check `submitted_at`, then write" is a time-of-check-to-time-of-use race: two workers both read an unsubmitted row and place two orders. This was a real bug, caught by a test |
| **Commit `PENDING` before calling Binance** | The row and its `client_order_id` are durable before anything irreversible happens, so any lost response is recoverable | Calling Binance first means a crash leaves a live order with no record at all |
| **Idempotency by DB `UNIQUE`, not an app check** | The only approach that survives concurrent requests and multiple workers | An application-level check is a time-of-check/time-of-use race |
| **Deterministic `client_order_id`** | Lets us ask Binance about a specific order after a timeout; also makes Binance reject duplicates itself | A random id would be unfindable after a lost response |
| **A separate `UNKNOWN` status** | "We do not know" is a real answer and must be representable, or the code is forced to guess | Collapsing it into `FAILED` silently orphans live positions |
| **Reconciliation queries, never retries** | A retry after a timeout can double a position | "Retry with backoff" is right for idempotent reads, wrong for orders |
| **Webhook returns 202 even when Binance rejects** | The webhook succeeded; the *trade* was rejected. The status carries the outcome | A 4xx invites TradingView to re-send an alert that will fail identically |
| **404, not 403, for another user's trade** | A 403 confirms the id exists and enables enumeration | 403 is right only when the caller already knows the resource exists |
| **`Decimal` and `NUMERIC`, never `float`** | Binary floating point cannot represent 0.1 exactly; quantities must round-trip | `float` is faster and wrong |
| **Alembic, not `create_all()`** | Versioned, reviewable, reversible; the app never reshapes the schema at runtime | `create_all()` cannot evolve a schema that already has data |
| **`native_enum=False` + `CHECK`** | A `VARCHAR` with a `CHECK` constraint; adding a status later is an ordinary migration | A PostgreSQL `ENUM` type needs `ALTER TYPE`, which is awkward inside transactions |
| **bcrypt used directly** | One well-understood dependency, no wrapper layer | `passlib` adds indirection and has had bcrypt-4.x compatibility breakage |
| **`HTTPBearer`, not `OAuth2PasswordRequestForm`** | Login stays JSON-and-Pydantic, and Swagger's Authorize button still works | The OAuth2 form flow forces `multipart/form-data` login for no benefit here |
| **Shared secret for the webhook** | The caller is a machine; there is nobody to log in | A JWT would need issuing and refreshing with no user involved |
| **No Redis / Celery / queue** | Nothing here needs one. Reconciliation is a script on a schedule | Every added component is another thing to run, monitor and explain |

---

## 20. Questions you should be able to answer

Try these out loud before an interview. If any answer is fuzzy, re-read the
section in brackets.

**Architecture**
1. Walk me through what happens when a TradingView alert hits your webhook. [§17]
2. Why is the business logic not in the route? [§10]
3. Why one service instead of microservices? [§1]

**Database**
4. What is a SQLAlchemy Session, and why one per request? [§6]
5. What does `get_db` do, and why does it not commit? [§6]
6. Where does PostgreSQL actually run, and what makes a commit durable? [§5]
7. Why `NUMERIC` and not `FLOAT` for quantity? [§9]

**Auth**
8. What is inside your JWT, and what makes it tamper-proof? [§7]
9. Is a JWT encrypted? What must you never put in one? [§7]
10. Why bcrypt and not SHA-256? [§7]
11. Authentication versus authorization — where is each enforced here? [§8]
12. Why do you return 404 rather than 403 for another user's trade? [§8]

**Idempotency**
13. TradingView sends the same alert twice. What happens? [§12]
14. What if both arrive at the same millisecond, on two workers? [§12]
15. Why is an application-level duplicate check not enough? [§12]

**The hard parts**
16. Binance times out. What do you do, and what do you refuse to do? [§14 B]
17. Why is a timeout different from a connection error? [§14 B]
18. Binance filled the order and then your database write failed. Now what? [§14 C]
19. Can you roll back a Binance order? [§14]
20. How would you detect and fix a trade stuck in `UNKNOWN`? [§15]

**Quality**
21. How do you test Binance failures without touching the network? [§18]
22. What stops this from ever placing a real-money order? [§13]
23. What do you deliberately never log, and why? [§16]
24. What would you change to make this multi-tenant? [§11]
25. What would you add before running this with real money? *(Position sizing
    and risk limits, a kill switch, symbol filters fetched from
    `GET /exchangeInfo` so quantities are pre-validated against `LOT_SIZE`,
    alerting on `UNKNOWN` trades, rate-limit backoff, an audit log, and
    two-person review on any change to the host allowlist.)*

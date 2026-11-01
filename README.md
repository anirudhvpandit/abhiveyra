# AbhiVeyra Intelligence

An organization-scoped inventory and procurement workbench with persistent SQLite storage, a stock movement ledger, purchase order receiving, demand baselines, and explainable reorder recommendations.

This repository is an intentionally small, dependency-free first release—not the complete enterprise SaaS specification. It uses Python's WSGI reference server, SQLite, and a vanilla browser client because the workspace was empty and had no Node.js, Docker, or backend packages installed. It is suitable for local evaluation and workflow demonstrations. **It is not production-ready**; see [known limitations](#known-limitations) and the [operational guide](docs/operations.md).

## Requirements

- Python 3.9+
- No third-party Python packages are needed.

## Run locally

```bash
python3 app.py migrate
python3 app.py create-admin --email admin@example.com --organization "Acme Operations" --name "Organization Admin"
python3 app.py serve
```

The administrator password is requested interactively and must be at least 12 characters. Visit <http://127.0.0.1:8000>. The server binds to loopback by default.

To load an explicitly synthetic demonstration workspace instead:

```bash
python3 app.py seed-demo
python3 app.py serve
```

The demo command prints its demo-only credentials. It is not run automatically and must never be used for a real organization. It creates deterministic catalog and inventory examples plus 180 calendar days of synthetic demand observations.

## Configuration

The application reads configuration from its process environment:

| Variable | Default | Description |
|---|---|---|
| `ABHIVEYRA_DB_PATH` | `./abhiveyra.sqlite3` | SQLite database path |
| `ABHIVEYRA_HOST` | `127.0.0.1` | Bind address |
| `ABHIVEYRA_PORT` | `8000` | HTTP port |
| `ABHIVEYRA_COOKIE_SECURE` | `false` | Set to `true` behind HTTPS so session cookies include `Secure` |

`.env.example` contains placeholders for reference; the application does not automatically load `.env` files.

## Workflows implemented

- Create an organization administrator, authenticate, and use an organization-scoped session.
- Create suppliers and products; record unit cost, lead time, minimum order, and order multiple.
- Record receipts, issues, and directional inventory adjustments. Inventory is held in thousandths of a unit; costs are held in cents.
- Require idempotency keys on direct stock movements and purchase order receipts. A retry with the same organization, endpoint, and key returns the original result.
- Prevent issues that exceed on-hand or reserved stock and preserve posted movement history.
- Create a purchase order with a supplier, receiving warehouse, expected date, and line; accept partial or complete receipts. A receipt updates the purchase order, stock balance, movement ledger, and audit log in one SQLite transaction.
- Record daily demand observations. Forecast pages compare a trailing 28-day mean and naive forecast on a chronological holdout using MAE and WAPE.
- Generate order suggestions only after at least 14 recorded demand dates. Recommendations include open purchase orders as incoming supply and explain the assumptions.
- View live stock, ledger, purchase orders, supplier directory, organization audit activity, and download an organization-scoped inventory CSV.

## Tests

```bash
python3 -m py_compile app.py database.py calculations.py tests/test_app.py
python3 -m unittest discover -s tests -v
```

Tests use isolated temporary SQLite files and cover authentication/CSRF, tenant-scoped reads and writes, duplicate stock posting, negative stock rejection, purchase order receipts, CSV tenant boundaries, and core forecast/reorder calculations.

## API

All JSON endpoints are versioned under `/api/v1`. The local server serves the browser interface at `/`.

| Endpoint | Methods | Purpose |
|---|---|---|
| `/api/v1/auth/login` | POST | Start a session |
| `/api/v1/auth/me` | GET | Current user and CSRF token |
| `/api/v1/auth/logout` | POST | Revoke current session |
| `/api/v1/health/live` | GET | Process liveness |
| `/api/v1/health/ready` | GET | Database readiness and schema version |
| `/api/v1/dashboard` | GET | Live organization metrics |
| `/api/v1/products` | GET, POST | List/create catalog items |
| `/api/v1/warehouses` | GET | List organization's warehouses |
| `/api/v1/suppliers` | GET, POST | List/create suppliers |
| `/api/v1/inventory` | GET | On-hand and available quantities |
| `/api/v1/inventory/movements` | GET, POST | Ledger / receipt, issue, adjustment |
| `/api/v1/demand` | POST | Record a daily demand observation |
| `/api/v1/forecasts` | GET | Baseline forecast and holdout metrics |
| `/api/v1/recommendations` | GET | Reorder-point and order quantity suggestions |
| `/api/v1/purchase-orders` | GET, POST | List/create orders |
| `/api/v1/purchase-orders/{id}/receive` | POST | Post receipt against an open order |
| `/api/v1/reports/inventory.csv` | GET | Current inventory CSV |
| `/api/v1/audit` | GET | Most recent 200 audit events |

Mutating endpoints require the session CSRF token in `X-CSRF-Token`. Stock movement and goods-receipt requests also require `Idempotency-Key`. Errors use a JSON envelope: `{"error":{"code":"...","message":"..."}}`.

## Known limitations

- SQLite is the only supported database; there is no PostgreSQL adapter or production migration chain beyond schema version 1.
- The built-in `wsgiref` server is for local use, not production load. No Docker, reverse proxy, deployment pipeline, Redis, worker queue, scheduler, telemetry, or managed-service integration is included.
- Authentication does not yet include password reset, email verification, MFA, configurable rate limiting, or fine-grained permissions. Roles are a simple hierarchy.
- No multi-step approvals, requisitions, transfers, reservations workflow, cycle counts, returns, import pipeline, XLSX/PDF export, notifications, or external ERP/supplier integration is implemented.
- Forecasting uses two simple baselines and a single chronological holdout. Forecast runs, model artifacts, intervals, stockout-censored demand, intermittent-demand models, promotions, and seasonal model selection are not persisted or implemented.
- Recommendations require 14 observations but calculate with a trailing 180-calendar-day window, filling unrecorded dates with zero. They assume stable lead time, a 95% service-level z-score, and a 14-day order cycle; these assumptions are not user-configurable.
- Product unit cost is the current manually entered cost; valuation is not FIFO or weighted-average accounting cost and does not include landed costs or currency conversion.
- Currency is displayed as USD; organizations cannot configure locale, currency, or timezone.
- No application-level login throttling, password-reset flow, comprehensive security test suite, or dependency scanner is included. Use HTTPS, a production WSGI server, isolated database permissions, backups, and a security review before exposing this application to a network.

See [architecture and calculations](docs/architecture.md), the [KPI dictionary](docs/kpi-dictionary.md), [forecast methodology](docs/forecasting-methodology.md), and [operations and security notes](docs/operations.md).
# abhiveyra

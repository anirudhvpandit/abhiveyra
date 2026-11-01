# Architecture and data integrity

## Runtime shape

```text
Browser (same-origin HTML/CSS/JavaScript)
         │ JSON /api/v1, session cookie, CSRF header
         ▼
Python WSGI application (app.py)
         │ parameterized SQL, explicit organization_id predicates
         ▼
SQLite (database.py, schema version 1)
```

The application is a modular monolith. `calculations.py` contains pure demand and replenishment calculations; `database.py` defines relational schema and transaction handling; `app.py` validates HTTP input, applies session and role checks, scopes data to the signed-in user's organization, and coordinates workflows. The browser has no separate business-rule authority.

## Persistence

The initial schema includes organizations, globally unique user emails, sessions, warehouses, suppliers, products, per-warehouse inventory balances, immutable stock movement rows, demand observations, purchase orders and lines, idempotency responses, and audit events. Foreign keys are enabled. Quantities use integer thousandths and monetary values use integer cents. Migrations are idempotent and record schema version 1.

All queries for tenant-owned data filter by the authenticated user's `organization_id`. API writes validate that referenced products, suppliers, and warehouses belong to that organization. Session cookies are HttpOnly and SameSite=Strict; production deployments must enable Secure cookies over HTTPS. Mutating API requests require the server-issued CSRF token.

## Inventory model

- `on_hand` is physical stock recorded at a warehouse.
- `reserved` is stored separately; no reservation creation workflow exists yet.
- `available = on_hand - reserved`.
- Open purchase-order quantities are incoming supply in planning only and never increase on-hand or available balances.
- Receipts and issues update balances and append ledger rows in a single transaction. Issues cannot make on-hand stock negative or consume reserved stock.
- A posted transaction is not edited in place. Correct it with a compensating movement.
- Idempotency response keys are unique by organization, endpoint, and client key.

## Forecasting and replenishment

Forecasts require at least 14 recorded demand dates, then compare trailing-28-day mean with last-observation naive forecast on a chronological holdout. The lower-MAE baseline is extended as a constant daily estimate. Metrics are MAE and WAPE, with WAPE omitted if holdout demand is zero.

For demand observations \(d_t\), the reorder-point baseline is:

```text
mean_daily_demand = mean(d_t)
safety_stock = 1.645 × population_stddev(d_t) × sqrt(lead_time_days)
reorder_point = mean_daily_demand × lead_time_days + safety_stock
target_stock = mean_daily_demand × (lead_time_days + 14) + safety_stock
order_quantity = max(0, target_stock − (on_hand − reserved + open_PO_quantity))
```

The order is rounded upward to the product's order multiple and minimum quantity. Demand is aggregated from observations on the product/warehouse and organization-wide observations. Warehouse-specific demand is used when any observations exist for that location; organization-level observations are a fallback only when no location-specific observations exist. This avoids summing organization-wide demand into every location. The z-score approximates 95% one-sided service under independent, normally distributed daily demand and fixed lead time; this assumption is not verified by the application. Recommendations are decision support, not commitments.

## Procurement transaction

Order creation validates tenant ownership and stores line quantities and costs. Goods receipt runs under a write transaction: it checks each outstanding amount, increments warehouse stock, updates line received quantity, appends a receipt movement, derives order status, writes an audit event, and saves the idempotent response. Duplicate line IDs in a single receipt are rejected. Over-receipt is rejected.

## Explicitly not production-verified

This implementation has no concurrency/load profile, PostgreSQL deployment, automated migration rollback, async job system, external integration, or comprehensive authorization matrix. It does not claim production readiness. See [operations](operations.md) before deploying.

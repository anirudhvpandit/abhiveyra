import os
import sqlite3
from contextlib import contextmanager


SCHEMA_VERSION = 1


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    email TEXT NOT NULL COLLATE NOCASE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'manager', 'operator', 'viewer')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (organization_id, email)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_global ON users(email COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash TEXT PRIMARY KEY,
    csrf_token TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON user_sessions(expires_at);
CREATE TABLE IF NOT EXISTS warehouses (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL,
    code TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (organization_id, code)
);
CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    lead_time_days INTEGER NOT NULL DEFAULT 14 CHECK (lead_time_days >= 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (organization_id, name)
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    sku TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'Uncategorized',
    unit TEXT NOT NULL DEFAULT 'ea',
    unit_cost_cents INTEGER NOT NULL DEFAULT 0 CHECK (unit_cost_cents >= 0),
    lead_time_days INTEGER NOT NULL DEFAULT 14 CHECK (lead_time_days >= 0),
    minimum_order_milli INTEGER NOT NULL DEFAULT 0 CHECK (minimum_order_milli >= 0),
    order_multiple_milli INTEGER NOT NULL DEFAULT 1000 CHECK (order_multiple_milli > 0),
    supplier_id INTEGER REFERENCES suppliers(id),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (organization_id, sku)
);
CREATE INDEX IF NOT EXISTS idx_products_org_name ON products(organization_id, name);
CREATE TABLE IF NOT EXISTS inventory_balances (
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    on_hand_milli INTEGER NOT NULL DEFAULT 0 CHECK (on_hand_milli >= 0),
    reserved_milli INTEGER NOT NULL DEFAULT 0 CHECK (reserved_milli >= 0),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (organization_id, warehouse_id, product_id)
);
CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    movement_type TEXT NOT NULL CHECK (movement_type IN ('receipt', 'issue', 'adjustment')),
    quantity_milli INTEGER NOT NULL CHECK (quantity_milli > 0),
    signed_quantity_milli INTEGER NOT NULL,
    reference TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    actor_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_movements_org_created
    ON stock_movements(organization_id, created_at DESC);
CREATE TABLE IF NOT EXISTS demand_history (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    warehouse_id INTEGER REFERENCES warehouses(id),
    demand_date TEXT NOT NULL,
    quantity_milli INTEGER NOT NULL CHECK (quantity_milli >= 0),
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (organization_id, product_id, warehouse_id, demand_date)
);
CREATE INDEX IF NOT EXISTS idx_demand_org_product_date
    ON demand_history(organization_id, product_id, demand_date);
CREATE TABLE IF NOT EXISTS purchase_orders (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
    order_number TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'partial', 'received', 'cancelled')),
    expected_date TEXT NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (organization_id, order_number)
);
CREATE INDEX IF NOT EXISTS idx_po_org_status ON purchase_orders(organization_id, status);
CREATE TABLE IF NOT EXISTS purchase_order_lines (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    purchase_order_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    ordered_milli INTEGER NOT NULL CHECK (ordered_milli > 0),
    received_milli INTEGER NOT NULL DEFAULT 0 CHECK (received_milli >= 0),
    unit_cost_cents INTEGER NOT NULL CHECK (unit_cost_cents >= 0)
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    endpoint TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (organization_id, endpoint, idempotency_key)
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    actor_id INTEGER NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_audit_org_created
    ON audit_events(organization_id, created_at DESC);
"""


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Database:
    def __init__(self, path=None):
        self.path = path or os.environ.get(
            "ABHIVEYRA_DB_PATH", os.path.join(os.path.dirname(__file__), "abhiveyra.sqlite3")
        )

    def connect(self):
        connection = sqlite3.connect(
            self.path, timeout=15, isolation_level=None, factory=ClosingConnection
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        if self.path != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def migrate(self):
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                (SCHEMA_VERSION,),
            )

    @contextmanager
    def transaction(self, immediate=True):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

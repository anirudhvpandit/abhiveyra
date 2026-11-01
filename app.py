import argparse
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http import cookies
from wsgiref.simple_server import make_server

from calculations import forecast_summary, replenishment
from database import Database


MAX_BODY_BYTES = 1_000_000
SESSION_DAYS = 12
COOKIE_NAME = "av_session"
STATIC_ROOT = os.path.join(os.path.dirname(__file__), "public")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ApiError(Exception):
    def __init__(self, status, message, code="invalid_request"):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def password_hash(password):
    if not isinstance(password, str) or len(password) < 12:
        raise ApiError(400, "Password must contain at least 12 characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return "pbkdf2_sha256${}${}".format(salt.hex(), digest.hex())


def password_matches(password, stored):
    try:
        algorithm, salt_hex, digest_hex = stored.split("$", 2)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 310_000
        )
        return hmac.compare_digest(actual.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


def parse_quantity(value, field="quantity"):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ApiError(400, "{} must be a number.".format(field))
    if not number.is_finite() or number < 0 or number.as_tuple().exponent < -3:
        raise ApiError(400, "{} must be non-negative with at most 3 decimal places.".format(field))
    return int((number * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_money_cents(value, field="unit_cost"):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ApiError(400, "{} must be a valid amount.".format(field))
    if not number.is_finite() or number < 0 or number > Decimal("999999999"):
        raise ApiError(400, "{} must be a non-negative amount.".format(field))
    return int((number * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError("Unsupported JSON value")


class Application:
    def __init__(self, database=None):
        self.database = database or Database()
        self.database.migrate()

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")
        try:
            if not path.startswith("/api/"):
                return self._static(path, start_response)
            body = self._read_json(environ) if method in ("POST", "PUT", "PATCH") else {}
            status, payload, headers = self._dispatch(environ, method, path, body)
            if isinstance(payload, bytes):
                content_type = next(
                    (value for name, value in headers if name.lower() == "content-type"),
                    "application/octet-stream",
                )
                return self._respond_bytes(start_response, status, payload, content_type)
            return self._respond(start_response, status, payload, headers)
        except ApiError as error:
            return self._respond(start_response, error.status, {
                "error": {"code": error.code, "message": error.message}
            })
        except sqlite3.IntegrityError:
            logging.exception("Request violated a database constraint")
            return self._respond(start_response, 409, {
                "error": {"code": "record_conflict", "message": "A record with the same unique identifier already exists."}
            })
        except sqlite3.DatabaseError:
            logging.exception("Database request failed")
            return self._respond(start_response, 500, {
                "error": {"code": "database_error", "message": "A database operation failed."}
            })
        except Exception:
            logging.exception("Unhandled request failure")
            return self._respond(start_response, 500, {
                "error": {"code": "internal_error", "message": "The request could not be completed."}
            })

    def _read_json(self, environ):
        try:
            length = int(environ.get("CONTENT_LENGTH") or "0")
        except ValueError:
            raise ApiError(400, "Invalid Content-Length.")
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "Request body is too large.", "payload_too_large")
        raw = environ["wsgi.input"].read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "Request body must be valid JSON.")
        if not isinstance(value, dict):
            raise ApiError(400, "Request body must be a JSON object.")
        return value

    def _respond(self, start_response, status, payload, extra_headers=None):
        content = json.dumps(payload, default=json_default, separators=(",", ":")).encode("utf-8")
        headers = [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(content))),
            ("Cache-Control", "no-store"),
        ]
        headers.extend(self._security_headers())
        if extra_headers:
            headers.extend(extra_headers)
        start_response("{} {}".format(status, _status_text(status)), headers)
        return [content]

    def _respond_bytes(self, start_response, status, content, content_type, filename=None):
        headers = [
            ("Content-Type", content_type),
            ("Content-Length", str(len(content))),
            ("Cache-Control", "no-store"),
        ]
        if filename:
            headers.append(("Content-Disposition", 'attachment; filename="{}"'.format(filename)))
        headers.extend(self._security_headers())
        start_response("{} {}".format(status, _status_text(status)), headers)
        return [content]

    def _security_headers(self):
        return [
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "strict-origin-when-cross-origin"),
            ("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'"),
        ]

    def _static(self, path, start_response):
        files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        }
        selected = files.get(path)
        if not selected:
            content = b"Not found"
            start_response("404 Not Found", [("Content-Type", "text/plain"), ("Content-Length", str(len(content)))])
            return [content]
        with open(os.path.join(STATIC_ROOT, selected[0]), "rb") as file_obj:
            content = file_obj.read()
        headers = [
            ("Content-Type", selected[1]),
            ("Content-Length", str(len(content))),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "strict-origin-when-cross-origin"),
            ("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'"),
        ]
        start_response("200 OK", headers)
        return [content]

    def _session(self, environ, write=False):
        raw_cookie = environ.get("HTTP_COOKIE", "")
        jar = cookies.SimpleCookie()
        try:
            jar.load(raw_cookie)
        except cookies.CookieError:
            raise ApiError(401, "Please sign in.", "authentication_required")
        morsel = jar.get(COOKIE_NAME)
        if morsel is None:
            raise ApiError(401, "Please sign in.", "authentication_required")
        token_hash = hashlib.sha256(morsel.value.encode("utf-8")).hexdigest()
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT u.id, u.organization_id, u.email, u.display_name, u.role,
                          o.name AS organization_name, s.csrf_token, s.expires_at
                   FROM user_sessions s JOIN users u ON u.id=s.user_id
                   JOIN organizations o ON o.id=u.organization_id
                   WHERE s.token_hash=?""",
                (token_hash,),
            ).fetchone()
        if row is None or row["expires_at"] <= datetime.now(timezone.utc).isoformat():
            raise ApiError(401, "Your session has expired. Please sign in again.", "session_expired")
        if write and not hmac.compare_digest(
            environ.get("HTTP_X_CSRF_TOKEN", ""), row["csrf_token"]
        ):
            raise ApiError(403, "The request could not be verified.", "csrf_failed")
        return dict(row)

    def _dispatch(self, environ, method, path, body):
        if path == "/api/v1/health/live" and method == "GET":
            return 200, {"status": "ok"}, []
        if path == "/api/v1/health/ready" and method == "GET":
            with self.database.connect() as connection:
                connection.execute("SELECT 1").fetchone()
                version = connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
            return 200, {"status": "ready", "schema_version": version}, []
        if path == "/api/v1/auth/login" and method == "POST":
            return self._login(body)
        if path == "/api/v1/auth/logout" and method == "POST":
            user = self._session(environ, write=True)
            cookie = self._cookie("", max_age=0)
            token = self._cookie_token(environ)
            with self.database.transaction() as connection:
                connection.execute("DELETE FROM user_sessions WHERE token_hash=?", (token,))
                self._audit(connection, user, "logout", "session", token[:12], {})
            return 200, {"ok": True}, [("Set-Cookie", cookie)]

        user = self._session(environ, write=method in ("POST", "PUT", "PATCH", "DELETE"))
        if path == "/api/v1/auth/me" and method == "GET":
            return 200, {"user": self._public_user(user)}, []
        if path == "/api/v1/dashboard" and method == "GET":
            return 200, {"data": self._dashboard(user)}, []
        if path == "/api/v1/products" and method == "GET":
            return 200, {"data": self._products(user)}, []
        if path == "/api/v1/products" and method == "POST":
            self._require_role(user, "manager")
            return 201, {"data": self._create_product(user, body)}, []
        if path == "/api/v1/warehouses" and method == "GET":
            return 200, {"data": self._warehouses(user)}, []
        if path == "/api/v1/suppliers" and method == "GET":
            return 200, {"data": self._suppliers(user)}, []
        if path == "/api/v1/suppliers" and method == "POST":
            self._require_role(user, "manager")
            return 201, {"data": self._create_supplier(user, body)}, []
        if path == "/api/v1/inventory" and method == "GET":
            return 200, {"data": self._inventory(user)}, []
        if path == "/api/v1/inventory/movements" and method == "GET":
            return 200, {"data": self._movements(user)}, []
        if path == "/api/v1/inventory/movements" and method == "POST":
            self._require_role(user, "operator")
            return 201, {"data": self._post_movement(user, body, environ)}, []
        if path == "/api/v1/demand" and method == "POST":
            self._require_role(user, "operator")
            return 201, {"data": self._record_demand(user, body)}, []
        if path == "/api/v1/forecasts" and method == "GET":
            return 200, {"data": self._forecasts(user)}, []
        if path == "/api/v1/recommendations" and method == "GET":
            return 200, {"data": self._recommendations(user)}, []
        if path == "/api/v1/purchase-orders" and method == "GET":
            return 200, {"data": self._purchase_orders(user)}, []
        if path == "/api/v1/purchase-orders" and method == "POST":
            self._require_role(user, "manager")
            return 201, {"data": self._create_purchase_order(user, body)}, []
        match = re.fullmatch(r"/api/v1/purchase-orders/(\d+)/receive", path)
        if match and method == "POST":
            self._require_role(user, "operator")
            return 200, {"data": self._receive_purchase_order(user, int(match.group(1)), body, environ)}, []
        if path == "/api/v1/reports/inventory.csv" and method == "GET":
            csv_data = self._inventory_csv(user)
            return 200, csv_data, [("Content-Type", "text/csv; charset=utf-8")]
        if path == "/api/v1/audit" and method == "GET":
            self._require_role(user, "viewer")
            return 200, {"data": self._audit_log(user)}, []
        raise ApiError(404, "The requested endpoint was not found.", "not_found")

    def _login(self, body):
        email = str(body.get("email", "")).strip().lower()
        password = body.get("password", "")
        if not EMAIL_PATTERN.match(email) or not isinstance(password, str):
            raise ApiError(400, "Enter a valid email address and password.")
        with self.database.connect() as connection:
            user = connection.execute(
                "SELECT id, organization_id, email, display_name, password_hash FROM users WHERE email=?",
                (email,),
            ).fetchone()
        if user is None or not password_matches(password, user["password_hash"]):
            raise ApiError(401, "Email or password is incorrect.", "invalid_credentials")
        token = secrets.token_urlsafe(36)
        csrf = secrets.token_urlsafe(24)
        expiry = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO user_sessions(token_hash, csrf_token, user_id, expires_at) VALUES (?, ?, ?, ?)",
                (token_hash, csrf, user["id"], expiry.isoformat()),
            )
            connection.execute(
                "DELETE FROM user_sessions WHERE expires_at < ?",
                (datetime.now(timezone.utc).isoformat(),),
            )
            self._audit(connection, {
                "id": user["id"], "organization_id": user["organization_id"]
            }, "login", "user", user["id"], {})
        return 200, {
            "user": {
                "id": user["id"],
                "email": user["email"],
                "display_name": user["display_name"],
                "csrf_token": csrf,
            }
        }, [("Set-Cookie", self._cookie(token))]

    def _cookie(self, token, max_age=SESSION_DAYS * 86400):
        secure = "; Secure" if os.environ.get("ABHIVEYRA_COOKIE_SECURE", "").lower() == "true" else ""
        return "{}={}; Path=/; HttpOnly; SameSite=Strict; Max-Age={}{}".format(
            COOKIE_NAME, token, max_age, secure
        )

    def _cookie_token(self, environ):
        jar = cookies.SimpleCookie()
        jar.load(environ.get("HTTP_COOKIE", ""))
        return hashlib.sha256(jar[COOKIE_NAME].value.encode("utf-8")).hexdigest()

    def _public_user(self, user):
        return {
            "id": user["id"],
            "email": user["email"],
            "display_name": user["display_name"],
            "role": user["role"],
            "organization_name": user["organization_name"],
            "csrf_token": user["csrf_token"],
        }

    def _require_role(self, user, minimum):
        ranks = {"viewer": 0, "operator": 1, "manager": 2, "admin": 3}
        if ranks.get(user["role"], -1) < ranks[minimum]:
            raise ApiError(403, "Your account does not have permission for this action.", "forbidden")

    def _audit(self, connection, user, action, entity_type, entity_id, details):
        connection.execute(
            """INSERT INTO audit_events
               (organization_id, actor_id, action, entity_type, entity_id, details)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                user["organization_id"], user["id"], action, entity_type,
                str(entity_id), json.dumps(details, separators=(",", ":")),
            ),
        )

    def _dashboard(self, user):
        organization_id = user["organization_id"]
        with self.database.connect() as connection:
            product_count = connection.execute(
                "SELECT count(*) FROM products WHERE organization_id=? AND active=1",
                (organization_id,),
            ).fetchone()[0]
            warehouse_count = connection.execute(
                "SELECT count(*) FROM warehouses WHERE organization_id=?", (organization_id,)
            ).fetchone()[0]
            stock = connection.execute(
                """SELECT COALESCE(SUM(b.on_hand_milli * p.unit_cost_cents), 0) AS value_milli_cents,
                          COALESCE(SUM(b.on_hand_milli), 0) AS units,
                          COALESCE(SUM(b.reserved_milli), 0) AS reserved
                   FROM inventory_balances b JOIN products p ON p.id=b.product_id
                   WHERE b.organization_id=?""",
                (organization_id,),
            ).fetchone()
            open_orders = connection.execute(
                """SELECT count(DISTINCT po.id), COALESCE(SUM(
                          (pol.ordered_milli-pol.received_milli)*pol.unit_cost_cents), 0)
                   FROM purchase_orders po JOIN purchase_order_lines pol
                     ON pol.purchase_order_id=po.id AND pol.organization_id=po.organization_id
                   WHERE po.organization_id=? AND po.status IN ('open','partial')""",
                (organization_id,),
            ).fetchone()
            movements = connection.execute(
                """SELECT substr(created_at,1,10) AS day, movement_type,
                          SUM(signed_quantity_milli) AS quantity
                   FROM stock_movements WHERE organization_id=?
                     AND created_at >= date('now','-6 day')
                   GROUP BY day, movement_type ORDER BY day""",
                (organization_id,),
            ).fetchall()
            product_rows = self._planning_rows(connection, organization_id)
        recommendations = []
        for item in product_rows:
            plan = self._make_recommendation(connection=None, item=item)
            if plan.get("available") and plan["recommended_order_milli"] > 0:
                recommendations.append({
                    "sku": item["sku"], "name": item["name"],
                    "warehouse": item["warehouse_name"], **plan,
                })
        low_stock = sum(1 for item in product_rows
                        if item["on_hand_milli"] - item["reserved_milli"] <= 0)
        return {
            "organization": user["organization_name"],
            "currency": "USD",
            "currency_note": "Amounts use USD because organization currency configuration is not yet available.",
            "inventory_value": round(stock["value_milli_cents"] / 100000, 2),
            "on_hand_units": round(stock["units"] / 1000, 3),
            "reserved_units": round(stock["reserved"] / 1000, 3),
            "open_purchase_orders": open_orders[0],
            "open_purchase_order_value": round(open_orders[1] / 100000, 2),
            "active_products": product_count,
            "warehouses": warehouse_count,
            "stockout_risks": len(recommendations),
            "zero_available_positions": low_stock,
            "weekly_movements": [dict(row) for row in movements],
            "actions": recommendations[:6],
            "source": "Live organization database records; zero values reflect no recorded data.",
        }

    def _products(self, user):
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT p.id,p.sku,p.name,p.category,p.unit,p.unit_cost_cents,p.lead_time_days,
                          p.minimum_order_milli,p.order_multiple_milli,p.supplier_id,
                          s.name AS supplier_name,p.active,
                          COALESCE(SUM(b.on_hand_milli),0) AS on_hand_milli,
                          COALESCE(SUM(b.reserved_milli),0) AS reserved_milli
                   FROM products p LEFT JOIN suppliers s ON s.id=p.supplier_id
                   LEFT JOIN inventory_balances b ON b.product_id=p.id
                   WHERE p.organization_id=? GROUP BY p.id ORDER BY p.name""",
                (user["organization_id"],),
            ).fetchall()
        return [self._product_json(row) for row in rows]

    def _product_json(self, row):
        value = dict(row)
        value["unit_cost"] = value.pop("unit_cost_cents") / 100
        for name in ("on_hand_milli", "reserved_milli", "minimum_order_milli", "order_multiple_milli"):
            if name in value:
                value[name.replace("_milli", "")] = value.pop(name) / 1000
        value["available"] = value.get("on_hand", 0) - value.get("reserved", 0)
        return value

    def _create_product(self, user, body):
        sku, name = str(body.get("sku", "")).strip(), str(body.get("name", "")).strip()
        if not sku or len(sku) > 80 or not name or len(name) > 200:
            raise ApiError(400, "SKU and product name are required.")
        lead = body.get("lead_time_days", 14)
        try:
            lead = int(lead)
        except (ValueError, TypeError):
            raise ApiError(400, "Lead time must be a whole number of days.")
        if lead < 0 or lead > 3650:
            raise ApiError(400, "Lead time must be between 0 and 3650 days.")
        minimum = parse_quantity(body.get("minimum_order", 0), "minimum_order")
        multiple = parse_quantity(body.get("order_multiple", 1), "order_multiple")
        if multiple == 0:
            raise ApiError(400, "Order multiple must be greater than zero.")
        cost = parse_money_cents(body.get("unit_cost", 0))
        supplier_id = body.get("supplier_id") or None
        with self.database.transaction() as connection:
            if supplier_id and not connection.execute(
                "SELECT 1 FROM suppliers WHERE id=? AND organization_id=?",
                (supplier_id, user["organization_id"]),
            ).fetchone():
                raise ApiError(400, "Select a supplier in your organization.")
            cursor = connection.execute(
                """INSERT INTO products
                   (organization_id,sku,name,category,unit,unit_cost_cents,lead_time_days,
                    minimum_order_milli,order_multiple_milli,supplier_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (user["organization_id"], sku, name,
                 str(body.get("category", "Uncategorized")).strip()[:100] or "Uncategorized",
                 str(body.get("unit", "ea")).strip()[:20] or "ea", cost, lead,
                 minimum, multiple, supplier_id),
            )
            self._audit(connection, user, "create", "product", cursor.lastrowid, {"sku": sku})
            product_id = cursor.lastrowid
        return next(product for product in self._products(user) if product["id"] == product_id)

    def _warehouses(self, user):
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id,name,code FROM warehouses WHERE organization_id=? ORDER BY name",
                (user["organization_id"],),
            ).fetchall()
        return [dict(row) for row in rows]

    def _suppliers(self, user):
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT s.id,s.name,s.email,s.lead_time_days,s.status,
                          count(DISTINCT po.id) AS orders,
                          count(DISTINCT CASE WHEN po.status='received' THEN po.id END) AS completed_orders
                   FROM suppliers s LEFT JOIN purchase_orders po
                     ON po.supplier_id=s.id AND po.organization_id=s.organization_id
                   WHERE s.organization_id=? GROUP BY s.id ORDER BY s.name""",
                (user["organization_id"],),
            ).fetchall()
        return [dict(row) for row in rows]

    def _create_supplier(self, user, body):
        name = str(body.get("name", "")).strip()
        email = str(body.get("email", "")).strip()
        if not name or len(name) > 200 or (email and not EMAIL_PATTERN.match(email)):
            raise ApiError(400, "Enter a supplier name and a valid email address.")
        try:
            lead = int(body.get("lead_time_days", 14))
        except (ValueError, TypeError):
            raise ApiError(400, "Lead time must be a whole number of days.")
        if lead < 0 or lead > 3650:
            raise ApiError(400, "Lead time must be between 0 and 3650 days.")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO suppliers(organization_id,name,email,lead_time_days) VALUES(?,?,?,?)",
                (user["organization_id"], name, email, lead),
            )
            self._audit(connection, user, "create", "supplier", cursor.lastrowid, {"name": name})
            supplier_id = cursor.lastrowid
        return next(supplier for supplier in self._suppliers(user) if supplier["id"] == supplier_id)

    def _inventory(self, user):
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT p.id AS product_id,p.sku,p.name,p.category,p.unit,p.unit_cost_cents,
                          w.id AS warehouse_id,w.name AS warehouse_name,w.code AS warehouse_code,
                          b.on_hand_milli,b.reserved_milli,b.updated_at
                   FROM inventory_balances b JOIN products p ON p.id=b.product_id
                   JOIN warehouses w ON w.id=b.warehouse_id
                   WHERE b.organization_id=? ORDER BY p.name,w.name""",
                (user["organization_id"],),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["on_hand"] = item.pop("on_hand_milli") / 1000
            item["reserved"] = item.pop("reserved_milli") / 1000
            item["available"] = item["on_hand"] - item["reserved"]
            item["unit_cost"] = item.pop("unit_cost_cents") / 100
            item["value"] = round(item["on_hand"] * item["unit_cost"], 2)
            result.append(item)
        return result

    def _movements(self, user):
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT m.id,m.movement_type,m.quantity_milli,m.signed_quantity_milli,
                          m.reason,m.reference,m.created_at,p.sku,p.name AS product_name,
                          w.name AS warehouse_name,u.display_name AS actor
                   FROM stock_movements m JOIN products p ON p.id=m.product_id
                   JOIN warehouses w ON w.id=m.warehouse_id JOIN users u ON u.id=m.actor_id
                   WHERE m.organization_id=? ORDER BY m.id DESC LIMIT 100""",
                (user["organization_id"],),
            ).fetchall()
        return [{
            **dict(row),
            "quantity": row["quantity_milli"] / 1000,
            "signed_quantity": row["signed_quantity_milli"] / 1000,
        } for row in rows]

    def _post_movement(self, user, body, environ):
        movement_type = body.get("movement_type")
        if movement_type not in ("receipt", "issue", "adjustment"):
            raise ApiError(400, "Movement type must be receipt, issue, or adjustment.")
        quantity = parse_quantity(body.get("quantity"), "quantity")
        if quantity <= 0:
            raise ApiError(400, "Quantity must be greater than zero.")
        try:
            product_id = int(body.get("product_id"))
            warehouse_id = int(body.get("warehouse_id"))
        except (ValueError, TypeError):
            raise ApiError(400, "Choose a product and warehouse.")
        reason = str(body.get("reason", "")).strip()
        if not reason or len(reason) > 300:
            raise ApiError(400, "A movement reason of 1 to 300 characters is required.")
        key = self._idempotency_key(environ)
        endpoint = "/api/v1/inventory/movements"
        with self.database.transaction() as connection:
            prior = self._idempotent_response(connection, user, endpoint, key)
            if prior is not None:
                return prior
            if not connection.execute(
                "SELECT 1 FROM products WHERE id=? AND organization_id=? AND active=1",
                (product_id, user["organization_id"]),
            ).fetchone() or not connection.execute(
                "SELECT 1 FROM warehouses WHERE id=? AND organization_id=?",
                (warehouse_id, user["organization_id"]),
            ).fetchone():
                raise ApiError(404, "Product or warehouse was not found in your organization.")
            sign = -1 if movement_type == "issue" else 1
            if movement_type == "adjustment":
                if body.get("direction") not in ("increase", "decrease"):
                    raise ApiError(400, "Adjustment direction must be increase or decrease.")
                sign = -1 if body["direction"] == "decrease" else 1
            signed = quantity * sign
            connection.execute(
                """INSERT INTO inventory_balances(organization_id,warehouse_id,product_id)
                   VALUES(?,?,?) ON CONFLICT(organization_id,warehouse_id,product_id) DO NOTHING""",
                (user["organization_id"], warehouse_id, product_id),
            )
            balance = connection.execute(
                """SELECT on_hand_milli,reserved_milli FROM inventory_balances
                   WHERE organization_id=? AND warehouse_id=? AND product_id=?""",
                (user["organization_id"], warehouse_id, product_id),
            ).fetchone()
            after = balance["on_hand_milli"] + signed
            if after < 0:
                raise ApiError(409, "This movement would make on-hand inventory negative.", "insufficient_stock")
            if movement_type == "issue" and after < balance["reserved_milli"]:
                raise ApiError(409, "Stock is reserved and cannot be issued.", "stock_reserved")
            connection.execute(
                """UPDATE inventory_balances SET on_hand_milli=?,updated_at=CURRENT_TIMESTAMP
                   WHERE organization_id=? AND warehouse_id=? AND product_id=?""",
                (after, user["organization_id"], warehouse_id, product_id),
            )
            cursor = connection.execute(
                """INSERT INTO stock_movements
                   (organization_id,warehouse_id,product_id,movement_type,quantity_milli,
                    signed_quantity_milli,reference,reason,actor_id)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (user["organization_id"], warehouse_id, product_id, movement_type, quantity,
                 signed, str(body.get("reference", ""))[:100], reason, user["id"]),
            )
            result = {"id": cursor.lastrowid, "movement_type": movement_type,
                      "quantity": quantity / 1000, "on_hand": after / 1000}
            self._audit(connection, user, "post", "stock_movement", cursor.lastrowid, result)
            self._store_idempotent_response(connection, user, endpoint, key, result)
            return result

    def _record_demand(self, user, body):
        try:
            product_id = int(body.get("product_id"))
            quantity = parse_quantity(body.get("quantity"), "quantity")
            demand_date = date.fromisoformat(str(body.get("date", "")))
            warehouse_id = int(body["warehouse_id"]) if body.get("warehouse_id") else None
        except (ValueError, TypeError):
            raise ApiError(400, "Enter a valid product, quantity, and ISO date.")
        if demand_date > date.today() or demand_date < date.today() - timedelta(days=3650):
            raise ApiError(400, "Demand date must be within the last 10 years and not in the future.")
        with self.database.transaction() as connection:
            if not connection.execute(
                "SELECT 1 FROM products WHERE id=? AND organization_id=?",
                (product_id, user["organization_id"]),
            ).fetchone():
                raise ApiError(404, "Product was not found in your organization.")
            if warehouse_id and not connection.execute(
                "SELECT 1 FROM warehouses WHERE id=? AND organization_id=?",
                (warehouse_id, user["organization_id"]),
            ).fetchone():
                raise ApiError(404, "Warehouse was not found in your organization.")
            connection.execute(
                """INSERT INTO demand_history
                   (organization_id,product_id,warehouse_id,demand_date,quantity_milli,source)
                   VALUES(?,?,?,?,?,'manual')
                   ON CONFLICT(organization_id,product_id,warehouse_id,demand_date)
                   DO UPDATE SET quantity_milli=excluded.quantity_milli,source='manual'""",
                (user["organization_id"], product_id, warehouse_id, demand_date.isoformat(), quantity),
            )
            self._audit(connection, user, "record", "demand", product_id, {
                "date": demand_date.isoformat(), "quantity": quantity / 1000
            })
        return {"product_id": product_id, "date": demand_date.isoformat(), "quantity": quantity / 1000}

    def _planning_rows(self, connection, organization_id):
        return connection.execute(
            """SELECT p.id,p.organization_id,p.sku,p.name,p.category,p.unit,p.unit_cost_cents,p.lead_time_days,
                      p.minimum_order_milli,p.order_multiple_milli,p.supplier_id,
                      s.name AS supplier_name,COALESCE(w.id,0) AS warehouse_id,
                      COALESCE(w.name,'All warehouses') AS warehouse_name,
                      COALESCE(b.on_hand_milli,0) AS on_hand_milli,
                      COALESCE(b.reserved_milli,0) AS reserved_milli
               FROM products p LEFT JOIN suppliers s ON s.id=p.supplier_id
               LEFT JOIN inventory_balances b ON b.product_id=p.id AND b.organization_id=p.organization_id
               LEFT JOIN warehouses w ON w.id=b.warehouse_id AND w.organization_id=p.organization_id
               WHERE p.organization_id=? AND p.active=1
               ORDER BY p.name,w.name""",
            (organization_id,),
        ).fetchall()

    def _daily_demand(self, connection, organization_id, product_id, warehouse_id=0, days=180):
        since = (date.today() - timedelta(days=days - 1)).isoformat()
        if warehouse_id:
            rows = connection.execute(
                """SELECT demand_date,SUM(quantity_milli) AS quantity FROM demand_history
                   WHERE organization_id=? AND product_id=? AND demand_date>=?
                     AND warehouse_id=? GROUP BY demand_date""",
                (organization_id, product_id, since, warehouse_id),
            ).fetchall()
            if not rows:
                rows = connection.execute(
                    """SELECT demand_date,SUM(quantity_milli) AS quantity FROM demand_history
                       WHERE organization_id=? AND product_id=? AND demand_date>=?
                         AND warehouse_id IS NULL GROUP BY demand_date""",
                    (organization_id, product_id, since),
                ).fetchall()
        else:
            rows = connection.execute(
                """SELECT demand_date,SUM(quantity_milli) AS quantity FROM demand_history
                   WHERE organization_id=? AND product_id=? AND demand_date>=?
                   GROUP BY demand_date""",
                (organization_id, product_id, since),
            ).fetchall()
        daily = {row["demand_date"]: row["quantity"] / 1000 for row in rows}
        return [
            daily.get((date.today() - timedelta(days=offset)).isoformat(), 0.0)
            for offset in range(days - 1, -1, -1)
        ]

    def _demand_observation_count(self, connection, organization_id, product_id, warehouse_id=0):
        if warehouse_id:
            row = connection.execute(
                """SELECT COUNT(DISTINCT demand_date) FROM demand_history
                   WHERE organization_id=? AND product_id=? AND demand_date>=?
                     AND warehouse_id=?""",
                (organization_id, product_id, (date.today() - timedelta(days=179)).isoformat(), warehouse_id),
            ).fetchone()
            if row[0]:
                return row[0]
            row = connection.execute(
                """SELECT COUNT(DISTINCT demand_date) FROM demand_history
                   WHERE organization_id=? AND product_id=? AND demand_date>=?
                     AND warehouse_id IS NULL""",
                (organization_id, product_id, (date.today() - timedelta(days=179)).isoformat()),
            ).fetchone()
        else:
            row = connection.execute(
                """SELECT COUNT(DISTINCT demand_date) FROM demand_history
                   WHERE organization_id=? AND product_id=? AND demand_date>=?""",
                (organization_id, product_id, (date.today() - timedelta(days=179)).isoformat()),
            ).fetchone()
        return row[0]

    def _forecasts(self, user):
        with self.database.connect() as connection:
            products = self._planning_rows(connection, user["organization_id"])
            result = []
            for product in products:
                observation_count = self._demand_observation_count(
                    connection, user["organization_id"], product["id"], product["warehouse_id"]
                )
                demand = self._daily_demand(
                    connection, user["organization_id"], product["id"], product["warehouse_id"]
                )
                forecast = forecast_summary(demand) if observation_count >= 14 else {
                    "available": False,
                    "reason": "At least 14 recorded demand dates are required.",
                }
                result.append({
                    "product_id": product["id"], "sku": product["sku"], "name": product["name"],
                    "warehouse_id": product["warehouse_id"],
                    "warehouse": product["warehouse_name"],
                    "observations": observation_count, **forecast,
                })
        return result

    def _make_recommendation(self, connection, item):
        if connection is None:
            with self.database.connect() as db:
                return self._make_recommendation(db, item)
        observation_count = self._demand_observation_count(
            connection, item["organization_id"], item["id"], item["warehouse_id"]
        )
        if observation_count < 14:
            return {
                "available": False, "observations": observation_count,
                "reason": "At least 14 recorded demand dates are required.",
                "recommended_order_milli": 0, "estimated_cost": 0,
            }
        demand = self._daily_demand(
            connection, item["organization_id"], item["id"], item["warehouse_id"]
        )
        incoming = connection.execute(
            """SELECT COALESCE(SUM(pol.ordered_milli-pol.received_milli),0)
               FROM purchase_order_lines pol JOIN purchase_orders po
                 ON po.id=pol.purchase_order_id AND po.organization_id=pol.organization_id
               WHERE pol.organization_id=? AND pol.product_id=? AND po.warehouse_id=?
                 AND po.status IN ('open','partial')""",
            (item["organization_id"], item["id"], item["warehouse_id"]),
        ).fetchone()[0] if item["warehouse_id"] else 0
        available = max(0, item["on_hand_milli"] - item["reserved_milli"] + incoming)
        plan = replenishment(
            available, demand, item["lead_time_days"],
            item["minimum_order_milli"], item["order_multiple_milli"],
        )
        plan["available_quantity"] = available / 1000
        plan["incoming_quantity"] = incoming / 1000
        plan["observations"] = observation_count
        plan["estimated_cost"] = round(
            plan.get("recommended_order_milli", 0) * item["unit_cost_cents"] / 100000, 2
        )
        plan["explanation"] = (
            "Target stock covers lead-time demand plus 14 days of cycle demand and 95% service-level safety stock. "
            "Demand is calculated from recorded daily observations over 180 calendar days."
        )
        return plan

    def _recommendations(self, user):
        with self.database.connect() as connection:
            rows = self._planning_rows(connection, user["organization_id"])
            result = []
            for row in rows:
                item = dict(row)
                item["organization_id"] = user["organization_id"]
                result.append({
                    "product_id": item["id"], "sku": item["sku"], "name": item["name"],
                    "warehouse_id": item["warehouse_id"], "warehouse": item["warehouse_name"],
                    "unit": item["unit"], **self._make_recommendation(connection, item),
                })
        return result

    def _idempotency_key(self, environ):
        key = environ.get("HTTP_IDEMPOTENCY_KEY", "").strip()
        if not key or len(key) > 128:
            raise ApiError(400, "An Idempotency-Key header of up to 128 characters is required.")
        return key

    def _idempotent_response(self, connection, user, endpoint, key):
        row = connection.execute(
            """SELECT response_json FROM idempotency_keys
               WHERE organization_id=? AND endpoint=? AND idempotency_key=?""",
            (user["organization_id"], endpoint, key),
        ).fetchone()
        if row:
            return json.loads(row["response_json"])
        return None

    def _store_idempotent_response(self, connection, user, endpoint, key, response):
        connection.execute(
            """INSERT INTO idempotency_keys(organization_id,endpoint,idempotency_key,response_json)
               VALUES(?,?,?,?)""",
            (user["organization_id"], endpoint, key, json.dumps(response, separators=(",", ":"))),
        )

    def _create_purchase_order(self, user, body):
        try:
            supplier_id = int(body.get("supplier_id"))
            warehouse_id = int(body.get("warehouse_id"))
        except (ValueError, TypeError):
            raise ApiError(400, "Choose a supplier and warehouse.")
        lines = body.get("lines")
        if not isinstance(lines, list) or not lines or len(lines) > 100:
            raise ApiError(400, "A purchase order must contain 1 to 100 lines.")
        expected = body.get("expected_date")
        try:
            expected_date = date.fromisoformat(str(expected))
        except ValueError:
            raise ApiError(400, "Expected date must use YYYY-MM-DD.")
        if expected_date < date.today() or expected_date > date.today() + timedelta(days=3650):
            raise ApiError(400, "Expected date must be between today and 10 years from today.")
        normalized = []
        for line in lines:
            try:
                product_id = int(line.get("product_id"))
            except (ValueError, TypeError):
                raise ApiError(400, "Every purchase order line needs a product.")
            quantity = parse_quantity(line.get("quantity"), "line quantity")
            if quantity <= 0:
                raise ApiError(400, "Purchase order line quantities must be positive.")
            normalized.append((product_id, quantity, parse_money_cents(line.get("unit_cost", 0))))
        with self.database.transaction() as connection:
            if not connection.execute(
                "SELECT 1 FROM suppliers WHERE id=? AND organization_id=? AND status='active'",
                (supplier_id, user["organization_id"]),
            ).fetchone() or not connection.execute(
                "SELECT 1 FROM warehouses WHERE id=? AND organization_id=?",
                (warehouse_id, user["organization_id"]),
            ).fetchone():
                raise ApiError(404, "Supplier or warehouse was not found in your organization.")
            for product_id, _, _ in normalized:
                if not connection.execute(
                    "SELECT 1 FROM products WHERE id=? AND organization_id=? AND active=1",
                    (product_id, user["organization_id"]),
                ).fetchone():
                    raise ApiError(404, "A purchase order product was not found in your organization.")
            order_number = "PO-{}-{}".format(date.today().strftime("%y%m%d"), secrets.token_hex(3).upper())
            cursor = connection.execute(
                """INSERT INTO purchase_orders
                   (organization_id,supplier_id,warehouse_id,order_number,status,expected_date,created_by)
                   VALUES(?,?,?,?,'open',?,?)""",
                (user["organization_id"], supplier_id, warehouse_id, order_number,
                 expected_date.isoformat(), user["id"]),
            )
            order_id = cursor.lastrowid
            for product_id, quantity, cost in normalized:
                connection.execute(
                    """INSERT INTO purchase_order_lines
                       (organization_id,purchase_order_id,product_id,ordered_milli,unit_cost_cents)
                       VALUES(?,?,?,?,?)""",
                    (user["organization_id"], order_id, product_id, quantity, cost),
                )
            self._audit(connection, user, "create", "purchase_order", order_id, {
                "order_number": order_number, "line_count": len(normalized)
            })
        return next(order for order in self._purchase_orders(user) if order["id"] == order_id)

    def _purchase_orders(self, user):
        with self.database.connect() as connection:
            orders = connection.execute(
                """SELECT po.id,po.order_number,po.status,po.expected_date,po.created_at,
                          s.name AS supplier_name,w.name AS warehouse_name,
                          COUNT(pol.id) AS line_count,
                          COALESCE(SUM(pol.ordered_milli*pol.unit_cost_cents),0) AS total_milli_cents
                   FROM purchase_orders po JOIN suppliers s ON s.id=po.supplier_id
                   JOIN warehouses w ON w.id=po.warehouse_id
                   LEFT JOIN purchase_order_lines pol ON pol.purchase_order_id=po.id
                     AND pol.organization_id=po.organization_id
                   WHERE po.organization_id=? GROUP BY po.id ORDER BY po.id DESC""",
                (user["organization_id"],),
            ).fetchall()
            result = []
            for order in orders:
                item = dict(order)
                item["total"] = round(item.pop("total_milli_cents") / 100000, 2)
                item["lines"] = [{
                    "id": row["id"], "product_id": row["product_id"], "sku": row["sku"],
                    "product_name": row["name"], "ordered": row["ordered_milli"] / 1000,
                    "received": row["received_milli"] / 1000,
                    "unit_cost": row["unit_cost_cents"] / 100,
                } for row in connection.execute(
                    """SELECT pol.id,pol.product_id,p.sku,p.name,pol.ordered_milli,
                              pol.received_milli,pol.unit_cost_cents
                       FROM purchase_order_lines pol JOIN products p ON p.id=pol.product_id
                       WHERE pol.purchase_order_id=? AND pol.organization_id=?""",
                    (order["id"], user["organization_id"]),
                ).fetchall()]
                result.append(item)
        return result

    def _receive_purchase_order(self, user, order_id, body, environ):
        key = self._idempotency_key(environ)
        endpoint = "/api/v1/purchase-orders/{}/receive".format(order_id)
        lines = body.get("lines")
        if not isinstance(lines, list) or not lines:
            raise ApiError(400, "At least one receipt line is required.")
        normalized = []
        for line in lines:
            try:
                line_id = int(line.get("line_id"))
            except (ValueError, TypeError):
                raise ApiError(400, "Every receipt line needs a purchase order line ID.")
            quantity = parse_quantity(line.get("quantity"), "receipt quantity")
            if quantity <= 0:
                raise ApiError(400, "Receipt quantities must be positive.")
            normalized.append((line_id, quantity))
        if len({line_id for line_id, _ in normalized}) != len(normalized):
            raise ApiError(400, "A purchase order line can appear only once in a receipt.")
        with self.database.transaction() as connection:
            prior = self._idempotent_response(connection, user, endpoint, key)
            if prior is not None:
                return prior
            order = connection.execute(
                """SELECT id,warehouse_id,status FROM purchase_orders
                   WHERE id=? AND organization_id=?""",
                (order_id, user["organization_id"]),
            ).fetchone()
            if order is None or order["status"] in ("received", "cancelled"):
                raise ApiError(404, "An open purchase order was not found.")
            received_total = 0
            for line_id, quantity in normalized:
                line = connection.execute(
                    """SELECT id,product_id,ordered_milli,received_milli FROM purchase_order_lines
                       WHERE id=? AND purchase_order_id=? AND organization_id=?""",
                    (line_id, order_id, user["organization_id"]),
                ).fetchone()
                if line is None:
                    raise ApiError(404, "A purchase order line was not found.")
                if line["received_milli"] + quantity > line["ordered_milli"]:
                    raise ApiError(409, "Receipt exceeds the outstanding quantity.", "over_receipt")
                connection.execute(
                    """INSERT INTO inventory_balances(organization_id,warehouse_id,product_id)
                       VALUES(?,?,?) ON CONFLICT(organization_id,warehouse_id,product_id) DO NOTHING""",
                    (user["organization_id"], order["warehouse_id"], line["product_id"]),
                )
                connection.execute(
                    """UPDATE inventory_balances SET on_hand_milli=on_hand_milli+?,
                       updated_at=CURRENT_TIMESTAMP
                       WHERE organization_id=? AND warehouse_id=? AND product_id=?""",
                    (quantity, user["organization_id"], order["warehouse_id"], line["product_id"]),
                )
                connection.execute(
                    "UPDATE purchase_order_lines SET received_milli=received_milli+? WHERE id=?",
                    (quantity, line_id),
                )
                connection.execute(
                    """INSERT INTO stock_movements
                       (organization_id,warehouse_id,product_id,movement_type,quantity_milli,
                        signed_quantity_milli,reference,reason,actor_id)
                       VALUES(?,?,?,'receipt',?,?,?,'Purchase order receipt',?)""",
                    (user["organization_id"], order["warehouse_id"], line["product_id"],
                     quantity, quantity, "PO {}".format(order_id), user["id"]),
                )
                received_total += quantity
            remaining = connection.execute(
                """SELECT COALESCE(SUM(ordered_milli-received_milli),0)
                   FROM purchase_order_lines WHERE purchase_order_id=? AND organization_id=?""",
                (order_id, user["organization_id"]),
            ).fetchone()[0]
            status = "received" if remaining == 0 else "partial"
            connection.execute("UPDATE purchase_orders SET status=? WHERE id=?", (status, order_id))
            result = {
                "purchase_order_id": order_id, "status": status,
                "received_quantity": received_total / 1000,
            }
            self._audit(connection, user, "receive", "purchase_order", order_id, result)
            self._store_idempotent_response(connection, user, endpoint, key, result)
            return result

    def _inventory_csv(self, user):
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["SKU", "Product", "Warehouse", "On hand", "Reserved", "Available", "Unit", "Unit cost USD", "Inventory value USD"])
        for item in self._inventory(user):
            writer.writerow([
                _safe_csv(item["sku"]), _safe_csv(item["name"]), _safe_csv(item["warehouse_name"]),
                item["on_hand"], item["reserved"], item["available"], _safe_csv(item["unit"]),
                item["unit_cost"], item["value"],
            ])
        return output.getvalue().encode("utf-8-sig")

    def _audit_log(self, user):
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT a.id,a.action,a.entity_type,a.entity_id,a.details,a.created_at,
                          u.display_name AS actor FROM audit_events a JOIN users u ON u.id=a.actor_id
                   WHERE a.organization_id=? ORDER BY a.id DESC LIMIT 200""",
                (user["organization_id"],),
            ).fetchall()
        return [dict(row) for row in rows]


def _safe_csv(value):
    text = str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


def _status_text(code):
    return {
        200: "OK", 201: "Created", 400: "Bad Request", 401: "Unauthorized",
        403: "Forbidden", 404: "Not Found", 409: "Conflict", 413: "Payload Too Large",
        500: "Internal Server Error",
    }.get(code, "OK")


def create_admin(database, email, organization, display_name, password):
    if not EMAIL_PATTERN.match(email):
        raise ValueError("A valid email address is required.")
    hashed = password_hash(password)
    with database.transaction() as connection:
        org = connection.execute(
            "SELECT id FROM organizations WHERE name=? COLLATE NOCASE", (organization,)
        ).fetchone()
        if org is None:
            cursor = connection.execute(
                "INSERT INTO organizations(name) VALUES(?)", (organization,)
            )
            organization_id = cursor.lastrowid
        else:
            organization_id = org["id"]
        connection.execute(
            """INSERT INTO users(organization_id,email,display_name,password_hash,role)
               VALUES(?,?,?,?,'admin')""",
            (organization_id, email.lower(), display_name, hashed),
        )
        connection.execute(
            "INSERT INTO warehouses(organization_id,name,code) VALUES(?,?,?)",
            (organization_id, "Main warehouse", "MAIN"),
        )
    return organization_id


def seed_demo(database):
    password = secrets.token_urlsafe(20)
    with database.transaction() as connection:
        if connection.execute(
            "SELECT 1 FROM organizations WHERE is_demo=1 LIMIT 1"
        ).fetchone():
            raise ValueError("A demo organization already exists in this database.")
        org_cursor = connection.execute(
            "INSERT INTO organizations(name,is_demo) VALUES('AbhiVeyra Demo Organization',1)"
        )
        organization_id = org_cursor.lastrowid
        user_cursor = connection.execute(
            """INSERT INTO users(organization_id,email,display_name,password_hash,role)
               VALUES(?,?,?,?, 'admin')""",
            (organization_id, "demo@abhiveyra.local", "Demo Administrator",
             password_hash(password)),
        )
        user_id = user_cursor.lastrowid
        warehouse = connection.execute(
            "INSERT INTO warehouses(organization_id,name,code) VALUES(?,?,?)",
            (organization_id, "North distribution center", "NORTH"),
        ).lastrowid
        second_warehouse = connection.execute(
            "INSERT INTO warehouses(organization_id,name,code) VALUES(?,?,?)",
            (organization_id, "South assembly depot", "SOUTH"),
        ).lastrowid
        supplier1 = connection.execute(
            "INSERT INTO suppliers(organization_id,name,email,lead_time_days) VALUES(?,?,?,?)",
            (organization_id, "Meridian Components (synthetic)", "meridian@example.invalid", 12),
        ).lastrowid
        supplier2 = connection.execute(
            "INSERT INTO suppliers(organization_id,name,email,lead_time_days) VALUES(?,?,?,?)",
            (organization_id, "Pioneer Industrial (synthetic)", "pioneer@example.invalid", 18),
        ).lastrowid
        products = [
            ("BRG-204", "Precision bearing 204", "Mechanical", 18_400, 12, 4000, 1000, supplier1),
            ("MTR-118", "Servo motor 1.8kW", "Electrical", 245_000, 18, 2000, 1000, supplier2),
            ("FIL-032", "Hydraulic filter 32mm", "Maintenance", 7_250, 9, 6000, 2000, supplier1),
            ("SNS-490", "Proximity sensor M12", "Electrical", 12_500, 15, 3000, 1000, supplier2),
            ("PKG-010", "Protective packaging set", "Packaging", 285, 6, 10000, 5000, supplier1),
            ("VAL-225", "Solenoid valve 24V", "Hydraulics", 38_900, 21, 1000, 500, supplier2),
        ]
        product_ids = []
        for sku, name, category, cost, lead, minimum, multiple, supplier_id in products:
            pid = connection.execute(
                """INSERT INTO products
                   (organization_id,sku,name,category,unit_cost_cents,lead_time_days,
                    minimum_order_milli,order_multiple_milli,supplier_id)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (organization_id, sku, name, category, cost, lead, minimum, multiple, supplier_id),
            ).lastrowid
            product_ids.append(pid)
            for wh, initial in ((warehouse, 8 + (pid * 11) % 38), (second_warehouse, 3 + (pid * 7) % 19)):
                qty = initial * 1000
                connection.execute(
                    """INSERT INTO inventory_balances
                       (organization_id,warehouse_id,product_id,on_hand_milli)
                       VALUES(?,?,?,?)""",
                    (organization_id, wh, pid, qty),
                )
                connection.execute(
                    """INSERT INTO stock_movements
                       (organization_id,warehouse_id,product_id,movement_type,quantity_milli,
                        signed_quantity_milli,reference,reason,actor_id)
                       VALUES(?,?,?,'receipt',?,?,?,'Synthetic opening balance',?)""",
                    (organization_id, wh, pid, qty, qty, "DEMO-OPENING", user_id),
                )
            for days_ago in range(179, -1, -1):
                day = date.today() - timedelta(days=days_ago)
                base = 1 + ((pid + day.weekday()) % 4)
                if sku == "MTR-118":
                    base = 1 if day.weekday() < 5 and day.day % 3 == 0 else 0
                if sku == "PKG-010":
                    base += 2 if day.month in (10, 11, 12) else 0
                connection.execute(
                    """INSERT INTO demand_history
                       (organization_id,product_id,warehouse_id,demand_date,quantity_milli,source)
                       VALUES(?,?,?,?,?,'synthetic_demo')""",
                    (organization_id, pid, warehouse, day.isoformat(), base * 1000),
                )
    return {"email": "demo@abhiveyra.local", "password": password}


def _run_cli():
    parser = argparse.ArgumentParser(description="AbhiVeyra Intelligence local application")
    parser.add_argument("command", choices=("serve", "migrate", "create-admin", "seed-demo"))
    parser.add_argument("--host", default=os.environ.get("ABHIVEYRA_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ABHIVEYRA_PORT", "8000")))
    parser.add_argument("--email")
    parser.add_argument("--name", default="Organization Administrator")
    parser.add_argument("--organization", default="My Organization")
    args = parser.parse_args()
    database = Database()
    database.migrate()
    if args.command == "migrate":
        print("Database schema is at version 1.")
    elif args.command == "create-admin":
        if not args.email:
            parser.error("--email is required for create-admin")
        import getpass
        password = getpass.getpass("Administrator password (12+ characters): ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            parser.error("Passwords do not match.")
        create_admin(database, args.email, args.organization, args.name, password)
        print("Administrator created. Sign in at http://{}:{}/".format(args.host, args.port))
    elif args.command == "seed-demo":
        credentials = seed_demo(database)
        print("Synthetic demo organization created.")
        print("Email: {}".format(credentials["email"]))
        print("Password: {}".format(credentials["password"]))
    else:
        server = make_server(args.host, args.port, Application(database))
        print("Serving AbhiVeyra at http://{}:{}/".format(args.host, args.port))
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down.")
        finally:
            server.server_close()


if __name__ == "__main__":
    import sqlite3
    _run_cli()

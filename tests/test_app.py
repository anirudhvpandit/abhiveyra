import json
import os
import tempfile
import unittest
from io import BytesIO
from http.cookies import SimpleCookie

from app import Application, create_admin, password_hash
from calculations import forecast_summary, replenishment
from database import Database


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(os.path.join(self.temp_dir.name, "test.sqlite3"))
        self.database.migrate()
        create_admin(
            self.database, "admin@example.test", "Test Organization",
            "Test Administrator", "test-password-long",
        )
        self.app = Application(self.database)
        with self.database.transaction() as connection:
            self.warehouse_id = connection.execute(
                "SELECT id FROM warehouses WHERE code='MAIN'"
            ).fetchone()["id"]
            self.supplier_id = connection.execute(
                """INSERT INTO suppliers(organization_id,name,lead_time_days)
                   VALUES(1,'Test Supplier',7)"""
            ).lastrowid
            self.product_id = connection.execute(
                """INSERT INTO products(organization_id,sku,name,unit_cost_cents,
                   lead_time_days,minimum_order_milli,order_multiple_milli,supplier_id)
                   VALUES(1,'SKU-1','Test Product',1250,7,1000,1000,?)""",
                (self.supplier_id,),
            ).lastrowid
            self.other_org_id = connection.execute(
                "INSERT INTO organizations(name) VALUES('Other Organization')"
            ).lastrowid
            self.other_warehouse_id = connection.execute(
                "INSERT INTO warehouses(organization_id,name,code) VALUES(?,?,?)",
                (self.other_org_id, "Other warehouse", "OTHER"),
            ).lastrowid
            self.other_product_id = connection.execute(
                """INSERT INTO products(organization_id,sku,name)
                   VALUES(?,?,?)""",
                (self.other_org_id, "OTHER-1", "Other tenant product"),
            ).lastrowid
        self.cookie, self.csrf = self.login()

    def tearDown(self):
        self.temp_dir.cleanup()

    def request(self, path, method="GET", body=None, cookie=None, csrf=None, idempotency=None):
        encoded = json.dumps(body or {}).encode("utf-8")
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "CONTENT_LENGTH": str(len(encoded)) if body is not None else "0",
            "CONTENT_TYPE": "application/json",
            "wsgi.input": BytesIO(encoded),
            "HTTP_COOKIE": cookie or "",
            "HTTP_X_CSRF_TOKEN": csrf or "",
            "HTTP_IDEMPOTENCY_KEY": idempotency or "",
        }
        result = {}

        def start_response(status, headers):
            result["status"] = int(status.split(" ", 1)[0])
            result["headers"] = dict(headers)

        payload = b"".join(self.app(environ, start_response))
        result["body"] = payload
        if result["headers"].get("Content-Type", "").startswith("application/json"):
            result["json"] = json.loads(payload.decode("utf-8"))
        return result

    def login(self, email="admin@example.test", password="test-password-long"):
        response = self.request(
            "/api/v1/auth/login", "POST",
            {"email": email, "password": password},
        )
        self.assertEqual(response["status"], 200)
        jar = SimpleCookie()
        jar.load(response["headers"]["Set-Cookie"])
        return "av_session={}".format(jar["av_session"].value), response["json"]["user"]["csrf_token"]

    def test_authentication_and_csrf_are_required(self):
        self.assertEqual(self.request("/api/v1/products")["status"], 401)
        denied = self.request(
            "/api/v1/inventory/movements", "POST",
            {"movement_type": "receipt"}, self.cookie,
        )
        self.assertEqual(denied["status"], 403)

    def test_health_endpoints_and_insufficient_history(self):
        self.assertEqual(self.request("/api/v1/health/live")["json"]["status"], "ok")
        self.assertEqual(self.request("/api/v1/health/ready")["json"]["status"], "ready")
        forecasts = self.request("/api/v1/forecasts", cookie=self.cookie)["json"]["data"]
        self.assertEqual(len(forecasts), 1)
        self.assertFalse(forecasts[0]["available"])
        self.assertEqual(forecasts[0]["observations"], 0)

    def test_idempotent_inventory_receipt_and_negative_stock_guard(self):
        body = {
            "product_id": self.product_id, "warehouse_id": self.warehouse_id,
            "movement_type": "receipt", "quantity": "4.125", "reason": "Opening receipt",
        }
        first = self.request(
            "/api/v1/inventory/movements", "POST", body,
            self.cookie, self.csrf, "receipt-001",
        )
        second = self.request(
            "/api/v1/inventory/movements", "POST", body,
            self.cookie, self.csrf, "receipt-001",
        )
        self.assertEqual(first["status"], 201)
        self.assertEqual(first["json"], second["json"])
        self.assertEqual(first["json"]["data"]["on_hand"], 4.125)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0], 1)

        issue = self.request(
            "/api/v1/inventory/movements", "POST",
            {**body, "movement_type": "issue", "quantity": 10},
            self.cookie, self.csrf, "issue-too-much",
        )
        self.assertEqual(issue["status"], 409)

    def test_tenant_scoped_product_access(self):
        response = self.request("/api/v1/products", cookie=self.cookie)
        ids = {item["id"] for item in response["json"]["data"]}
        self.assertIn(self.product_id, ids)
        self.assertNotIn(self.other_product_id, ids)
        movement = self.request(
            "/api/v1/inventory/movements", "POST",
            {
                "product_id": self.other_product_id,
                "warehouse_id": self.warehouse_id,
                "movement_type": "receipt", "quantity": 1, "reason": "Cross-tenant attempt",
            },
            self.cookie, self.csrf, "cross-tenant",
        )
        self.assertEqual(movement["status"], 404)

    def test_viewer_cannot_create_products(self):
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO users(organization_id,email,display_name,password_hash,role)
                   VALUES(1,'viewer@example.test','Read Only',?,'viewer')""",
                (password_hash("viewer-password-long"),),
            )
        viewer_cookie, viewer_csrf = self.login("viewer@example.test", "viewer-password-long")
        response = self.request(
            "/api/v1/products", "POST",
            {"sku": "NO-WRITE", "name": "Unauthorized"},
            viewer_cookie, viewer_csrf,
        )
        self.assertEqual(response["status"], 403)

    def test_purchase_receipt_updates_stock_exactly_once(self):
        order = self.request(
            "/api/v1/purchase-orders", "POST",
            {
                "supplier_id": self.supplier_id, "warehouse_id": self.warehouse_id,
                "expected_date": "2027-01-15",
                "lines": [{"product_id": self.product_id, "quantity": 3, "unit_cost": 12.5}],
            },
            self.cookie, self.csrf,
        )
        self.assertEqual(order["status"], 201)
        order_id = order["json"]["data"]["id"]
        line_id = order["json"]["data"]["lines"][0]["id"]
        dashboard = self.request("/api/v1/dashboard", cookie=self.cookie)["json"]["data"]
        self.assertEqual(dashboard["open_purchase_order_value"], 37.5)
        receipt_body = {"lines": [{"line_id": line_id, "quantity": 3}]}
        first = self.request(
            "/api/v1/purchase-orders/{}/receive".format(order_id), "POST",
            receipt_body, self.cookie, self.csrf, "receipt-po-001",
        )
        retry = self.request(
            "/api/v1/purchase-orders/{}/receive".format(order_id), "POST",
            receipt_body, self.cookie, self.csrf, "receipt-po-001",
        )
        self.assertEqual(first["status"], 200)
        self.assertEqual(first["json"], retry["json"])
        self.assertEqual(first["json"]["data"]["status"], "received")
        inventory = self.request("/api/v1/inventory", cookie=self.cookie)["json"]["data"]
        self.assertEqual(inventory[0]["on_hand"], 3)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0], 1)

    def test_csv_report_is_tenant_scoped_and_downloadable(self):
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO inventory_balances
                   (organization_id,warehouse_id,product_id,on_hand_milli)
                   VALUES(1,?,?,2500)""",
                (self.warehouse_id, self.product_id),
            )
        response = self.request("/api/v1/reports/inventory.csv", cookie=self.cookie)
        self.assertEqual(response["status"], 200)
        self.assertIn("text/csv", response["headers"]["Content-Type"])
        self.assertIn(b"SKU-1", response["body"])
        self.assertNotIn(b"OTHER-1", response["body"])


class CalculationTests(unittest.TestCase):
    def test_forecast_uses_chronological_holdout_and_baseline_comparison(self):
        result = forecast_summary([2] * 30 + [4] * 10)
        self.assertTrue(result["available"])
        self.assertEqual(result["holdout_days"], 8)
        self.assertIn(result["model"], ("28-day moving average", "naive"))
        self.assertEqual(result["observations"], 40)
        self.assertIsNotNone(result["mae"])

    def test_forecast_refuses_insufficient_history(self):
        self.assertFalse(forecast_summary([1] * 13)["available"])

    def test_replenishment_rounds_to_order_multiple(self):
        result = replenishment(
            available_milli=0, daily_demand=[2] * 30, lead_time_days=10,
            minimum_order_milli=5000, order_multiple_milli=3000,
        )
        self.assertTrue(result["available"])
        self.assertGreaterEqual(result["recommended_order_milli"], 5000)
        self.assertEqual(result["recommended_order_milli"] % 3000, 0)
        self.assertAlmostEqual(result["average_daily_demand"], 2)


if __name__ == "__main__":
    unittest.main()

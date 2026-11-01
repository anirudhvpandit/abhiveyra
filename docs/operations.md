# Operations, setup, and security

## Local operations

Use Python 3.9 or later. No package install is required.

```bash
python3 app.py migrate
python3 app.py create-admin --email admin@example.com --organization "Acme Operations" --name "Organization Admin"
python3 app.py serve
```

The database path is selected by `ABHIVEYRA_DB_PATH`; `ABHIVEYRA_HOST` and `ABHIVEYRA_PORT` control the local server. `ABHIVEYRA_COOKIE_SECURE=true` adds the Secure cookie attribute and should be used with HTTPS. Copying `.env.example` does not load those values; export variables in the process environment.

The `seed-demo` command explicitly creates a synthetic tenant with demo credentials and sample records. It is not part of application startup. Never use its credentials for a real deployment.

## Database backup and restore

Stop the local server before copying the SQLite database and its WAL files, or use SQLite's online backup API. Example online backup:

```bash
python3 - <<'PY'
import os
import sqlite3
source = sqlite3.connect(os.environ.get("ABHIVEYRA_DB_PATH", "abhiveyra.sqlite3"))
target = sqlite3.connect("abhiveyra-backup.sqlite3")
source.backup(target)
target.close()
source.close()
PY
```

To restore, stop the application, retain a copy of the current database, place a verified backup at the configured database path, and run `python3 app.py migrate` before starting the service. No automated backup retention, restore test, or point-in-time recovery is implemented; operators must verify backups and recovery in a separate environment.

## Security controls present

- PBKDF2-HMAC-SHA256 password hashes with a random salt.
- Opaque random session tokens; only SHA-256 token hashes are persisted.
- HttpOnly, SameSite=Strict session cookies; optional Secure attribute.
- CSRF header checks on mutating authenticated routes.
- Server-side minimum role checks, input bounds, body-size limit, parameterized SQL, tenant-scoped lookups, and same-origin API design.
- Security response headers and formula-injection prefixing for untrusted CSV text fields.
- Audit events for authentication and important product, supplier, inventory, and PO actions.

## Required before network or production exposure

- Run behind a maintained production WSGI server and TLS reverse proxy; `wsgiref` is a local development server.
- Enable Secure cookies and set restrictive network/database file permissions.
- Implement login throttling, password reset and recovery, MFA, granular permissions, and a security incident process.
- Use a managed relational database with a reviewed forward/rollback migration strategy and tested backups.
- Add dependency/build scanning, rate limits, monitoring, log retention/redaction review, accessibility and browser automation, and independent security testing.
- Validate the authorization model, tenant isolation, concurrent stock writes, and backup restoration in a production-like environment.

## Operational limitations

The process has no background workers, scheduled evaluations, alert delivery, health/readiness endpoints, structured request correlation, metrics, or deployment pipeline. SQLite WAL and transaction locking support local use but do not replace multi-user production database operations. There is no documented retention automation for sessions, audit data, or exports.

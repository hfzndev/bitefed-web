"""Database connection — shared SQLite with nutrition-bot."""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "nutrition-bot", "data", "nutrition.db")
DB_PATH = os.path.abspath(DB_PATH)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_all_tables():
    """Create all tables if not exist."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            order_code    TEXT UNIQUE NOT NULL,
            telegram_id   INTEGER NOT NULL,
            plan_type     TEXT CHECK(plan_type IN ('trial','monthly','yearly')) NOT NULL,
            amount        INTEGER NOT NULL,
            status        TEXT DEFAULT 'pending' CHECK(status IN ('pending','paid','expired','cancelled')),
            created_at    TEXT DEFAULT (datetime('now')),
            paid_at       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_orders_code ON orders(order_code);
        CREATE INDEX IF NOT EXISTS idx_orders_telegram ON orders(telegram_id);

        CREATE TABLE IF NOT EXISTS subscriptions (
            telegram_id   INTEGER PRIMARY KEY,
            plan_type     TEXT CHECK(plan_type IN ('trial','monthly','yearly')) NOT NULL,
            status        TEXT DEFAULT 'active' CHECK(status IN ('active','expired','cancelled')),
            started_at    TEXT DEFAULT (datetime('now')),
            expires_at    TEXT NOT NULL,
            trial_used    INTEGER DEFAULT 0,
            created_at    TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()


# Backward compat
init_orders_table = init_all_tables


def create_order(telegram_id: int, plan_type: str, amount: int) -> str:
    """Create a new order, returns order_code."""
    import secrets
    code = f"BITE-{secrets.token_hex(3).upper()}"
    conn = get_db()
    conn.execute(
        "INSERT INTO orders (order_code, telegram_id, plan_type, amount) VALUES (?, ?, ?, ?)",
        (code, telegram_id, plan_type, amount),
    )
    conn.commit()
    conn.close()
    return code


def get_order(order_code: str) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM orders WHERE order_code = ?", (order_code,)).fetchone()
    conn.close()
    return dict(row) if row else None


DURATIONS = {"trial": 3, "monthly": 30, "yearly": 365}


def activate_subscription(order_code: str) -> dict | None:
    """Activate subscription after payment. Returns subscription info or None."""
    conn = get_db()

    # Get order
    order = conn.execute(
        "SELECT * FROM orders WHERE order_code = ? AND status = 'pending'",
        (order_code,),
    ).fetchone()

    if not order:
        conn.close()
        return None

    # Mark order as paid
    conn.execute(
        "UPDATE orders SET status = 'paid', paid_at = datetime('now') WHERE order_code = ?",
        (order_code,),
    )

    # Calculate expiry
    days = DURATIONS[order["plan_type"]]

    # Insert or update subscription
    conn.execute("""
        INSERT INTO subscriptions (telegram_id, plan_type, status, expires_at)
        VALUES (?, ?, 'active', datetime('now', '+' || ? || ' days'))
        ON CONFLICT(telegram_id) DO UPDATE SET
            plan_type = excluded.plan_type,
            status = 'active',
            started_at = datetime('now'),
            expires_at = datetime('now', '+' || ? || ' days')
    """, (order["telegram_id"], order["plan_type"], days, days))

    # Also update user in nutrition-bot users table (if exists)
    conn.execute("""
        UPDATE users SET is_active = 1, access_type = 'permanent'
        WHERE telegram_id = ?
    """, (order["telegram_id"],))

    conn.commit()

    sub = conn.execute(
        "SELECT * FROM subscriptions WHERE telegram_id = ?",
        (order["telegram_id"],),
    ).fetchone()
    conn.close()

    return dict(sub)

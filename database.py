"""Database connection — shared SQLite with nutrition-bot.

Now writes directly to the same `subscription_payments` table that the bot uses,
so there is a single source of truth for payment tracking.
"""
import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "nutrition-bot", "data", "nutrition.db")
DB_PATH = os.path.abspath(DB_PATH)

# ── Plan config (mirrors bot's Database.PLAN_DURATIONS) ──────────
DURATIONS = {"trial": 3, "monthly": 30, "yearly": 365}
PLAN_PRICES = {"trial": 0, "monthly": 25000, "yearly": 250000}


def get_db() -> sqlite3.Connection:
    """Return a connection to the shared SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_all_tables():
    """Ensure the shared tables exist.

    The main tables (users, subscription_payments, etc.) are created by
    nutrition-bot's schema.py / db.py. Here we only add tables that are
    specific to the web front-end (OTP verification).
    """
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS otp_codes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id   INTEGER NOT NULL,
            code          TEXT NOT NULL,
            created_at    TEXT DEFAULT (datetime('now', '+7 hours')),
            used          INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_otp_telegram ON otp_codes(telegram_id);

        CREATE TABLE IF NOT EXISTS sessions (
            token         TEXT PRIMARY KEY,
            telegram_id   INTEGER NOT NULL,
            created_at    TEXT DEFAULT (datetime('now', '+7 hours')),
            expires_at    TEXT DEFAULT (datetime('now', '+7 days', '+7 hours'))
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_telegram ON sessions(telegram_id);
    """)
    conn.commit()
    conn.close()


# Backward compat
init_orders_table = init_all_tables


# ── Payment tracking (unified with bot's subscription_payments) ──


def create_order(telegram_id: int, plan_type: str, amount: int) -> str:
    """Create a new order by writing directly to subscription_payments.

    Returns the generated order_code (also stored as telegram_payment_charge_id).
    """
    import secrets
    code = f"BITE-{secrets.token_hex(3).upper()}"

    conn = get_db()
    conn.execute(
        """INSERT INTO subscription_payments
           (telegram_id, plan_type, amount, currency,
            telegram_payment_charge_id, provider_payment_charge_id, status)
           VALUES (?, ?, ?, 'IDR', ?, 'midtrans', 'pending')""",
        (telegram_id, plan_type, amount, code),
    )
    conn.commit()
    conn.close()
    return code


def get_order(order_code: str) -> dict | None:
    """Look up a payment by its order code (stored in telegram_payment_charge_id)."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM subscription_payments WHERE telegram_payment_charge_id = ?",
        (order_code,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def activate_subscription(order_code: str) -> dict | None:
    """Activate a subscription after successful Midtrans payment.

    1. Marks the subscription_payments row as 'completed'.
    2. Grants user access in the users table (mirrors bot's grant_access logic).
    """
    conn = get_db()

    # Get payment row
    payment = conn.execute(
        """SELECT * FROM subscription_payments
           WHERE telegram_payment_charge_id = ? AND status = 'pending'""",
        (order_code,),
    ).fetchone()

    if not payment:
        conn.close()
        return None

    telegram_id = payment["telegram_id"]
    plan_type = payment["plan_type"]
    days = DURATIONS.get(plan_type, 30)

    # Prevent duplicate trial activation (allow webhook replay for paid plans)
    if plan_type == "trial":
        existing = conn.execute(
            "SELECT trial_used FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if existing and existing["trial_used"]:
            # Trial already used — still mark payment completed but don't re-grant
            conn.execute(
                """UPDATE subscription_payments SET
                   status = 'completed',
                   completed_at = datetime('now', '+7 hours')
                   WHERE telegram_payment_charge_id = ?""",
                (order_code,),
            )
            conn.commit()
            sub = conn.execute(
                "SELECT * FROM subscription_payments WHERE telegram_payment_charge_id = ?",
                (order_code,),
            ).fetchone()
            conn.close()
            return dict(sub)

    # Calculate expiry — same logic as bot's mark_payment_completed
    now_wib = datetime.utcnow() + timedelta(hours=7)
    expires_at = (now_wib + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")

    # Mark payment as completed
    conn.execute(
        """UPDATE subscription_payments SET
           status = 'completed',
           completed_at = datetime('now', '+7 hours')
           WHERE telegram_payment_charge_id = ?""",
        (order_code,),
    )

    # Grant access in users table (same as bot's Database.grant_access for non-trial)
    # Ensure user record exists (might not if they haven't /start the bot yet)
    conn.execute(
        "INSERT OR IGNORE INTO users (telegram_id) VALUES (?)",
        (telegram_id,),
    )

    if plan_type == "trial":
        conn.execute(
            """UPDATE users SET
               is_active = 1,
               access_type = 'temp',
               access_expires_at = ?,
               trial_used = 1,
               updated_at = datetime('now', '+7 hours')
               WHERE telegram_id = ?""",
            (expires_at, telegram_id),
        )
    else:
        conn.execute(
            """UPDATE users SET
               is_active = 1,
               access_type = 'permanent',
               access_expires_at = ?,
               updated_at = datetime('now', '+7 hours')
               WHERE telegram_id = ?""",
            (expires_at, telegram_id),
        )

    conn.commit()

    # Return subscription info
    sub = conn.execute(
        """SELECT * FROM subscription_payments
           WHERE telegram_payment_charge_id = ?""",
        (order_code,),
    ).fetchone()
    conn.close()

    return dict(sub) if sub else None


# ── OTP verification ──────────────────────────────────────────


def store_otp(telegram_id: int, code: str):
    """Store a new OTP code. All previous OTPs for this user are left intact
    but only the most recent unused one will be valid."""
    conn = get_db()
    conn.execute(
        "INSERT INTO otp_codes (telegram_id, code) VALUES (?, ?)",
        (telegram_id, code),
    )
    conn.commit()
    conn.close()


def verify_otp(telegram_id: int, code: str) -> bool:
    """Check if the OTP is valid (correct code, not expired, not yet used).

    OTPs expire after 5 minutes.
    """
    conn = get_db()
    row = conn.execute(
        """SELECT id, code, created_at, used FROM otp_codes
           WHERE telegram_id = ?
             AND used = 0
             AND datetime(created_at, '+5 minutes') > datetime('now', '+7 hours')
           ORDER BY created_at DESC LIMIT 1""",
        (telegram_id,),
    ).fetchone()

    if not row or row["code"] != code:
        conn.close()
        return False

    # Mark as used
    conn.execute("UPDATE otp_codes SET used = 1 WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    return True


# ── Dashboard queries ────────────────────────────────────────


def get_subscription_status(telegram_id: int) -> dict:
    """Get subscription info from users table (bot-managed)."""
    conn = get_db()
    user = conn.execute(
        "SELECT is_active, access_type, access_expires_at, trial_used FROM users WHERE telegram_id = ?",
        (telegram_id,),
    ).fetchone()
    conn.close()

    if not user:
        return {"active": False, "plan": None, "expires": None, "days_left": 0, "trial_used": False}

    days_left = 0
    if user["access_expires_at"]:
        now = datetime.utcnow() + timedelta(hours=7)
        try:
            expires = datetime.fromisoformat(user["access_expires_at"])
            days_left = max(0, (expires - now).days)
        except (ValueError, TypeError):
            pass

    plan_label = {"temp": "Trial", "permanent": "Berbayar"}.get(user["access_type"], "Unknown")

    return {
        "active": bool(user["is_active"]),
        "plan": user["access_type"],
        "plan_label": plan_label,
        "expires": user["access_expires_at"],
        "days_left": days_left,
        "trial_used": bool(user["trial_used"]),
    }


def get_today_nutrition(telegram_id: int) -> dict:
    """Get today's total macros from food_log (WIB timezone)."""
    conn = get_db()
    row = conn.execute("""
        SELECT COALESCE(SUM(calories), 0) as calories,
               COALESCE(SUM(protein_g), 0) as protein,
               COALESCE(SUM(carbs_g), 0) as carbs,
               COALESCE(SUM(fat_g), 0) as fat,
               COUNT(*) as total_logs
        FROM food_log
        WHERE telegram_id = ?
          AND date(logged_at, '+7 hours') = date('now', '+7 hours')
    """, (telegram_id,)).fetchone()
    conn.close()
    return dict(row) if row else {"calories": 0, "protein": 0, "carbs": 0, "fat": 0, "total_logs": 0}


def get_user_stats(telegram_id: int) -> dict:
    """Get streak, total logs, and daily average calories."""
    conn = get_db()

    streak = conn.execute(
        "SELECT COUNT(DISTINCT date(logged_at, '+7 hours')) as streak_days FROM food_log WHERE telegram_id = ?",
        (telegram_id,),
    ).fetchone()

    total = conn.execute(
        "SELECT COUNT(*) as total FROM food_log WHERE telegram_id = ?",
        (telegram_id,),
    ).fetchone()

    avg = conn.execute("""
        SELECT ROUND(AVG(daily_cal), 0) as avg_daily
        FROM (
            SELECT date(logged_at, '+7 hours') as d, SUM(calories) as daily_cal
            FROM food_log
            WHERE telegram_id = ?
              AND logged_at > datetime('now', '-30 days', '+7 hours')
            GROUP BY d
        )
    """, (telegram_id,)).fetchone()

    conn.close()
    return {
        "streak": streak["streak_days"] if streak else 0,
        "total_logs": total["total"] if total else 0,
        "avg_daily": int(avg["avg_daily"]) if avg and avg["avg_daily"] else 0,
    }


def get_order_history(telegram_id: int) -> list[dict]:
    """Get all orders from subscription_payments, newest first."""
    conn = get_db()
    rows = conn.execute(
        """SELECT telegram_payment_charge_id as order_code,
                  plan_type, amount, currency, status,
                  created_at, completed_at
           FROM subscription_payments
           WHERE telegram_id = ?
           ORDER BY created_at DESC""",
        (telegram_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Session management (SQLite-backed) ─────────────────────


def create_db_session(telegram_id: int) -> str:
    """Create a persistent session token, returns the token string."""
    import secrets
    token = secrets.token_urlsafe(32)
    conn = get_db()
    conn.execute(
        "INSERT INTO sessions (token, telegram_id) VALUES (?, ?)",
        (token, telegram_id),
    )
    conn.commit()
    conn.close()
    return token


def get_session_telegram_id(token: str) -> int | None:
    """Look up a session token, returns telegram_id or None."""
    if not token:
        return None
    conn = get_db()
    row = conn.execute(
        "SELECT telegram_id FROM sessions WHERE token = ? AND expires_at > datetime('now', '+7 hours')",
        (token,),
    ).fetchone()
    conn.close()
    return row["telegram_id"] if row else None


def delete_session(token: str) -> None:
    """Delete a session (logout)."""
    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()

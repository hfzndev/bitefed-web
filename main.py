"""Bite Fed Web — Landing Page + Subscription Backend + Midtrans."""
import json
import os
import asyncio
import secrets
import requests
from functools import wraps
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from database import (
    init_all_tables, create_order, activate_subscription,
    store_otp, verify_otp,
    get_subscription_status, get_today_nutrition, get_user_stats, get_order_history,
    create_db_session, get_session_telegram_id, delete_session,
)
from models import CreateOrderRequest, RequestOtpRequest, VerifyOtpRequest, PLAN_CONFIG
from midtrans_client import create_snap_token, verify_webhook, CLIENT_KEY

# ── Load config ────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    # Load this project's .env first, then bot's .env for fallback
    load_dotenv()
    # Also try loading from nutrition-bot/.env for shared credentials
    bot_env = os.path.join(os.path.dirname(__file__), "..", "nutrition-bot", ".env")
    if os.path.exists(bot_env):
        load_dotenv(bot_env, override=False)
except ImportError:
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", "0"))
REGISTER_TOPIC_ID = int(os.getenv("REGISTER_TOPIC_ID", "0"))
DEPLOY_SECRET = os.getenv("DEPLOY_SECRET", secrets.token_urlsafe(32))

app = FastAPI(title="Bite Fed Web", version="2.5.0")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ── Session helpers (DB-backed, survives restarts) ──

def get_current_user(request: Request) -> int | None:
    """Extract telegram_id from session cookie (DB query)."""
    token = request.cookies.get("bitefed_session")
    if not token:
        return None
    return get_session_telegram_id(token)


def login_required(f):
    """Decorator: redirect to landing if not authenticated."""
    import inspect
    is_async = inspect.iscoroutinefunction(f)

    if is_async:
        @wraps(f)
        async def wrapper(request: Request, *args, **kwargs):
            if get_current_user(request) is None:
                return RedirectResponse("/?auth=required", status_code=302)
            return await f(request, *args, **kwargs)
    else:
        @wraps(f)
        def wrapper(request: Request, *args, **kwargs):
            if get_current_user(request) is None:
                return RedirectResponse("/?auth=required", status_code=302)
            return f(request, *args, **kwargs)

    return wrapper


# ── Middleware: inject user state into every request ──

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Attach telegram_id to request.state for all routes."""
    request.state.telegram_id = get_current_user(request)
    response = await call_next(request)
    return response


@app.on_event("startup")
def startup():
    init_all_tables()


# ── Telegram notification helpers ──────────────────────────

def _send_telegram_message(chat_id: int, text: str, message_thread_id: int | None = None) -> dict | None:
    """Send a message via Telegram Bot API (sync, uses requests)."""
    if not BOT_TOKEN:
        print("[notify] BOT_TOKEN not set — skipping notification")
        return None
    try:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if message_thread_id:
            payload["message_thread_id"] = message_thread_id
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=10,
        )
        return resp.json()
    except Exception as e:
        print(f"[notify] Failed to send message to {chat_id}: {e}")
        return None


async def notify_user(telegram_id: int, plan_type: str, order_code: str):
    """Send subscription-activated notification to the user (async wrapper)."""
    plan_label = PLAN_CONFIG.get(plan_type, {}).get("label", plan_type)
    duration = PLAN_CONFIG.get(plan_type, {}).get("duration", "N/A")

    text = (
        f"🎉 <b>Pembayaran Berhasil!</b>\n\n"
        f"📦 Plan: {plan_label}\n"
        f"📅 Durasi: {duration}\n"
        f"🧾 Order: <code>{order_code}</code>\n\n"
        f"Langganan kamu sudah aktif. Ketik /start di bot untuk mulai onboarding! 🚀"
    )
    await asyncio.to_thread(_send_telegram_message, telegram_id, text)


async def notify_admin(telegram_id: int, plan_type: str, amount: int, order_code: str):
    """Send new-subscription notification to admin group (async wrapper)."""
    if not ADMIN_GROUP_ID:
        return
    plan_label = PLAN_CONFIG.get(plan_type, {}).get("label", plan_type)
    emoji = {"trial": "🧪", "monthly": "📅", "yearly": "🚀"}.get(plan_type, "💳")

    text = (
        f"{emoji} <b>Langganan Baru (Web)!</b>\n\n"
        f"Telegram ID: <code>{telegram_id}</code>\n"
        f"Plan: <b>{plan_label}</b>\n"
        f"Jumlah: <b>Rp {amount:,}</b>\n"
        f"Order: <code>{order_code}</code>"
    )
    thread_id = REGISTER_TOPIC_ID if REGISTER_TOPIC_ID else None
    await asyncio.to_thread(_send_telegram_message, ADMIN_GROUP_ID, text, thread_id)


# ── Pages ──────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def landing_page(request: Request):
    return templates.TemplateResponse("landing.html", {
        "request": request,
        "plans": PLAN_CONFIG,
        "midtrans_client_key": CLIENT_KEY,
        "bot_username": os.getenv("BOT_USERNAME", "normGizi_bot"),
        "user": request.state.telegram_id,
    })


# ── OTP API ────────────────────────────────────────

@app.post("/api/request-otp")
async def api_request_otp(data: RequestOtpRequest):
    """Generate a 6-digit OTP, store it, and send it to the user via Telegram."""
    telegram_id = data.telegram_id

    # Generate OTP
    otp = f"{secrets.randbelow(1000000):06d}"

    # Store in shared DB
    store_otp(telegram_id, otp)

    # Send via Telegram
    text = (
        f"🔐 <b>Kode Verifikasi Bite Fed</b>\n\n"
        f"OTP: <code>{otp}</code>\n\n"
        f"Gunakan kode ini di halaman web untuk verifikasi. "
        f"Kode berlaku 5 menit."
    )
    await asyncio.to_thread(_send_telegram_message, telegram_id, text)

    return {"status": "ok", "message": "OTP sent to your Telegram"}


@app.post("/api/verify-otp")
def api_verify_otp(data: VerifyOtpRequest, request: Request):
    """Verify the OTP. Sets session cookie and returns redirect URL."""
    if verify_otp(data.telegram_id, data.otp):
        token = create_db_session(data.telegram_id)
        resp = JSONResponse({
            "status": "ok", "verified": True,
            "message": "OTP valid",
            "redirect": "/dashboard",
        })
        resp.set_cookie(
            key="bitefed_session", value=token,
            httponly=True, secure=True, samesite="lax",
            max_age=86400 * 7,
        )
        return resp
    return JSONResponse(
        status_code=400,
        content={"status": "error", "verified": False, "message": "OTP tidak valid atau sudah kadaluarsa"},
    )


# ── Dashboard ──────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
@login_required
def dashboard_page(request: Request):
    telegram_id = request.state.telegram_id
    sub = get_subscription_status(telegram_id)
    nutrition = get_today_nutrition(telegram_id)
    stats = get_user_stats(telegram_id)
    orders = get_order_history(telegram_id)
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "telegram_id": telegram_id,
        "bot_username": os.getenv("BOT_USERNAME", "normGizi_bot"),
        "subscription": sub,
        "nutrition": nutrition,
        "stats": stats,
        "orders": orders,
        "midtrans_client_key": CLIENT_KEY,
    })


# ── Payment retry ─────────────────────────────────

@app.post("/api/order/{order_code}/retry")
def api_retry_payment(order_code: str, request: Request):
    """Retry payment for a pending order — regenerate Snap token."""
    telegram_id = request.state.telegram_id
    if not telegram_id:
        raise HTTPException(status_code=401)

    order = get_order(order_code)
    if not order or order.get("telegram_id") != telegram_id:
        raise HTTPException(status_code=404, detail="Order not found")

    if order["status"] not in ("pending",):
        return {"status": "error", "message": "Order sudah diproses"}

    plan_type = order["plan_type"]
    amount = order["amount"]
    plan_label = PLAN_CONFIG.get(plan_type, {}).get("label", plan_type)

    try:
        snap = create_snap_token(order_code, plan_label, amount, telegram_id)
        return {
            "status": "ok",
            "snap_token": snap["snap_token"],
            "redirect_url": snap["redirect_url"],
        }
    except Exception:
        return {"status": "error", "message": "Payment gateway belum tersedia"}


# ── Logout ────────────────────────────────────────

@app.get("/logout")
def logout(request: Request):
    token = request.cookies.get("bitefed_session", "")
    if token:
        delete_session(token)
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie("bitefed_session")
    return resp


# ── Deploy webhook ─────────────────────────────────

@app.post("/api/deploy")
async def deploy_webhook(request: Request):
    """GitHub-style deploy webhook. Pull + restart service."""
    body = await request.body()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    token = request.headers.get("X-Deploy-Token", "")
    if not secrets.compare_digest(token, DEPLOY_SECRET):
        raise HTTPException(status_code=403, detail="Invalid deploy token")

    import subprocess
    result = subprocess.run(
        ["git", "-C", "/home/hfzndev/normgizi-web", "pull", "origin", "main"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        subprocess.run(["sudo", "systemctl", "restart", "bitefed-web"], timeout=10)
        return {"status": "deployed", "git": result.stdout.strip()}
    return {"status": "error", "git": result.stderr.strip()}


# ── Order API ──────────────────────────────────────

@app.post("/api/order/create")
async def api_create_order(data: CreateOrderRequest):
    plan = PLAN_CONFIG[data.plan_type]
    code = create_order(data.telegram_id, data.plan_type, plan["amount"])

    # If amount is 0 (trial), skip Midtrans and activate directly
    if plan["amount"] == 0:
        sub = activate_subscription(code)
        # Notify user and admin
        await notify_user(data.telegram_id, data.plan_type, code)
        await notify_admin(data.telegram_id, data.plan_type, 0, code)
        return {
            "order_code": code,
            "plan_type": data.plan_type,
            "amount": 0,
            "status": "active",
            "message": f"Trial 3 hari aktif! Gunakan Telegram ID {data.telegram_id} di bot.",
            "snap_token": None,
        }

    # Create Midtrans Snap transaction
    try:
        snap = create_snap_token(code, plan["label"], plan["amount"], data.telegram_id)
        return {
            "order_code": code,
            "plan_type": data.plan_type,
            "amount": plan["amount"],
            "snap_token": snap["snap_token"],
            "redirect_url": snap["redirect_url"],
            "message": f"Order {code} dibuat. Silakan selesaikan pembayaran.",
        }
    except Exception as e:
        # Midtrans not configured yet — fallback with manual payment info
        return {
            "order_code": code,
            "plan_type": data.plan_type,
            "amount": plan["amount"],
            "snap_token": None,
            "message": (
                f"⚠️ Payment gateway belum dikonfigurasi. "
                f"Order {code} tersimpan. Admin akan mengaktifkan manual."
            ),
        }


@app.post("/api/webhooks/midtrans")
async def midtrans_webhook(request: Request):
    """Receive Midtrans payment notification."""
    body = await request.body()
    data = json.loads(body)

    # Verify signature
    if not verify_webhook(data):
        raise HTTPException(status_code=403, detail="Invalid signature")

    order_id = data.get("order_id", "")
    transaction_status = data.get("transaction_status", "")
    fraud_status = data.get("fraud_status", "accept")

    # Only activate on successful payment
    if transaction_status in ("capture", "settlement"):
        if fraud_status == "accept":
            sub = activate_subscription(order_id)
            if sub:
                # Send notifications
                await notify_user(
                    sub["telegram_id"],
                    sub["plan_type"],
                    order_id,
                )
                await notify_admin(
                    sub["telegram_id"],
                    sub["plan_type"],
                    sub["amount"],
                    order_id,
                )
                return {"status": "ok", "message": f"Subscription activated for {order_id}"}

    # Log other statuses (pending, deny, expire, etc.)
    print(f"[Webhook] {order_id}: {transaction_status} (fraud: {fraud_status})")
    return {"status": "received"}


# ── Run ────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

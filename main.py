"""Bite Fed Web — Landing Page + Subscription Backend + Midtrans."""
import json
import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from database import init_all_tables, create_order, activate_subscription
from models import CreateOrderRequest, PLAN_CONFIG
from midtrans_client import create_snap_token, verify_webhook, CLIENT_KEY

app = FastAPI(title="Bite Fed Web", version="2.1.0")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
def startup():
    init_all_tables()


# ── Pages ──────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def landing_page(request: Request):
    return templates.TemplateResponse("landing.html", {
        "request": request,
        "plans": PLAN_CONFIG,
        "midtrans_client_key": CLIENT_KEY,
        "bot_username": os.getenv("BOT_USERNAME", "normGizi_bot"),
    })


# ── API ────────────────────────────────────────────

@app.post("/api/order/create")
def api_create_order(data: CreateOrderRequest):
    plan = PLAN_CONFIG[data.plan_type]
    code = create_order(data.telegram_id, data.plan_type, plan["amount"])

    # If amount is 0 (trial), skip Midtrans and activate directly
    if plan["amount"] == 0:
        sub = activate_subscription(code)
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
                return {"status": "ok", "message": f"Subscription activated for {order_id}"}

    # Log other statuses (pending, deny, expire, etc.)
    print(f"[Webhook] {order_id}: {transaction_status} (fraud: {fraud_status})")
    return {"status": "received"}


# ── Run ────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

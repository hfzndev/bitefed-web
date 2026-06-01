"""Midtrans Snap integration — create transaction + verify webhook."""
import hashlib
import json
import os
from midtransclient import Snap, CoreApi

from dotenv import load_dotenv
load_dotenv()

SERVER_KEY = os.getenv("MIDTRANS_SERVER_KEY")
CLIENT_KEY = os.getenv("MIDTRANS_CLIENT_KEY")
IS_PRODUCTION = os.getenv("MIDTRANS_IS_PRODUCTION", "false").lower() == "true"


def create_snap_token(order_code: str, plan_label: str, amount: int, telegram_id: int) -> dict:
    """Create Midtrans Snap transaction, returns snap_token + redirect_url."""
    snap = Snap(
        is_production=IS_PRODUCTION,
        server_key=SERVER_KEY,
        client_key=CLIENT_KEY,
    )

    param = {
        "transaction_details": {
            "order_id": order_code,
            "gross_amount": amount,
        },
        "item_details": [{
            "id": order_code,
            "price": amount,
            "quantity": 1,
            "name": f"Bite Fed - {plan_label}",
        }],
        "customer_details": {
            "first_name": f"User {telegram_id}",
            "phone": str(telegram_id),  # Midtrans requires phone, use telegram_id as placeholder
        },
        "enabled_payments": [
            "gopay", "shopeepay", "qris", "bank_transfer",
            "bca_va", "bni_va", "bri_va", "mandiri_va",
            "cstore", "akulaku", "bca_klikpay", "bca_klikbca",
        ],
    }

    transaction = snap.create_transaction(param)
    return {
        "snap_token": transaction.get("token"),
        "redirect_url": transaction.get("redirect_url"),
    }


def verify_webhook(data: dict) -> bool:
    """Verify Midtrans webhook signature. Returns True if valid."""
    order_id = data.get("order_id", "")
    status_code = str(data.get("status_code", ""))
    gross_amount = str(data.get("gross_amount", ""))

    signature_key = hashlib.sha512(
        f"{order_id}{status_code}{gross_amount}{SERVER_KEY}".encode()
    ).hexdigest()

    return signature_key == data.get("signature_key", "")

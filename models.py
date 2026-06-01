from pydantic import BaseModel, Field


class CreateOrderRequest(BaseModel):
    telegram_id: int = Field(..., gt=0, description="Telegram user ID")
    plan_type: str = Field(..., pattern=r"^(trial|monthly|yearly)$")


class CreateOrderResponse(BaseModel):
    order_code: str
    plan_type: str
    amount: int
    message: str


PLAN_CONFIG = {
    "trial": {
        "amount": 0,
        "label": "Trial 3 Hari",
        "duration": "3 hari",
        "price_display": "Gratis",
        "period": "3 hari",
        "badge": "Pemula",
        "badge_class": "bg-brand-100 text-brand-700",
        "card_class": "border-brand-200 bg-brand-50/50",
        "features": [
            "Semua fitur",
            "AI foto makanan",
            "Insight harian",
        ],
        "cta": "Coba Gratis",
    },
    "monthly": {
        "amount": 25000,
        "label": "Bulanan",
        "duration": "30 hari",
        "price_display": "Rp 25rb",
        "period": "/ bulan",
        "badge": "Populer",
        "badge_class": "bg-brand-100 text-brand-700",
        "card_class": "border-brand-300 bg-white",
        "features": [
            "Semua fitur Trial",
            "Akses tanpa batas",
            "Prioritas support",
        ],
        "cta": "Langganan Bulanan",
    },
    "yearly": {
        "amount": 250000,
        "label": "Tahunan",
        "duration": "365 hari",
        "price_display": "Rp 250rb",
        "period": "/ tahun",
        "badge": "Hemat 17%",
        "badge_class": "bg-brand-100 text-brand-700",
        "card_class": "border-brand-300 bg-white",
        "features": [
            "Semua fitur Bulanan",
            "Hemat Rp 50rb",
            "Early access fitur baru",
        ],
        "cta": "Langganan Tahunan",
    },
}

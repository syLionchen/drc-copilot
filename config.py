# Loads configuration from .env (via python-dotenv).
# Exposes PayPal sandbox credentials, demo account emails, and LLM provider
# settings to the rest of the app. Never hardcode secrets here.
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET = os.getenv("PAYPAL_SECRET", "")
PAYPAL_BASE_URL = os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com").rstrip("/")

MERCHANT_EMAIL = os.getenv("MERCHANT_EMAIL", "")
ESCROW_EMAIL = os.getenv("ESCROW_EMAIL", "")
CONSUMER_A_EMAIL = os.getenv("CONSUMER_A_EMAIL", "")
CONSUMER_B_EMAIL = os.getenv("CONSUMER_B_EMAIL", "")

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

CURRENCY = "EUR"

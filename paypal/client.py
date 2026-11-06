# PayPal REST API client: OAuth2 token fetch + caching (auto-refresh before
# expiry), shared request helper with error handling (print debug_id) and
# retries. All other paypal/* modules call through this client.
import threading
import time

import requests

import config

# Refresh the token this many seconds before PayPal says it expires.
TOKEN_REFRESH_MARGIN = 60
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_STATUS = {429, 500, 502, 503, 504}

_token = None
_token_expires_at = 0.0  # monotonic time
_lock = threading.Lock()


class PayPalError(Exception):
    """Raised when a PayPal API call fails. Carries status code and debug_id."""

    def __init__(self, message, status_code=None, debug_id=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.debug_id = debug_id
        self.body = body


def _extract_debug_id(resp):
    debug_id = resp.headers.get("Paypal-Debug-Id")
    if debug_id:
        return debug_id
    try:
        return resp.json().get("debug_id")
    except ValueError:
        return None


def _fail(resp, what):
    debug_id = _extract_debug_id(resp)
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    print(f"[PayPal ERROR] {what} -> HTTP {resp.status_code} | debug_id={debug_id}")
    print(f"[PayPal ERROR] response body: {body}")
    raise PayPalError(
        f"{what} failed with HTTP {resp.status_code} (debug_id={debug_id})",
        status_code=resp.status_code,
        debug_id=debug_id,
        body=body,
    )


def _fetch_token():
    global _token, _token_expires_at
    if not config.PAYPAL_CLIENT_ID or not config.PAYPAL_SECRET:
        raise PayPalError("PAYPAL_CLIENT_ID / PAYPAL_SECRET are not set in .env")
    resp = requests.post(
        f"{config.PAYPAL_BASE_URL}/v1/oauth2/token",
        auth=(config.PAYPAL_CLIENT_ID, config.PAYPAL_SECRET),
        data={"grant_type": "client_credentials"},
        headers={"Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    if not resp.ok:
        _fail(resp, "Token request")
    data = resp.json()
    _token = data["access_token"]
    _token_expires_at = time.monotonic() + int(data.get("expires_in", 300))


def get_access_token(force_refresh=False):
    """Return a valid access token; fetch a new one if missing or about to expire."""
    with _lock:
        if (
            force_refresh
            or _token is None
            or time.monotonic() >= _token_expires_at - TOKEN_REFRESH_MARGIN
        ):
            _fetch_token()
        return _token


def request(method, path, json=None, params=None, headers=None):
    """Call a PayPal endpoint and return the parsed JSON (or {} for empty bodies).

    - Adds the Bearer token; on HTTP 401 refreshes the token once and retries.
    - Retries connection errors / 429 / 5xx with exponential backoff.
    - On final failure prints debug_id and raises PayPalError.
    """
    url = f"{config.PAYPAL_BASE_URL}{path}"
    refreshed = False
    last_error = None

    for attempt in range(MAX_RETRIES + 1):
        req_headers = {
            "Authorization": f"Bearer {get_access_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if headers:
            req_headers.update(headers)

        try:
            resp = requests.request(
                method, url, json=json, params=params,
                headers=req_headers, timeout=REQUEST_TIMEOUT,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            print(f"[PayPal ERROR] {method} {path} network failure: {exc}")
            raise PayPalError(f"{method} {path} network failure: {exc}") from exc

        if resp.status_code == 401 and not refreshed:
            refreshed = True
            get_access_token(force_refresh=True)
            continue

        if resp.status_code in RETRY_STATUS and attempt < MAX_RETRIES:
            time.sleep(2 ** attempt)
            continue

        if not resp.ok:
            _fail(resp, f"{method} {path}")

        if not resp.content:
            return {}
        return resp.json()

    # Only reachable if every attempt was consumed by retries.
    raise PayPalError(f"{method} {path} failed after retries: {last_error}")

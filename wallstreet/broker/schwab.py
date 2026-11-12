"""Charles Schwab Trader API connector: MARKET DATA AND ACCOUNT READS ONLY.

Status: written against Schwab's published OAuth flow and endpoint paths, NOT tested against a live Schwab account
(none exists for this project yet). It is off unless SCHWAB_APP_KEY and SCHWAB_APP_SECRET are set.

There is deliberately no method that places, previews, replaces or cancels orders. Adding one would be a code change
anyone can see in review, not a setting someone can flip.

How Schwab access works (individual developer):
  1. Register at developer.schwab.com, create an app with the "Market Data Production" (and optionally
     "Accounts and Trading Production") products, callback URL e.g. https://127.0.0.1. Approval takes a few days.
  2. Run `python -m wallstreet.broker.schwab login`: it prints a Schwab login URL; you sign in on Schwab's own site and
     paste back the URL you were redirected to. Your Schwab password never touches this program.
  3. Access tokens last 30 minutes and refresh automatically. The refresh token expires after 7 days, so step 2 must be
     repeated weekly (a Schwab rule, not ours).
Tokens are stored in var/schwab_tokens.json with owner-only file permissions and are never logged.
"""
import base64
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import config

AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
MD = "https://api.schwabapi.com/marketdata/v1"
TRADER = "https://api.schwabapi.com/trader/v1"
TOKEN_FILE = config.DATA_DIR / "schwab_tokens.json"


class SchwabDisabled(RuntimeError):
    pass


def _creds():
    key, secret = os.environ.get("SCHWAB_APP_KEY"), os.environ.get("SCHWAB_APP_SECRET")
    if not key or not secret:
        raise SchwabDisabled("Schwab is not configured (set SCHWAB_APP_KEY and SCHWAB_APP_SECRET in .env)")
    return key, secret, os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1")


STATE_FILE = config.DATA_DIR / "schwab_oauth_state"


def login_url():
    """Login link with a one-time random `state`, checked on return so a forged redirect can't plant someone else's login."""
    key, _, cb = _creds()
    state = secrets.token_urlsafe(24)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(STATE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(state)
    return AUTH_URL + "?" + urllib.parse.urlencode({"client_id": key, "redirect_uri": cb, "state": state})


def _token_request(data):
    key, secret, _ = _creds()
    basic = base64.b64encode(("%s:%s" % (key, secret)).encode()).decode()
    req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(data).encode(),
                                 headers={"Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            tok = json.loads(r.read())
    except urllib.error.HTTPError as e:                  # never echo the response body: it can contain token material
        raise SchwabDisabled("Schwab token request failed (HTTP %s). Log in again." % e.code) from None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise SchwabDisabled("Schwab token request failed: %s" % type(e).__name__) from None
    tok["obtained_at"] = int(time.time())
    if "refresh_token" in tok:
        tok["refresh_obtained_at"] = tok["obtained_at"]
    return tok


def _save(tok):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()                                     # O_CREAT keeps an old file's permissions; start fresh
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tok, f)
    os.replace(tmp, TOKEN_FILE)


def _load():
    if not TOKEN_FILE.exists():
        raise SchwabDisabled("not logged in to Schwab: run python -m wallstreet.broker.schwab login")
    return json.loads(TOKEN_FILE.read_text())


def finish_login(redirected_url):
    _, _, cb = _creds()
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(redirected_url).query)
    code = qs.get("code", [None])[0]
    if not code:
        raise SchwabDisabled("no ?code= in that URL")
    expected = STATE_FILE.read_text().strip() if STATE_FILE.exists() else None
    if not expected or not secrets.compare_digest(expected, qs.get("state", [""])[0]):
        raise SchwabDisabled("login state doesn't match: start again with a fresh login link")
    STATE_FILE.unlink()
    _save(_token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": cb}))


def access_token():
    tok = _load()
    now = int(time.time())
    if now - tok.get("refresh_obtained_at", 0) > 7 * 86400 - 300:
        raise SchwabDisabled("Schwab's 7-day login has expired: run python -m wallstreet.broker.schwab login again")
    if now - tok["obtained_at"] > tok.get("expires_in", 1800) - 120:
        new = _token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        new.setdefault("refresh_token", tok["refresh_token"])
        new["refresh_obtained_at"] = tok.get("refresh_obtained_at", now)
        _save(new)
        tok = new
    return tok["access_token"]


def _get(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + access_token(), "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


# ---- read-only endpoints
def quotes(symbols):
    return _get(MD + "/quotes", {"symbols": ",".join(symbols)})


def price_history(symbol, period_type="year", period=10, frequency_type="daily", frequency=1):
    return _get(MD + "/pricehistory", {"symbol": symbol, "periodType": period_type, "period": period,
                                       "frequencyType": frequency_type, "frequency": frequency})


def movers(index="$SPX", sort="PERCENT_CHANGE_UP"):
    return _get(MD + "/movers/" + urllib.parse.quote(index), {"sort": sort})


def mask(number):
    s = str(number or "")
    return "****" + s[-4:] if len(s) > 4 else "****"


def accounts():
    """Balances and positions with every account number masked to its last 4 digits before it leaves this module."""
    data = _get(TRADER + "/accounts", {"fields": "positions"})

    def scrub(x):
        if isinstance(x, dict):
            return {k: (mask(v) if "accountnumber" in k.lower() or k.lower() == "hashvalue" else scrub(v)) for k, v in x.items()}
        if isinstance(x, list):
            return [scrub(v) for v in x]
        return x
    return scrub(data)


if __name__ == "__main__":
    if sys.argv[1:] == ["login"]:
        print("1. Open this URL, sign in on Schwab's site and approve:\n   " + login_url())
        finish_login(input("2. Paste the full URL your browser was sent to: ").strip())
        print("Logged in. This lasts 7 days.")
    else:
        print(__doc__)

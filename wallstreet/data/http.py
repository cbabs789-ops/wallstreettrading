"""HTTP with retries, timeouts and per-host rate limits. Failures raise DataError; callers decide what an outage means.

Never returns half-parsed data: a response is either valid JSON/text or an exception.
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.request

from .. import config

log = logging.getLogger("pws.http")


class DataError(RuntimeError):
    """A data source failed or returned something unusable."""


_LIMITS = {"sec.gov": 0.12, "data.sec.gov": 0.12, "query1.finance.yahoo.com": 0.25, "query2.finance.yahoo.com": 0.25}
_last = {}
_lock = threading.Lock()


def _throttle(host):
    gap = next((v for k, v in _LIMITS.items() if host.endswith(k)), 0.0)
    if not gap:
        return
    with _lock:
        wait = _last.get(host, 0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[host] = time.monotonic()


def get(url, kind="json", headers=None, timeout=20, retries=3):
    host = url.split("/")[2]
    h = {"User-Agent": config.SEC_USER_AGENT if "sec.gov" in host else config.HTTP_USER_AGENT,
         "Accept-Encoding": "identity"}
    h.update(headers or {})
    err = None
    for attempt in range(retries):
        _throttle(host)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                body = r.read()
            if kind == "json":
                return json.loads(body)
            return body.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            err = "HTTP %s" % e.code
            if e.code in (400, 401, 403, 404):
                break                                   # retrying won't help
            time.sleep(min(2 ** attempt * 2, 20))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
            err = str(e)[:200]
            time.sleep(min(2 ** attempt * 2, 20))
    log.warning("fetch failed %s: %s", url[:120], err)
    raise DataError("%s: %s" % (host, err))

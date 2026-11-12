"""Publish a stats snapshot to a private GitHub repo (like gatekeeper-stats), so results can be reviewed anywhere,
including by Claude in a later session.

Needs PWS_STATS_TOKEN: a fine-grained GitHub token with Contents read/write on ONE repo (PWS_STATS_REPO).
Files written: latest.json (the dashboard state), latest.md (the briefing), performance.md (this month's report),
history/YYYY-MM-DD.md (one briefing per day). Nothing secret is ever included: no tokens, no keys, no account numbers.
"""
import base64
import json
import logging
import time
import urllib.error
import urllib.request

from . import briefing, config, performance

API = config.env("PWS_GITHUB_API", "https://api.github.com")
log = logging.getLogger("pws.stats")
SECRET_ENV = ("TELEGRAM_BOT_TOKEN", "PWS_STATS_TOKEN", "PWS_DASH_KEY", "SCHWAB_APP_KEY", "SCHWAB_APP_SECRET")


def configured():
    return bool(config.env("PWS_STATS_TOKEN") and config.env("PWS_STATS_REPO"))


def _req(method, path, body=None, token=None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "wallstreet-stats"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 404 and method == "GET":
            return None
        raise RuntimeError("GitHub %s %s: HTTP %s" % (method, path.split("?")[0], e.code)) from None


def files(con):
    p, md, g = briefing.write(con)
    _, perf = performance.monthly_report(con, briefing.wf_results(con))
    out = {"latest.json": json.dumps(g, default=str, indent=1), "latest.md": md, "performance.md": perf,
           "history/%s.md" % time.strftime("%Y-%m-%d"): md}
    for name, text in out.items():                       # belt and braces: refuse to publish anything secret
        for k in SECRET_ENV:
            v = config.env(k)
            if v and len(v) >= 8 and v in text:
                raise RuntimeError("refusing to publish %s: it contains the value of %s" % (name, k))
    return out


def publish(con, repo=None, token=None):
    repo = repo or config.env("PWS_STATS_REPO")
    token = token or config.env("PWS_STATS_TOKEN")
    if not (repo and token):
        return 0
    n = 0
    for path, text in files(con).items():
        cur = _req("GET", "/repos/%s/contents/%s" % (repo, path), token=token)
        content = base64.b64encode(text.encode()).decode()
        if cur and cur.get("content", "").replace("\n", "") == content:
            continue                                     # unchanged: no empty commit
        body = {"message": "stats %s" % time.strftime("%Y-%m-%d %H:%M"), "content": content}
        if cur and cur.get("sha"):
            body["sha"] = cur["sha"]
        _req("PUT", "/repos/%s/contents/%s" % (repo, path), body, token)
        n += 1
    return n

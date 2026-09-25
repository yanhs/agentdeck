#!/usr/bin/env python3
"""Regenerate the README screenshots in docs/screenshots/ from STAGED demo data.

Nothing live is touched: web/ is served from a local http.server, the dashboard's
/api/* calls are answered by page.route() with English demo data, each terminal
iframe (/sess/?arg=<id>) gets a mock Claude Code screen, and /tasks/ is the REAL
tasks-dashboard server.py handler reading a temp TRACKER_STATE.

    python3 tests/make_screenshots.py            # writes docs/screenshots/*.png

Every shot is checked for Cyrillic in the page's (and its frames') innerText.
"""
from __future__ import annotations

import html
import http.server
import importlib
import json
import os
import re
import socket
import sys
import tempfile
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "web"
TASKS_DIR = ROOT / "tasks-dashboard"
OUT = ROOT / "docs" / "screenshots"
CYR = re.compile(r"[Ѐ-ӿ]")
SCALE = 2


# ─────────────────────────────────────────────────────────── demo data ──
NOW = int(datetime.now(timezone.utc).timestamp())

SESSIONS = [
    # id, name, active, status, last_used(ago s), archived
    ("3f9a1c07", "Auth: refresh tokens", True, "working", 60, False),
    ("8b2e4d10", "Docs: API reference", True, "idle", 600, False),
    ("c41d9e2a", "Data pipeline backfill", True, "working", 120, False),
    ("5d07b3f8", "Landing page redesign", True, "working", 240, False),
    ("7c1f0b2e", "Flaky CI: checkout e2e", True, "working", 300, False),
    ("a9e61c34", "Scraper rate limits", False, "off", 3 * 3600, False),
    ("17fc8e05", "Research: vector DBs", False, "off", 26 * 3600, False),
    ("e2b04a9d", "Release notes v2", False, "off", 50 * 3600, False),
    ("0b7d2e91", "Stripe webhooks retry", False, "off", 9 * 86400, True),
    ("6ac3f150", "Prototype: voice notes", False, "off", 14 * 86400, True),
    ("d95e0a7b", "Q2 dependency upgrades", False, "off", 30 * 86400, True),
]


def library_payload(with_archived: bool) -> dict:
    out = []
    for i, (sid, name, active, status, ago, arch) in enumerate(SESSIONS):
        if arch and not with_archived:
            continue
        out.append({"id": sid, "name": name, "cwd": "/srv/work", "created": NOW - 90 * 86400 + i,
                    "last_used": NOW - ago, "archived": arch, "active": active,
                    "attached": False, "status": status})
    return {"max_active": 12, "sessions": out,
            "shell": {"active": True, "attached": False, "status": "idle"},
            "_system": {"cpu_pct": 66, "ram_pct": 46, "ram_used_mb": 7420, "ram_total_mb": 16000}}


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def board_state() -> dict:
    now = datetime.now(timezone.utc)
    ago = lambda **kw: iso(now - timedelta(**kw))  # noqa: E731

    def it(title, status, note="", **kw):
        return {"title": title, "status": status, "note": note, "updated": ago(**kw)}

    tasks = [
        {"id": "auth-rotation", "title": "Refresh-token rotation for the auth service",
         "agent": "claude · api", "status": "active", "activity": "working", "session": "3f9a1c07",
         "created": ago(hours=2, minutes=40), "updated": ago(minutes=1),
         "items": [
             it("Read the current token code", "done", "tokens.py + auth middleware", hours=2, minutes=31),
             it("Failing tests for rotation and reuse", "done", "4 red", hours=2, minutes=12),
             it("Rotate on refresh, revoke the used token", "done", "jti kept in Redis, 14-day TTL", hours=1, minutes=20),
             it("Full test suite", "done", "126 passed", minutes=48),
             it("GET/DELETE /auth/sessions (list + revoke devices)", "active", "endpoints in, writing tests", minutes=1),
             it("Deploy to staging", "todo", hours=2, minutes=40),
         ]},
        {"id": "docs-api-ref", "title": "Generate the API reference for the docs site",
         "agent": "claude · docs", "status": "active", "session": "8b2e4d10",
         "created": ago(hours=6), "updated": ago(minutes=10),
         "items": [it("OpenAPI spec from the routers", "done", "58 endpoints", hours=5),
                   it("Reference pages + examples", "done", "", hours=1),
                   it("Link check", "active", "3 broken anchors left", minutes=10)]},
        {"id": "pipeline-backfill", "title": "Backfill 18 months of events into the warehouse",
         "agent": "claude · data", "status": "active", "activity": "working", "session": "c41d9e2a",
         "created": ago(days=1, hours=3), "updated": ago(minutes=2),
         "items": [it("Dry run on one month", "done", "row counts match", hours=20),
                   it("Backfill in monthly batches", "active", "month 11 of 18 · 2.4M rows", minutes=2)]},
        {"id": "flaky-ci-checkout", "title": "Fix the flaky checkout e2e test",
         "agent": "claude · ci", "status": "active", "activity": "working", "session": "7c1f0b2e",
         "created": ago(hours=1, minutes=15), "updated": ago(minutes=5),
         "items": [it("Reproduce locally (50 runs)", "done", "fails 6/50", minutes=40),
                   it("Wait on the payment iframe, not a timeout", "active", "", minutes=5)]},
        {"id": "landing-redesign", "title": "Landing page redesign",
         "agent": "claude · web", "status": "active", "session": "5d07b3f8",
         "created": ago(days=2), "updated": ago(minutes=30),
         "items": [it("Hero + pricing sections", "done", "", hours=5),
                   it("Lighthouse pass", "active", "perf 94, a11y 100", minutes=30)]},
        {"id": "scraper-rate-limits", "title": "Respect rate limits in the product scraper",
         "agent": "claude · scraper", "status": "blocked", "session": "a9e61c34",
         "created": ago(days=1), "updated": ago(hours=3),
         "items": [it("Backoff on 429 + Retry-After", "done", "41 passed", hours=4),
                   it("Higher quota key", "blocked", "waiting for the partner's API key", hours=3)]},
        {"id": "release-notes-v2", "title": "Release notes for v2.0",
         "agent": "claude · docs", "status": "active", "activity": "blocked",
         "activity_note": "needs changelog sign-off", "session": "e2b04a9d",
         "created": ago(days=2, hours=4), "updated": ago(days=2), "items": []},
        {"id": "vector-db-research", "title": "Research: pgvector vs Qdrant vs LanceDB",
         "agent": "claude · research", "status": "paused", "session": "17fc8e05",
         "created": ago(days=3), "updated": ago(days=1), "items": [
             it("Benchmark on 1M embeddings", "done", "p95 table in notes.md", days=1),
             it("Write-up", "todo", days=3)]},
        {"id": "arm64-runners", "title": "Move CI to arm64 runners",
         "agent": "claude · infra", "status": "todo",
         "created": ago(days=4), "updated": ago(days=4), "items": []},
    ]
    done = [
        ("Upgrade to Postgres 16", "claude · infra", 1), ("Dark mode for the dashboard", "claude · web", 1),
        ("Nightly DB backups to S3", "claude · infra", 2), ("Sentry alerts for 5xx spikes", "claude · api", 2),
        ("CSV export for invoices", "claude · api", 3), ("Search: typo tolerance", "claude · api", 4),
        ("Docs: quick-start guide", "claude · docs", 5), ("Remove unused feature flags", "claude · web", 6),
        ("Cache product images at the edge", "claude · infra", 7), ("Onboarding email sequence", "claude · web", 8),
        ("Password reset flow", "claude · api", 9), ("Weekly metrics report", "claude · data", 10),
    ]
    # the auth terminal's earlier tasks: tasks-filtered.png shows them under its chip
    auth_done = {"Sentry alerts for 5xx spikes", "CSV export for invoices", "Password reset flow"}
    for i, (title, agent, d) in enumerate(done):
        t = {"id": re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-"), "title": title,
             "agent": agent, "status": "done", "created": ago(days=d, hours=3),
             "updated": ago(days=d, minutes=i * 7),
             "items": [it("Implement + tests", "done", "", days=d)]}
        if title in auth_done:
            t["session"] = "3f9a1c07"
        tasks.append(t)
    return {"title": "Task board", "updated": ago(minutes=1), "tasks": tasks}


# ─────────────────────────────────────────── server status (GET /api/server) ──
CORES = 8


def _m(mid, name, kind, status, cpu, rss, what, children=(), **kw):
    """One member, shaped like server_status.Collector._totals() output."""
    d = {"id": mid, "name": name, "kind": kind, "status": status, "cpu_pct": cpu, "rss_mb": rss,
         "count": 1 + sum(c[3] if len(c) > 3 else 1 for c in children), "what": what,
         "children": [{"name": c[0], "cpu_pct": c[1], "rss_mb": c[2], "count": c[3] if len(c) > 3 else 1}
                      for c in children]}
    d.update(kw)
    return d


def server_payload() -> dict:
    """A plausible 8-core box at ~65% CPU with a filled 15-minute history."""
    import math
    import random

    groups = [
        ("agents", "Agents", [
            _m("agent:41822", "Auth: refresh tokens", "claude", "working", 118.4, 812,
               "Claude Code in ~/work/api · running pytest",
               [("pytest", 96.2, 310), ("node mcp-server.js", 3.1, 74), ("redis-cli", 0.4, 6)],
               terminal="cs-3f9a1c07"),
            _m("agent:40517", "Data pipeline backfill", "claude", "working", 86.9, 1240,
               "Claude Code in ~/work/data · running python3 backfill.py",
               [("python3 backfill.py --month 11", 81.5, 902)], terminal="cs-c41d9e2a"),
            _m("agent:43390", "Flaky CI: checkout e2e", "claude", "working", 71.3, 1480,
               "Claude Code in ~/work/shop · running npx playwright test",
               [("npx playwright test", 38.0, 260), ("chromium", 29.6, 910, 6)], terminal="cs-7c1f0b2e"),
            _m("agent:39904", "Landing page redesign", "claude", "working", 57.8, 1010,
               "Claude Code in ~/work/site · running npm run build",
               [("npm run build", 52.1, 640), ("node", 3.2, 110)], terminal="cs-5d07b3f8"),
            _m("agent:38211", "Docs: API reference", "claude", "idle", 1.2, 402,
               "Claude Code in ~/work/docs", terminal="cs-8b2e4d10"),
            _m("agent:37650", "Nightly code review", "claude", "idle", 0.8, 356,
               "Claude Code in ~/work/api · no terminal · started by nightly-review.service"),
        ]),
        ("apps", "Sites & apps", [
            _m("docker:web-prod", "web-prod", "container", "working", 46.3, 1320,
               "Storefront (Next.js), production", [("next-server", 41.8, 980), ("node", 4.5, 340)],
               detail="Up 3 days"),
            _m("docker:postgres", "postgres", "container", "working", 37.9, 1640,
               "Docker container (postgres:16)", [("postgres", 37.9, 1640, 14)], detail="Up 3 weeks"),
            _m("docker:api-staging", "api-staging", "container", "working", 21.7, 690,
               "Public API (FastAPI), staging", [("uvicorn app:api", 17.2, 480), ("python3 worker.py", 4.5, 210)],
               detail="Up 5 hours"),
            _m("unit:embedder.service", "Embedder", "service", "running", 8.6, 820,
               "Text embeddings for search (ONNX)", [("python3 embed_server.py", 8.6, 820)],
               detail="embedder.service"),
            _m("unit:nginx.service", "nginx", "service", "running", 3.1, 48,
               "Web server and reverse proxy for every site", [("nginx", 3.1, 48, 5)],
               detail="nginx.service"),
            _m("unit:redis-server.service", "redis", "service", "running", 1.9, 96,
               "In-memory cache and job queue", [("redis-server", 1.9, 96)], detail="redis-server.service"),
            _m("unit:telegram-bot.service", "Telegram bot", "service", "running", 0.6, 88,
               "Telegram bridge to the terminals", [("python3 tg_bridge.py", 0.6, 88)],
               detail="telegram-bot.service"),
            _m("unit:backup-s3.service", "Backups to S3", "service", "stopped", 0, 0,
               "Nightly database backup (runs at 03:00)", detail="backup-s3.service", count=0),
        ]),
        ("jobs", "Background jobs", [
            _m("job:52210", "python3 import_catalog.py", "job", "working", 47.2, 610,
               "Started from ssh session · in ~/work/shop", owner="ssh session"),
            _m("job:51877", "rsync -a media/ backup:/media", "job", "running", 5.8, 42,
               "Started from detached · in ~/work/site", owner="detached"),
        ]),
        ("system", "System", [
            _m("sys:kernel", "Kernel threads", "kernel", "running", 4.1, 0, "Linux kernel workers", count=148),
            _m("sys:other", "Other", "system", "running", 7.4, 430,
               "92 small processes: shells, daemons, tmux",
               [("sshd", 1.6, 18, 3), ("tmux", 1.2, 24), ("containerd", 2.1, 71), ("systemd-journald", 0.9, 64)]),
        ]),
    ]
    gs = [{"id": gid, "title": title, "count": len(ms), "members": ms,
           "cpu_pct": round(sum(x["cpu_pct"] for x in ms), 1),
           "rss_mb": round(sum(x["rss_mb"] for x in ms), 1)} for gid, title, ms in groups]
    now_g = {g["id"]: g["cpu_pct"] for g in gs}

    # 15 minutes at 5 s: agents ramp up as the tests start, the catalog import
    # kicks in ~6 minutes ago, a build spike around 9 minutes ago.
    rnd = random.Random(20260925)
    n, pts, t_end = 180, [], float(NOW)
    walk = {k: 0.0 for k in now_g}
    for i in range(n):
        f = i / (n - 1)
        for k in walk:
            walk[k] = walk[k] * 0.93 + rnd.gauss(0, 0.35)
        agents = now_g["agents"] * (0.55 + 0.45 * f) + 14 * walk["agents"] \
            + 90 * math.exp(-((i - 72) / 7) ** 2)
        apps = now_g["apps"] * (0.9 + 0.1 * math.sin(i / 9)) + 6 * walk["apps"]
        jobs = (now_g["jobs"] if i > 108 else 6.0) + 4 * walk["jobs"]
        system = now_g["system"] + 2 * walk["system"]
        if i == n - 1:
            agents, apps, jobs, system = now_g["agents"], now_g["apps"], now_g["jobs"], now_g["system"]
        g = {"agents": round(max(5, agents), 1), "apps": round(max(20, apps), 1),
             "jobs": round(max(0, jobs), 1), "system": round(max(3, system), 1)}
        cpu = min(100.0, sum(g.values()) / CORES + 1.5)
        pts.append({"t": t_end - (n - 1 - i) * 5, "cpu": round(cpu, 1),
                    "ram": round(44.5 + 2.2 * f + 0.4 * walk["apps"], 1), "g": g})
    host_cpu = pts[-1]["cpu"]
    return {
        "ts": t_end, "interval": 5,
        "host": {"hostname": "build-box", "cores": CORES, "load": [5.9, 5.4, 4.7], "cpu_pct": host_cpu,
                 "cpu_per_core": [88, 81, 76, 71, 64, 58, 52, 37],
                 "ram_total_mb": 16000, "ram_used_mb": 7420, "ram_available_mb": 8580, "ram_pct": 46.4,
                 "swap_total_mb": 0, "swap_used_mb": 0,
                 "disk_total_gb": 240.0, "disk_used_gb": 131.6, "disk_free_gb": 108.4, "disk_pct": 54.8,
                 "uptime_s": 23 * 86400 + 5 * 3600 + 17 * 60,
                 "pressure": "busy", "pressure_reason": f"CPU at {round(host_cpu)}%"},
        "groups": gs,
        "history": {"interval": 5, "points": pts},
        "collector": {"sample_ms": 38, "cpu_pct": 0.7, "processes": 412},
    }


# ───────────────────────────────────────────────────── mock terminal ──
TERM_CSS = """
html,body{margin:0;height:100%;background:#1b1d23;color:#d4d7dd;overflow:hidden}
body{font:13.5px/1.4 'JetBrains Mono','DejaVu Sans Mono',ui-monospace,monospace;padding:12px 20px 10px;box-sizing:border-box;display:flex;flex-direction:column;justify-content:flex-end}
@media (max-width:600px){body{font-size:12px;padding:8px 10px}body .l{white-space:pre-wrap;padding-left:2ch;text-indent:-2ch}.diff{display:none}.hint{font-size:11px}}
.l{white-space:pre}.dim{color:#7d828d}.u{color:#9fb4ff}.b{font-weight:600;color:#fff}
.dot{color:#e6e8ec}.ok{color:#4ade80}.tool{font-weight:600;color:#e6e8ec}.arg{color:#aeb3bc}
.add{color:#86efac;background:#12301f}.del{color:#fca5a5;background:#3a1717}.warn{color:#f0b35a}
.or{color:#d97757}.box{border:1px solid #4a4e58;border-radius:6px;padding:4px 10px;margin-top:10px;color:#e6e8ec}
.hint{color:#7d828d;font-size:12.5px;margin-top:4px}.cur{background:#d4d7dd;color:#1b1d23}
"""


def term_html(sid: str) -> str:
    name = next((s[1] for s in SESSIONS if s[0] == sid), "Terminal")
    L = []

    def line(s: str = "", cls: str = "l"):
        L.append(f'<div class="{cls}">{s or "&nbsp;"}</div>')

    if sid != "3f9a1c07":        # an idle terminal: a finished docs session
        line('<span class="or">&#x273B;</span> <span class="b">Claude Code</span> <span class="dim">&middot; Opus &middot; /srv/work/docs</span>')
        line()
        line('<span class="dim">&gt;</span> <span class="u">Generate an API reference page for every public endpoint, with a curl</span>')
        line('  <span class="u">example each, and add it to the docs sidebar</span>')
        line()
        line('<span class="dot">&#9679;</span> <span class="tool">Bash</span><span class="arg">(python scripts/export_openapi.py &gt; build/openapi.json)</span>')
        line('  <span class="dim">&#9151;  Exported 58 operations from 9 routers</span>')
        line('<span class="dot">&#9679;</span> <span class="tool">Write</span><span class="arg">(scripts/gen_reference.py)</span>')
        line('  <span class="dim">&#9151;  Wrote 142 lines to scripts/gen_reference.py</span>')
        line('<span class="dot">&#9679;</span> <span class="tool">Bash</span><span class="arg">(python scripts/gen_reference.py build/openapi.json docs/api/)</span>')
        line('  <span class="dim">&#9151;  Generated 58 pages in docs/api/</span>')
        line('<span class="dot">&#9679;</span> <span class="tool">Update</span><span class="arg">(docs/sidebars.js)</span>')
        line('  <span class="dim">&#9151;  Updated docs/sidebars.js with 12 additions</span>')
        line('<span class="dot">&#9679;</span> <span class="tool">Bash</span><span class="arg">(npm run build &amp;&amp; npm run check-links)</span>')
        line('  <span class="dim">&#9151;  </span><span class="ok">Build succeeded</span><span class="dim"> &middot; </span><span class="warn">3 broken anchors</span>')
        line('<span class="dot">&#9679;</span> <span class="tool">Update</span><span class="arg">(docs/api/billing.md)</span>')
        line('  <span class="dim">&#9151;  Updated docs/api/billing.md with 3 additions and 3 removals</span>')
        line('<span class="dot">&#9679;</span> <span class="tool">Bash</span><span class="arg">(npm run check-links)</span>')
        line('  <span class="dim">&#9151;  </span><span class="ok">0 broken links</span><span class="dim"> (1,284 checked)</span>')
        line()
        line('<span class="dot">&#9679;</span> Done. The reference lives under <span class="b">/docs/api/</span>:')
        line('  - 58 endpoint pages generated from the OpenAPI spec, each with a curl example')
        line('  - a new "API reference" group in the sidebar, grouped by router')
        line('  - fixed 3 stale anchors in billing.md; the link check is clean')
        line()
        line('<span class="or">&#x273B;</span> <span class="dim">Crunched for 4m 12s</span>')
    else:
        _auth_session(line)
    body = "\n".join(L)
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(name)}</title>"
            "<link href='https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&display=swap' rel='stylesheet'>"
            f"<style>{TERM_CSS}</style></head><body><div>{body}</div>"
            f"<div class='box'><span class='dim'>&gt;</span> <span class='cur'>&nbsp;</span></div>"
            f"<div class='hint'>&#x23F5;&#x23F5; bypass permissions on</div></body></html>")


def _auth_session(line):

    line('<span class="or">&#x273B;</span> <span class="b">Claude Code</span> <span class="dim">&middot; Opus &middot; /srv/work/api</span>')
    line()
    line('<span class="dim">&gt;</span> <span class="u">Add refresh-token rotation to the auth service. A used refresh token</span>')
    line('  <span class="u">must be rejected, and reusing one should revoke the whole family.</span>')
    line()
    line('<span class="dot">&#9679;</span> I\'ll read the current token code, then write failing tests first.')
    line()
    line('<span class="dot">&#9679;</span> <span class="tool">Read</span><span class="arg">(src/auth/tokens.py)</span>')
    line('  <span class="dim">&#9151;  Read 142 lines</span>')
    line('<span class="dot">&#9679;</span> <span class="tool">Write</span><span class="arg">(tests/test_refresh_rotation.py)</span>')
    line('  <span class="dim">&#9151;  Wrote 58 lines to tests/test_refresh_rotation.py</span>')
    line('<span class="dot">&#9679;</span> <span class="tool">Bash</span><span class="arg">(pytest tests/test_refresh_rotation.py -q)</span>')
    line('  <span class="dim">&#9151;  </span><span class="warn">4 failed</span><span class="dim"> in 0.41s</span>')
    line('<span class="dot">&#9679;</span> <span class="tool">Update</span><span class="arg">(src/auth/tokens.py)</span>')
    line('  <span class="dim">&#9151;  Updated src/auth/tokens.py with 31 additions and 6 removals</span>')
    line(cls="l diff", s='      <span class="dim"> 88</span>  <span class="del">-    return issue_pair(user_id)                                  </span>')
    line(cls="l diff", s='      <span class="dim"> 88</span>  <span class="add">+    if store.was_used(claims["jti"]):                           </span>')
    line(cls="l diff", s='      <span class="dim"> 89</span>  <span class="add">+        store.revoke_family(claims["fam"])                      </span>')
    line(cls="l diff", s='      <span class="dim"> 90</span>  <span class="add">+        raise TokenReuse(claims["fam"])                         </span>')
    line(cls="l diff", s='      <span class="dim"> 91</span>  <span class="add">+    store.mark_used(claims["jti"], ttl=REFRESH_TTL)             </span>')
    line(cls="l diff", s='      <span class="dim"> 92</span>  <span class="add">+    return issue_pair(user_id, family=claims["fam"])            </span>')
    line('<span class="dot">&#9679;</span> <span class="tool">Bash</span><span class="arg">(pytest -q)</span>')
    line('  <span class="dim">&#9151;  </span><span class="ok">126 passed</span><span class="dim"> in 7.82s</span>')
    line()
    line('<span class="dot">&#9679;</span> Done. Refresh tokens now rotate on every use:')
    line('  - each refresh issues a new pair and marks the old token used (jti in Redis, 14-day TTL)')
    line('  - replaying a used token revokes the whole family and returns 401')
    line('  - 4 new tests; the full suite is green: <span class="ok">126 passed</span>')
    line()
    line('<span class="dim">&gt;</span> <span class="u">Now add /auth/sessions so users can list and revoke their devices</span>')
    line()
    line('<span class="dot">&#9679;</span> <span class="tool">Read</span><span class="arg">(src/auth/routes.py)</span>')
    line('  <span class="dim">&#9151;  Read 97 lines</span>')
    line('<span class="dot">&#9679;</span> <span class="tool">Update</span><span class="arg">(src/auth/routes.py)</span>')
    line('  <span class="dim">&#9151;  Updated src/auth/routes.py with 24 additions</span>')
    line()
    line('<span class="or">&#x2736; Wiring up the device list&hellip;</span> <span class="dim">(38s &middot; &darr; 1.2k tokens &middot; esc to interrupt)</span>')


# ───────────────────────────────────────────────────────────── servers ──
def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(state_path: Path) -> str:
    """One origin: web/ static files + the REAL tasks-dashboard handler under /tasks/."""
    os.environ["TRACKER_STATE"] = str(state_path)
    sys.path.insert(0, str(TASKS_DIR))
    sys.modules.pop("server", None)
    tsrv = importlib.import_module("server")

    class Handler(tsrv.H, http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/tasks"):
                self.path = self.path[len("/tasks"):] or "/"   # like nginx proxy_pass .../
                return tsrv.H.do_GET(self)
            return http.server.SimpleHTTPRequestHandler.do_GET(self)

        def log_message(self, *a):
            pass

    port = _free_port()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(WEB_DIR)))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}"


# ───────────────────────────────────────────────────────────── helpers ──
PROBLEMS: list[str] = []


def check_english(pg, name: str) -> None:
    texts = []
    for fr in pg.frames:
        try:
            texts.append(fr.evaluate("() => document.body ? document.body.innerText : ''"))
        except Exception as e:  # noqa: BLE001
            PROBLEMS.append(f"{name}: frame {fr.url} unreadable: {e}")
    hits = sorted({m.group(0) for t in texts for m in CYR.finditer(t)})
    status = "OK (no Cyrillic)" if not hits else f"CYRILLIC FOUND: {''.join(hits)}"
    print(f"  {name}: {len(pg.frames)} frame(s), {sum(map(len, texts))} chars -> {status}")
    if hits:
        PROBLEMS.append(f"{name}: Cyrillic {''.join(hits)}")


def save(pg, name: str, **kw) -> None:
    path = OUT / name
    pg.screenshot(path=str(path), **kw)
    check_english(pg, name)
    print(f"  wrote {path} ({path.stat().st_size // 1024} KB)")


def open_dashboard(browser, base, *, width, height, mobile=False, tasks_open=True,
                   show_archived=False, color_scheme="dark", theme="dark"):
    ctx = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=SCALE,
                              is_mobile=mobile, has_touch=mobile, color_scheme=color_scheme)
    ctx.set_default_timeout(15000)
    ctx.add_init_script(f"""try {{
        localStorage.setItem('lib-tasks-open', '{1 if tasks_open else 0}');
        localStorage.setItem('lib-show-archived', '{1 if show_archived else 0}');
        localStorage.removeItem('stats-hidden');
        localStorage.setItem('lib-server-open', '0');
        localStorage.setItem('agentdeck-theme', '{theme}');
    }} catch (e) {{}}""")
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))

    def lib(route):
        q = urlparse(route.request.url).query
        if route.request.method != "GET":
            return route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(library_payload("archived=1" in q)))

    def sess(route):
        arg = (parse_qs(urlparse(route.request.url).query).get("arg") or [""])[0]
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=term_html(arg))

    pg.route(re.compile(r"/api/library(/|\?|$)"), lib)
    pg.route(re.compile(r"/api/terminal-status(\?|$)"),
             lambda r: r.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"_system": library_payload(False)["_system"]})))
    pg.route(re.compile(r"/api/page-version"), lambda r: r.abort())
    pg.route(re.compile(r"/sess/"), sess)
    server_json = json.dumps(server_payload())
    pg.route(re.compile(r"/api/server(\?|$)"),
             lambda r: r.fulfill(status=200, content_type="application/json", body=server_json))
    pg.goto(base + "/index.html")
    pg.wait_for_selector(".card[data-sid]")
    pg.wait_for_timeout(400)
    return ctx, pg, errors


def settle(pg):
    pg.mouse.move(2, 2)
    try:
        pg.evaluate("() => document.fonts.ready")
    except Exception:  # noqa: BLE001
        pass
    pg.wait_for_timeout(900)


# ───────────────────────────────────────────────────────── telegram mock ──
def tg_html() -> str:
    def esc(s):
        return html.escape(s).replace("\n", "<br>")

    def out(text, t, voice=False):
        body = (f'<div class="voice"><span class="play"><svg width="14" height="16" viewBox="0 0 14 16"><path d="M1 1l12 7-12 7z" fill="#fff"/></svg></span><span class="wave">{"".join(f"<i style=height:{h}px></i>" for h in [6,10,14,9,18,12,7,15,20,11,8,16,12,6,10,14,9,5,12,8])}</span><span class="dur">0:05</span></div>'
                if voice else f'<span class="txt">{esc(text)}</span>')
        return f'<div class="row out"><div class="bub out">{body}<span class="tm">{t} <b>&#10003;&#10003;</b></span></div></div>'

    def inc(text, t, kb=None):
        k = ""
        if kb:
            k = '<div class="kb">' + "".join(f'<div class="kbtn">{esc(b)}</div>' for b in kb) + "</div>"
        return (f'<div class="row in"><div class="col"><div class="bub in"><span class="txt">{esc(text)}</span>'
                f'<span class="tm">{t}</span></div>{k}</div></div>')

    msgs = [
        out("/list", "14:02"),
        inc("Current: cs-3f9a1c07 «Auth: refresh tokens»\n\n"
            "Topics (🟢 loaded · ⚪️ unloaded · ⚙️ working):\n"
            "🟢 «Auth: refresh tokens» · 3f9a1c07 ⚙️ ← current\n"
            "🟢 «Docs: API reference» · 8b2e4d10\n"
            "🟢 «Data pipeline backfill» · c41d9e2a ⚙️\n"
            "⚪️ «Scraper rate limits» · a9e61c34\n"
            "⚪️ «Release notes v2» · e2b04a9d", "14:02"),
        out("/new Invoice PDF export", "14:02"),
        inc("🆕 ✅ Current topic: cs-4e8d2b61 «Invoice PDF export»\n▶️ was unloaded — loading…", "14:02"),
        out("/use scraper", "14:03"),
        inc("✅ Current topic: cs-a9e61c34 «Scraper rate limits»\n▶️ was unloaded — loading…", "14:03"),
        out("Back off exponentially on 429s and run the tests", "14:03"),
        inc("Read(scraper/fetch.py)\n⎿ Read 96 lines\n"
            "Update(scraper/fetch.py)\n⎿ Updated scraper/fetch.py with 18 additions\n"
            "Bash(pytest -q)\n⎿ 41 passed in 2.3s\n"
            "✻ Crunched for 52s\n\n"
            "Done — a 429 now waits 1s → 2s → 4s … (capped at 60s) and honours Retry-After when the site sends it.",
            "14:04"),
        out("", "14:06", voice=True),
        inc('🎙 "commit it and deploy to staging"', "14:06"),
        inc("📋 Which environment should I deploy to?\n"
            "▫️ 1. Staging\n      ↳ staging.example.com, safe to break\n"
            "▫️ 2. Production\n      ↳ live traffic, needs a green CI run",
            "14:06", kb=["1. Staging", "2. Production", "💬 Talk"]),
    ]
    dots = "".join(f'<i class="bokeh" style="left:{x}px;top:{y}px;width:{r}px;height:{r}px"></i>'
                   for x, y, r in [(40, 80, 20), (310, 140, 16), (160, 330, 20), (360, 390, 18),
                                   (70, 620, 14), (300, 760, 20), (120, 930, 16), (340, 1080, 14)])
    return """<!doctype html><html><head><meta charset="utf-8"><style>
*{box-sizing:border-box}
html,body{margin:0;background:#cfe6bf}
body{width:420px;font:14.5px/1.38 -apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,'Noto Color Emoji',sans-serif;color:#111}
.phone{position:relative;width:420px;background:linear-gradient(180deg,#d7ecc6 0%,#c9e3b6 55%,#c2dfae 100%);overflow:hidden}
.bokeh{position:absolute;border-radius:50%;background:rgba(255,255,255,.28)}
.status{display:flex;justify-content:space-between;align-items:center;padding:8px 14px 0 14px;font-weight:700;font-size:13.5px;color:#1c2a17;position:relative}
.head{display:flex;align-items:center;gap:10px;padding:10px 10px 10px 12px;position:relative}
.circ{width:38px;height:38px;border-radius:50%;background:#fff;display:flex;align-items:center;justify-content:center;font-size:20px;color:#333;box-shadow:0 1px 2px rgba(0,0,0,.08)}
.ava{width:40px;height:40px;border-radius:50%;background:linear-gradient(135deg,#4aa3ff,#1f6fe0);display:flex;align-items:center;justify-content:center;font-size:21px}
.nm{flex:1}.nm b{display:block;font-size:17px;color:#16240f}.nm span{color:#4b7a5a;font-size:13px}
.chat{padding:4px 10px 12px;display:flex;flex-direction:column;gap:6px;position:relative}
.row{display:flex}.row.out{justify-content:flex-end}.col{max-width:88%}
.bub{border-radius:16px;padding:7px 11px 6px;box-shadow:0 1px 1px rgba(0,0,0,.08);position:relative;max-width:88%}
.col .bub{max-width:100%}
.bub.out{background:#effdde;border-bottom-right-radius:5px}
.bub.in{background:#fff;border-bottom-left-radius:5px}
.txt{white-space:pre-wrap;word-wrap:break-word}
.tm{float:right;margin:6px 0 -3px 10px;font-size:11.5px;color:#8a9a8a;position:relative;top:2px}
.out .tm{color:#5fa35a}.tm b{font-weight:400;letter-spacing:-3px;margin-right:3px}
.kb{display:flex;flex-direction:column;gap:5px;margin-top:5px}
.kbtn{background:rgba(255,255,255,.55);backdrop-filter:blur(2px);border-radius:10px;text-align:center;padding:8px 6px;color:#2a7fcf;font-weight:500;box-shadow:0 1px 1px rgba(0,0,0,.06)}
.voice{display:inline-flex;align-items:center;gap:9px;min-width:190px}
.play{width:36px;height:36px;border-radius:50%;background:#5fbf55;color:#fff;display:flex;align-items:center;justify-content:center;font-size:14px;padding-left:3px}
.wave{display:flex;align-items:center;gap:2px}.wave i{display:block;width:3px;border-radius:2px;background:#6bbd63}
.dur{font-size:12px;color:#5fa35a}
.bar{display:flex;align-items:center;gap:10px;background:#fff;padding:9px 10px;border-top:1px solid #e3e8e0}
.menu{background:#55a8f0;color:#fff;border-radius:18px;padding:7px 14px;font-weight:700;font-size:14px}
.msg{flex:1;color:#9aa3ab;font-size:15px}.ic{font-size:20px;color:#8c96a0}
.mic{width:38px;height:38px;border-radius:50%;background:#55a8f0;display:flex;align-items:center;justify-content:center;font-size:18px}
</style></head><body><div class="phone">""" + dots + """
<div class="status"><span>14:06</span><span>&#x1F4F6; &#x1F50B;</span></div>
<div class="head"><div class="circ">&larr;</div><div class="ava">&#x1F916;</div>
<div class="nm"><b>AgentDeck</b><span>bot</span></div><div class="circ" style="font-size:18px">&#8942;</div></div>
<div class="chat">""" + "".join(msgs) + """</div>
<div class="bar"><span class="menu">&#9776; Menu</span><span class="ic">&#9786;</span><span class="msg">Message</span>
<span class="ic">&#x1F4CE;</span><span class="mic">&#x1F3A4;</span></div>
</div></body></html>"""


# ───────────────────────────────────────────────────────────────── main ──
def main() -> int:
    from playwright.sync_api import sync_playwright

    only = set(sys.argv[1:])            # optional: regenerate just these names
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="agentdeck-shots-"))
    state_path = tmp / "state.json"
    state_path.write_text(json.dumps(board_state(), ensure_ascii=False))
    base = start_server(state_path)
    print("serving", base, "state", state_path)

    def want(name):
        return not only or name in only

    def done(ctx, errs, name):
        ctx.close()
        PROBLEMS.extend(f"{name}: pageerror {e}" for e in errs)

    def open_terminal(pg, sid="3f9a1c07"):
        pg.click(f'#list .card[data-sid="{sid}"] .proj')
        pg.wait_for_selector("#wrap iframe")
        pg.frame_locator("#wrap iframe").locator(".box").wait_for()

    def open_tasks_tab(pg):
        pg.click("#list .tasks-row .proj")
        pg.wait_for_selector("#wrap iframe")
        fr = pg.frame_locator("#wrap iframe")
        fr.locator(".task").first.wait_for()
        fr.locator('.task[data-id="auth-rotation"] .task-row').click()

    def open_server_tab(pg, expand=("agent:41822",)):
        pg.click("#serverBtn")
        pg.wait_for_selector("#list .server-row.sel")
        fr = pg.frame_locator("#wrap iframe")
        fr.locator(".row[data-id]").first.wait_for()
        for mid in expand:
            fr.locator(f'.row[data-id="{mid}"] .main').click()

    with sync_playwright() as p:
        b = p.chromium.launch()

        # dashboard.png — a working terminal selected
        if want("dashboard.png"):
            print("dashboard.png")
            ctx, pg, errs = open_dashboard(b, base, width=1440, height=820)
            open_terminal(pg)
            settle(pg)
            save(pg, "dashboard.png")
            done(ctx, errs, "dashboard.png")

        # tasks-tab.png — the Tasks row selected, the board inside the viewer
        if want("tasks-tab.png"):
            print("tasks-tab.png")
            ctx, pg, errs = open_dashboard(b, base, width=1440, height=820)
            open_tasks_tab(pg)
            settle(pg)
            save(pg, "tasks-tab.png")
            done(ctx, errs, "tasks-tab.png")

        # tasks-filtered.png — the terminal's id in the top bar clicked: its tasks only
        if want("tasks-filtered.png"):
            print("tasks-filtered.png")
            ctx, pg, errs = open_dashboard(b, base, width=1440, height=820)
            open_terminal(pg)
            pg.click("#tNum.link")
            pg.wait_for_selector("#list .tasks-row.sel")
            fr = pg.frame_locator("#wrap iframe")
            fr.locator("#sess-chip:not([hidden])").wait_for()
            fr.locator('.task[data-id="auth-rotation"] .task-row').click()
            fr.locator('section.group[data-group="done"] .group-head').click()   # its finished tasks too
            settle(pg)
            save(pg, "tasks-filtered.png")
            done(ctx, errs, "tasks-filtered.png")

        # server.png — the Server tab (gauge button), one agent expanded
        if want("server.png"):
            print("server.png")
            ctx, pg, errs = open_dashboard(b, base, width=1440, height=820)
            open_server_tab(pg)
            settle(pg)
            save(pg, "server.png")
            done(ctx, errs, "server.png")

        # server-mobile.png — the same page on a phone
        if want("server-mobile.png"):
            print("server-mobile.png")
            ctx, pg, errs = open_dashboard(b, base, width=390, height=844, mobile=True, tasks_open=False)
            pg.tap("#serverBtn")
            pg.wait_for_selector("#wrap iframe")
            pg.frame_locator("#wrap iframe").locator(".row[data-id]").first.wait_for()
            settle(pg)
            save(pg, "server-mobile.png")
            done(ctx, errs, "server-mobile.png")

        # light-theme.png — the light theme, a terminal open (the terminal stays dark)
        if want("light-theme.png"):
            print("light-theme.png")
            ctx, pg, errs = open_dashboard(b, base, width=1440, height=820, theme="light",
                                           color_scheme="light")
            open_terminal(pg)
            settle(pg)
            save(pg, "light-theme.png")
            done(ctx, errs, "light-theme.png")

        # light-tasks.png — the Tasks tab in the light theme
        if want("light-tasks.png"):
            print("light-tasks.png")
            ctx, pg, errs = open_dashboard(b, base, width=1440, height=820, theme="light",
                                           color_scheme="light")
            open_tasks_tab(pg)
            settle(pg)
            save(pg, "light-tasks.png")
            done(ctx, errs, "light-tasks.png")

        # archive.png — "Show archived" on
        if want("archive.png"):
            print("archive.png")
            ctx, pg, errs = open_dashboard(b, base, width=1440, height=820, tasks_open=False)
            pg.click("#showArchived")
            pg.wait_for_selector('#list .card.archived[data-sid="0b7d2e91"]')
            pg.click('#list .card[data-sid="8b2e4d10"] .proj')
            pg.wait_for_selector("#wrap iframe")
            settle(pg)
            pg.hover('#list .card[data-sid="0b7d2e91"]')
            pg.wait_for_timeout(300)
            save(pg, "archive.png")
            done(ctx, errs, "archive.png")

        # mobile.png — the phone list
        if want("mobile.png"):
            print("mobile.png")
            ctx, pg, errs = open_dashboard(b, base, width=390, height=844, mobile=True, tasks_open=False)
            pg.tap('#list .card[data-sid="3f9a1c07"] .proj')
            pg.wait_for_selector("#wrap iframe")
            pg.frame_locator("#wrap iframe").locator(".box").wait_for()
            settle(pg)
            save(pg, "mobile.png")
            done(ctx, errs, "mobile.png")

        # task-board.png (+ dark) — the board alone
        for theme, name in (("light", "task-board.png"), ("dark", "task-board-dark.png")):
            if not want(name):
                continue
            print(name)
            ctx = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=SCALE,
                                color_scheme=theme)
            ctx.add_init_script(f"try {{ localStorage.setItem('agentdeck-theme', '{theme}'); }} catch (e) {{}}")
            pg = ctx.new_page()
            errs: list[str] = []
            pg.on("pageerror", lambda e, errs=errs: errs.append(str(e)))
            pg.goto(base + "/tasks/")
            pg.wait_for_selector(".task")
            pg.click('.task[data-id="auth-rotation"] .task-row')
            settle(pg)
            save(pg, name, full_page=True)
            done(ctx, errs, name)

        # telegram.png — the bridge, with its real reply formats (in English)
        if want("telegram.png"):
            print("telegram.png")
            ctx = b.new_context(viewport={"width": 420, "height": 800}, device_scale_factor=SCALE)
            pg = ctx.new_page()
            pg.set_content(tg_html())
            pg.wait_for_timeout(300)
            save(pg, "telegram.png", full_page=True)
            ctx.close()
        b.close()

    print("\nPROBLEMS:" if PROBLEMS else "\nall shots OK")
    for x in PROBLEMS:
        print("  -", x)
    return 1 if PROBLEMS else 0


if __name__ == "__main__":
    sys.exit(main())

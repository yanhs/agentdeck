"""Browser contract: a guest's dashboard shows today's Claude spend (decision 30.10.2026).

GET /api/quota (tests/test_quota_api.py) is faked here. On the owner's own install it says
{"available": false} and nothing is shown; in a guest's sandbox a line under the header
reads "Today 603K of 37M tokens" with a bar — the same numbers the owner sees in Guests.
"""
from __future__ import annotations

import json
import re
import time

import pytest

from tests.test_index_lib import FakeAPI, _open, browser, site  # noqa: F401

USAGE = {"available": True, "spent_today": 603374, "limit": 37_300_000, "enabled": True,
         "unlimited_until": 0, "resets_at": int(time.time()) + 5 * 3600 + 120}


def open_with(browser, site, quota, **kw):  # noqa: F811
    ctx, pg = _open(browser, site, FakeAPI(), init_script=None, **kw)
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    calls = []

    def handle(route):
        calls.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(quota))
    pg.route(re.compile(r"/api/quota(\?|$)"), handle)
    pg.reload()
    pg.wait_for_selector(".card[data-sid]")
    pg.calls = calls
    return ctx, pg


def test_the_owners_own_dashboard_shows_nothing(browser, site):  # noqa: F811
    ctx, pg = open_with(browser, site, {"available": False})
    pg.wait_for_timeout(500)
    assert pg.calls, "the page asks /api/quota"
    assert pg.is_hidden("#quota")
    assert pg.errors == []
    ctx.close()


def test_a_guest_sees_today_of_the_limit(browser, site):  # noqa: F811
    ctx, pg = open_with(browser, site, USAGE)
    pg.wait_for_selector("#quota:not([hidden])")
    text = pg.inner_text("#quota")
    assert "Today 603K of 37M tokens" in text
    width = pg.eval_on_selector("#quota .bar i", "e => parseFloat(e.style.width)")
    assert 1 <= width <= 2.5                       # 603K of 37.3M ≈ 1.6%
    title = pg.get_attribute("#quota", "title")
    assert "resets in 5 h" in title and "Claude" in title
    assert pg.errors == []
    ctx.close()


def test_near_the_limit_it_turns_red(browser, site):  # noqa: F811
    ctx, pg = open_with(browser, site, dict(USAGE, spent_today=34_000_000))
    pg.wait_for_selector("#quota:not([hidden])")
    assert "crit" in pg.get_attribute("#quota", "class")
    ctx.close()


def test_at_the_limit_it_says_when_it_comes_back(browser, site):  # noqa: F811
    ctx, pg = open_with(browser, site, dict(USAGE, spent_today=37_400_000))
    pg.wait_for_selector("#quota:not([hidden])")
    text = pg.inner_text("#quota")
    assert "Daily limit reached" in text and "resets in 5 h" in text
    ctx.close()


def test_a_lifted_limit_is_said_so(browser, site):  # noqa: F811
    ctx, pg = open_with(browser, site, dict(USAGE, spent_today=40_000_000,
                                            unlimited_until=int(time.time()) + 3600))
    pg.wait_for_selector("#quota:not([hidden])")
    text = pg.inner_text("#quota")
    assert "no limit for now" in text and "Daily limit reached" not in text
    ctx.close()


@pytest.mark.parametrize("width,height,mobile", [(1440, 900, False), (390, 844, True)],
                         ids=["desktop", "phone"])
def test_it_fits(browser, site, width, height, mobile):  # noqa: F811
    ctx, pg = open_with(browser, site, USAGE, width=width, height=height, mobile=mobile)
    pg.wait_for_selector("#quota:not([hidden])")
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    box = pg.eval_on_selector("#quota", "e => { const r = e.getBoundingClientRect(); "
                                        "return [r.width, e.scrollWidth, e.clientWidth]; }")
    assert box[1] <= box[2] + 1                     # its own text is not cut off
    ctx.close()


def test_the_dashboard_pages_stay_the_same_file():
    from pathlib import Path
    web = Path(__file__).resolve().parents[1] / "web"
    assert (web / "index.html").read_text() == (web / "index-lib.html").read_text()

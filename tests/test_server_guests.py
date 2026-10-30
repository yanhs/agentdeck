"""Browser contract for the Guests block and its setup wizard on web/server.html (2.0).

/api/server, /api/services and /api/guests are faked by route(); see test_guests_api.py
for the API itself. Screenshots go to tests/artifacts/ (untracked).
"""
from __future__ import annotations

import copy
import json
import re

import pytest

from tests.test_server_page import FakeServerAPI, browser, shot, site  # noqa: F401

# `guestctl list --json` rows: a 5-hour and a weekly limit, like the subscription's (30.10.2026)
GUESTS = [
    {"name": "masha", "telegram_id": 111, "port": 18101, "state": "running", "enabled": True,
     "five_hour": {"used": 8_000_000, "limit": 20_000_000, "resets_at": 1793430000},
     "seven_day": {"used": 16_800_000, "limit": 140_000_000, "resets_at": 1793689200},
     "url": "https://sb-masha.example.com", "created": "2026-10-20 10:00:00"},
    {"name": "petya", "telegram_id": 222, "port": 18102, "state": "exited", "enabled": False,
     "five_hour": {"used": 0, "limit": 10_000_000, "resets_at": 0},
     "seven_day": {"used": 35_000_000, "limit": 70_000_000, "resets_at": 1793689200},
     "url": None, "created": "2026-10-21 10:00:00"},
]
CHECKS_BAD = [
    {"id": "docker", "title": "Docker", "ok": True, "detail": "Docker 26.1 works", "fix": ""},
    {"id": "bot", "title": "Guest bot", "ok": False, "detail": "No bot token yet",
     "fix": "Create a bot with @BotFather and paste its token"},
    {"id": "owner", "title": "Your Telegram ID", "ok": False, "detail": "Not set", "fix": "Paste your numeric ID"},
    {"id": "domain", "title": "Domain", "ok": False, "detail": "Not set", "fix": "Point *.example.com here"},
    {"id": "services", "title": "Services", "ok": False, "detail": "not installed",
     "fix": "Run guests/install.sh"},
]

CHECKS_OK = {"bot": "@my_guests_bot answers", "owner": "Telegram ID 42",
             "domain": "sb-test.example.com points to this server (203.0.113.7)"}


class FakeGuests:
    def __init__(self, configured=True, admin=True):
        self.configured = configured
        self.admin = {"bot_set": admin, "owner_id": "42" if admin else ""}
        self.guests = copy.deepcopy(GUESTS)
        self.checks = None
        self.jobs = {}
        self.posts = []
        self.open_error = None
        self.env = ({"bot_token_set": True, "owner_id": "42", "domain": "example.com"}
                    if configured else {"bot_token_set": False, "owner_id": "", "domain": ""})

    def handle(self, route):
        req = route.request
        path = re.sub(r"^https?://[^/]+", "", req.url).split("?")[0]
        if req.method == "POST":
            body = json.loads(req.post_data or "{}")
            self.posts.append((path, body, req.headers.get("content-type", "")))
            what = path.rsplit("/", 1)[1]
            if what == "check":
                self.checks = CHECKS_BAD
            elif what == "setup":
                if body.get("bot_token") == "bad":
                    return route.fulfill(status=400, content_type="application/json",
                                         body=json.dumps({"error": "That doesn't look like a bot token"}))
                self.checks = [dict(c, ok=c["id"] != "services", detail=CHECKS_OK.get(c["id"], c["detail"]))
                               for c in CHECKS_BAD]
                self.env = {"bot_token_set": True, "owner_id": body.get("owner_id", ""),
                            "domain": body.get("domain", "")}
            elif what == "open":
                if self.open_error:
                    return route.fulfill(status=409, content_type="application/json",
                                         body=json.dumps({"error": self.open_error}))
                return route.fulfill(status=200, content_type="application/json", body=json.dumps(
                    {"url": f"https://sb-{body['name']}.example.com/_owner-entry?t=T0KEN"}))
            else:
                g = next(x for x in self.guests if x["name"] == body["name"])
                if what == "stop":
                    g.update(state="exited", enabled=False)
                elif what == "start":
                    g.update(state="running", enabled=True)
                elif what == "remove":
                    self.guests.remove(g)
                elif what == "limit":            # guestctl: the week; 5 hours = a seventh
                    g["seven_day"]["limit"] = body["limit"]
                    g["five_hour"]["limit"] = round(body["limit"] / 7)
                self.jobs[body["name"]] = {"action": what, "done": True, "ok": True, "output": ""}
            return route.fulfill(status=200, content_type="application/json", body='{"ok": true}')
        state = {"installed": True, "configured": self.configured, "dir": "/home/me/agentkennel/guests",
                 "env": self.env, "admin": self.admin, "guests": self.guests if self.configured else None,
                 "list_error": None, "checks": self.checks, "checking": False, "jobs": self.jobs}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(state))


def services_none(route):
    route.fulfill(status=200, content_type="application/json",
                  body=json.dumps({"available": True, "services": {}, "dir": "/w/.services", "now": 0}))


def open_page(browser, site, fake, width=1280, height=900, mobile=False):  # noqa: F811
    ctx = browser.new_context(viewport={"width": width, "height": height},
                              is_mobile=mobile, has_touch=mobile)
    ctx.set_default_timeout(10000)
    ctx.route(re.compile(r"/api/services(/|\?|$)"), services_none)
    ctx.route(re.compile(r"/api/guests(/|\?|$)"), fake.handle)
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.on("console", lambda msg: pg.errors.append(msg.text) if msg.type == "error"
          and "Failed to load resource" not in msg.text else None)
    pg.route(re.compile(r"^https://fonts\.(googleapis|gstatic)\.com/"), lambda r: r.abort())
    pg.route(re.compile(r"/api/server(\?|$)"), FakeServerAPI().handle)
    pg.goto(site + "?poll=300")
    pg.wait_for_selector(".row[data-id]")
    return ctx, pg


@pytest.fixture
def fake():
    return FakeGuests()


@pytest.fixture
def page(browser, site, fake):  # noqa: F811
    ctx, pg = open_page(browser, site, fake)
    yield pg
    assert pg.errors == []
    ctx.close()


def guest_rows(pg):
    pg.wait_for_selector("#guests .guest[data-name]")
    return pg.eval_on_selector_all("#guests .guest[data-name]",
                                   "els => els.map(e => [e.dataset.name, e.innerText])")


def acts(pg, name):
    return pg.eval_on_selector_all(f'#guests .guest[data-name="{name}"] button[data-act]',
                                   "els => els.map(e => e.dataset.act)")


def bar_width(pg, name):
    return pg.eval_on_selector(f'#guests .guest[data-name="{name}"] .gspend .bar i',
                               "e => parseFloat(e.style.width)")


def test_lists_guests_with_their_five_hours_and_week(page):
    got = dict(guest_rows(page))
    assert set(got) == {"masha", "petya"}
    assert "5h 40% · wk 12%" in got["masha"]
    assert "5h 0% · wk 50%" in got["petya"]
    assert "sb-masha.example.com" in got["masha"]
    assert "off" in got["petya"].lower()
    assert page.get_attribute('#guests .guest[data-name="masha"] a', "href") == "https://sb-masha.example.com"
    assert "2 guests" in page.inner_text("#gSum")
    assert "week 52M" in page.inner_text("#gSum")          # the summary counts the week
    shot(page, "server-guests-desktop.png")


def test_the_bar_is_the_fuller_of_the_two(page):
    guest_rows(page)
    assert bar_width(page, "masha") == 40                  # 5 hours 40%, week 12%
    assert bar_width(page, "petya") == 50                  # 5 hours 0%, week 50%


def test_the_numbers_behind_the_percents_are_in_the_title(page):
    guest_rows(page)
    title = page.get_attribute('#guests .guest[data-name="masha"] .gspend', "title")
    assert "8M of 20M" in title and "17M of 140M" in title, title


def test_buttons_match_the_state(page):
    guest_rows(page)
    # Open only where there is a running dashboard with an address of its own
    assert acts(page, "masha") == ["open", "limit", "stop", "remove"]
    assert acts(page, "petya") == ["limit", "start", "remove"]


def test_spend_column_lines_up(page):
    """Rows have different buttons (Open only on a running guest with an address): the
    spend column still starts at one x on every row."""
    guest_rows(page)
    xs = page.eval_on_selector_all("#guests .guest .gspend",
                                   "els => els.map(e => Math.round(e.getBoundingClientRect().left))")
    assert len(xs) == 2 and len(set(xs)) == 1, xs


def guest_page(route):
    route.fulfill(status=200, content_type="text/html", body="<title>masha</title><p>guest dashboard</p>")


def test_open_signs_in_to_the_guest_dashboard(page, fake):
    """Decision 29.10.2026: the owner opens a guest's dashboard without their password."""
    guest_rows(page)
    page.context.route(re.compile(r"^https://sb-masha\.example\.com/"), guest_page)
    with page.expect_popup() as pop:
        page.click('#guests .guest[data-name="masha"] button[data-act="open"]')
    tab = pop.value
    tab.wait_for_url("https://sb-masha.example.com/_owner-entry?t=T0KEN")
    assert fake.posts[-1][:2] == ("/api/guests/open", {"name": "masha"})
    assert tab.evaluate("window.opener") is None       # the guest's page can't reach back
    title = page.get_attribute('#guests .guest[data-name="masha"] button[data-act="open"]', "title")
    assert "password" in title.lower() and "masha" in title
    tab.close()


def test_open_failure_says_why_and_closes_the_empty_tab(page, fake):
    guest_rows(page)
    fake.open_error = "masha's sandbox is not running"
    with page.expect_popup() as pop:
        page.click('#guests .guest[data-name="masha"] button[data-act="open"]')
    page.wait_for_selector("#gErr:not([hidden])")
    assert "masha's sandbox is not running" in page.inner_text("#gErr")
    page.wait_for_timeout(1000)                         # a few polls later: still there
    assert "masha's sandbox is not running" in page.inner_text("#gErr")
    tab = pop.value
    tab.wait_for_event("close", timeout=3000) if not tab.is_closed() else None
    assert tab.is_closed()


def test_stop_and_start(page, fake):
    guest_rows(page)
    page.click('#guests .guest[data-name="masha"] button[data-act="stop"]')
    page.wait_for_selector('#guests .guest[data-name="masha"] button[data-act="start"]')
    assert fake.posts[0][0] == "/api/guests/stop" and fake.posts[0][1] == {"name": "masha"}
    assert fake.posts[0][2].startswith("application/json")


def test_remove_asks_first(page, fake):
    guest_rows(page)
    seen = []
    page.once("dialog", lambda d: (seen.append(d.message), d.dismiss()))
    page.click('#guests .guest[data-name="petya"] button[data-act="remove"]')
    page.wait_for_timeout(300)
    assert seen and "petya" in seen[0] and fake.posts == []
    page.once("dialog", lambda d: d.accept())
    page.click('#guests .guest[data-name="petya"] button[data-act="remove"]')
    page.wait_for_selector('#guests .guest[data-name="petya"]', state="detached")
    assert fake.posts[-1][:2] == ("/api/guests/remove", {"name": "petya"})


def test_change_the_weekly_limit(page, fake):
    guest_rows(page)
    page.click('#guests .guest[data-name="masha"] button[data-act="limit"]')
    inp = page.wait_for_selector('#guests .guest[data-name="masha"] input.glimit-in')
    assert inp.input_value() == "140"
    editor = page.inner_text('#guests .guest[data-name="masha"] .glimit')
    assert "Weekly limit" in editor and "5 hours: a seventh" in editor, editor
    inp.fill("210")
    page.click('#guests .guest[data-name="masha"] button.glimit-save')
    page.wait_for_function(
        "() => document.querySelector('#guests .guest[data-name=\"masha\"]').innerText.includes('5h 27% · wk 8%')")
    assert fake.posts[-1][:2] == ("/api/guests/limit", {"name": "masha", "limit": 210_000_000})


def test_off_offers_the_wizard(browser, site):  # noqa: F811
    fake = FakeGuests(configured=False)
    ctx, pg = open_page(browser, site, fake)
    pg.wait_for_selector("#gOff:not([hidden])")
    assert "other people" in pg.inner_text("#gOff").lower()
    pg.click("#gSetup")
    pg.wait_for_selector("#gWizard:not([hidden])")
    # opening the wizard runs the checks
    pg.wait_for_selector('#gWizard .gstep[data-step="docker"][data-ok="1"]')
    assert fake.posts[0][0] == "/api/guests/check"
    assert pg.get_attribute('#gWizard .gstep[data-step="bot"]', "data-ok") == "0"
    assert "@BotFather" in pg.inner_text('#gWizard .gstep[data-step="bot"]')
    assert "Paste your numeric ID" not in pg.inner_text('#gWizard .gstep[data-step="bot"]')
    shot(pg, "server-guests-wizard.png")
    # no id to type: requests go to your own bot, whose owner is named
    assert pg.query_selector("#gOwner") is None
    assert "42" in pg.inner_text('#gWizard .gstep[data-step="bot"] .gadmin')
    # step 2 + 3: the token and the domain go in one save
    pg.fill("#gToken", "123456789:AAH" + "x" * 32)
    pg.fill("#gDomain", "example.com")
    pg.click("#gSave")
    pg.wait_for_selector('#gWizard .gstep[data-step="domain"][data-ok="1"]')
    path, body, _ = fake.posts[-1]
    assert path == "/api/guests/setup"
    assert body == {"bot_token": "123456789:AAH" + "x" * 32, "domain": "example.com"}
    assert pg.input_value("#gToken") == ""             # never kept on the page
    # step 4: the one command to run, with the module's real folder
    assert "sudo /home/me/agentkennel/guests/install.sh" in pg.inner_text('#gWizard .gstep[data-step="services"]')
    shot(pg, "server-guests-wizard-filled.png")
    assert pg.errors == []
    ctx.close()


def test_wizard_shows_a_refused_value(browser, site):  # noqa: F811
    fake = FakeGuests(configured=False)
    ctx, pg = open_page(browser, site, fake)
    pg.click("#gSetup")
    pg.fill("#gToken", "bad")
    pg.click("#gSave")
    pg.wait_for_selector("#gWizErr:not([hidden])")
    assert "bot token" in pg.inner_text("#gWizErr")
    assert pg.errors == []
    ctx.close()


def test_no_bot_of_your_own_sends_you_there_first(browser, site):  # noqa: F811
    fake = FakeGuests(configured=False, admin=False)
    ctx, pg = open_page(browser, site, fake)
    pg.click("#gSetup")
    step = pg.wait_for_selector('#gWizard .gstep[data-step="bot"] .gadmin a')
    assert step.get_attribute("href") == "/telegram"
    assert "your own" in pg.inner_text('#gWizard .gstep[data-step="bot"] .gadmin').lower()
    assert pg.get_attribute('#gWizard .gstep[data-step="bot"]', "data-ok") == "0"
    assert pg.is_disabled("#gSave")
    shot(pg, "server-guests-no-admin-bot.png")
    assert pg.errors == []
    ctx.close()


def test_not_installed_hides_the_block(browser, site):  # noqa: F811
    fake = FakeGuests(configured=False)
    orig = fake.handle

    def handle(route):
        if route.request.method == "GET":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"installed": False, "configured": False, "dir": "/x", "env": fake.env,
                 "guests": None, "list_error": None, "checks": None, "checking": False, "jobs": {}}))
        return orig(route)
    fake.handle = handle
    ctx, pg = open_page(browser, site, fake)
    pg.wait_for_timeout(700)
    assert pg.is_hidden("#guests")
    ctx.close()


def test_phone_width(browser, site):  # noqa: F811
    ctx, pg = open_page(browser, site, FakeGuests(), width=390, height=844, mobile=True)
    guest_rows(pg)
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(pg, "server-guests-phone.png")
    assert pg.errors == []
    ctx.close()


def test_phone_wizard(browser, site):  # noqa: F811
    ctx, pg = open_page(browser, site, FakeGuests(configured=False), width=390, height=844, mobile=True)
    pg.click("#gSetup")
    pg.wait_for_selector('#gWizard .gstep[data-step="docker"][data-ok="1"]')
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(pg, "server-guests-wizard-phone.png")
    ctx.close()

"""Browser contract: the Server page in a guest's sandbox (decision 30.10.2026).

server_status reports host.scope == "sandbox" when the cgroup limits it
(tests/test_server_status_sandbox.py): its own cores and RAM, "load" = cores in use now.
The page says it is the sandbox and drops what only makes sense for the whole machine:
the 5- and 15-minute load averages, the per-core bars and the host's uptime.
"""
from __future__ import annotations

from tests.test_server_page import FakeServerAPI, _open, browser, site  # noqa: F401


def sandbox_api():
    api = FakeServerAPI()
    api.snap["host"].update(scope="sandbox", hostname="maria", cores=2, cpu_pct=50.0,
                            cpu_per_core=[], load=[1.0, 1.0, 1.0], ram_total_mb=4096,
                            ram_used_mb=1024, ram_available_mb=3072, ram_pct=25.0)
    return api


def test_the_sandbox_shows_its_own_machine(browser, site):  # noqa: F811
    ctx, pg = _open(browser, site, sandbox_api())
    pg.wait_for_function("() => document.querySelector('#host').textContent.includes('maria')")
    assert "sandbox" in pg.inner_text("#host").lower()
    assert pg.inner_text("#g-cpu .gs") == "2 cores"
    assert pg.eval_on_selector_all("#g-cpu .cores .core", "els => els.length") == 0
    assert "4" in pg.inner_text("#g-ram .gs") and "GB" in pg.inner_text("#g-ram .gs")
    load = pg.inner_text("#g-load .gs")
    assert "of 2 cores" in load and "5m" not in load and "15m" not in load
    assert pg.inner_text("#g-load .gv") == "1.0"
    assert pg.inner_text("#uptime").strip() == ""
    ctx.close()


def test_the_owners_server_is_unchanged(browser, site):  # noqa: F811
    ctx, pg = _open(browser, site, FakeServerAPI())
    pg.wait_for_function("() => document.querySelector('#host').textContent.includes('demo-box')")
    assert "sandbox" not in pg.inner_text("#host").lower()
    assert "5m" in pg.inner_text("#g-load .gs") and "of 8 cores" in pg.inner_text("#g-load .gs")
    assert "up" in pg.inner_text("#uptime")
    ctx.close()

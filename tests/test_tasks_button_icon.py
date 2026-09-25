"""Dashboard's link to the task board shows a 'T' mark, not the 📋 notebook (owner 2026-09-25)."""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"


def test_tasks_link_is_a_T_mark():
    for name in ("index-lib.html", "index.html"):
        html = (WEB / name).read_text(encoding="utf-8")
        m = re.search(r'<a[^>]*href="/tasks/"[^>]*>(.*?)</a>', html, re.S)
        assert m, name
        assert re.sub(r"<[^>]+>", "", m.group(1)).strip() == "T", name
        assert "📋" not in m.group(1), name

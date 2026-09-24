"""Session library: named topic-sessions instead of numbered slots.

A session is one Claude conversation. Its identity is the first 8 hex chars of
the Claude session UUID (the transcript file is <uuid>.jsonl), so a log line
`cs-3beb2a44` greps straight to the transcript. Up to MAX_ACTIVE live in RAM;
the rest sit on disk and reload on open.
"""
import importlib.util
import json
import os

_spec = importlib.util.spec_from_file_location(
    "library", os.path.join(os.path.dirname(__file__), "..", "library.py"))
lib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lib)

U1 = "3beb2a44-a4d9-4494-96ae-8bbffa94cb1e"
U2 = "bef2d270-188b-468a-973e-4c66174ee289"


# ── identity ────────────────────────────────────────────────────────────────
def test_id_is_first_8_hex_of_uuid():
    assert lib.id_from_uuid(U1) == "3beb2a44"


def test_tmux_name():
    assert lib.tmux_name("3beb2a44") == "cs-3beb2a44"
    assert lib.id_from_tmux("cs-3beb2a44") == "3beb2a44"
    assert lib.id_from_tmux("claude-terminal-3") is None


def test_validate_id_rejects_anything_but_8_hex():
    assert lib.valid_id("3beb2a44")
    for bad in ["3BEB2A44", "3beb2a4", "3beb2a445", "3beb2a4g", "; rm -rf", "", None, "../x", "3beb2a44\n"]:
        assert not lib.valid_id(bad), bad


# ── create / load / save ────────────────────────────────────────────────────
def test_create_assigns_fresh_uuid_and_unique_id(tmp_path):
    L = lib.empty()
    a = lib.create(L, "ImmAppeal деплой", cwd="/home/ubuntu/pr", now=100)
    b = lib.create(L, "Налоги", cwd="/home/ubuntu/pr", now=101)
    assert a["id"] != b["id"] and lib.valid_id(a["id"])
    assert a["id"] == lib.id_from_uuid(a["uuid"])
    assert a["name"] == "ImmAppeal деплой" and a["created"] == 100 and a["last_used"] == 100
    assert a["archived"] is False


def test_create_with_known_uuid_refuses_duplicate():
    L = lib.empty()
    lib.create(L, "x", cwd="/", now=1, uuid=U1)
    try:
        lib.create(L, "y", cwd="/", now=2, uuid=U1)
        assert False, "duplicate id accepted"
    except ValueError:
        pass


def test_blank_name_gets_a_dated_default():
    L = lib.empty()
    e = lib.create(L, "   ", cwd="/", now=1790227538)   # 2026-09-24 05:25:38 UTC
    assert e["name"].startswith("Тема ")


def test_save_and_load_roundtrip(tmp_path):
    p = tmp_path / "library.json"
    L = lib.empty()
    lib.create(L, "Тема один", cwd="/", now=1, uuid=U1)
    lib.save(str(p), L)
    assert lib.load(str(p)) == L
    assert json.loads(p.read_text())["sessions"][0]["name"] == "Тема один"   # utf-8, readable


def test_load_missing_file_is_empty(tmp_path):
    assert lib.load(str(tmp_path / "nope.json")) == lib.empty()


def test_update_is_atomic_read_modify_write(tmp_path):
    p = str(tmp_path / "library.json")
    with lib.update(p) as L:
        lib.create(L, "a", cwd="/", now=1, uuid=U1)
    with lib.update(p) as L:
        lib.rename(L, "3beb2a44", "b")
    assert lib.find(lib.load(p), "3beb2a44")["name"] == "b"


# ── edit ────────────────────────────────────────────────────────────────────
def test_rename_archive_touch():
    L = lib.empty()
    lib.create(L, "a", cwd="/", now=1, uuid=U1)
    lib.rename(L, "3beb2a44", "  новое имя ")
    lib.touch(L, "3beb2a44", now=50)
    lib.archive(L, "3beb2a44")
    e = lib.find(L, "3beb2a44")
    assert e["name"] == "новое имя" and e["last_used"] == 50 and e["archived"] is True
    lib.archive(L, "3beb2a44", archived=False)
    assert lib.find(L, "3beb2a44")["archived"] is False


def test_unknown_id_raises_keyerror():
    L = lib.empty()
    for f in (lambda: lib.rename(L, "deadbeef", "x"), lambda: lib.touch(L, "deadbeef", 1),
              lambda: lib.archive(L, "deadbeef")):
        try:
            f()
            assert False
        except KeyError:
            pass


# ── search / resolve / order ────────────────────────────────────────────────
def _lib3():
    L = lib.empty()
    lib.create(L, "ImmAppeal деплой", cwd="/", now=10, uuid=U1)
    lib.create(L, "Налоги 2026", cwd="/", now=20, uuid=U2)
    lib.create(L, "Старая тема", cwd="/", now=5, uuid="aaaaaaaa-0000-0000-0000-000000000000")
    lib.archive(L, "aaaaaaaa")
    return L


def test_search_by_name_case_insensitive_cyrillic_and_id_prefix():
    L = _lib3()
    assert [e["id"] for e in lib.search(L, "НАЛОГ")] == ["bef2d270"]
    assert [e["id"] for e in lib.search(L, "immappeal")] == ["3beb2a44"]
    assert [e["id"] for e in lib.search(L, "3beb")] == ["3beb2a44"]
    assert lib.search(L, "старая") == []                       # archived hidden
    assert [e["id"] for e in lib.search(L, "старая", include_archived=True)] == ["aaaaaaaa"]
    assert len(lib.search(L, "")) == 2


def test_resolve_prefers_exact_id_then_prefix_then_name():
    L = _lib3()
    assert [e["id"] for e in lib.resolve(L, "bef2d270")] == ["bef2d270"]
    assert [e["id"] for e in lib.resolve(L, "bef2")] == ["bef2d270"]
    assert [e["id"] for e in lib.resolve(L, "деплой")] == ["3beb2a44"]
    assert lib.resolve(L, "нет такого") == []


def test_display_order_active_first_then_recent():
    L = _lib3()
    lib.archive(L, "aaaaaaaa", archived=False)
    order = [e["id"] for e in lib.display_order(L, active_ids={"aaaaaaaa"})]
    # active first (even though oldest), then inactive by last_used desc
    assert order == ["aaaaaaaa", "bef2d270", "3beb2a44"]


# ── LRU eviction ────────────────────────────────────────────────────────────
def test_pick_victim_is_least_recent_idle_unattached():
    live = [dict(id="a", last_used=30, attached=False, working=False),
            dict(id="b", last_used=10, attached=True, working=False),    # tab open
            dict(id="c", last_used=5, attached=False, working=True),     # busy
            dict(id="d", last_used=20, attached=False, working=False)]
    assert lib.pick_victim(live) == "d"


def test_pick_victim_none_when_everyone_is_busy_or_watched():
    live = [dict(id="a", last_used=1, attached=True, working=False),
            dict(id="b", last_used=2, attached=False, working=True)]
    assert lib.pick_victim(live) is None


def test_needs_eviction_only_at_limit():
    assert lib.needs_eviction(active_count=11, limit=12) is False
    assert lib.needs_eviction(active_count=12, limit=12) is True


# ── migration from numbered slots ───────────────────────────────────────────
def test_migrate_from_slots(tmp_path):
    sd = tmp_path / ".sessions"
    sd.mkdir()
    (sd / "agent-2.id").write_text(U1 + "\n")
    (sd / "agent-8.id").write_text(U2 + "\n")
    (sd / "agent-9.id").write_text("not-a-uuid\n")                  # skipped, reported
    agents = {"2": {"project": "app - PIPE, news"}, "_order": ["2", "8"]}
    L, report = lib.migrate_from_slots(str(sd), agents, cwd="/home/ubuntu/pr", now=100,
                                       last_used={"3beb2a44": 90})
    by = {e["id"]: e for e in L["sessions"]}
    assert by["3beb2a44"]["name"] == "app - PIPE, news" and by["3beb2a44"]["legacy_slot"] == 2
    assert by["3beb2a44"]["last_used"] == 90
    assert by["bef2d270"]["name"] == "терминал 8"
    assert [r for r in report if r["slot"] == 9][0]["skipped"]
    assert len(L["sessions"]) == 2


def test_migrate_is_idempotent_merge():
    L = lib.empty()
    lib.create(L, "уже есть", cwd="/", now=1, uuid=U1)
    L2 = lib.merge(L, {"sessions": [dict(lib.find(L, "3beb2a44"), name="другое")]})
    assert len(L2["sessions"]) == 1 and lib.find(L2, "3beb2a44")["name"] == "уже есть"

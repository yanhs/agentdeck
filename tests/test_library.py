"""Session library: named topic-sessions instead of numbered slots.

A session is one Claude conversation. Its identity is the first 8 hex chars of
the Claude session UUID (the transcript file is <uuid>.jsonl), so a log line
`cs-aaaaaaaa` greps straight to the transcript. Up to MAX_ACTIVE live in RAM;
the rest sit on disk and reload on open.
"""
import importlib.util
import json
import os

import pytest

_spec = importlib.util.spec_from_file_location(
    "library", os.path.join(os.path.dirname(__file__), "..", "library.py"))
lib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lib)

U1 = "aaaaaaaa-1111-4111-8111-111111111111"
U2 = "bbbbbbbb-2222-4222-8222-222222222222"


# ── identity ────────────────────────────────────────────────────────────────
def test_id_is_first_8_hex_of_uuid():
    assert lib.id_from_uuid(U1) == "aaaaaaaa"


def test_tmux_name():
    assert lib.tmux_name("aaaaaaaa") == "cs-aaaaaaaa"
    assert lib.id_from_tmux("cs-aaaaaaaa") == "aaaaaaaa"
    assert lib.id_from_tmux("claude-terminal-3") is None


def test_validate_id_rejects_anything_but_8_hex():
    assert lib.valid_id("aaaaaaaa")
    for bad in ["AAAAAAAA", "aaaaaaa", "aaaaaaaa5", "aaaaaaag", "; rm -rf", "", None, "../x", "aaaaaaaa\n"]:
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
    assert e["name"] == "Terminal 24.09 05:25"   # English, «Terminal DD.MM HH:MM»


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
        lib.rename(L, "aaaaaaaa", "b")
    assert lib.find(lib.load(p), "aaaaaaaa")["name"] == "b"


# ── edit ────────────────────────────────────────────────────────────────────
def test_rename_archive_touch():
    L = lib.empty()
    lib.create(L, "a", cwd="/", now=1, uuid=U1)
    lib.rename(L, "aaaaaaaa", "  новое имя ")
    lib.touch(L, "aaaaaaaa", now=50)
    lib.archive(L, "aaaaaaaa")
    e = lib.find(L, "aaaaaaaa")
    assert e["name"] == "новое имя" and e["last_used"] == 50 and e["archived"] is True
    lib.archive(L, "aaaaaaaa", archived=False)
    assert lib.find(L, "aaaaaaaa")["archived"] is False


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
    lib.create(L, "Старая тема", cwd="/", now=5, uuid="cccccccc-0000-0000-0000-000000000000")
    lib.archive(L, "cccccccc")
    return L


def test_search_by_name_case_insensitive_cyrillic_and_id_prefix():
    L = _lib3()
    assert [e["id"] for e in lib.search(L, "НАЛОГ")] == ["bbbbbbbb"]
    assert [e["id"] for e in lib.search(L, "immappeal")] == ["aaaaaaaa"]
    assert [e["id"] for e in lib.search(L, "aaaa")] == ["aaaaaaaa"]
    assert lib.search(L, "старая") == []                       # archived hidden
    assert [e["id"] for e in lib.search(L, "старая", include_archived=True)] == ["cccccccc"]
    assert len(lib.search(L, "")) == 2


def test_resolve_prefers_exact_id_then_prefix_then_name():
    L = _lib3()
    assert [e["id"] for e in lib.resolve(L, "bbbbbbbb")] == ["bbbbbbbb"]
    assert [e["id"] for e in lib.resolve(L, "bbbb")] == ["bbbbbbbb"]
    assert [e["id"] for e in lib.resolve(L, "деплой")] == ["aaaaaaaa"]
    assert lib.resolve(L, "нет такого") == []


def test_resolve_in_the_archive_only_when_asked():
    L = _lib3()
    assert lib.resolve(L, "старая") == []                      # archived hidden by default
    assert [e["id"] for e in lib.resolve(L, "старая", archived=True)] == ["cccccccc"]
    assert [e["id"] for e in lib.resolve(L, "cccc", archived=True)] == ["cccccccc"]
    assert lib.resolve(L, "налоги", archived=True) == []       # live ones are not in the archive


def test_display_order_active_first_then_recent():
    L = _lib3()
    lib.archive(L, "cccccccc", archived=False)
    order = [e["id"] for e in lib.display_order(L, active_ids={"cccccccc"})]
    # active first (even though oldest), then inactive by last_used desc
    assert order == ["cccccccc", "bbbbbbbb", "aaaaaaaa"]


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
                                       last_used={"aaaaaaaa": 90})
    by = {e["id"]: e for e in L["sessions"]}
    assert by["aaaaaaaa"]["name"] == "app - PIPE, news" and by["aaaaaaaa"]["legacy_slot"] == 2
    assert by["aaaaaaaa"]["last_used"] == 90
    assert by["bbbbbbbb"]["name"] == "терминал 8"
    assert [r for r in report if r["slot"] == 9][0]["skipped"]
    assert len(L["sessions"]) == 2


def test_migrate_is_idempotent_merge():
    L = lib.empty()
    lib.create(L, "уже есть", cwd="/", now=1, uuid=U1)
    L2 = lib.merge(L, {"sessions": [dict(lib.find(L, "aaaaaaaa"), name="другое")]})
    assert len(L2["sessions"]) == 1 and lib.find(L2, "aaaaaaaa")["name"] == "уже есть"


# ── a corrupt registry is never silently replaced ──────────────────────────
def test_load_corrupt_json_raises_a_clear_error(tmp_path):
    p = tmp_path / "library.json"
    p.write_text('{"sessions": [ {"id": "aaaa')                    # truncated write
    with pytest.raises(lib.CorruptRegistry) as ex:
        lib.load(str(p))
    assert str(p) in str(ex.value)


def test_load_non_object_json_raises(tmp_path):
    p = tmp_path / "library.json"
    p.write_text("[1, 2]")
    with pytest.raises(lib.CorruptRegistry):
        lib.load(str(p))


def test_update_on_corrupt_registry_raises_and_keeps_the_file(tmp_path):
    p = tmp_path / "library.json"
    p.write_text("{broken")
    with pytest.raises(lib.CorruptRegistry):
        with lib.update(str(p)) as L:
            lib.create(L, "x", cwd="/", now=1, uuid=U1)
    assert p.read_text() == "{broken"


def test_update_keeps_a_backup_of_the_previous_file(tmp_path):
    p = str(tmp_path / "library.json")
    with lib.update(p) as L:
        lib.create(L, "первая", cwd="/", now=1, uuid=U1)
    before = open(p, encoding="utf-8").read()
    with lib.update(p) as L:
        lib.rename(L, "aaaaaaaa", "вторая")
    assert open(p + ".bak", encoding="utf-8").read() == before
    assert lib.find(lib.load(p), "aaaaaaaa")["name"] == "вторая"


# ── eviction order: the later of last_used and last screen output ──────────
def test_pick_victim_uses_the_later_of_last_used_and_last_output():
    live = [dict(id="a", last_used=10, last_output=500, attached=False, working=False),
            dict(id="b", last_used=100, last_output=50, attached=False, working=False),
            dict(id="c", last_used=90, last_output=None, attached=False, working=False)]
    assert lib.pick_victim(live) == "c"          # a printed at 500, b used at 100, c at 90


# ── hold markers (a pending timer keeps a session loaded) ──────────────────
def test_hold_marker_lives_next_to_the_registry(tmp_path):
    reg = str(tmp_path / "reg" / "library.json")
    lib.set_hold("aaaaaaaa", 1000, lib_file=reg)
    assert (tmp_path / "reg" / "hold-aaaaaaaa").read_text().strip() == "1000"
    assert lib.hold_until("aaaaaaaa", lib_file=reg) == 1000
    assert lib.held("aaaaaaaa", now=999, lib_file=reg) is True
    assert lib.held("aaaaaaaa", now=1000, lib_file=reg) is False     # expired
    assert lib.held("bbbbbbbb", now=1, lib_file=reg) is False        # none


def test_hold_never_shortens_an_existing_one(tmp_path):
    reg = str(tmp_path / "library.json")
    lib.set_hold("aaaaaaaa", 5000, lib_file=reg)
    lib.set_hold("aaaaaaaa", 2000, lib_file=reg)
    assert lib.hold_until("aaaaaaaa", lib_file=reg) == 5000
    lib.set_hold("aaaaaaaa", 9000, lib_file=reg)
    assert lib.hold_until("aaaaaaaa", lib_file=reg) == 9000


def test_hold_rejects_bad_ids_and_ignores_garbage_files(tmp_path):
    reg = str(tmp_path / "library.json")
    for bad in ("../x", "AAAAAAAA", "", None, "aaaaaaaa/../y"):
        with pytest.raises(ValueError):
            lib.set_hold(bad, 10, lib_file=reg)
        assert lib.held(bad, now=1, lib_file=reg) is False
    (tmp_path / "hold-aaaaaaaa").write_text("not a number")
    assert lib.held("aaaaaaaa", now=1, lib_file=reg) is False


# ── delete (archived topics only; the API enforces that) ────────────────────
def test_delete_removes_only_that_entry():
    L = lib.empty()
    lib.create(L, "a", cwd="/x", now=1, uuid=U1)
    lib.create(L, "b", cwd="/x", now=2, uuid=U2)
    e = lib.delete(L, "aaaaaaaa")
    assert e["uuid"] == U1
    assert [x["id"] for x in L["sessions"]] == ["bbbbbbbb"]
    with pytest.raises(KeyError):
        lib.delete(L, "aaaaaaaa")
    with pytest.raises(KeyError):
        lib.delete(L, "deadbeef")


def test_cwd_slug_matches_claude_project_dir_names():
    assert lib.cwd_slug("/home/ubuntu/pr") == "-home-ubuntu-pr"
    assert lib.cwd_slug("/home/ubuntu/pr/Светлота 💡").startswith("-home-ubuntu-pr-")
    # UTF-16 code units, as Claude Code's regex sees them: 💡 is two
    assert lib.cwd_slug("/home/ubuntu/pr/Светлота 💡 (Rus)") == "-home-ubuntu-pr--------------Rus-"


def test_trash_transcript_finds_a_long_cwds_folder(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    monkeypatch.setenv("AGENTDECK_CLAUDE_PROJECTS", str(projects))
    cwd = "/home/ubuntu/" + "deep/" * 45 + "end"
    d = projects / (("-home-ubuntu-" + "deep-" * 45 + "end")[:200] + "-cqcn6p")
    d.mkdir(parents=True)
    (d / f"{U1}.jsonl").write_text("talk")
    out = lib.trash_transcript(_entry(cwd=cwd), lib_file=str(tmp_path / "library.json"))
    assert out == str(tmp_path / "trash" / f"{U1}.jsonl")
    assert not (d / f"{U1}.jsonl").exists()


def _entry(cwd="/home/ubuntu/pr", u=U1):
    return {"id": lib.id_from_uuid(u), "uuid": u, "name": "x", "cwd": cwd}


def test_trash_transcript_moves_file_and_sibling_folder(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    monkeypatch.setenv("AGENTDECK_CLAUDE_PROJECTS", str(projects))
    d = projects / "-home-ubuntu-pr"
    (d / U1).mkdir(parents=True)
    (d / U1 / "sub.jsonl").write_text("sub")
    (d / f"{U1}.jsonl").write_text("talk")
    (d / f"{U2}.jsonl").write_text("other")               # a neighbour: untouched
    reg = tmp_path / "reg" / "library.json"
    out = lib.trash_transcript(_entry(), lib_file=str(reg))
    trash = tmp_path / "reg" / "trash"
    assert out == str(trash / f"{U1}.jsonl")
    assert (trash / f"{U1}.jsonl").read_text() == "talk"
    assert (trash / U1 / "sub.jsonl").read_text() == "sub"
    assert not (d / f"{U1}.jsonl").exists() and not (d / U1).exists()
    assert (d / f"{U2}.jsonl").read_text() == "other"


def test_trash_transcript_missing_file_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTDECK_CLAUDE_PROJECTS", str(tmp_path / "projects"))
    reg = tmp_path / "library.json"
    assert lib.trash_transcript(_entry(), lib_file=str(reg)) is None


def test_trash_transcript_never_overwrites_an_earlier_trashed_copy(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    monkeypatch.setenv("AGENTDECK_CLAUDE_PROJECTS", str(projects))
    d = projects / "-home-ubuntu-pr"
    d.mkdir(parents=True)
    reg = tmp_path / "library.json"
    (tmp_path / "trash").mkdir()
    (tmp_path / "trash" / f"{U1}.jsonl").write_text("older")
    (d / f"{U1}.jsonl").write_text("newer")
    out = lib.trash_transcript(_entry(), lib_file=str(reg))
    assert out and out != str(tmp_path / "trash" / f"{U1}.jsonl")
    assert open(out).read() == "newer"
    assert (tmp_path / "trash" / f"{U1}.jsonl").read_text() == "older"


def test_trash_transcript_refuses_a_malformed_uuid(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTDECK_CLAUDE_PROJECTS", str(tmp_path / "projects"))
    for bad in ("../../etc/passwd", "", None, "aaaaaaaa"):
        with pytest.raises(ValueError):
            lib.trash_transcript({"id": "aaaaaaaa", "uuid": bad, "cwd": "/x"},
                                 lib_file=str(tmp_path / "library.json"))


# ── manual order (drag-and-drop on the page) ────────────────────────────────
U3 = "cccccccc-3333-4333-8333-333333333333"
U4 = "dddddddd-4444-4444-8444-444444444444"


def _lib4():
    L = lib.empty()
    for u, t in ((U1, 10), (U2, 20), (U3, 30), (U4, 40)):
        lib.create(L, u[:1], cwd="/", now=t, uuid=u)
    return L


def test_reorder_sets_pos_and_display_order_follows_it():
    L = _lib4()
    assert [e["id"][:1] for e in lib.display_order(L, set())] == ["d", "c", "b", "a"]  # by recency
    lib.reorder(L, ["aaaaaaaa", "cccccccc", "bbbbbbbb", "dddddddd"])
    assert [e["id"][:1] for e in lib.display_order(L, set())] == ["a", "c", "b", "d"]
    assert [lib.find(L, s)["pos"] for s in ("aaaaaaaa", "cccccccc")] == [0, 1]


def test_active_group_still_comes_first_with_manual_order():
    L = _lib4()
    lib.reorder(L, ["aaaaaaaa", "bbbbbbbb", "cccccccc", "dddddddd"])
    order = [e["id"][:1] for e in lib.display_order(L, {"cccccccc", "bbbbbbbb"})]
    assert order == ["b", "c", "a", "d"]


def test_entries_without_pos_go_by_recency_after_positioned_ones():
    L = _lib4()
    lib.reorder(L, ["aaaaaaaa", "bbbbbbbb"])
    assert [e["id"][:1] for e in lib.display_order(L, set())] == ["a", "b", "d", "c"]


def test_new_entry_goes_to_the_top_of_its_group_when_order_is_manual():
    L = _lib4()
    lib.reorder(L, ["aaaaaaaa", "bbbbbbbb", "cccccccc", "dddddddd"])
    e = lib.create(L, "new", cwd="/", now=1, uuid="eeeeeeee-5555-4555-8555-555555555555")
    assert [x["id"][:1] for x in lib.display_order(L, set())][0] == "e"
    assert e["pos"] < 0
    # without any manual order a new one is on top by recency, no pos needed
    L2 = lib.empty()
    lib.create(L2, "old", cwd="/", now=10, uuid=U1)
    n = lib.create(L2, "new", cwd="/", now=20, uuid=U2)
    assert "pos" not in n and lib.display_order(L2, set())[0]["id"] == n["id"]


def test_reorder_unknown_id_raises_and_changes_nothing():
    L = _lib4()
    with pytest.raises(KeyError):
        lib.reorder(L, ["aaaaaaaa", "deadbeef"])
    assert all("pos" not in e for e in L["sessions"])

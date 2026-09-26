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


# ── one number per terminal: the terminal follows its conversation ─────────
# When the conversation live in terminal A becomes B (Claude's bypass-consent
# relaunch, /clear, /resume), the terminal becomes B. What happens to A depends
# on whether A ever held a conversation (a transcript with messages).
UA = "a1a1a1a1-1111-4111-8111-111111111111"
UB = "b2b2b2b2-2222-4222-8222-222222222222"


def _terminal(L, name="Deploy", now=100, u=UA, pos=None, archived=False):
    e = lib.create(L, name, cwd="/home/ubuntu/pr", now=now, uuid=u)
    if pos is not None:
        e["pos"] = pos
    e["archived"] = archived
    return e


def test_switch_without_messages_moves_the_terminal_to_the_new_number():
    # the consent case: A never held a conversation, so A simply was never one
    L = lib.empty()
    _terminal(L, pos=3)
    L["sessions"][0]["last_used"] = 150
    e = lib.switch(L, "a1a1a1a1", UB, False, now=500)
    assert [x["id"] for x in L["sessions"]] == ["b2b2b2b2"]
    assert e is L["sessions"][0]
    assert e["uuid"] == UB and e["id"] == lib.id_from_uuid(e["uuid"])
    assert e["name"] == "Deploy" and e["cwd"] == "/home/ubuntu/pr"
    assert e["created"] == 100 and e["last_used"] == 150 and e["pos"] == 3
    assert e["archived"] is False
    assert e["aliases"] == ["a1a1a1a1"] and e["prev_id"] == "a1a1a1a1" and e["switched_at"] == 500
    assert lib.find(L, "a1a1a1a1") is None


def test_switch_without_messages_keeps_earlier_aliases_once():
    L = lib.empty()
    e = _terminal(L)
    e["aliases"] = ["0000aaaa"]
    lib.switch(L, "a1a1a1a1", UB, False, now=500)
    UC = "c3c3c3c3-3333-4333-8333-333333333333"
    e = lib.switch(L, "b2b2b2b2", UC, False, now=600)
    assert e["id"] == "c3c3c3c3" and e["aliases"] == ["0000aaaa", "a1a1a1a1", "b2b2b2b2"]
    assert e["prev_id"] == "b2b2b2b2" and e["switched_at"] == 600


def test_switch_with_messages_keeps_the_old_conversation_as_an_earlier_row():
    # /clear after work: A is a real conversation; the terminal (name, place in
    # the list, archive state) goes on as B, A stays as its own unloaded row
    L = lib.empty()
    a = _terminal(L, pos=2)
    a["aliases"] = ["0000aaaa"]
    b = lib.switch(L, "a1a1a1a1", UB, True, now=500)
    assert b["id"] == "b2b2b2b2" and b["uuid"] == UB
    assert b["name"] == "Deploy" and b["cwd"] == "/home/ubuntu/pr" and b["created"] == 100
    assert b["last_used"] == 100 and b["archived"] is False and b["pos"] == 2
    assert b["prev_id"] == "a1a1a1a1" and b["switched_at"] == 500
    assert "aliases" not in b                     # A still exists: no alias to it
    a = lib.find(L, "a1a1a1a1")
    assert a["uuid"] == UA and a["name"] == "Deploy (earlier)" and "pos" not in a
    assert a["aliases"] == ["0000aaaa"]
    for e in L["sessions"]:
        assert e["id"] == lib.id_from_uuid(e["uuid"])


def test_switch_with_messages_cuts_a_long_name_before_the_suffix():
    L = lib.empty()
    _terminal(L, name="x" * 200)
    lib.switch(L, "a1a1a1a1", UB, True, now=500)
    assert lib.find(L, "a1a1a1a1")["name"] == "x" * 190 + " (earlier)"
    assert lib.find(L, "b2b2b2b2")["name"] == "x" * 200


def test_switch_to_a_conversation_already_in_the_library():
    # /resume of a terminal's conversation that sits archived in the list:
    # B comes back (keeps its own name), A goes or stays by its transcript
    for a_msgs in (False, True):
        L = lib.empty()
        a = _terminal(L, name="Scratch", now=100)
        a["aliases"] = ["0000aaaa"]
        _terminal(L, name="Taxes", now=50, u=UB, archived=True)
        b = lib.switch(L, "a1a1a1a1", UB, a_msgs, now=700)
        assert b is lib.find(L, "b2b2b2b2")
        assert b["name"] == "Taxes" and b["archived"] is False
        assert b["prev_id"] == "a1a1a1a1" and b["switched_at"] == 700 and b["last_used"] == 700
        if a_msgs:
            assert lib.find(L, "a1a1a1a1")["name"] == "Scratch"      # unchanged
            assert "aliases" not in b
        else:
            assert lib.find(L, "a1a1a1a1") is None
            assert b["aliases"] == ["a1a1a1a1", "0000aaaa"]


def test_switch_refuses_an_8hex_collision_with_another_uuid():
    L = lib.empty()
    _terminal(L)
    other = "b2b2b2b2-9999-4999-8999-999999999999"        # same 8 hex, other conversation
    _terminal(L, name="Other", u=other)
    before = json.dumps(L, sort_keys=True)
    with pytest.raises(ValueError):
        lib.switch(L, "a1a1a1a1", UB, False, now=500)
    with pytest.raises(ValueError):
        lib.switch(L, "a1a1a1a1", "not-a-uuid", False, now=500)
    with pytest.raises(KeyError):
        lib.switch(L, "deadbeef", UB, False, now=500)
    assert json.dumps(L, sort_keys=True) == before


def test_find_or_alias_and_resolve_follow_a_vanished_number():
    L = lib.empty()
    _terminal(L)
    lib.switch(L, "a1a1a1a1", UB, False, now=500)
    assert lib.find(L, "a1a1a1a1") is None                  # find() stays exact
    assert lib.find_or_alias(L, "a1a1a1a1")["id"] == "b2b2b2b2"
    assert lib.find_or_alias(L, "b2b2b2b2")["id"] == "b2b2b2b2"
    assert lib.find_or_alias(L, "deadbeef") is None
    assert [e["id"] for e in lib.resolve(L, "a1a1a1a1")] == ["b2b2b2b2"]
    # an exact id wins over someone's alias
    _terminal(L, name="New", u="a1a1a1a1-5555-4555-8555-555555555555")
    assert lib.find_or_alias(L, "a1a1a1a1")["name"] == "New"


def test_create_never_draws_a_number_that_is_an_alias(monkeypatch):
    L = lib.empty()
    _terminal(L)
    lib.switch(L, "a1a1a1a1", UB, False, now=500)
    draws = iter([UA, "c3c3c3c3-3333-4333-8333-333333333333"])
    monkeypatch.setattr(lib._uuid, "uuid4", lambda: next(draws))
    e = lib.create(L, "fresh", cwd="/", now=600)
    assert e["id"] == "c3c3c3c3"


def test_has_messages(tmp_path):
    p = tmp_path / "t.jsonl"
    assert lib.has_messages(str(p)) is False                 # no file
    p.write_text('{"type":"summary","summary":"x"}\n'
                 '{"type":"file-history-snapshot","snapshot":{}}\n')
    assert lib.has_messages(str(p)) is False
    p.write_text(p.read_text() + "not json\n"
                 '{"type":"user","message":{"role":"user","content":"hi"}}\n')
    assert lib.has_messages(str(p)) is True
    q = tmp_path / "a.jsonl"
    q.write_text('{"parentUuid":null,"type":"assistant","message":{"content":[]}}\n')
    assert lib.has_messages(str(q)) is True
    # a mention inside some other record is not a message
    r = tmp_path / "r.jsonl"
    r.write_text('{"type":"summary","summary":"\\"type\\":\\"user\\""}\n')
    assert lib.has_messages(str(r)) is False
    assert lib.has_messages(str(tmp_path)) is False          # a directory


def test_a_conversation_holding_only_slash_commands_has_no_messages(tmp_path):
    # what Claude Code 2.1.283 writes right after /clear (seen on a live probe)
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(d) for d in [
        {"type": "user", "isMeta": True, "message": {"role": "user", "content":
            "<local-command-caveat>Caveat: The messages below were generated by the user"
            " while running local commands.</local-command-caveat>"}},
        {"type": "user", "message": {"role": "user", "content":
            "<command-name>/clear</command-name>\n <command-message>clear</command-message>"}},
        {"type": "user", "message": {"role": "user", "content":
            "<local-command-stdout></local-command-stdout>"}},
        {"type": "assistant", "message": {"model": "<synthetic>", "content": []}},
        {"type": "attachment", "attachment": {"type": "hook_success"}},
    ]) + "\n")
    assert lib.has_messages(str(p)) is False
    with open(p, "a") as f:                                    # a prompt makes it one
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "deploy it"}]}}) + "\n")
    assert lib.has_messages(str(p)) is True


def test_move_hold_carries_the_hold_to_the_new_number(tmp_path):
    reg = str(tmp_path / "library.json")
    lib.set_hold("a1a1a1a1", 5000, lib_file=reg)
    lib.move_hold("a1a1a1a1", "b2b2b2b2", lib_file=reg)
    assert lib.hold_until("b2b2b2b2", lib_file=reg) == 5000
    assert not (tmp_path / "hold-a1a1a1a1").exists()
    lib.set_hold("b2b2b2b2", 9000, lib_file=reg)               # a later one is kept
    lib.set_hold("c3c3c3c3", 100, lib_file=reg)
    lib.move_hold("c3c3c3c3", "b2b2b2b2", lib_file=reg)
    assert lib.hold_until("b2b2b2b2", lib_file=reg) == 9000
    lib.move_hold("deadbeef", "b2b2b2b2", lib_file=reg)        # none: nothing happens
    assert lib.hold_until("b2b2b2b2", lib_file=reg) == 9000

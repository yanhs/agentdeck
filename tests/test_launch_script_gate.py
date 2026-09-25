"""Integration test: what each launch-claude*.sh does TODAY.

Slots 1-9 were migrated into the session library (migrate_library.py): their
launch-claude*.sh is a shim (the original is kept as <script>.pre-library).
  * legacy tmux session claude-terminal[-N] still running -> attach to it
    (exact target "=name"), nothing else;
  * otherwise -> exec open-session.sh <LIB_ID> (the library loads cs-<id>).
  The order gate (_order_gate.py / agents.json _order) does NOT apply to them:
  the library decides what loads, so the shim never consults it.

Slots 10-12 were not migrated and still use the gated scripts:
  * id in _order (or no _order at all) -> `tmux new-session`;
  * id NOT in _order -> "is not in /agents/" notice, no new-session.

Everything runs in a temp stage dir with a fake `tmux` first on PATH and
AGENTDECK_TMUX_SOCKET set to a private name, so no real tmux server (and no
live claude-terminal*/cs-* session) is ever reached; open-session.sh is a
recording stub, so the real registry is never read.
"""
from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest


TERMINAL = Path(__file__).resolve().parents[1]
MIGRATED = {n: TERMINAL / ("launch-claude.sh" if n == 1 else f"launch-claude-{n}.sh")
            for n in range(1, 10)}
GATED = {n: TERMINAL / f"launch-claude-{n}.sh" for n in (10, 11, 12)}


def _legacy_name(n: int) -> str:
    return "claude-terminal" if n == 1 else f"claude-terminal-{n}"


def _lib_id(script: Path) -> str:
    m = re.search(r'^LIB_ID="([0-9a-f]{8})"$', script.read_text(), re.M)
    assert m, f"{script.name}: no LIB_ID — not a library shim"
    return m.group(1)


def _stage(tmp: Path, script: Path, agents_json: str = "{}", running: tuple = ()):
    """Copy `script` into a private stage dir next to _order_gate.py, a fixture
    agents.json and a recording open-session.sh stub; a fake tmux reports
    `running` sessions and logs every call."""
    call_log = tmp / "tmux-calls.log"
    call_log.touch()
    open_log = tmp / "open-session.log"
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    listed = "\n".join(running)
    has = " ".join(f'"={s}"' for s in running)
    tmux = bin_dir / "tmux"
    tmux.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        echo "$@" >> "{call_log}"
        [ "$1" = "-L" ] && shift 2
        case "$1" in
          list-sessions) printf '%s\\n' "{listed}" | sed '/^$/d'; exit 0 ;;
          has-session) for s in {has}; do [ "$3" = "$s" ] && exit 0; done; exit 1 ;;
          capture-pane) echo "bypass permissions"; exit 0 ;;
          *) exit 0 ;;
        esac
    """))
    tmux.chmod(0o755)

    stage = tmp / "stage"
    stage.mkdir()
    (stage / "_order_gate.py").write_bytes((TERMINAL / "_order_gate.py").read_bytes())
    (stage / "agents.json").write_text(agents_json)
    stub = stage / "open-session.sh"
    stub.write_text(f'#!/bin/bash\necho "$@" >> "{open_log}"\n')
    stub.chmod(0o755)
    target = stage / script.name
    target.write_bytes(script.read_bytes())
    target.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["DRY_RUN"] = "0"
    env["AGENTDECK_TMUX_SOCKET"] = f"agentdeck-test-launch-{os.getpid()}"
    env["HOME"] = str(tmp / "home")                    # no real ~/.claude transcripts
    env["AGENTDECK_WORKDIR"] = str(tmp / "work")
    (tmp / "home").mkdir()
    (tmp / "work").mkdir()
    proc = subprocess.run(["bash", str(target)], env=env, capture_output=True,
                          text=True, timeout=15)
    opened = open_log.read_text() if open_log.exists() else ""
    return proc.returncode, call_log.read_text(), opened, proc.stdout + proc.stderr


# ── slots 1-9: library shims ────────────────────────────────────────────────

@pytest.mark.parametrize("n", sorted(MIGRATED))
def test_migrated_slot_is_a_library_shim_without_order_gate(n):
    text = MIGRATED[n].read_text()
    assert "MIGRATED-BY migrate_library.py" in text
    assert f'SESSION="{_legacy_name(n)}"' in text
    assert "_order_gate" not in text
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "display-message" not in code          # tmux 3.2a crashes the whole server
    assert Path(str(MIGRATED[n]) + ".pre-library").exists(), "original must be kept"


@pytest.mark.parametrize("n", sorted(MIGRATED))
def test_migrated_slot_opens_its_library_topic_when_no_legacy_session(tmp_path, n):
    # _order excludes the slot: the gate would have blocked it; the shim must not care
    rc, calls, opened, out = _stage(tmp_path, MIGRATED[n], '{"_order": ["99"]}')
    assert opened.split() == [_lib_id(MIGRATED[n])], f"{calls}\n---\n{out}"
    assert "new-session" not in calls and "attach-session" not in calls
    assert "is not in /agents/" not in out


@pytest.mark.parametrize("n", sorted(MIGRATED))
def test_migrated_slot_attaches_to_its_running_legacy_session(tmp_path, n):
    # a similarly named session must not match (exact name, not a prefix)
    rc, calls, opened, out = _stage(tmp_path, MIGRATED[n],
                                    running=(_legacy_name(n) + "x", _legacy_name(n)))
    assert f"attach-session -t ={_legacy_name(n)}" in calls, f"{calls}\n---\n{out}"
    assert opened == ""
    assert "new-session" not in calls


def test_migrated_slot_ignores_a_prefix_named_session(tmp_path):
    rc, calls, opened, out = _stage(tmp_path, MIGRATED[3], running=("claude-terminal-30",))
    assert "attach-session" not in calls
    assert opened.split() == [_lib_id(MIGRATED[3])]


def test_migrated_slot_uses_the_private_tmux_socket(tmp_path):
    rc, calls, opened, out = _stage(tmp_path, MIGRATED[2], running=("claude-terminal-2",))
    assert all(line.startswith(f"-L agentdeck-test-launch-{os.getpid()} ")
               for line in calls.splitlines()), calls


# ── slots 10-12: still the order-gated scripts ─────────────────────────────

@pytest.mark.parametrize("n", sorted(GATED))
def test_unmigrated_slot_proceeds_when_in_order(tmp_path, n):
    rc, calls, opened, out = _stage(tmp_path, GATED[n], f'{{"_order": ["{n}"]}}')
    assert "new-session" in calls, f"expected new-session call, got:\n{calls}\n---\n{out}"
    assert "is not in /agents/" not in out
    assert opened == ""


@pytest.mark.parametrize("n", sorted(GATED))
def test_unmigrated_slot_blocked_when_not_in_order(tmp_path, n):
    rc, calls, opened, out = _stage(tmp_path, GATED[n], '{"_order": ["1", "2"]}')
    assert rc == 0, f"gate must exit cleanly, got rc={rc}"
    assert "new-session" not in calls, f"gate FAILED:\n{calls}\n---\n{out}"
    assert f"Agent #{n} is not in /agents/" in out


def test_unmigrated_slot_proceeds_when_no_order_field(tmp_path):
    """No _order field -> fail-open (back-compat with fresh installs)."""
    rc, calls, opened, out = _stage(tmp_path, GATED[10], "{}")
    assert "new-session" in calls, f"missing _order should allow launch:\n{calls}\n{out}"

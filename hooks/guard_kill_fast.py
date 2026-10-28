"""guard_kill fast path: decide from the text alone whether the command needs a look at all.

Shared by the entry point (guard_kill.py) and the core (guard_kill_core.py: nested `bash -c`,
eval, text piped into a shell), so the rule lives in one place. Only `re` is imported here:
this module is loaded on EVERY Bash call and must not pull in anything heavy.

Three answers:

* ``"strict"`` - the text has a word that sends signals (kill, tmux, systemctl, xargs...),
  also after the shell strips quotes and backslashes: `k'i'll`, `k\\ill`, `"ki"ll` are the
  same kill. Strict parsing: whatever cannot be parsed is blocked.
* ``"lenient"`` - no such word, but a command name may be assembled at run time: a `$...`
  or `` `...` `` substitution, a `$'\\x6b...'` string, a `k?ll` glob, text read by a shell
  or eval (`printf ... | bash`), `service` (a systemctl wrapper). The core parses it and
  blocks only what really reaches a signal; a command it cannot parse is let through.
* ``None`` - nothing to look at, exit without loading the core.
"""
import re

FAST = re.compile(r"kill|xargs|pidof|tmux|systemctl|fuser")
# What the shell strips before it looks at the command name.
UNQUOTE = re.compile(r"\\\n|['\"\\]")
# Signs that a word is only assembled at run time.
COMPUTED = re.compile(r"[$`\\*?\[]|\b(?:ba|da|z|k|mk|a)?sh\b|\beval\b|\bsource\b|\bservice\b")


def look(text):
    if FAST.search(text):
        return "strict"
    if ("'" in text or '"' in text or "\\" in text) and FAST.search(UNQUOTE.sub("", text)):
        return "strict"
    if COMPUTED.search(text):
        return "lenient"
    return None

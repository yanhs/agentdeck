"""guard_kill.py core: parse the command and work out whom a signal would reach.

Split out of guard_kill.py for speed: the guard runs before every Bash call, and a big file
run as a script is recompiled on every run (~40 ms). A module is compiled once and loaded
from __pycache__. The rule itself is described in guard_kill.py.

Modules that start processes (subprocess, tempfile, select, signal, glob, fnmatch) are loaded
lazily, where they are used: the core also looks at commands without signal words (a command
name assembled at run time), and paying ~15 ms of imports for each of those is pointless.
"""
import os
import re
import stat

from guard_kill_fast import UNQUOTE, look

SUB_TIMEOUT = 5.0          # per read-only command
MAX_OUT = 1 << 20          # more read-only command output than ever needed
MAX_DEPTH = 12             # nesting of bash -c / $( ) / eval
LIST_MAX = 5

# Marks a command that sends signals - for words whose value cannot be known.
KILLISH = re.compile(r"\b(kill|pkill|killall5?|skill|killp|killw)\b|\bfuser\b[^;&|]*\s-\w*k"
                     r"|\bkill-(server|session|window|pane)\b|\brespawn(-pane|-window|p|w)\b"
                     r"|\bsystemctl\b[^;&|]*\b(stop|restart|kill|isolate|freeze|reload)\b")

# Environment variables the guard and the command share (the agent does not set them).
STABLE_ENV = ("HOME", "USER", "LOGNAME", "XDG_RUNTIME_DIR", "TMPDIR", "SHELL")

OWN = "\x00own"            # $!, $$, $BASHPID, jobs -p: certainly our own process
RAND = "\x00random"        # $RANDOM: a name for our own socket, but not a PID
FRESH = "/nonexistent-fresh-mktemp"   # $(mktemp ...): a new path, never a live socket


class ParseError(Exception):
    pass


class Dynamic(Exception):
    """The value cannot be known before the command runs."""


# ==========================================================================================
# Command line parsing
# ==========================================================================================

class Part:
    __slots__ = ("kind", "text", "quoted", "raw", "prog")

    def __init__(self, kind, text, quoted=False, raw="", prog=None):
        self.kind = kind        # lit | var | cmd | arith | proc
        self.text = text        # lit: value; var: name; cmd/proc: inner text
        self.quoted = quoted
        self.raw = raw or text
        self.prog = prog


class Word:
    __slots__ = ("parts", "raw", "array")

    def __init__(self, parts, raw, array=None):
        self.parts = parts
        self.raw = raw
        self.array = array

    def literal(self):
        return all(p.kind == "lit" for p in self.parts)

    def value(self):
        return "".join(p.text for p in self.parts)

    def script(self):
        """Text that `bash -c` gets: literals as is, substitutions as source."""
        return "".join(p.text if p.kind == "lit" else p.raw for p in self.parts)

    def unquoted_glob(self):
        return any(p.kind == "lit" and not p.quoted and re.search(r"[*?\[]", p.text)
                   for p in self.parts)


class Redirect:
    __slots__ = ("op", "target", "body", "delim", "strip")

    def __init__(self, op, target=None, delim=None, strip=False):
        self.op = op
        self.target = target
        self.body = None
        self.delim = delim
        self.strip = strip


class Simple:
    __slots__ = ("assigns", "words", "redirs", "start", "end", "src")

    def __init__(self, src, start):
        self.assigns = []       # [(name, Word, export_hint)]
        self.words = []
        self.redirs = []
        self.src = src
        self.start = start
        self.end = start

    def empty(self):
        return not (self.assigns or self.words or self.redirs)

    @property
    def raw(self):
        return self.src[self.start:self.end].strip()


class Group:
    __slots__ = ("prog", "redirs", "raw")

    def __init__(self, prog, raw):
        self.prog = prog
        self.redirs = []
        self.raw = raw


ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\+?=)")
REDIR = re.compile(r"(\d*)(&>>|&>|>>|>\||>&|<<<|<<-|<<|<>|<&|>|<)")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Lexer:
    def __init__(self, src, depth=0):
        if depth > MAX_DEPTH:
            raise ParseError("nesting too deep")
        self.s = src
        self.n = len(src)
        self.i = 0
        self.depth = depth
        self.pending = []

    # --- command list --------------------------------------------------------------------
    def parse(self):
        prog = self.parse_list(stop_paren=False)
        return prog

    def parse_list(self, stop_paren):
        s = self.s
        prog, pipe = [], []
        cur = Simple(s, self.i)

        def end_cmd():
            nonlocal cur
            if isinstance(cur, Group) or not cur.empty():
                pipe.append(cur)
            cur = Simple(s, self.i)

        def end_pipe():
            nonlocal pipe
            end_cmd()
            if pipe:
                prog.append(pipe)
            pipe = []

        while True:
            self.skip_blanks()
            if self.i >= self.n:
                if stop_paren:
                    raise ParseError("unclosed $( / (")
                break
            c = s[self.i]
            if isinstance(cur, Simple) and cur.empty():
                cur.start = self.i
            if c == "#":
                j = s.find("\n", self.i)
                self.i = self.n if j < 0 else j
                continue
            if c == "\n":
                self.i += 1
                end_pipe()
                self.read_heredocs()
                continue
            m = REDIR.match(s, self.i)
            if m and not (m.group(1) == "" and s.startswith(("<(", ">("), self.i)):
                if isinstance(cur, Simple) or isinstance(cur, Group):
                    self.i = m.end()
                    cur.redirs.append(self.read_redirect(m.group(2)))
                    if isinstance(cur, Simple):
                        cur.end = self.i
                    continue
            for op in ("&&", "||", ";;&", ";;", ";&"):
                if s.startswith(op, self.i):
                    self.i += len(op)
                    end_pipe()
                    break
            else:
                if s.startswith("|&", self.i) or c == "|":
                    self.i += 2 if s.startswith("|&", self.i) else 1
                    end_cmd()
                    continue
                if c in ";&":
                    self.i += 1
                    end_pipe()
                    continue
                if c == "(":
                    if isinstance(cur, Simple) and not cur.words and not cur.assigns:
                        if s.startswith("((", self.i):
                            self.skip_arith_command()
                            end_cmd()
                            continue
                        start = self.i
                        self.i += 1
                        sub = self.parse_list(stop_paren=True)
                        cur = Group(sub, s[start:self.i])
                        continue
                    # `f() { ...; }` - a function definition: skip the parentheses
                    self.i += 1
                    continue
                if c == ")":
                    self.i += 1
                    if stop_paren:
                        end_pipe()
                        return prog
                    end_pipe()          # a case pattern and the like: not an error
                    continue
                if isinstance(cur, Group):
                    end_cmd()
                w = self.read_word()
                if w is None:
                    raise ParseError(f"unexpected character {s[self.i]!r}")
                m2 = ASSIGN.match(w.raw)
                if not cur.words and m2 and (w.parts and w.parts[0].kind == "lit"
                                             and not w.parts[0].quoted
                                             and w.parts[0].text.startswith(m2.group(0))):
                    cur.assigns.append((m2.group(1), strip_prefix(w, len(m2.group(0)))))
                else:
                    cur.words.append(w)
                cur.end = self.i
                continue
            continue
        end_pipe()
        return prog

    def skip_blanks(self):
        s = self.s
        while self.i < self.n:
            if s[self.i] in " \t":
                self.i += 1
            elif s.startswith("\\\n", self.i):
                self.i += 2
            else:
                break

    def skip_arith_command(self):
        depth = 0
        j = self.i
        while j < self.n:
            if self.s[j] == "(":
                depth += 1
            elif self.s[j] == ")":
                depth -= 1
                if depth == 0:
                    self.i = j + 1
                    return
            j += 1
        raise ParseError("unclosed (( ))")

    def read_redirect(self, op):
        self.skip_blanks()
        if op in ("<<", "<<-"):
            w = self.read_word()
            if w is None:
                raise ParseError("heredoc without a delimiter")
            delim = w.value() if w.literal() else w.raw
            r = Redirect(op, delim=delim, strip=(op == "<<-"))
            self.pending.append(r)
            return r
        w = self.read_word()
        if w is None:
            raise ParseError(f"redirection {op} without a target")
        return Redirect(op, target=w)

    def read_heredocs(self):
        s = self.s
        for r in self.pending:
            lines = []
            while self.i < self.n:
                j = s.find("\n", self.i)
                line = s[self.i:] if j < 0 else s[self.i:j]
                self.i = self.n if j < 0 else j + 1
                cmp = line.lstrip("\t") if r.strip else line
                if cmp == r.delim:
                    break
                lines.append(line)
            r.body = "\n".join(lines)
        self.pending = []

    # --- word ----------------------------------------------------------------------------
    def read_word(self):
        s = self.s
        start = self.i
        parts = []

        def lit(text, quoted):
            if parts and parts[-1].kind == "lit" and parts[-1].quoted == quoted:
                parts[-1].text += text
                parts[-1].raw += text
            else:
                parts.append(Part("lit", text, quoted))

        while self.i < self.n:
            c = s[self.i]
            if c in "<>" and s.startswith("(", self.i + 1):
                if self.i != start:
                    break
                ps = self.i
                self.i += 2
                prog = self.parse_list(stop_paren=True)
                parts.append(Part("proc", s[ps + 2:self.i - 1], False, s[ps:self.i], prog))
                continue
            if c == "(" and parts and len(parts) == 1 and parts[0].kind == "lit" \
                    and not parts[0].quoted and re.fullmatch(r"[A-Za-z_]\w*\+?=", parts[0].text):
                self.i += 1
                arr = []
                while True:
                    while self.i < self.n and s[self.i] in " \t\n":
                        self.i += 1
                    if self.i >= self.n:
                        raise ParseError("unclosed array (...)")
                    if s[self.i] == ")":
                        self.i += 1
                        break
                    w = self.read_word()
                    if w is None:
                        raise ParseError("unreadable array element")
                    arr.append(w)
                return Word(parts, s[start:self.i], array=arr)
            if c in " \t\n;&|()<>":
                break
            if c == "\\":
                if self.i + 1 < self.n:
                    if s[self.i + 1] == "\n":
                        self.i += 2
                        continue
                    lit(s[self.i + 1], True)
                    self.i += 2
                else:
                    lit("\\", False)
                    self.i += 1
                continue
            if c == "'":
                j = s.find("'", self.i + 1)
                if j < 0:
                    raise ParseError("unclosed single quote")
                lit(s[self.i + 1:j], True)
                self.i = j + 1
                continue
            if c == '"':
                self.i += 1
                self.read_dquoted(parts, lit)
                continue
            if c == "$":
                nxt = s[self.i + 1] if self.i + 1 < self.n else ""
                if nxt == "'":
                    self.i += 2
                    lit(self.read_ansi_c(), True)
                    continue
                if nxt == '"':
                    self.i += 2
                    self.read_dquoted(parts, lit)
                    continue
                p = self.read_dollar(quoted=False)
                if p is None:
                    lit("$", False)
                    self.i += 1
                else:
                    parts.append(p)
                continue
            if c == "`":
                parts.append(self.read_backtick(quoted=False))
                continue
            lit(c, False)
            self.i += 1
        if self.i == start:
            return None
        return Word(parts, s[start:self.i])

    def read_dquoted(self, parts, lit):
        s = self.s
        while True:
            if self.i >= self.n:
                raise ParseError("unclosed double quote")
            c = s[self.i]
            if c == '"':
                self.i += 1
                if not parts:
                    lit("", True)
                return
            if c == "\\" and self.i + 1 < self.n:
                nx = s[self.i + 1]
                if nx == "\n":
                    self.i += 2
                    continue
                if nx in '$`"\\':
                    lit(nx, True)
                    self.i += 2
                    continue
                lit("\\", True)
                self.i += 1
                continue
            if c == "$":
                p = self.read_dollar(quoted=True)
                if p is None:
                    lit("$", True)
                    self.i += 1
                else:
                    parts.append(p)
                continue
            if c == "`":
                parts.append(self.read_backtick(quoted=True))
                continue
            lit(c, True)
            self.i += 1

    def read_ansi_c(self):
        s = self.s
        out = []
        esc = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b",
               "f": "\f", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}
        while True:
            if self.i >= self.n:
                raise ParseError("unclosed $'...' string")
            c = s[self.i]
            if c == "'":
                self.i += 1
                return "".join(out)
            if c == "\\" and self.i + 1 < self.n:
                nx = s[self.i + 1]
                if nx in esc:
                    out.append(esc[nx])
                    self.i += 2
                    continue
                m = re.match(r"x([0-9A-Fa-f]{1,2})|([0-7]{1,3})", s[self.i + 1:])
                if m:
                    out.append(chr(int(m.group(1), 16) if m.group(1) else int(m.group(2), 8)))
                    self.i += 1 + m.end()
                    continue
                out.append("\\" + nx)
                self.i += 2
                continue
            out.append(c)
            self.i += 1

    def read_dollar(self, quoted):
        """Parse $... at the '$'. None means it is just a '$' character."""
        s = self.s
        i = self.i
        nxt = s[i + 1] if i + 1 < self.n else ""
        if s.startswith("$((", i):
            depth = 0
            j = i + 1
            while j < self.n:
                if s[j] == "(":
                    depth += 1
                elif s[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j < self.n and s.startswith("))", j - 1) and depth == 0:
                self.i = j + 1
                return Part("arith", s[i:self.i], quoted, s[i:self.i])
            # not arithmetic: $( (subshell) ) - parse it as a command substitution
        if nxt == "(":
            self.i = i + 2
            prog = self.parse_list(stop_paren=True)
            return Part("cmd", s[i + 2:self.i - 1], quoted, s[i:self.i], prog)
        if nxt == "{":
            depth = 0
            j = i + 1
            while j < self.n:
                if s[j] == "{":
                    depth += 1
                elif s[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                elif s[j] == "\\":
                    j += 1
                j += 1
            if j >= self.n:
                raise ParseError("unclosed ${")
            body = s[i + 2:j]
            self.i = j + 1
            m = re.match(r"([A-Za-z_][A-Za-z0-9_]*|[0-9]+|[@*#?$!-])", body)
            name = m.group(1) if m else body
            rest = body[m.end():] if m else "?"
            simple = rest == "" or re.fullmatch(r"\[[@*0-9]+\]", rest or "")
            return Part("var", name if simple else "\x00complex:" + name, quoted, s[i:self.i])
        m = NAME.match(s, i + 1)
        if m:
            self.i = m.end()
            return Part("var", m.group(0), quoted, s[i:self.i])
        if nxt and nxt in "0123456789!$?#@*-":
            self.i = i + 2
            return Part("var", nxt, quoted, s[i:self.i])
        return None

    def read_backtick(self, quoted):
        s = self.s
        i = self.i
        j = i + 1
        buf = []
        while j < self.n:
            c = s[j]
            if c == "\\" and j + 1 < self.n and s[j + 1] in "`$\\":
                buf.append(s[j + 1])
                j += 2
                continue
            if c == "`":
                break
            buf.append(c)
            j += 1
        if j >= self.n:
            raise ParseError("unclosed backtick")
        inner = "".join(buf)
        prog = Lexer(inner, self.depth + 1).parse()
        self.i = j + 1
        return Part("cmd", inner, quoted, s[i:self.i], prog)


def lit_word(text):
    return Word([Part("lit", text, True)], text)


def strip_prefix(w, k):
    """Word `NAME=value` -> word `value`."""
    parts = []
    first = w.parts[0]
    rest = first.text[k:]
    if rest:
        parts.append(Part("lit", rest, first.quoted))
    parts.extend(w.parts[1:])
    return Word(parts, w.raw[k:], array=w.array)


def parse(src, depth=0):
    lx = Lexer(src, depth)
    prog = lx.parse()
    return prog


# ==========================================================================================
# Processes
# ==========================================================================================

def _read(path, limit=65536):
    try:
        with open(path, "rb") as f:
            return f.read(limit)
    except OSError:
        return b""


def _stat(pid):
    raw = _read(f"/proc/{pid}/stat").decode("utf-8", "replace")
    if not raw:
        return None
    l, r = raw.find("("), raw.rfind(")")
    rest = raw[r + 2:].split()
    try:
        return raw[l + 1:r], rest[0], int(rest[1]), int(rest[2])   # comm, state, ppid, pgrp
    except (IndexError, ValueError):
        return None


def cmdline(pid):
    raw = _read(f"/proc/{pid}/cmdline", 4096)
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def is_claude(pid, comm):
    if comm == "claude":
        return True
    argv0 = _read(f"/proc/{pid}/cmdline", 4096).split(b"\0")[0].decode("utf-8", "replace")
    return os.path.basename(argv0) == "claude"


def find_root():
    """Nearest Claude ancestor process. None found: the guard's parent (run by hand)."""
    pid = os.getppid()
    for _ in range(128):
        if pid <= 1:
            break
        st = _stat(pid)
        if not st:
            break
        if is_claude(pid, st[0]):
            return pid, True
        pid = st[2]
    return os.getppid(), False


class Table:
    def __init__(self):
        self.procs = {}
        for d in os.listdir("/proc"):
            if d.isdigit():
                st = _stat(d)
                if st:
                    self.procs[int(d)] = st
        self.root, self.found = find_root()
        kids = {}
        for pid, (_, _, ppid, _) in self.procs.items():
            kids.setdefault(ppid, []).append(pid)
        own, todo = set(), [self.root]
        while todo:
            p = todo.pop()
            for c in kids.get(p, ()):
                if c not in own:
                    own.add(c)
                    todo.append(c)
        if not self.found:
            own.add(self.root)
        own.add(os.getpid())
        self.own = own

    def tgid(self, pid):
        if pid in self.procs:
            return pid
        raw = _read(f"/proc/{pid}/status").decode("utf-8", "replace")
        m = re.search(r"^Tgid:\s*(\d+)", raw, re.M)
        return int(m.group(1)) if m else None

    def foreign(self, pids):
        out = set()
        for pid in pids:
            t = self.tgid(pid)
            if t is None or t not in self.procs:
                continue                     # the process is already gone
            if self.procs[t][1] == "Z":
                continue                     # a signal cannot hurt a zombie
            if t not in self.own and t > 0:
                out.add(t)
        return out

    def all_foreign(self):
        return {p for p, st in self.procs.items()
                if p not in self.own and p != 2 and st[2] != 2 and st[1] != "Z"}

    def group(self, pgid):
        return {p for p, st in self.procs.items() if st[3] == pgid}

    def rank(self, pid):
        comm = self.procs.get(pid, ("",))[0]
        return (0 if comm.startswith("tmux") else 1 if comm == "claude" else 2, pid)

    def describe(self, pids, limit=LIST_MAX):
        pids = sorted(pids, key=self.rank)
        lines = []
        for pid in pids[:limit]:
            cmd = cmdline(pid) or f"[{self.procs.get(pid, ('?',))[0]}]"
            if len(cmd) > 110:
                cmd = cmd[:107] + "..."
            lines.append(f"    pid {pid:<8} {cmd}")
        if len(pids) > limit:
            lines.append(f"    ...and {len(pids) - limit} more")
        return "\n".join(lines)


# ==========================================================================================
# Read-only commands: the guard runs them to learn where a signal would go
# ==========================================================================================

PRODUCERS = {"pgrep", "pidof", "ps", "grep", "egrep", "fgrep", "head", "tail", "cut", "sort",
             "uniq", "tr", "wc", "awk", "gawk", "mawk", "cat", "echo", "printf", "lsof",
             "fuser", "ss", "netstat"}

# Where the guard takes read-only commands from. Not from the command's PATH or directory:
# the guard runs BEFORE approval, and `./cat`, `/tmp/x/pgrep`, `PATH=/tmp/x pgrep` would run
# someone else's program before a human has seen it.
SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
# Assignments in front of a read-only command. Only those that change the output format
# (PASS) go into the guard's environment. With one that replaces the program or loads code
# (PROGRAM) the guard does not run the command: it cannot predict the output. The rest
# (FOREGROUND_OK=1 and other markers) are dropped: they never reach the program.
PASS_ASSIGN = re.compile(r"LC_[A-Z]+|LANG|LANGUAGE|TZ|COLUMNS|POSIXLY_CORRECT|PS_FORMAT|"
                         r"PS_PERSONALITY|TERM|NO_COLOR|GREP_COLORS?")
PROGRAM_ASSIGN = re.compile(r"PATH|LD_[A-Z_]+|GCONV_PATH|BASH_ENV|ENV|SHELLOPTS|BASHOPTS|"
                            r"GLIBC_TUNABLES|AWKPATH|AWKLIBPATH|IFS|PS4")


def trusted_exe(name, cwd=None):
    """The system binary for a read-only command. A name with "/" must be that file."""
    base = os.path.basename(name)
    exe = None
    for d in SAFE_PATH.split(":"):
        p = os.path.join(d, base)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            exe = p
            break
    if exe is None:
        raise Dynamic(f"'{base}' is not in the system directories - the guard does not run it")
    if "/" in name:
        full = name if os.path.isabs(name) else os.path.join(cwd or os.getcwd(), name)
        if os.path.realpath(full) != os.path.realpath(exe):
            raise Dynamic(f"'{short(name, 60)}' is not the system {base}; the guard does "
                          f"not run a program the command names")
    return exe


def regular_file(path):
    """A path the guard reads: a regular file. A pipe or a device could hang the guard."""
    if path == "/dev/null":
        return path
    try:
        if stat.S_ISREG(os.stat(path).st_mode):
            return path
    except OSError:
        return path                          # no file: reads as empty, as in the shell
    raise Dynamic(f"{short(path, 60)} is not a regular file (pipe, device), the guard "
                  f"does not read it")


def run_capture(argv_list, env=None, cwd=None, stdin_path=None, stderr_to_out=None,
                stdin_data=None):
    """Run a pipeline of read-only commands without a shell; return the last stdout, code."""
    import select
    import subprocess
    import tempfile
    import time
    cwd = cwd if cwd and os.path.isdir(cwd) else None
    exes = [trusted_exe(argv[0], cwd) for argv in argv_list]
    env = dict(os.environ if env is None else env)
    env["PATH"] = SAFE_PATH                  # egrep/fgrep are sh scripts, find grep in PATH
    if stdin_path:
        regular_file(stdin_path)
    procs = []
    prev = None
    try:
        for k, argv in enumerate(argv_list):
            if k == 0 and stdin_data is not None:
                stdin = tempfile.TemporaryFile()
                stdin.write(stdin_data.encode("utf-8", "replace"))
                stdin.seek(0)
                stdin_path = True
            elif k == 0:
                stdin = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL
            else:
                stdin = prev.stdout
            errs = subprocess.STDOUT if (stderr_to_out and stderr_to_out[k]) else subprocess.DEVNULL
            p = subprocess.Popen(argv, executable=exes[k], stdin=stdin, stdout=subprocess.PIPE,
                                 stderr=errs, env=env, cwd=cwd)
            if k == 0 and stdin_path:
                stdin.close()
            if prev is not None:
                prev.stdout.close()
            procs.append(p)
            prev = p
    except OSError as e:
        for p in procs:
            p.kill()
        raise Dynamic(f"could not start {argv_list[len(procs)][0]}: {e.strerror}")
    out = bytearray()
    deadline = time.time() + SUB_TIMEOUT
    last = procs[-1]
    fd = last.stdout.fileno()
    cut = False
    while True:
        left = deadline - time.time()
        if left <= 0:
            cut = "timeout"
            break
        r, _, _ = select.select([fd], [], [], left)
        if not r:
            continue
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        out += chunk
        if len(out) > MAX_OUT:
            cut = "overflow"
            break
    for p in procs:                       # the guard's own child processes
        try:
            if cut:
                raise subprocess.TimeoutExpired(p.args, 0)
            p.wait(timeout=max(0.2, deadline - time.time()))
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    last.stdout.close()
    if cut == "timeout":
        raise Dynamic(f"'{' '.join(argv_list[0])[:60]}' did not answer in {SUB_TIMEOUT:.0f} s")
    return out.decode("utf-8", "replace"), last.returncode


def pgrep(args, why):
    """pgrep with the same arguments as pkill. Returns a set of pids."""
    out, rc = run_capture([["pgrep"] + list(args)])
    if rc == 1:
        return set()
    if rc != 0:
        raise Dynamic(f"pgrep {' '.join(args)[:80]} rejected the arguments ({why})")
    return {int(x) for x in out.split() if x.isdigit()}


def ere_escape(s):
    return re.sub(r"([][.^$*+?(){}|\\])", r"\\\1", s)


# ==========================================================================================
# Signals
# ==========================================================================================

_SIGNAMES = set()


def _signames():
    if not _SIGNAMES:
        import signal
        _SIGNAMES.update(n[3:] for n in dir(signal)
                         if n.startswith("SIG") and not n.startswith("SIG_"))
        _SIGNAMES.update({"CLD", "POLL", "IOT", "STKFLT", "PWR", "RTMIN", "RTMAX", "UNUSED"})
    return _SIGNAMES


def signal_value(spec):
    """Signal number by name or number; None means not a signal."""
    u = spec.upper()
    if u.startswith("SIG"):
        u = u[3:]
    if u in ("EXIT", "NULL", "0"):
        return 0
    if u.isdigit():
        return int(u)
    if u in _signames():
        import signal
        return getattr(signal, "SIG" + u, 99)
    if re.fullmatch(r"RTM(IN\+|AX-)\d+", u):
        return 99
    return None


# ==========================================================================================
# What the command means
# ==========================================================================================

SHELLS = {"bash", "sh", "dash", "zsh", "ksh", "mksh", "ash"}
RESERVED_SKIP = {"!", "{", "}", "do", "then", "else", "elif", "if", "while", "until",
                 "done", "fi", "esac", "coproc"}


class Binding:
    __slots__ = ("kind", "word", "words", "scope", "upstream", "index", "count", "values")

    def __init__(self, kind, word=None, words=None, scope=None, upstream=None, index=0,
                 count=1, values=None):
        self.kind = kind            # word | array | for | read | values
        self.word = word
        self.words = words
        self.scope = scope
        self.upstream = upstream
        self.index = index
        self.count = count
        self.values = values

    def raw(self):
        if self.word is not None:
            return self.word.raw
        if self.words:
            return " ".join(w.raw for w in self.words)
        if self.upstream:
            return " | ".join(c.raw for c in self.upstream if isinstance(c, Simple))
        return ""


class Report:
    def __init__(self):
        self.hits = []          # (label, set(pid))
        self.all = []           # label
        self.groups = []        # (label, pgid)
        self.dynamic = []       # (label, reason)
        self.tmux = []          # (label, what, socket)
        self.units = []         # (label, unit, anchors)
        self.errors = []        # text

    def empty(self):
        return not (self.hits or self.all or self.groups or self.dynamic or self.tmux
                    or self.units or self.errors)


def short(text, n=160):
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 3] + "..."


class Ctx:
    def __init__(self, report, cwd, depth=0, bindings=None, exported=None):
        self.report = report
        self.cwd = cwd
        self.depth = depth
        self.bindings = bindings if bindings is not None else {}
        self.exported = exported if exported is not None else {}
        self.cache = {}

    def child(self):
        c = Ctx(self.report, self.cwd, self.depth + 1, dict(self.bindings), dict(self.exported))
        c.cache = self.cache
        return c

    # --- values --------------------------------------------------------------------------
    def lookup(self, part):
        name = part.text
        if name.startswith("\x00complex:"):
            base = name.split(":", 1)[1]
            b = self.bindings.get(base)
            if b is None:
                raise Dynamic(f"{part.raw} - value unknown")
            return self.binding_values(b, part.raw)
        if name in ("!", "$", "BASHPID"):
            return [OWN]
        if name in ("RANDOM", "SRANDOM") and name not in self.bindings:
            return [RAND]
        b = self.bindings.get(name)
        if b is None:
            if name == "PWD":
                return [self.cwd]
            if name in ("UID", "EUID"):
                return [str(os.getuid())]
            if name in STABLE_ENV and name in os.environ:
                return [os.environ[name]]
            raise Dynamic(f"{part.raw} - value unknown before the command runs")
        return self.binding_values(b, part.raw)

    def binding_values(self, b, raw):
        if b.values is not None:
            return b.values
        sub = Ctx(self.report, self.cwd, self.depth, b.scope or {}, self.exported)
        sub.cache = self.cache
        if b.kind == "word":
            vals = sub.expand(b.word, split=False)
        elif b.kind in ("array", "for"):
            vals = []
            for w in b.words:
                vals.extend(sub.expand(w))
        elif b.kind == "read":
            out = sub.eval_pipeline(b.upstream)
            vals = []
            for line in out.splitlines():
                f = line.split(None, b.count - 1) if b.count > 1 else [line.strip()]
                if len(f) > b.index:
                    vals.append(f[b.index].strip())
        else:
            raise Dynamic(f"{raw} - value unknown")
        b.values = vals
        return vals

    def expand(self, word, split=True):
        """List of fields after substitutions. Dynamic if it cannot be known."""
        if word.array is not None:
            vals = []
            for w in word.array:
                vals.extend(self.expand(w))
            return vals
        fields = [["", False]]
        for p in word.parts:
            if p.kind == "lit":
                fields[-1][0] += p.text
                fields[-1][1] = True
                continue
            if p.kind == "var":
                vals = self.lookup(p)
            elif p.kind == "cmd":
                vals = [self.eval_cmdsub(p).rstrip("\n")]
            else:
                raise Dynamic(f"{short(p.raw, 60)} - computed at run time")
            if p.quoted or not split:
                if len(vals) <= 1:
                    fields[-1][0] += vals[0] if vals else ""
                    fields[-1][1] = True
                else:                       # several possible values: one each
                    head = fields.pop()[0]
                    fields.extend([head + v, True] for v in vals)
                continue
            pieces = [pc for v in vals for pc in ([v] if v == OWN else v.split())]
            if not pieces:
                continue
            fields[-1][0] += pieces[0]
            fields[-1][1] = True
            fields.extend([pc, True] for pc in pieces[1:])
        res = [f for f, t in fields if t]
        if word.unquoted_glob() and word.literal() and len(res) == 1:
            pat = res[0] if os.path.isabs(res[0]) else os.path.join(self.cwd, res[0])
            import glob as globmod
            hits = sorted(globmod.glob(pat))
            if hits:
                if os.path.isabs(res[0]):
                    return hits
                return [os.path.relpath(h, self.cwd) for h in hits]
        return res

    def soft(self, words):
        """For commands where not every word matters: an unknown word gives its source + a flag."""
        out = []
        for w in words:
            try:
                out.extend((v, False) for v in self.expand(w))
            except Dynamic:
                out.append((w.script(), True))
        return out

    def expand_all(self, words):
        out = []
        for w in words:
            out.extend(self.expand(w))
        return out

    def eval_cmdsub(self, part):
        key = id(part)
        if key in self.cache:
            v = self.cache[key]
            if isinstance(v, Dynamic):
                raise v
            return v
        try:
            prog = part.prog
            if len(prog) != 1:
                raise Dynamic(f"$({short(part.text, 60)}) - several commands, not computing it")
            v = self.eval_pipeline(prog[0])
        except Dynamic as e:
            self.cache[key] = e
            raise
        self.cache[key] = v
        return v

    def eval_pipeline(self, cmds):
        """Run a read-only pipeline (pgrep, pidof, ps|grep|awk, cat of a file...)."""
        argvs, errs = [], []
        stdin_path = stdin_data = None
        env = dict(os.environ)
        env["COLUMNS"] = "100000"          # ps cuts the command line to a narrow screen
        env.pop("LINES", None)
        for k, c in enumerate(cmds):
            if not isinstance(c, Simple):
                raise Dynamic("subshell in a pipeline - not computing it")
            words = strip_reserved(c.words)
            for name, w in c.assigns:
                if PROGRAM_ASSIGN.fullmatch(name):
                    raise Dynamic(f"{name}=... in front of '{short(c.raw, 60)}' may "
                                  f"replace the program - the guard does not run that")
                if not PASS_ASSIGN.fullmatch(name):
                    continue
                vals = self.expand(w, split=False)
                env[name] = vals[0] if vals else ""
            to_out = False
            for r in c.redirs:
                tgt = self.expand(r.target, split=False)[0] if r.target is not None else ""
                if r.op in (">&", "<&") or (r.op in (">", ">>", "&>") and tgt == "/dev/null"):
                    if r.op == ">&" and tgt == "1":
                        to_out = True
                    continue
                if r.op == "<" and k == 0:
                    stdin_path = regular_file(tgt if os.path.isabs(tgt)
                                              else os.path.join(self.cwd, tgt))
                    continue
                if r.op in ("<<", "<<-") and k == 0 and r.body is not None:
                    body = r.body
                    if r.strip:
                        body = "\n".join(line.lstrip("\t") for line in body.split("\n"))
                    stdin_data = body + "\n"
                    continue
                if r.op == "<<<" and k == 0:
                    stdin_data = tgt + "\n"
                    continue
                raise Dynamic(f"redirection {r.op} in '{short(c.raw, 60)}' - not running it")
            if not words:
                if stdin_path and len(cmds) == 1:          # $(< file)
                    return _read(regular_file(stdin_path), MAX_OUT).decode("utf-8", "replace")
                if stdin_data is not None and len(cmds) == 1:
                    return ""
                raise Dynamic("empty command in a pipeline")
            argv = self.expand_all(words)
            if any("\x00" in a for a in argv):
                raise Dynamic(f"'{short(c.raw, 60)}' depends on $$/$!/$RANDOM - not computing it")
            base = os.path.basename(argv[0])
            if base == "jobs" and len(cmds) == 1:
                return OWN
            if base == "mktemp":
                return FRESH
            if base not in PRODUCERS:
                raise Dynamic(f"'{base}' is not one of the read-only commands the guard "
                              f"runs itself (pgrep, pidof, ps, grep, awk, cat...)")
            check_producer(base, argv)
            argvs.append(argv)
            errs.append(to_out)
        out, _ = run_capture(argvs, env=env, cwd=self.cwd, stdin_path=stdin_path,
                             stderr_to_out=errs, stdin_data=stdin_data)
        return out


def _long_opt(a, *names):
    """`--comp`, `--outp=f`: GNU getopt accepts any unambiguous prefix of a long option."""
    key = a[2:].split("=", 1)[0]
    return bool(key) and any(n.startswith(key) for n in names)


def _short_opts(args, with_arg):
    """Short option letters up to the first that takes an argument (`-nk2` -> n, k)."""
    for a in args:
        if a == "--":
            return
        if a.startswith("-") and not a.startswith("--") and len(a) > 1:
            for ch in a[1:]:
                yield ch
                if ch in with_arg:
                    break


def check_producer(base, argv):
    args = argv[1:]
    bad = None
    if base == "sort":
        if "o" in _short_opts(args, "kStT") or any(
                _long_opt(a, "output", "compress-program") for a in args if a.startswith("--")):
            bad = "sort -o/--output writes a file, --compress-program runs a program"
    elif base == "uniq" and len([a for a in args if not a.startswith("-")]) > 1:
        bad = "uniq with an output file"
    elif base in ("awk", "gawk", "mawk"):
        # Only -F and -v: the other gawk/mawk options load code (-f, -i, -l, -E, --source,
        # -W exec). The program text must have no system/getline/redirection/@include/@load.
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--":
                i += 1
                break
            if a in ("-F", "-v"):
                i += 2
                continue
            if re.match(r"-[Fv].", a) or a.startswith(("--field-separator=", "--assign=")):
                i += 1
                continue
            if a.startswith("-") and a != "-":
                bad = f"awk {a}: the guard runs awk only with -F and -v"
            break
        if not bad and re.search(r"system|getline|[|>]|@\s*(load|include)", " ".join(args[i:])):
            bad = "awk with system/getline/redirection/@include"
    elif base == "ss" and ("K" in _short_opts(args, "fANFD")
                           or "D" in _short_opts(args, "fANF")
                           or any(_long_opt(a, "kill", "diag")
                                  for a in args if a.startswith("--"))):
        bad = "ss -K closes connections, -D writes a file"
    elif base == "tail" and any(a in ("-f", "-F") or a.startswith("--follow") for a in args):
        bad = "tail -f never ends"
    elif base == "fuser" and any(re.fullmatch(r"-[a-zA-Z]*[kiw][a-zA-Z]*", a) or
                                 a in ("--kill", "--interactive") for a in args):
        bad = "fuser -k sends signals itself"
    elif base == "xargs":
        bad = "xargs inside a substitution"
    if bad:
        raise Dynamic(bad)


def strip_reserved(words):
    out = list(words)
    while out and out[0].literal() and out[0].value() in RESERVED_SKIP:
        out.pop(0)
    if out and out[0].literal() and out[0].value() == "time":
        out.pop(0)
        while out and out[0].literal() and out[0].value() in ("-p", "--"):
            out.pop(0)
    return out


# --- walking the program ------------------------------------------------------------------

def analyze_program(prog, ctx):
    for pipe in prog:
        for idx, cmd in enumerate(pipe):
            if isinstance(cmd, Group):
                for r in cmd.redirs:
                    nested_parts(r.target, ctx)
                analyze_program(cmd.prog, ctx)
            else:
                analyze_simple(cmd, ctx, pipe, idx)


def nested_parts(word, ctx):
    """Substitutions inside a word are executed: their commands are checked too."""
    if word is None:
        return
    words = [word] + list(word.array or [])
    for w in words:
        for p in w.parts:
            if p.kind in ("cmd", "proc") and p.prog is not None:
                analyze_program(p.prog, ctx.child())


def is_assign_word(w):
    if not w.parts or w.parts[0].kind != "lit" or w.parts[0].quoted:
        return None
    m = ASSIGN.match(w.parts[0].text)
    return m


def analyze_simple(cmd, ctx, pipe, idx):
    for _, w in cmd.assigns:
        nested_parts(w, ctx)
    for w in cmd.words:
        nested_parts(w, ctx)
    for r in cmd.redirs:
        nested_parts(r.target, ctx)

    words = strip_reserved(cmd.words)
    assigns = list(cmd.assigns)
    if len(words) != len(cmd.words):          # `do x=$(...)`, `then p=...`: assignments
        while words and is_assign_word(words[0]):
            m = is_assign_word(words[0])
            assigns.append((m.group(1), strip_prefix(words[0], len(m.group(0)))))
            words = words[1:]
    if words and words[0].literal():
        head = words[0].value()
        if head in ("for", "select"):
            if len(words) >= 2 and words[1].literal():
                name = words[1].value()
                rest = words[2:]
                if rest and rest[0].literal() and rest[0].value() == "in":
                    ctx.bindings[name] = Binding("for", words=rest[1:], scope=dict(ctx.bindings))
                else:
                    ctx.bindings.pop(name, None)
            return
        if head == "cd":
            target = words[1] if len(words) > 1 else None
            if target is None:
                ctx.cwd = os.environ.get("HOME", ctx.cwd)
            elif target.literal() and target.value() != "-":
                ctx.cwd = os.path.normpath(os.path.join(ctx.cwd,
                                                        os.path.expanduser(target.value())))
            return
        if head in ("case", "function", "[[", "[", "test", "((", "return", "exit", "break",
                    "continue", ":", "true", "false", "wait", "shift", "trap"):
            return
    if not words:
        for name, w in assigns:
            assign(ctx, name, w, export=name in ctx.exported or name in os.environ)
        return
    env = {}
    for name, w in assigns:
        env[name] = w
    try:
        dispatch(words, ctx, cmd, pipe, idx, env_changes=[("set", env)])
    except Dynamic as e:
        ctx.report.dynamic.append((label_of(cmd, pipe), str(e)))


def assign(ctx, name, w, export=False):
    if w.array is not None:
        ctx.bindings[name] = Binding("array", words=w.array, scope=dict(ctx.bindings))
    else:
        ctx.bindings[name] = Binding("word", word=w, scope=dict(ctx.bindings))
    if export:
        ctx.exported[name] = ctx.bindings[name]


def label_of(cmd, pipe=None):
    """Command text for the message; for a pipeline all of it (pgrep ... | xargs kill)."""
    if pipe and len(pipe) > 1 and all(isinstance(c, Simple) and c.src is cmd.src for c in pipe):
        return short(cmd.src[pipe[0].start:pipe[-1].end])
    return short(cmd.raw)


def dispatch(words, ctx, cmd, pipe, idx, env_changes, extra=None, repl=None, uid=None):
    """Strip wrappers (sudo, env, timeout, bash -c ...) and parse what will run."""
    rep = ctx.report
    label = label_of(cmd, pipe)
    uid = os.getuid() if uid is None else uid
    while words:
        w0 = words[0]
        try:
            vals = ctx.expand(w0)
        except Dynamic as e:
            # The command name is unknown. Block only if its text or the variable's text
            # carries a signal mark: `$K 123` with K="kill -9", `$T kill-server`.
            raws = [w.raw for w in words] + [ctx.bindings[p.text].raw() for p in w0.parts
                                             if p.kind == "var" and p.text in ctx.bindings]
            if any(KILLISH.search(r) or KILLISH.search(UNQUOTE.sub("", r)) for r in raws):
                rep.dynamic.append((label, f"the command name is computed at run time: {e}"))
            return
        if not vals:                       # empty substitution: the next word is the command
            words = words[1:]
            continue
        if not w0.literal():               # `$T args`: fields are both the name and args
            words = [lit_word(v) for v in vals] + list(words[1:])
        name = vals[0]
        break
    else:
        return
    base = os.path.basename(name)
    args = words[1:]

    def lit_args():
        return [a.value() if a.literal() else None for a in args]

    # --- wrappers --------------------------------------------------------------------
    if base in ("sudo", "doas"):
        i, preserve, target_uid = 0, False, 0
        with_arg = set("CDghpRrTtUu") if base == "sudo" else set("uC")
        while i < len(args):
            a = args[i].value() if args[i].literal() else ""
            if a == "--":
                i += 1
                break
            if a.startswith("--"):
                key = a[2:].split("=", 1)[0]
                if key in ("preserve-env",):
                    preserve = True
                if key in ("user", "group", "host", "prompt", "chdir", "close-from", "role",
                           "type", "command-timeout", "other-user", "chroot") and "=" not in a:
                    i += 1
                    if key == "user" and i < len(args):
                        target_uid = uid_of(args[i].value())
                elif key == "user":
                    target_uid = uid_of(a.split("=", 1)[1])
                i += 1
                continue
            if a.startswith("-") and len(a) > 1:
                j = 1
                while j < len(a):
                    ch = a[j]
                    if ch == "E":
                        preserve = True
                    if ch in with_arg:
                        val = a[j + 1:]
                        if not val:
                            i += 1
                            val = args[i].value() if i < len(args) and args[i].literal() else ""
                        if ch == "u":
                            target_uid = uid_of(val)
                        break
                    j += 1
                i += 1
                continue
            break
        rest = args[i:]
        env = {}
        while rest and rest[0].literal() and ASSIGN.match(rest[0].value()):
            m = ASSIGN.match(rest[0].value())
            env[m.group(1)] = strip_prefix(rest[0], len(m.group(0)))
            rest = rest[1:]
        changes = list(env_changes) + ([] if preserve else [("reset", None)]) + [("set", env)]
        return dispatch(rest, ctx, cmd, pipe, idx, changes, extra, repl, target_uid)

    if base == "env":
        i, changes = 0, list(env_changes)
        while i < len(args):
            a = args[i].value() if args[i].literal() else None
            if a is None:
                break
            if a == "--":
                i += 1
                break
            if a in ("-i", "--ignore-environment", "-"):
                changes.append(("clear", None))
            elif a in ("-u", "--unset"):
                i += 1
                if i < len(args):
                    changes.append(("unset", args[i].value()))
            elif a.startswith("--unset="):
                changes.append(("unset", a.split("=", 1)[1]))
            elif a.startswith("-u") and len(a) > 2:
                changes.append(("unset", a[2:]))
            elif a in ("-C", "--chdir"):
                i += 1
            elif a in ("-S", "--split-string") or a.startswith(("-S", "--split-string=")):
                if a in ("-S", "--split-string"):
                    i += 1
                    text = args[i].script() if i < len(args) else ""
                else:
                    text = a.split("=", 1)[1] if a.startswith("--") else a[2:]
                text += " " + " ".join(w.script() for w in args[i + 1:])
                return analyze_script(text, ctx, label)
            elif a.startswith("-") and not ASSIGN.match(a):
                pass
            elif ASSIGN.match(a):
                m = ASSIGN.match(a)
                changes.append(("set", {m.group(1): strip_prefix(args[i], len(m.group(0)))}))
            else:
                break
            i += 1
        return dispatch(args[i:], ctx, cmd, pipe, idx, changes, extra, repl, uid)

    if base == "timeout":
        i = 0
        while i < len(args):
            a = args[i].value() if args[i].literal() else ""
            if a in ("-s", "--signal", "-k", "--kill-after"):
                i += 2
                continue
            if a.startswith("-") and not re.fullmatch(r"-?\d+(\.\d+)?[smhd]?", a):
                i += 1
                continue
            break
        return dispatch(args[i + 1:], ctx, cmd, pipe, idx, env_changes, extra, repl, uid)

    if base in ("nohup", "setsid", "stdbuf", "nice", "ionice", "command", "builtin", "exec",
                "time", "chrt", "taskset", "unbuffer", "systemd-run", "flock", "caffeinate",
                "nsenter"):
        takes = {"stdbuf": "ioe", "nice": "n", "ionice": "cn", "exec": "a", "time": "fo",
                 "nsenter": "tSG",
                 "systemd-run": "upEHMs", "flock": "wEc", "taskset": "", "chrt": ""}.get(base, "")
        i = 0
        while i < len(args):
            a = args[i].value() if args[i].literal() else ""
            if base == "command" and a in ("-v", "-V"):
                return
            if base == "ionice" and a.startswith("-p"):
                return
            if base == "flock" and a in ("-c", "--command"):
                if i + 1 < len(args):
                    return analyze_word_script(args[i + 1], ctx, label)
                return
            if a == "--":
                i += 1
                break
            if a.startswith("--"):
                i += 1
                continue
            if a.startswith("-") and len(a) > 1:
                if len(a) == 2 and a[1] in takes:
                    i += 2
                    continue
                i += 1
                continue
            break
        rest = args[i:]
        if base == "flock" and rest:
            rest = rest[1:]                       # the lock file
            if rest and rest[0].literal() and rest[0].value() in ("-c", "--command"):
                return analyze_word_script(rest[1], ctx, label) if len(rest) > 1 else None
        if base in ("chrt", "taskset") and rest:
            rest = rest[1:]                       # priority / mask
        return dispatch(rest, ctx, cmd, pipe, idx, env_changes, extra, repl, uid)

    if base in SHELLS:
        i, script, operands = 0, None, []
        while i < len(args):
            a = args[i].value() if args[i].literal() else None
            if a is None:
                break
            if a == "--":
                i += 1
                break
            if a in ("-o", "+o", "--rcfile", "--init-file"):
                i += 2
                continue
            if a.startswith("--"):
                i += 1
                continue
            if re.fullmatch(r"[-+][A-Za-z]+", a):
                if "c" in a and a[0] == "-":
                    script = True
                i += 1
                continue
            break
        operands = args[i:]
        if script:
            if not operands:
                return
            positional = operands[1:]
            return analyze_word_script(operands[0], ctx, label, positional, extra, repl)
        if not operands:
            for r in cmd.redirs:
                if r.op in ("<<", "<<-") and r.body is not None:
                    return analyze_script(r.body, ctx, label)
                if r.op == "<<<" and r.target is not None:
                    return analyze_word_script(r.target, ctx, label)
            if idx > 0:                           # echo '...' | bash
                try:
                    text = ctx.eval_pipeline(pipe[:idx])
                except Dynamic as e:
                    up = " | ".join(c.raw + " " + " ".join(r.body or "" for r in c.redirs)
                                    for c in pipe[:idx] if isinstance(c, Simple))
                    if look(up) == "strict":
                        rep.dynamic.append(
                            (label, f"the shell reads commands from a pipeline: {e}"))
                    return
                return analyze_script(text, ctx, label)
        return                                    # bash script.sh: the file is not read

    if base == "su":
        for k, a in enumerate(args):
            if a.literal() and a.value() in ("-c", "--command") and k + 1 < len(args):
                return analyze_word_script(args[k + 1], ctx, label)
            if a.literal() and a.value().startswith("--command="):
                return analyze_script(a.value().split("=", 1)[1], ctx, label)
        return

    if base == "eval":
        try:
            text = " ".join(ctx.expand_all(args))
        except Dynamic:
            text = " ".join(a.script() for a in args)
        return analyze_script(text, ctx, label)

    if base == "watch":
        i, exec_mode = 0, False
        while i < len(args):
            a = args[i].value() if args[i].literal() else ""
            if a in ("-x", "--exec"):
                exec_mode = True
                i += 1
                continue
            if a in ("-n", "--interval", "-q", "--equexit"):
                i += 2
                continue
            if a.startswith("-"):
                i += 1
                continue
            break
        if exec_mode:
            return dispatch(args[i:], ctx, cmd, pipe, idx, env_changes, extra, repl, uid)
        return analyze_script(" ".join(w.script() for w in args[i:]), ctx, label)

    if base == "xargs":
        return analyze_xargs(args, ctx, cmd, pipe, idx, env_changes, uid)

    if base in ("export", "declare", "typeset", "local", "readonly"):
        export = base == "export"
        for a in args:
            if a.literal() and a.value().startswith("-"):
                if "x" in a.value():
                    export = True
                continue
            m = ASSIGN.match(a.raw) if a.parts and a.parts[0].kind == "lit" else None
            if m and a.parts[0].text.startswith(m.group(0)):
                assign(ctx, m.group(1), strip_prefix(a, len(m.group(0))), export)
            elif a.literal() and export and a.value() in ctx.bindings:
                ctx.exported[a.value()] = ctx.bindings[a.value()]
        return

    if base == "unset":
        for a in args:
            if a.literal() and not a.value().startswith("-"):
                ctx.bindings.pop(a.value(), None)
                ctx.exported[a.value()] = None
        return

    if base == "read":
        names = []
        i = 0
        while i < len(args):
            a = args[i].value() if args[i].literal() else ""
            if a in ("-a", "-d", "-p", "-t", "-n", "-N", "-u", "-i"):
                i += 2
                continue
            if a.startswith("-"):
                i += 1
                continue
            names.append(a)
            i += 1
        names = names or ["REPLY"]
        for k, nm in enumerate(names):
            if idx > 0:
                ctx.bindings[nm] = Binding("read", upstream=pipe[:idx], scope=dict(ctx.bindings),
                                           index=k, count=len(names))
            else:
                ctx.bindings.pop(nm, None)
        return

    # --- what sends signals ----------------------------------------------------------
    if base == "kill":
        return analyze_kill(args, ctx, label, extra)
    if base == "pkill":
        return analyze_pkill(args, ctx, label, extra)
    if base == "killall":
        return analyze_killall(args, ctx, label, extra)
    if base in ("killall5", "skill"):
        rep.dynamic.append((label, f"{base} hits all processes or a pattern - "
                                   "the guard does not evaluate it"))
        return
    if base == "fuser":
        return analyze_fuser(args, ctx, label)
    if base == "tmux":
        return analyze_tmux(args, ctx, label, env_changes, uid)
    if base == "systemctl":
        return analyze_systemctl(args, ctx, label)
    if base == "service":
        return analyze_service(args, ctx, label)


def uid_of(user):
    try:
        import pwd
        if user.startswith("#") and user[1:].isdigit():
            return int(user[1:])
        return pwd.getpwnam(user).pw_uid
    except (KeyError, ImportError, AttributeError):
        return 0


def analyze_word_script(word, ctx, label, positional=(), extra=None, repl=None):
    try:
        vals = ctx.expand(word, split=False)
        text = vals[0] if len(vals) == 1 else word.script()
    except Dynamic:
        text = word.script()
    sub = ctx.child()
    if positional or extra is not None:
        try:
            pos = sub.expand_all(positional) if positional else []
        except Dynamic:
            pos = None
        items = []
        if extra is not None:
            try:
                items = extra()
            except Dynamic as e:
                if look(text) == "strict":
                    ctx.report.dynamic.append((label, str(e)))
                return
        if repl and items:
            for it in items:
                analyze_script(text.replace(repl, it), ctx, label)
            return
        if pos is None:
            for n in [str(k) for k in range(10)] + ["@", "*"]:
                sub.bindings.pop(n, None)
        else:
            allv = pos[1:] + items
            for k, v in enumerate(pos[:1] + allv):
                sub.bindings[str(k)] = Binding("values", values=[v])
            for n in [str(k) for k in range(len(pos[:1] + allv), 10)]:
                sub.bindings[n] = Binding("values", values=allv if items else [""])
            sub.bindings["@"] = Binding("values", values=allv)
            sub.bindings["*"] = Binding("values", values=allv)
    return analyze_script(text, sub, label, same_ctx=True)


def analyze_script(text, ctx, label, same_ctx=False):
    """Text the shell will run. Checked by the same rule as the whole command
    (guard_kill_fast.look): a signal word means strict parsing; a name assembled at run time
    means lenient parsing, where unparsed text without a signal word is no reason to block."""
    mode = look(text)
    if mode is None:
        return
    if ctx.depth >= MAX_DEPTH:
        if mode == "strict":
            raise ParseError("nesting of bash -c / eval too deep")
        return
    try:
        prog = parse(text, ctx.depth + 1)
    except ParseError:
        if mode == "strict":
            raise
        return
    analyze_program(prog, ctx if same_ctx else ctx.child())


# --- kill ---------------------------------------------------------------------------------

def _pid_tokens(vals):
    out = []
    for v in vals:
        out.extend(v.split() if v != OWN else [OWN])
    return out


def analyze_kill(args, ctx, label, extra=None):
    # Options are parsed softly: `kill -0 $PID` sends nothing, so there is no point in
    # computing $PID for it. Targets strictly: an unknown target means "unknown whom".
    flat = [(t, d, w) for w in args for t, d in ctx.soft([w])]
    sig, saw, i = None, False, 0
    while i < len(flat):
        t, dyn, _ = flat[i]
        if dyn:
            break
        if t in ("-l", "-L", "--list", "--table") or t.startswith("--list="):
            return
        if t in ("-s", "-n", "--signal"):
            if i + 1 < len(flat) and flat[i + 1][1]:
                raise Dynamic(f"signal {flat[i + 1][2].raw} is computed at run time")
            sig = flat[i + 1][0] if i + 1 < len(flat) else None
            saw = True
            i += 2
            continue
        if t.startswith("--signal="):
            sig, saw = t.split("=", 1)[1], True
            i += 1
            continue
        if t == "--":
            i += 1
            break
        if t.startswith("-") and len(t) > 1 and not saw:
            sig = t[2:] if re.match(r"-[sn][A-Za-z0-9]", t) else t[1:]
            saw = True
            i += 1
            continue
        break
    if sig is not None and signal_value(sig) == 0:
        return                                 # kill -0: a liveness check, sends nothing
    targets = []
    for t, dyn, w in flat[i:]:
        if dyn:
            ctx.expand(w)                      # raises Dynamic with the reason
            raise Dynamic(f"{short(w.raw, 60)} - value unknown before the command runs")
        targets.extend([t] if t == OWN else (t.split() or [t]))
    if extra is not None:
        targets += _pid_tokens(extra())
    pids = set()
    for t in targets:
        if t == OWN or t.startswith("%"):
            continue
        if "\x00" in t:
            raise Dynamic("PID is computed at run time ($RANDOM)")
        if not re.fullmatch(r"[+-]?\d+", t):
            continue                           # kill refuses it itself
        n = int(t)
        if n == 0:
            continue                           # own process group: each Bash call has one
        if n == -1:
            ctx.report.all.append(label)
        elif n < -1:
            ctx.report.groups.append((label, -n))
        else:
            pids.add(n)
    if pids:
        ctx.report.hits.append((label, pids))


# --- pkill --------------------------------------------------------------------------------

PK_SHORT_ARG = set("qFPOgsuUGtr")
PK_SHORT_FLAG = set("eLcfinoxhVv")
PK_LONG_ARG = {"signal", "queue", "pgroup", "group", "parent", "session", "terminal", "euid",
               "uid", "pidfile", "older", "runstates", "ns", "nslist", "delimiter"}


def analyze_pkill(args, ctx, label, extra=None):
    toks = ctx.expand_all(args)
    if extra is not None:
        toks += extra()
    # like procps signal_option(): the first argument of the form -SIGNAL is removed
    for k, t in enumerate(toks):
        if t.startswith("-") and len(t) > 1 and t != OWN:
            v = signal_value(t[1:])
            if v is None and t[1].isdigit():
                v = int(re.match(r"\d+", t[1:]).group(0))
            if v is not None and v >= 0:
                toks = toks[:k] + toks[k + 1:]
                break
    out, i, operands_only = [], 0, False
    while i < len(toks):
        t = toks[i]
        if operands_only or t == "-" or not t.startswith("-") or t == OWN:
            if t == OWN or OWN in t:
                raise Dynamic("pkill pattern depends on $$/$!")
            out.append(t)
            i += 1
            continue
        if t == "--":
            operands_only = True
            out.append(t)
            i += 1
            continue
        if t.startswith("--"):
            key, eq, val = t[2:].partition("=")
            if key in ("help", "version"):
                return
            if key in ("echo", "count"):
                i += 1
                continue
            if key in PK_LONG_ARG and not eq:
                val = toks[i + 1] if i + 1 < len(toks) else ""
                i += 1
            if key in ("signal", "queue"):
                i += 1
                continue
            if key == "parent" and val == OWN or key in ("pgroup", "session") and val == "0":
                return                               # own only: the shell's kids/group/session
            out.append(f"--{key}={val}" if key in PK_LONG_ARG else t)
            i += 1
            continue
        letters = t[1:]
        keep = ""
        j = 0
        while j < len(letters):
            ch = letters[j]
            if ch in ("h", "V"):
                return
            if ch in PK_SHORT_ARG:
                val = letters[j + 1:]
                if not val:
                    i += 1
                    val = toks[i] if i < len(toks) else ""
                if ch == "P" and val == OWN or ch in "gs" and val == "0":
                    return
                if OWN in val:
                    raise Dynamic("pkill argument depends on $$/$!")
                if ch != "q":
                    if keep:
                        out.append("-" + keep)
                        keep = ""
                    out.extend(["-" + ch, val])
                break
            if ch not in ("e", "c"):
                keep += ch
            j += 1
        if keep:
            out.append("-" + keep)
        i += 1
    pids = pgrep(out, "pkill")
    if pids:
        ctx.report.hits.append((label, pids))


# --- killall ------------------------------------------------------------------------------

def analyze_killall(args, ctx, label, extra=None):
    toks = ctx.expand_all(args)
    if extra is not None:
        toks += extra()
    regex = icase = pgroup = False
    user = None
    names = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "--":
            names += toks[i + 1:]
            break
        if t.startswith("--"):
            key, eq, val = t[2:].partition("=")
            if key in ("list", "version", "help"):
                return
            if key == "regexp":
                regex = True
            elif key == "ignore-case":
                icase = True
            elif key == "process-group":
                pgroup = True
            elif key in ("user", "signal", "younger-than", "older-than", "ns", "context"):
                if not eq:
                    i += 1
                    val = toks[i] if i < len(toks) else ""
                if key == "user":
                    user = val
            i += 1
            continue
        if t.startswith("-") and len(t) > 1:
            if signal_value(t[1:]) is not None:
                i += 1
                continue
            letters = t[1:]
            for j, ch in enumerate(letters):
                if ch in ("l", "V"):
                    return
                if ch == "r":
                    regex = True
                elif ch == "I":
                    icase = True
                elif ch == "g":
                    pgroup = True
                elif ch in "yosunZ":
                    val = letters[j + 1:]
                    if not val:
                        i += 1
                        val = toks[i] if i < len(toks) else ""
                    if ch == "u":
                        user = val
                    break
            i += 1
            continue
        names.append(t)
        i += 1
    pids = set()
    for nm in names:
        if OWN in nm:
            raise Dynamic("killall name depends on $$/$!")
        if "/" in nm and not regex:
            pids |= procs_with_exe(nm, ctx.cwd)
            continue
        base = ["-i"] if icase else []
        if user:
            base += ["-u", user]
        if regex:
            pids |= pgrep(base + ["--", nm], "killall -r")
        else:
            pids |= pgrep(base + ["-x", "--", ere_escape(nm[:15])], "killall")
    if pgroup and pids:
        for pid in pids:
            st = _stat(pid)
            if st:
                ctx.report.groups.append((label, st[3]))
    if pids:
        ctx.report.hits.append((label, pids))


def procs_with_exe(path, cwd):
    path = path if os.path.isabs(path) else os.path.join(cwd, path)
    try:
        st = os.stat(path)
    except OSError:
        return set()
    out = set()
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                e = os.stat(f"/proc/{d}/exe")
            except OSError:
                continue
            if (e.st_dev, e.st_ino) == (st.st_dev, st.st_ino):
                out.add(int(d))
    return out


# --- fuser -k -----------------------------------------------------------------------------

def analyze_fuser(args, ctx, label):
    soft = ctx.soft(args)
    kills = any(t in ("--kill",) or re.fullmatch(r"-[a-zA-Z]*k[a-zA-Z]*", t) for t, _ in soft)
    if not kills:
        return
    toks = ctx.expand_all(args)
    argv = []
    for t in toks:
        if t in ("--kill", "--interactive"):
            continue
        if t.startswith("-") and not t.startswith("--") and len(t) > 1:
            if signal_value(t[1:]) is not None:
                continue
            letters = "".join(ch for ch in t[1:] if ch not in "kiw")
            if letters:
                argv.append("-" + letters)
            continue
        argv.append(t)
    out, _ = run_capture([["fuser"] + argv], cwd=ctx.cwd)
    pids = {int(x) for x in re.findall(r"\b(\d+)\b", out)}
    if pids:
        ctx.report.hits.append((label, pids))


# --- xargs --------------------------------------------------------------------------------

def analyze_xargs(args, ctx, cmd, pipe, idx, env_changes, uid):
    null, delim, repl, argfile = False, None, None, None
    i = 0
    with_arg = set("adEILnPs")
    while i < len(args):
        if not args[i].literal():
            break
        a = args[i].value()
        if a == "--":
            i += 1
            break
        if not a.startswith("-") or a == "-":
            break
        if a.startswith("--"):
            key, eq, val = a[2:].partition("=")
            if key in ("version", "help", "show-limits"):
                return
            if key == "null":
                null = True
            elif key == "delimiter":
                delim = val
            elif key == "arg-file":
                argfile = val
            elif key == "replace":
                repl = val or "{}"
            i += 1
            continue
        letters = a[1:]
        j = 0
        while j < len(letters):
            ch = letters[j]
            if ch == "0":
                null = True
            elif ch == "i":
                repl = letters[j + 1:] or "{}"
                break
            elif ch in ("e", "l"):
                break
            elif ch in with_arg:
                val = letters[j + 1:]
                if not val:
                    i += 1
                    val = args[i].value() if i < len(args) and args[i].literal() else ""
                if ch == "I":
                    repl = val
                elif ch == "d":
                    delim = val
                elif ch == "a":
                    argfile = val
                break
            j += 1
        i += 1
    sub = args[i:]
    if not sub:
        return

    def items():
        if argfile:
            path = argfile if os.path.isabs(argfile) else os.path.join(ctx.cwd, argfile)
            text = _read(regular_file(path), MAX_OUT).decode("utf-8", "replace")
        elif idx > 0:
            text = ctx.eval_pipeline(pipe[:idx])
        else:
            text = ""
            for r in cmd.redirs:
                if r.op == "<" and r.target is not None:
                    p = ctx.expand(r.target, split=False)[0]
                    p = p if os.path.isabs(p) else os.path.join(ctx.cwd, p)
                    text = _read(regular_file(p), MAX_OUT).decode("utf-8", "replace")
                elif r.op in ("<<", "<<-") and r.body is not None:
                    text = r.body
                elif r.op == "<<<" and r.target is not None:
                    text = " ".join(ctx.expand(r.target))
        if null:
            return [x for x in text.split("\0") if x]
        if delim:
            d = {"\\n": "\n", "\\t": "\t", "\\0": "\0"}.get(delim, delim[:1])
            return [x for x in text.split(d) if x]
        return text.split()

    cache = {}

    def lazy():
        if "v" not in cache:
            try:
                cache["v"] = items()
            except Dynamic as e:
                cache["v"] = Dynamic(f"cannot compute what is fed into xargs: {e}")
        if isinstance(cache["v"], Dynamic):
            raise cache["v"]
        return cache["v"]

    dispatch(sub, ctx, cmd, pipe, idx, env_changes, extra=lazy, repl=repl, uid=uid)


# --- tmux ---------------------------------------------------------------------------------

TMUX_ARG = set("cfLST")
# Subcommands whose trailing arguments are a shell command; the letters are the options
# that take an argument.
TMUX_SHELL_CMDS = {
    "new-session": "cefFnstxy", "new": "cefFnstxy",
    "new-window": "ceFnt", "neww": "ceFnt",
    "split-window": "celtF", "splitw": "celtF",
    "respawn-pane": "cet", "respawnp": "cet",
    "respawn-window": "cet", "respawnw": "cet",
    "run-shell": "dt", "run": "dt",
    "if-shell": "t", "if": "t",
    "display-popup": "bcdehsStTwxy", "popup": "bcdehsStTwxy",
    "pipe-pane": "t", "pipep": "t",
}


def effective_env(ctx, env_changes):
    env = dict(os.environ)
    for k, b in ctx.exported.items():
        if b is None:
            env.pop(k, None)
            continue
        try:
            v = ctx.binding_values(b, k)
            env[k] = v[0] if v else ""
        except Dynamic:
            env[k] = None
    for kind, val in env_changes:
        if kind == "reset":
            for k in ("TMUX", "TMUX_TMPDIR"):
                env.pop(k, None)
        elif kind == "clear":
            env = {}
        elif kind == "unset":
            env.pop(val, None)
        elif kind == "set":
            for k, w in val.items():
                try:
                    v = ctx.expand(w, split=False)
                    env[k] = v[0] if v else ""
                except Dynamic:
                    env[k] = None
    return env


def protected_sockets():
    uid = os.getuid()
    out = {os.path.realpath(f"/tmp/tmux-{uid}/default")}
    tmpdir = os.environ.get("TMUX_TMPDIR") or "/tmp"
    out.add(os.path.realpath(f"{tmpdir}/tmux-{uid}/default"))
    tm = os.environ.get("TMUX")
    if tm:
        out.add(os.path.realpath(tm.split(",")[0]))
    return out


def socket_server(path):
    """pid of the tmux server listening on the socket, from /proc/net/unix, read-only."""
    inodes = set()
    for line in _read("/proc/net/unix", 1 << 22).decode("utf-8", "replace").splitlines()[1:]:
        f = line.split()
        if len(f) >= 8 and os.path.realpath(f[7]) == path:
            inodes.add(f"socket:[{f[6]}]")
    if not inodes:
        return None
    for d in os.listdir("/proc"):
        if not d.isdigit() or not _read(f"/proc/{d}/comm").startswith(b"tmux"):
            continue
        try:
            for fd in os.listdir(f"/proc/{d}/fd"):
                try:
                    if os.readlink(f"/proc/{d}/fd/{fd}") in inodes:
                        return int(d)
                except OSError:
                    continue
        except OSError:
            continue
    return None


def analyze_tmux(args, ctx, label, env_changes, uid):
    soft = ctx.soft(args)
    toks = [t for t, _ in soft]
    dyn = {k for k, (_, d) in enumerate(soft) if d}
    sock_s = sock_l = None
    sock_dyn = False
    i = 0
    while i < len(toks) and toks[i].startswith("-") and toks[i] != "-":
        t = toks[i]
        if t == "--":
            i += 1
            break
        j = 1
        while j < len(t):
            ch = t[j]
            if ch in TMUX_ARG:
                val = t[j + 1:]
                k = i
                if not val:
                    i += 1
                    k = i
                    val = toks[i] if i < len(toks) else ""
                if ch in "SL" and k in dyn:
                    sock_dyn = True
                if ch == "S":
                    sock_s = val
                elif ch == "L":
                    sock_l = val
                elif ch == "c":
                    analyze_script(val, ctx, label)
                break
            j += 1
        i += 1
    cmds, cur = [], []
    for t in toks[i:]:
        if t == ";":
            cmds.append(cur)
            cur = []
        elif t.endswith(";") and not t.endswith("\\;"):
            cur.append(t[:-1])
            cmds.append(cur)
            cur = []
        else:
            cur.append(t)
    cmds.append(cur)
    danger = []
    for c in cmds:
        if not c:
            continue
        name, rest = c[0], c[1:]
        flags = "".join(a[1:] for a in rest if a.startswith("-") and len(a) > 1)
        if name.startswith("kill") or name in ("killp", "killw") \
                or (name.startswith("respawn") and "k" in flags) \
                or (name in ("unlink-window", "unlinkw") and "k" in flags) \
                or (name in ("detach-client", "detach") and "P" in flags):
            danger.append(name)
        if name in TMUX_SHELL_CMDS:
            takes = TMUX_SHELL_CMDS[name]
            k = 0
            while k < len(rest) and rest[k].startswith("-") and len(rest[k]) > 1:
                opt = rest[k]
                if opt == "--":
                    k += 1
                    break
                arg_at = next((q for q, ch in enumerate(opt[1:]) if ch in takes), None)
                k += 2 if arg_at is not None and arg_at == len(opt) - 2 else 1
            tail = rest[k:]
            if name in ("if-shell", "if"):
                for tm in tail[1:3]:                # the 2nd and 3rd are tmux commands
                    tw = tm.split()
                    if tw and (tw[0].startswith("kill") or tw[0] in ("killp", "killw")):
                        danger.append(tw[0])
                tail = tail[:1]
            if tail:
                analyze_script(" ".join(tail), ctx, label)
    if not danger:
        return
    if sock_dyn:
        raise Dynamic("the tmux socket (-S/-L) is computed at run time - "
                      "cannot tell whether it is live")
    env = effective_env(ctx, env_changes)
    if sock_s:
        if any(x in sock_s for x in (OWN, FRESH, RAND)):
            return
        path = os.path.realpath(sock_s if os.path.isabs(sock_s) else os.path.join(ctx.cwd, sock_s))
    else:
        if sock_l is None and "TMUX" in env and env["TMUX"] is None:
            raise Dynamic("$TMUX is computed at run time - "
                          "cannot tell which socket tmux will use")
        if sock_l is None and env.get("TMUX"):
            path = os.path.realpath(env["TMUX"].split(",")[0])
        else:
            tmpdir = env.get("TMUX_TMPDIR", "")
            if tmpdir is None:
                raise Dynamic("TMUX_TMPDIR is computed at run time - "
                              "cannot tell which socket tmux will use")
            if any(x in (tmpdir or "") + (sock_l or "") for x in (OWN, FRESH, RAND)):
                return
            path = os.path.realpath(f"{tmpdir or '/tmp'}/tmux-{uid}/{sock_l or 'default'}")
    if path in protected_sockets():
        ctx.report.tmux.append((label, ", ".join(danger), path))


# --- systemctl ----------------------------------------------------------------------------

SC_DANGER = {"stop", "restart", "try-restart", "reload-or-restart", "try-reload-or-restart",
             "condrestart", "force-reload", "reload", "kill", "freeze"}
SC_SYSTEM = {"isolate", "rescue", "emergency", "halt", "poweroff", "reboot", "kexec",
             "soft-reboot", "default"}
SC_NOW = {"disable", "mask"}
SC_SHORT_ARG = set("tpsHMno")
SC_LONG_ARG = {"type", "property", "signal", "host", "machine", "lines", "output", "kill-whom",
               "kill-value", "job-mode", "root", "state", "what", "timestamp", "message",
               "boot-loader-entry", "reboot-argument", "image", "drop-in", "when", "preset-mode",
               "check-inhibitors", "image-policy"}
UNIT_SUFFIX = re.compile(r"\.(service|socket|target|scope|slice|mount|automount|swap|timer|"
                         r"path|device)$")
HARD_PATTERNS = ("agentdeck-*",)            # AgentKennel's own units: always protected


def protected_units():
    """Units whose cgroups hold tmux servers and Claude processes -> sample processes."""
    units = {"-.slice": set()}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        comm = _read(f"/proc/{d}/comm").strip()
        if comm != b"claude" and not comm.startswith(b"tmux: server"):
            continue
        for line in _read(f"/proc/{d}/cgroup").decode("utf-8", "replace").splitlines():
            path = line.split(":", 2)[-1]
            for comp in path.split("/"):
                if comp:
                    units.setdefault(comp, set()).add(int(d))
    return units


def analyze_systemctl(args, ctx, label):
    soft = ctx.soft(args)
    toks = [t for t, _ in soft]
    dyn = {t for t, d in soft if d}
    pos, now, i = [], False, 0
    while i < len(toks):
        t = toks[i]
        if t == "--":
            pos += toks[i + 1:]
            break
        if t.startswith("--"):
            key, eq, _ = t[2:].partition("=")
            if key == "now":
                now = True
            if key in SC_LONG_ARG and not eq:
                i += 1
            i += 1
            continue
        if t.startswith("-") and len(t) > 1:
            for j, ch in enumerate(t[1:]):
                if ch in SC_SHORT_ARG:
                    if not t[j + 2:]:
                        i += 1
                    break
            i += 1
            continue
        pos.append(t)
        i += 1
    if not pos:
        return
    check_units(pos[0], pos[1:], dyn, now, ctx, label)


def analyze_service(args, ctx, label):
    """`service NAME ACTION` is a wrapper around systemctl ACTION NAME."""
    soft = ctx.soft(args)
    toks = [t for t, _ in soft]
    dyn = {t for t, d in soft if d}
    if len(toks) < 2 or toks[0].startswith("-"):
        return                                # --status-all, --help, no action
    verb = "restart" if toks[1] == "--full-restart" else toks[1]
    check_units(verb, toks[:1], dyn, False, ctx, label)


def check_units(verb, units, dyn, now, ctx, label):
    import fnmatch
    if verb in dyn:
        verb = "stop"                         # an unknown action counts as dangerous
    if verb in SC_SYSTEM:
        ctx.report.units.append((label, verb, None))
        return
    if not (verb in SC_DANGER or (verb in SC_NOW and now)):
        return
    prot = protected_units()
    for u in units:
        if u in dyn:
            raise Dynamic(f"unit {short(u, 60)} is computed at run time")
        globby = bool(re.search(r"[*?\[]", u))
        nu = u if UNIT_SUFFIX.search(u) or globby else u + ".service"
        hit = set()
        found = False
        for name, anchors in prot.items():
            if (fnmatch.fnmatchcase(name, nu) if globby else name == nu):
                found = True
                hit |= anchors
        if any(fnmatch.fnmatchcase(nu, p) for p in HARD_PATTERNS):
            found = True
        if globby:
            prefix = re.split(r"[*?\[]", u, 1)[0]
            if "agentdeck-".startswith(prefix):
                found = True
        if found:
            ctx.report.units.append((label, u, hit))


# ==========================================================================================
# Result
# ==========================================================================================

WHY = ("In 2026 an agent ran `pkill -f \"noconftest 2>&1 | grep\"` and closed every live "
       "terminal:\n  in pkill -f the pattern is a regex and \"|\" means OR, so \" grep\" "
       "matched the tmux server\n  that hosted all of them.")


def render(report, table, cmd):
    out = ["BLOCKED by guard_kill: the command would signal processes OUTSIDE your own "
           "process tree.", ""]
    for label, pids in report.hits:
        foreign = table.foreign(pids)
        if foreign:
            out.append(f"- `{label}` would hit {len(foreign)} foreign process(es):")
            out.append(table.describe(foreign))
    for label in report.all:
        foreign = table.all_foreign()
        out.append(f"- `{label}`: kill -1 signals EVERY process ({len(foreign)} foreign):")
        out.append(table.describe(foreign))
    for label, pgid in report.groups:
        foreign = table.foreign(table.group(pgid))
        if foreign:
            out.append(f"- `{label}`: process group {pgid} holds foreign processes:")
            out.append(table.describe(foreign))
    for label, what, path in report.tmux:
        out.append(f"- `{label}`: tmux {what} on the LIVE socket {path} - every terminal "
                   "lives there.")
        srv = socket_server(path)
        if srv:
            out.append(table.describe({srv}))
    for label, unit, anchors in report.units:
        if anchors is None:
            out.append(f"- `{label}`: systemctl {unit} takes the whole system down, "
                       "terminals included.")
        else:
            out.append(f"- `{label}`: unit {unit} holds the terminals' tmux server or "
                       "Claude sessions:")
            if anchors:
                out.append(table.describe(anchors))
    for label, reason in report.dynamic:
        out.append(f"- `{label}`: {reason}.\n  The guard cannot tell whom the signal would hit.")
    for text in report.errors:
        out.append(f"- {text}")
    out += [
        "",
        "Rule: signals may only go to your own processes - descendants of this Claude "
        f"session (pid {table.root}).",
        "",
        "Do this instead:",
        "  - kill the PID you saved with $!:  cmd & pid=$!; ...; kill $pid",
        "  - see what a pattern matches, read-only: pgrep -af '<pattern>'",
        "  - check whether a process is alive: ps -p <pid> or kill -0 <pid>",
        "  - run your own tmux on your own socket: tmux -L <name> ...",
        "  - start long-lived processes with run_in_background so they stay in your tree",
        "",
        "Why: " + WHY,
        "",
        "Really needed, and you checked what it hits? Prefix the command with KILL_OK=1 "
        "(visible in the transcript).",
    ]
    return "\n".join(out)


def evaluate(cmd, cwd, strict=True):
    """strict: the text has a signal word, so anything unparsed is blocked. Otherwise (the
    command name is assembled at run time) block only what the parse took to a signal."""
    report = Report()
    ctx = Ctx(report, cwd)
    try:
        prog = parse(cmd)
        analyze_program(prog, ctx)
    except ParseError as e:
        if strict:
            report.errors.append(f"could not parse the command ({e}), and it contains "
                                 "kill/tmux/systemctl/xargs - not letting it through blind.")
    except RecursionError:
        if strict:
            report.errors.append("command too deeply nested to check.")
    if report.empty():
        return report, None
    table = Table()
    # Keep only what hits foreign processes.
    report.hits = [(lab, p) for lab, p in report.hits if table.foreign(p)]
    report.groups = [(lab, g) for lab, g in report.groups if table.foreign(table.group(g))]
    return report, table


def run(cmd, cwd, strict=True):
    """The block message, or None if the command may run."""
    report, table = evaluate(cmd, cwd, strict)
    if report.empty():
        return None
    return render(report, table, cmd)

#!/usr/bin/env python3
"""Claude Code PreToolUse hook: a git commit / git push must not publish a secret.

Why this exists: an agent once committed and pushed a server IP address and Telegram ids
into a public repository. "Never commit secrets" was written in its instructions, and it
still happened: a sentence in a document loses to momentum, a hook does not. Anything that
reaches a pushed commit is public at once and stays in history after the file is fixed.

What is checked (Bash tool calls only):
  git commit   the lines that would go into the commit: the staged diff; with -a/--all or
               with paths also the tracked unstaged changes; plus files named by a
               `git add ...` earlier in the same command (it has not run yet when this
               hook runs), including untracked files for `git add -A` / `git add .`.
  git push     the added lines of every commit being pushed that is not on the remote
               yet (`git log -p <refs> --not --remotes[=<remote>]`, at most 200 commits);
               pushed tags count too.
Only added lines are scanned; a line that removes a secret is welcome.

What counts as a secret: private key blocks, Telegram bot tokens, Anthropic, GitHub, AWS
and Slack keys, and this server's public IPv4 address. The address set is the union of
  AGENTKENNEL_PUBLIC_IPS        comma list (environment)
  AGENTKENNEL_PUBLIC_IPS_FILE   one address per line, '#' comments allowed
                              (default ~/.claude/agentkennel-public-ips, or the earlier
                              ~/.claude/agentdeck-public-ips; never expires; lets
                              a sandbox that only sees private interfaces know the host's
                              address)
  the global IPv4 addresses on this machine's interfaces (`ip -4 -o addr`, fallback
  `hostname -I`), cached in AGENTKENNEL_SECRETS_CACHE (default
  ~/.cache/agentkennel/public-ipv4) for a day. No network request is ever made.

Findings are printed masked (first 4 characters + last 2, or 203.0.x.x for an address);
the full value is never printed. Escape hatch for a false positive: the visible prefix
SECRETS_OK=1 on the command.

The fast path (no `git ... commit|push` in the command) costs one regex. Any internal error
(git missing, not a repository, timeout, odd output) fails OPEN: the guard must never break
work.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import envcompat  # noqa: E402  (AGENTKENNEL_*, then the old AGENTDECK_*)

FAST_RE = re.compile(r"\bgit\b.*?\b(?:commit|push)\b", re.S)
OVERRIDE_RE = re.compile(r"(?<![\w-])SECRETS_OK=1(?!\w)")
ELLIPSIS = "\u2026"
DASH = "\u2014"
GIT_TIMEOUT = 5.0
MAX_BYTES = 5 * 1024 * 1024
MAX_COMMITS = 200
CACHE_MAX_AGE = 86400
MAX_SHOWN = 20
IP_KIND = "this server's public IP"

SEPARATOR_CHARS = ";&|()\n"
WRAPPERS = {"sudo", "env", "command", "exec", "time", "nohup", "nice"}
SHELLS = {"bash", "sh", "zsh", "dash"}
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
DOTTED_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def mentions_commit_or_push(cmd: str) -> bool:
    return bool(FAST_RE.search(cmd or ""))


# -- public IP addresses -------------------------------------------------------------------
def _valid_ipv4(text: str) -> bool:
    import ipaddress
    try:
        ipaddress.IPv4Address(text)
        return True
    except ValueError:
        return False


def keep_public(addrs) -> list:
    """Global unicast IPv4 addresses only, in order, without duplicates."""
    import ipaddress
    cgnat = ipaddress.IPv4Network("100.64.0.0/10")
    out = []
    for a in addrs:
        try:
            ip = ipaddress.IPv4Address(str(a).strip())
        except ValueError:
            continue
        if (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip in cgnat):
            continue
        if str(ip) not in out:
            out.append(str(ip))
    return out


def parse_ip_output(text: str) -> list:
    """Addresses from `ip -4 -o addr show` output, or from `hostname -I` output."""
    if "inet " in text:
        return re.findall(r"\binet\s+(\d{1,3}(?:\.\d{1,3}){3})", text)
    return [t for t in text.split() if DOTTED_RE.match(t)]


def probe_interfaces() -> list:
    """Raw IPv4 addresses on this machine's interfaces (unfiltered). Never uses the network."""
    import subprocess
    for cmd in (["ip", "-4", "-o", "addr", "show", "scope", "global"], ["hostname", "-I"]):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        except Exception:
            continue
        if p.returncode == 0:
            addrs = parse_ip_output(p.stdout)
            if addrs:
                return addrs
    return []


def cache_path() -> str:
    return envcompat.get("SECRETS_CACHE") or os.path.join(
        os.path.expanduser("~"), ".cache", "agentkennel", "public-ipv4")


def ips_file_paths() -> list:
    """AGENTKENNEL_PUBLIC_IPS_FILE, else ~/.claude/agentkennel-public-ips and the file's
    name before v2.1.0, ~/.claude/agentdeck-public-ips (compat: both are read)."""
    configured = envcompat.get("PUBLIC_IPS_FILE")
    if configured:
        return [configured]
    claude = os.path.join(os.path.expanduser("~"), ".claude")
    return [os.path.join(claude, "agentkennel-public-ips"),
            os.path.join(claude, "agentdeck-public-ips")]   # compat: the old name


def discover_global_ipv4(cache=None, now=None, probe=None) -> set:
    """Global IPv4 addresses of this machine's interfaces, cached for a day."""
    import time
    path = cache or cache_path()
    now = time.time() if now is None else now
    try:
        if now - os.path.getmtime(path) < CACHE_MAX_AGE:
            with open(path) as f:
                return set(keep_public(f.read().split()))
    except (OSError, ValueError):
        pass
    found = keep_public((probe or probe_interfaces)())
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            f.write("".join(a + "\n" for a in found))
        os.replace(tmp, path)
    except OSError:
        pass
    return set(found)


def read_ip_file(path) -> set:
    """Addresses from a file: one or more per line, '#' starts a comment. Missing = empty."""
    out = set()
    try:
        with open(path) as f:
            for line in f:
                for tok in line.split("#", 1)[0].replace(",", " ").split():
                    if _valid_ipv4(tok):
                        out.add(tok)
    except (OSError, UnicodeDecodeError):
        pass
    return out


def env_ips() -> set:
    raw = envcompat.get("PUBLIC_IPS", "")
    return {t for t in raw.replace(",", " ").split() if _valid_ipv4(t)}


def public_ips() -> set:
    """Every address treated as "this server's public IP": env + known-IPs file + interfaces."""
    files = set().union(*(read_ip_file(f) for f in ips_file_paths()))
    return env_ips() | files | discover_global_ipv4()


# -- detection -----------------------------------------------------------------------------
def mask(value: str) -> str:
    return value[:4] + ELLIPSIS + value[-2:]


def mask_ip(value: str) -> str:
    parts = value.split(".")
    return ".".join(parts[:2] + ["x", "x"])


def detectors(ips) -> list:
    pats = [
        ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        ("Telegram bot token", r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
        ("Anthropic API key", r"sk-ant-[A-Za-z0-9_-]{20,}"),
        ("GitHub token", r"ghp_[A-Za-z0-9]{36}"),
        ("GitHub token", r"github_pat_[A-Za-z0-9_]{22,}"),
        ("AWS access key", r"AKIA[0-9A-Z]{16}"),
        ("Slack token", r"xox[bp]-[A-Za-z0-9-]{10,}"),
    ]
    out = [(kind, re.compile(rx)) for kind, rx in pats]
    for ip in sorted(ips):
        out.append((IP_KIND, re.compile(r"(?<![\d.])" + re.escape(ip) + r"(?![\d.])")))
    return out


def shown(kind: str, value: str) -> str:
    if kind == IP_KIND:
        return mask_ip(value)
    if kind == "private key":
        return value        # the BEGIN header line is not secret; the key body is never shown
    return mask(value)


def added_lines(text: str):
    """(commit, path, line number, text) for each added line of `git diff` / `git log -p`."""
    commit, path, lineno, old_left, new_left = "", "", 0, 0, 0
    for raw in text.split("\n"):
        if old_left > 0 or new_left > 0:
            if raw.startswith("+"):
                yield commit, path, lineno, raw[1:]
                lineno += 1
                new_left -= 1
                continue
            if raw.startswith("-"):
                old_left -= 1
                continue
            if raw.startswith("\\"):
                continue
            if raw.startswith(" "):
                old_left -= 1
                new_left -= 1
                lineno += 1
                continue
            old_left = new_left = 0
        if raw.startswith("commit "):
            commit = raw[7:].strip()
        elif raw.startswith("+++ "):
            p = raw[4:].strip()
            if p.startswith('"') and p.endswith('"'):
                p = p[1:-1]
            path = "" if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
        else:
            m = HUNK_RE.match(raw)
            if m:
                old_left = int(m.group(1) if m.group(1) is not None else 1)
                lineno = int(m.group(2))
                new_left = int(m.group(3) if m.group(3) is not None else 1)


# -- git -----------------------------------------------------------------------------------
class Scanner:
    def __init__(self, ips):
        self.detectors = detectors(ips)
        self.budget = MAX_BYTES
        self.findings = []
        self._seen = set()

    def git(self, cwd, args, limit=None):
        """stdout of a git call, bounded in time and size; None on error."""
        import selectors
        import subprocess
        import time
        limit = max(0, self.budget if limit is None else limit)
        env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0",
                   GIT_PAGER="cat")
        cmd = ["git", "-c", "core.quotepath=off", "-c", "color.ui=never", *args]
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        chunks, size, cut = [], 0, False
        deadline = time.monotonic() + GIT_TIMEOUT
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        try:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    cut = True
                    break
                if not sel.select(left):
                    continue
                buf = os.read(proc.stdout.fileno(), 65536)
                if not buf:
                    break
                chunks.append(buf)
                size += len(buf)
                if size >= limit:
                    cut = True
                    break
        finally:
            sel.close()
            if cut:
                proc.kill()
            try:
                rc = proc.wait(timeout=GIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait()
            proc.stdout.close()
        if rc != 0 and not cut:
            return None
        data = b"".join(chunks)[:limit]
        return data.decode("utf-8", "replace")

    def git_scan(self, cwd, args):
        out = self.git(cwd, args)
        if out:
            self.budget -= len(out.encode("utf-8", "replace"))
            self.scan_diff(out)

    def add(self, kind, commit, path, lineno, value):
        sh = shown(kind, value)
        key = (kind, path, sh, commit)
        if key in self._seen:
            return
        self._seen.add(key)
        where = f"{path}:{lineno}" + (f" (commit {commit})" if commit else "")
        self.findings.append((kind, where, sh))

    def scan_line(self, commit, path, lineno, line):
        for kind, rx in self.detectors:
            for m in rx.finditer(line):
                self.add(kind, commit, path, lineno, m.group(0))

    def scan_diff(self, text):
        for commit, path, lineno, line in added_lines(text):
            self.scan_line(commit, path, lineno, line)

    def scan_file(self, top, rel):
        import stat
        full = os.path.join(top, rel)
        try:
            if not stat.S_ISREG(os.lstat(full).st_mode) or self.budget <= 0:
                return
            with open(full, "rb") as f:
                data = f.read(self.budget)
        except OSError:
            return
        self.budget -= len(data)
        if b"\0" in data[:8192]:
            return
        for n, line in enumerate(data.decode("utf-8", "replace").split("\n"), 1):
            self.scan_line("", rel, n, line)


DIFF = ["diff", "-U0", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/"]
LOG = ["log", "-p", "-U0", "--no-color", "--no-ext-diff", "--src-prefix=a/",
       "--dst-prefix=b/", "--format=commit %h", f"--max-count={MAX_COMMITS}"]


def base_rev(sc, d) -> str:
    """HEAD, or the empty tree on an unborn branch."""
    if sc.git(d, ["rev-parse", "--verify", "-q", "HEAD^{commit}"], limit=4096):
        return "HEAD"
    empty = sc.git(d, ["hash-object", "-t", "tree", "/dev/null"], limit=4096)
    return (empty or "").strip() or "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


# -- command parsing -----------------------------------------------------------------------
def tokenize(cmd: str) -> list:
    import shlex
    try:
        lx = shlex.shlex(cmd, posix=True, punctuation_chars=SEPARATOR_CHARS)
        lx.whitespace = " \t\r"
        lx.whitespace_split = True
        lx.commenters = ""
        return list(lx)
    except ValueError:
        return re.sub(r"([;&|()\n])", r" \1 ", cmd).split()


def segments(tokens):
    seg = []
    for t in tokens:
        if t and all(c in SEPARATOR_CHARS for c in t):
            if seg:
                yield seg
            seg = []
        else:
            seg.append(t)
    if seg:
        yield seg


def _join(cur, target):
    return os.path.normpath(os.path.join(cur, os.path.expandvars(os.path.expanduser(target))))


GIT_GLOBAL_ARG = {"-c", "--git-dir", "--work-tree", "--namespace", "--config-env",
                  "--super-prefix"}


def find_invocations(cmd: str, cwd: str, depth: int = 0) -> list:
    """[(dir, subcommand, args)] for git add / commit / push, in command order."""
    cur, out = cwd, []
    for seg in segments(tokenize(cmd)):
        i = 0
        while i < len(seg) and (ASSIGN_RE.match(seg[i]) or seg[i] in WRAPPERS):
            i += 1
        if i >= len(seg):
            continue
        head, rest = os.path.basename(seg[i]), seg[i + 1:]
        if head in ("cd", "pushd"):
            target = next((a for a in rest if not a.startswith("-")), "~")
            new = _join(cur, target) if target != "-" else cur
            if os.path.isdir(new):        # a failing cd leaves the shell where it was
                cur = new
            continue
        if head in SHELLS and "-c" in rest and depth < 2:
            j = rest.index("-c")
            if j + 1 < len(rest):
                out += find_invocations(rest[j + 1], cur, depth + 1)
            continue
        if head != "git":
            continue
        d, j = cur, 0
        while j < len(rest) and rest[j].startswith("-"):
            if rest[j] == "-C" and j + 1 < len(rest):
                d = _join(d, rest[j + 1])
                j += 2
            elif rest[j] in GIT_GLOBAL_ARG:
                j += 2
            else:
                j += 1
        if j < len(rest) and rest[j] in ("add", "commit", "push"):
            out.append((d, rest[j], rest[j + 1:]))
    return out


COMMIT_LONG_ARG = {"--message", "--file", "--author", "--date", "--template",
                   "--reuse-message", "--reedit-message", "--fixup", "--squash", "--cleanup",
                   "--trailer", "--pathspec-from-file"}


def parse_commit(args):
    """(all_flag, paths)"""
    all_flag, paths, j = False, [], 0
    while j < len(args):
        a = args[j]
        j += 1
        if a == "--":
            paths += args[j:]
            break
        if a == "--all":
            all_flag = True
        elif a.startswith("--"):
            if a in COMMIT_LONG_ARG:
                j += 1
        elif a.startswith("-") and len(a) > 1:
            for k, ch in enumerate(a[1:], 1):
                if ch == "a":
                    all_flag = True
                elif ch in "mFCct":
                    if k == len(a) - 1:
                        j += 1
                    break
                elif ch in "Su":
                    break
        else:
            paths.append(a)
    return all_flag, paths


def parse_add(args):
    """(paths, everything, tracked_only, force)"""
    paths, everything, tracked_only, force, j = [], False, False, False, 0
    while j < len(args):
        a = args[j]
        j += 1
        if a == "--":
            paths += args[j:]
            break
        if a in ("--all", "--no-ignore-removal"):
            everything = True
        elif a == "--update":
            tracked_only = True
        elif a == "--force":
            force = True
        elif a.startswith("--"):
            continue
        elif a.startswith("-") and len(a) > 1:
            everything |= "A" in a
            tracked_only |= "u" in a
            force |= "f" in a
        else:
            paths.append(a)
    return paths, everything, tracked_only, force


PUSH_ARG = {"--repo", "-o", "--push-option", "--receive-pack", "--exec"}


def parse_push(args):
    """(remote, refspecs, flags)"""
    remote, refspecs, flags, j = None, [], set(), 0
    while j < len(args):
        a = args[j]
        j += 1
        if a == "--":
            continue
        if a in PUSH_ARG:
            j += 1
        elif a.startswith("-"):
            name = a.split("=", 1)[0]
            if name in ("--tags", "--all", "--branches", "--mirror", "--delete", "-d"):
                flags.add(name)
        elif remote is None:
            remote = a
        else:
            refspecs.append(a)
    return remote, refspecs, flags


# -- scanning per invocation ---------------------------------------------------------------
def scan_add(sc, d, args, top):
    paths, everything, tracked_only, force = parse_add(args)
    if not paths and not everything and not tracked_only:
        return
    spec = ["--"] + paths if paths else []
    sc.git_scan(d, DIFF + [base_rev(sc, d)] + spec)
    if tracked_only:
        return
    ls = ["ls-files", "-z", "--others", "--full-name"]
    if not force:
        ls.append("--exclude-standard")
    listing = sc.git(d, ls + spec, limit=1024 * 1024)
    for rel in (listing or "").split("\0"):
        if rel and sc.budget > 0:
            sc.scan_file(top, rel)


def scan_commit(sc, d, args, adds):
    top = (sc.git(d, ["rev-parse", "--show-toplevel"], limit=65536) or "").strip()
    if not top:
        return
    all_flag, paths = parse_commit(args)
    if all_flag:
        sc.git_scan(d, DIFF + [base_rev(sc, d)])
    else:
        sc.git_scan(d, DIFF + ["--cached"])
        if paths:
            sc.git_scan(d, DIFF + [base_rev(sc, d), "--"] + paths)
    for add_dir, add_args in adds:
        scan_add(sc, add_dir, add_args, top)


def scan_push(sc, d, args):
    """Scan the unpushed commits; returns the `git reset --soft` target for the message."""
    remote, refspecs, flags = parse_push(args)
    if not sc.git(d, ["rev-parse", "--git-dir"], limit=65536):
        return None
    revs = []
    if flags & {"--all", "--branches", "--mirror"}:
        revs.append("--branches")
    if flags & {"--tags", "--mirror"}:
        revs.append("--tags")
    if not flags & {"--delete", "-d"}:
        for r in refspecs:
            src = r.lstrip("+").split(":", 1)[0]
            if not src:
                continue
            if "*" in src:
                revs.append(f"--glob={src}")
                continue
            sha = sc.git(d, ["rev-parse", "--verify", "-q", src + "^{commit}"], limit=4096)
            if sha and sha.strip():
                revs.append(sha.strip())
        if not refspecs and not revs:
            revs.append("HEAD")
    if not revs:
        return None
    names = (sc.git(d, ["remote"], limit=65536) or "").split()
    branch = (sc.git(d, ["symbolic-ref", "--short", "-q", "HEAD"], limit=4096) or "").strip()
    target = ""
    if remote is None:
        up = (sc.git(d, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{push}"],
                     limit=4096) or "").strip()
        if up and "/" in up:
            target = up
            remote = up.split("/", 1)[0]
    exclude = f"--remotes={remote}" if remote in names else "--remotes"
    sc.git_scan(d, LOG + revs + ["--not", exclude])
    return target or f"{remote if remote in names else 'origin'}/{branch or '<branch>'}"


def block_message(findings, did_commit, reset_targets) -> str:
    what = "commit/push" if did_commit and reset_targets else (
        "push" if reset_targets else "commit")
    lines = [f"BLOCKED {DASH} guard_secrets: this {what} would publish "
             f"{len(findings)} secret(s):", ""]
    w_kind = max(len(k) for k, _, _ in findings[:MAX_SHOWN])
    w_where = max(len(w) for _, w, _ in findings[:MAX_SHOWN])
    for kind, where, value in findings[:MAX_SHOWN]:
        lines.append(f"  {kind:<{w_kind}}  {where:<{w_where}}  {value}")
    if len(findings) > MAX_SHOWN:
        lines.append(f"  ... and {len(findings) - MAX_SHOWN} more")
    lines += [
        "",
        "A pushed commit is public at once and keeps the value in history even after the",
        "file is fixed.",
        "",
        "How to fix:",
        "  1. Remove the value from the file. Use a made-up placeholder in examples, and read",
        "     the real value from an environment variable or from an untracked file that is",
        "     listed in .gitignore.",
    ]
    if did_commit:
        lines += [
            "  2. Commit: unstage the file with  git restore --staged <file> , fix it, then",
            "     git add it again and commit.",
        ]
    for target in reset_targets:
        lines += [
            "  2. Push: the value is inside a local commit that is not on the remote yet.",
            "     Rewrite the unpushed commits (interactive rebase is not available to agents):",
            f"       git reset --soft {target}    # undoes them, keeps the changes staged",
            "     then fix the file, git add, git commit, and push again.",
        ]
    lines += [
        "  3. If the value was ever pushed anywhere (another remote, a fork, a paste), treat",
        "     it as leaked and rotate it: revoke the key or token and issue a new one.",
        "",
        "False positive (a documented test value, a placeholder that only looks real)?",
        "Re-run the command with the visible prefix:  SECRETS_OK=1 <command>",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        return 0
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        return 0
    cmd = (data.get("tool_input") or {}).get("command") or ""
    if not isinstance(cmd, str) or not mentions_commit_or_push(cmd):
        return 0
    if OVERRIDE_RE.search(cmd):
        return 0
    cwd = data.get("cwd") or os.getcwd()
    invocations = find_invocations(cmd, cwd)
    if not any(sub in ("commit", "push") for _, sub, _ in invocations):
        return 0
    sc = Scanner(public_ips())
    adds, did_commit, reset_targets = [], False, []
    for d, sub, args in invocations:
        try:
            if sub == "add":
                adds.append((d, args))
            elif sub == "commit":
                before = len(sc.findings)
                scan_commit(sc, d, args, [a for a in adds if a[0] == d])
                did_commit |= len(sc.findings) > before
            else:
                before = len(sc.findings)
                target = scan_push(sc, d, args)
                if target and len(sc.findings) > before and target not in reset_targets:
                    reset_targets.append(target)
        except Exception:
            continue
    if not sc.findings:
        return 0
    msg = block_message(sc.findings, did_commit, reset_targets)
    sys.stderr.buffer.write(msg.encode("utf-8"))
    sys.stderr.flush()
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[guard_secrets] internal error, allowing: {e}", file=sys.stderr)
        sys.exit(0)

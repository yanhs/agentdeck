#!/usr/bin/env python3
"""Claude Code PreToolUse hook: email to other people goes out only on explicit approval.

Why this exists: a sent message cannot be recalled. It lands with someone outside this
session, and the person the agent works for answers for it. "Ask before sending mail" is an
easy rule to write down and an easy one to skip when the agent is sure the text is fine.

The rule: a Bash command that sends mail is blocked unless the same command carries the
visible prefix SEND_APPROVED=1. The agent puts it there only after the person saw the
message text, the sender address and the recipient address, and said "send". One yes
covers one message; approval for an earlier message does not carry over, and "write an
email" is not "send it". Because the prefix sits in the transcript next to the person's
reply, approval cannot be faked quietly.

Blocked (sending):
  Python smtplib / SMTP(...), send_email(...) / send_mail(...) calls, sendmail / sendMail(),
  command-line clients (mail -s, mailx, mutt, neomutt, msmtp, ssmtp, s-nail, swaks),
  curl --mail-rcpt (SMTP), HTTP calls to mail APIs (Resend, SendGrid, Mailgun, Postmark,
  Brevo, SparkPost, Amazon SES, Gmail API send, Microsoft Graph sendMail),
  aws ses|sesv2 send-email, git send-email -- also inside docker exec / ssh / sh -c /
  python -c / a heredoc fed to an interpreter.

Not blocked (preparing or reading):
  drafts (create_draft, IMAP / imaplib), --dry-run, commands that only read or search
  (grep, cat, git log, ...), package installs, GET/HEAD requests to a provider's API
  (reading logs), and heredocs written to a file (writing a file is not sending).

Known limitation: the hook sees the command line only. `python3 send.py` whose file sends
mail is not detected; reading every script would block every app that contains mailing
code. Treat the hook as a net for the obvious case, not as a sandbox.

Exit 2 with the reason on stderr blocks the call. Non-Bash tools are ignored. Any internal
error fails OPEN: a broken guard must never wedge the session.
"""
import json
import re
import sys

OVERRIDE = re.compile(r"(?<![\w-])SEND_APPROVED=1(?!\w)")

API_WHY = "an HTTP call to a mail provider's API"

# (pattern, what was detected, read-only requests allowed)
SENDERS = [
    (re.compile(r"\b(?:aio)?smtplib\b|\bSMTP(?:_SSL)?\s*\("),
     "Python smtplib (direct SMTP)", False),
    # send_email( / send_mail( are kept on purpose. In a shell command (as opposed to a file
    # edit) a call with parentheses is code that runs: python -c, manage.py shell -c, a
    # heredoc fed to an interpreter. It is the conventional name of the sending function
    # (Django send_mail, boto3 SES send_email, most app mailers). The places where the name
    # appears only as text -- grep/rg, git log, commit messages, a heredoc written to a
    # file -- are exempt below, so the false-positive risk is low. send_message( is NOT a
    # detector: chat bots use the same name.
    (re.compile(r"\bsend_e?mail\s*\("), "a send_email() / send_mail() call", False),
    (re.compile(r"\bsendmail\b(?![.\w-])", re.I), "sendmail / sendMail()", False),
    (re.compile(r"\b(?:msmtp|mutt|neomutt|mailx|s-nail|ssmtp|swaks)\b(?![.\w-])"),
     "a command-line mail client", False),
    # `mail` is an ordinary word (mail.log, ~/Mail, grep mail), so the bare command counts
    # only when it is given a sending flag or an address: mail -s subj, echo x | mail a@b.
    (re.compile(r"(?<![\w./~$-])mail(?:\s+-[A-Za-z]+)*\s+"
                r"(?:-[sacbrtA]\b|['\"]?[\w.+-]+@[\w-]+(?:\.[\w-]+)+)"),
     "the mail command with a recipient", False),
    (re.compile(r"--mail-rcpt\b|--mail-from\b"), "curl over SMTP (--mail-rcpt)", False),
    (re.compile(r"\bapi\.(?:eu\.)?(?:resend|sendgrid|mailgun|postmarkapp|brevo|sendinblue|"
                r"sparkpost)\.(?:com|net)\b"), API_WHY, True),
    (re.compile(r"googleapis\.com/\S*/(?:messages|drafts)/send\b"), "the Gmail API send", True),
    (re.compile(r"graph\.microsoft\.com/\S*/sendMail\b", re.I), "Microsoft Graph sendMail",
     True),
    (re.compile(r"\bemail(?:-smtp)?\.[\w-]+\.amazonaws\.com"), "Amazon SES (HTTP API)", True),
    (re.compile(r"\bses(?:v2)?\s+send-[\w-]*email\b"), "Amazon SES (aws ses send-email)",
     False),
    (re.compile(r"\bgit\s+send-email\b"), "git send-email", False),
]

# Preparing and reading are not sending. Checked per part of the command, so a dry run in
# one part does not excuse a send in another; never covers raw SMTP.
SAFE = re.compile(r"\bcreate_draft\b|\bsave_draft\b|\bdrafts?\.create\b|\bIMAP\b|\bimaplib\b|"
                  r"--dry-run\b")
NEVER_SAFE = re.compile(r"\b(?:aio)?smtplib\b|\bSMTP(?:_SSL)?\s*\(")

MSG = """BLOCKED: this command sends email ({why}).

Email to other people goes out only with explicit approval. A sent message cannot be
recalled: it is already with someone outside this session, and the person you work for
answers for it, not you.

What to do:
  1. Show the person, in this conversation, the message TEXT, the SENDER address and the
     RECIPIENT address.
  2. Wait for an explicit "send" for this message.
  3. Repeat the command with the prefix SEND_APPROVED=1 .
No approval yet? Make a draft instead: drafts are not blocked.

A yes covers that one message only. Approval for an earlier message does not carry over,
and "write an email" is not "send it".

Command: {cmd}"""

# --- Heredocs: data when written to a file, code when an interpreter reads them ----------
# Without this the guard would block writing a test or a mailer module to disk, and a guard
# that fires on harmless commands gets switched off entirely.
_HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1([^\n]*)\n(.*?)^[ \t]*\2[ \t]*$",
                      re.S | re.M)
_INTERPRETER = re.compile(r"(?<![\w.-])(?:python[\d.]*|node|deno|bun|bash|sh|zsh|dash|ksh|"
                          r"perl|ruby|php|psql|ssh|pwsh|osascript)(?![\w./-])")


def strip_data_heredocs(cmd: str) -> str:
    def repl(m):
        line_start = cmd.rfind("\n", 0, m.start()) + 1
        opening = cmd[line_start:m.start()] + m.group(3)
        if _INTERPRETER.search(opening):
            return m.group(0)          # python3 - <<EOF, cat <<EOF | sh, ... && python3 f
        return m.group(0)[:m.start(4) - m.start()]
    return _HEREDOC.sub(repl, cmd)


# --- Parts of a command that cannot send ------------------------------------------------
# Reading and searching (the mail word is what is looked for), package installs and git
# bookkeeping. `git` is not exempt as a whole: `git send-email` really sends.
_HARMLESS = re.compile(
    r"^(?:cd|pwd|grep|egrep|fgrep|rg|ag|ack|find|ls|cat|head|tail|less|more|sed|wc|diff|"
    r"file|stat|tree|jq|sort|uniq|cut|nl|column|echo|printf|basename|dirname|realpath|"
    r"md5sum|sha256sum|man|which|whereis|type|apropos|journalctl|true|"
    r"docker\s+(?:ps|images|logs|inspect)|"
    r"git\s+(?:log|grep|show|diff|status|blame|ls-files|cat-file|branch|remote|rev-parse|"
    r"add|commit)|"
    r"(?:pip3?|python3?\s+-m\s+pip|uv\s+pip|apt|apt-get|dnf|yum|brew|npm|pnpm|yarn|gem|"
    r"cargo)\s+(?:install|add|remove|uninstall|show|info|search|list))\b"
)
_PREFIX = re.compile(r"^(?:\w+=\S*\s+|sudo(?:\s+-\S+)*\s+)+")   # FOO=1 sudo -E grep ...
_SUBSHELL = re.compile(r"\$\(|`")       # anything can hide in $(...) or backticks
_RUNS_COMMAND = re.compile(r"\s-(?:exec|execdir|ok|okdir)\b")   # find -exec ...


def segments(cmd: str) -> list:
    """Split on |, ;, &&, || and newlines, leaving quoted text and escapes intact.

    A plain regex split is wrong: in `grep "a\\|b"` the bar is part of the pattern."""
    parts, buf, quote = [], [], None
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch == "\\" and i + 1 < len(cmd):
            buf.append(ch)
            buf.append(cmd[i + 1])
            i += 1
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
        elif ch in "|;\n":
            parts.append("".join(buf))
            buf = []
            if i + 1 < len(cmd) and cmd[i + 1] == ch:
                i += 1
        elif ch == "&" and i + 1 < len(cmd) and cmd[i + 1] == "&":
            parts.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [s.strip() for s in parts if s.strip()]


def harmless(part: str) -> bool:
    return bool(_HARMLESS.match(_PREFIX.sub("", part))) and not _RUNS_COMMAND.search(part)


def skippable_parts(cmd: str, parts: list) -> bool:
    """May the harmless parts be left out of the scan?

    Only when nothing can hide in $(...) and no part runs an interpreter: in
    `grep smtplib x.py | python3 -` or `grep -l smtplib *.py | xargs python3` the read
    feeds code, so the mail word in it counts."""
    return not _SUBSHELL.search(cmd) and not any(_INTERPRETER.search(p) for p in parts)


# A provider sends on a request WITH a body. Without a body (or a config file that may hold
# one) and without a write method, the request reads logs or checks reachability. Judged on
# the whole command: in a multi-line script the URL and the .post( may sit on different lines.
_HTTP_BODY = re.compile(r"(?:^|\s)(?:-d|--data|--data-\w+|--json|-F|--form|-T|--upload-file|"
                        r"-K|--config|--post-data|--post-file)\b")
_HTTP_WRITE = re.compile(r"(?:-X|--request|--method)[\s=]*['\"]?(?:POST|PUT|PATCH)|\bPOST\b|"
                         r"\.(?:post|put|patch)\s*\(", re.I)
_SEND_ENDPOINT = re.compile(r"/send\b|/sendMail\b|/smtp/email\b|/transmissions\b|"
                            r"outbound-emails\b", re.I)


def only_reads_provider(cmd: str, part: str) -> bool:
    return not (_HTTP_BODY.search(cmd) or _HTTP_WRITE.search(cmd)
                or _SEND_ENDPOINT.search(part))


def detect(cmd: str):
    """Return what was detected, or None when the command does not send mail."""
    cmd = strip_data_heredocs(cmd)
    parts = segments(cmd)
    skip_harmless = skippable_parts(cmd, parts)
    for part in parts:
        if skip_harmless and harmless(part):
            continue
        if SAFE.search(part) and not NEVER_SAFE.search(part):
            continue
        for rx, why, reads_ok in SENDERS:
            if rx.search(part) and not (reads_ok and only_reads_provider(cmd, part)):
                return why
    return None


def main() -> int:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        return 0
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        return 0
    cmd = (data.get("tool_input") or {}).get("command") or ""
    if not cmd or OVERRIDE.search(cmd):
        return 0
    why = detect(cmd)
    if why is None:
        return 0
    print(MSG.format(why=why, cmd=cmd[:300]), file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[guard_email] internal error, allowing: {e}", file=sys.stderr)
        sys.exit(0)

"""hooks/guard_email.py -- Claude Code PreToolUse guard for outgoing email.

The rule: mail to other people goes out only with a visible SEND_APPROVED=1 prefix in the
same command, and that prefix is put there only after the person saw the text, the sender
and the recipient and said "send". Drafts and reading mail are free.

The guard must stop SENDING and let READING through. A guard that fires on harmless
commands stops being read, and the one time it fires for real it gets scrolled past. So
every relaxation below has a paired check that it did not let a real send through.

The hook is run the way Claude Code runs it: hook JSON on stdin, in a subprocess.
Exit 2 = blocked (reason on stderr), exit 0 = allowed.

All addresses and hosts here are made up (example.com, 203.0.113.0/24).
"""
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO, "hooks", "guard_email.py")
NON_ASCII = re.compile(r"[^\x00-\x7F]")
# the author's own domain, spelled in parts so the repo itself never contains it
OWNER_DOMAIN = "re" + "imake"


def run_raw(raw):
    return subprocess.run([sys.executable, HOOK], input=raw,
                          capture_output=True, text=True, timeout=30)


def run(command, tool="Bash"):
    payload = {"tool_name": tool, "tool_input": {"command": command}}
    return run_raw(json.dumps(payload))


def blocked(command):
    p = run(command)
    assert p.returncode in (0, 2), p
    return p.returncode == 2


# -- Pass: searching and reading files -------------------------------------------------
# None of these send anything. The mail word in them is what is searched for or read.

def test_cd_before_search_passes():
    # Real commands usually start with `cd`; a relaxation that only matches a bare grep
    # passes the tests and never fires in practice.
    assert not blocked('cd /srv/app && grep -rln "smtplib" --include=*.py . | head -3')


def test_cd_before_send_does_not_help():
    assert blocked("cd /srv/app && python3 -c 'import smtplib'")


def test_grep_for_smtplib_passes():
    # The `\|` inside the pattern is not a pipe.
    assert not blocked('grep -rln "smtp.example.com\\|SMTP_HOST\\|smtplib" --include=*.py .')


def test_ripgrep_for_send_email_passes():
    assert not blocked("rg -n 'send_email\\(' --type py")


def test_sed_print_of_mailer_passes():
    assert not blocked("sed -n '1,80p' outreach_email.py")


def test_cat_of_mailer_passes():
    assert not blocked("cat /srv/app/outreach_email.py")


def test_git_log_grep_passes():
    assert not blocked('git log --oneline --grep="sendmail"')


def test_find_by_name_passes():
    assert not blocked("find . -name 'msmtp*' -print")


def test_ls_piped_to_grep_passes():
    assert not blocked("ls -la scripts/ | grep msmtp")


# -- Pass: everyday commands that merely mention mail ----------------------------------

def test_grep_word_mail_passes():
    assert not blocked("grep -r mail .")


def test_cat_mail_log_passes():
    assert not blocked("cat mail.log")


def test_tail_sendmail_log_passes():
    assert not blocked("sudo tail -n 50 /var/log/sendmail.log")


def test_git_log_author_address_passes():
    assert not blocked("git log --author=x@example.com")


def test_ls_mail_dir_passes():
    assert not blocked("ls ~/Mail")


def test_pip_install_sendgrid_passes():
    assert not blocked("pip install sendgrid")


def test_apt_install_mutt_passes():
    assert not blocked("sudo apt-get install -y mutt msmtp")


def test_man_sendmail_passes():
    assert not blocked("man sendmail")


def test_git_commit_message_mentioning_send_email_passes():
    assert not blocked('git commit -m "retry send_email() on timeout"')


def test_running_tests_for_mailer_passes():
    assert not blocked("python3 -m pytest -q tests/test_mailer.py")


def test_logs_piped_to_grep_for_sendmail_passes():
    # The grep part only filters text; nothing in the pipeline runs code.
    assert not blocked("kubectl logs deploy/mailer | grep -i sendmail")


def test_mail_reading_its_mailbox_passes():
    assert not blocked("mail -f ~/mbox")


def test_telegram_send_message_passes():
    # send_message() is a common name outside mail (chat bots); it is not a detector.
    assert not blocked("python3 -c \"bot.send_message(chat_id, 'deploy done')\"")


def test_edit_of_mailer_py_passes():
    p = run_raw(json.dumps({"tool_name": "Edit", "tool_input": {
        "file_path": "/srv/app/mailer.py",
        "old_string": "x", "new_string": "import smtplib\nsmtplib.SMTP('203.0.113.5')"}}))
    assert p.returncode == 0


def test_write_of_file_with_smtplib_passes():
    p = run_raw(json.dumps({"tool_name": "Write", "tool_input": {
        "file_path": "/srv/app/mailer.py", "content": "import smtplib\n"}}))
    assert p.returncode == 0


def test_heredoc_written_to_file_passes():
    # Writing a file is not sending; the body of a heredoc that goes to `cat >` is data.
    assert not blocked("cat > mailer.py <<'EOF'\nimport smtplib\n"
                       "s = smtplib.SMTP('203.0.113.5')\nEOF")


# -- Pass: reading from a mail provider ------------------------------------------------
# Providers send on a request WITH a body (POST). A request without a body and without a
# write method cannot send a message: it reads logs or checks reachability.

def test_curl_get_provider_log_passes():
    assert not blocked('curl -s -H "Authorization: Bearer $MAIL_TOKEN" '
                       '"https://api.resend.com/emails?limit=5"')


def test_curl_get_through_docker_passes():
    assert not blocked("docker exec app sh -c 'curl -s -o /tmp/r.json "
                       "-H \"Authorization: Bearer $MAIL_TOKEN\" "
                       "\"https://api.resend.com/emails\"'")


def test_curl_explicit_get_passes():
    assert not blocked("curl -X GET https://api.resend.com/emails/abc-123")


def test_curl_head_passes():
    assert not blocked("curl -I https://api.mailgun.net/v3/domains")


# -- Pass: drafts, IMAP, dry runs ------------------------------------------------------

def test_imap_draft_passes():
    assert not blocked("python3 -c \"import imaplib; m = imaplib.IMAP4_SSL('203.0.113.7'); "
                       "m.append('Drafts', '', None, msg)\"")


def test_create_draft_passes():
    assert not blocked("python3 -c 'from mailer import create_draft; create_draft(msg)'")


def test_dry_run_passes():
    assert not blocked("git send-email --dry-run --to=b@example.com 0001.patch")


# -- Pass: approval prefix -------------------------------------------------------------

def test_approval_prefix_passes():
    assert not blocked('SEND_APPROVED=1 python3 -c "import smtplib"')


def test_approval_prefix_on_script_passes():
    assert not blocked("SEND_APPROVED=1 python3 send.py")


def test_limitation_script_file_is_not_inspected():
    # KNOWN LIMITATION, pinned on purpose: the hook sees the command line only. A script
    # file whose content sends mail is invisible to it. Reading every script an agent runs
    # would block every app that merely contains mailing code. If the hook ever learns to
    # look inside scripts, flip this assertion.
    assert not blocked("python3 send.py")


# -- Pass: not a Bash call, or nothing to check ----------------------------------------

def test_non_bash_tool_passes():
    assert run("smtplib.SMTP('x')", tool="Read").returncode == 0


def test_bad_json_passes():
    assert run_raw("not json").returncode == 0


def test_non_dict_json_passes():
    assert run_raw("[1, 2]").returncode == 0


def test_empty_command_passes():
    assert run("").returncode == 0


def test_internal_error_fails_open():
    p = run_raw(json.dumps({"tool_name": "Bash", "tool_input": ["smtplib"]}))
    assert p.returncode == 0
    assert "internal error" in p.stderr


# -- Block: real sending ---------------------------------------------------------------
# The half the guard exists for. If one of these turns green, a relaxation cut a hole:
# roll the relaxation back, do not prop it up.

def test_smtplib_blocked():
    assert blocked("python3 -c \"import smtplib; smtplib.SMTP('203.0.113.5')\"")


def test_smtp_through_heredoc_blocked():
    assert blocked("python3 - <<'EOF'\nimport smtplib\ns = smtplib.SMTP('h')\n"
                   "s.send_message(m)\nEOF")


def test_heredoc_to_interpreter_without_dash_blocked():
    assert blocked("python3 <<'EOF'\nimport smtplib\nsmtplib.SMTP('h').sendmail(a, b, m)\nEOF")


def test_heredoc_piped_to_interpreter_blocked():
    assert blocked("cat <<'EOF' | python3\nimport smtplib\nEOF")


def test_heredoc_file_then_run_blocked():
    assert blocked("cat > /tmp/s.py <<'EOF' && python3 /tmp/s.py\nimport smtplib\n"
                   "smtplib.SMTP('h')\nEOF")


def test_docker_exec_python_smtplib_blocked():
    assert blocked("docker exec app python3 -c \"import smtplib; "
                   "smtplib.SMTP('203.0.113.5').sendmail('a@example.com', ['b@example.com'], m)\"")


def test_curl_post_to_resend_blocked():
    assert blocked('curl -X POST https://api.resend.com/emails -d \'{"to":"b@example.com"}\'')


def test_curl_body_without_method_blocked():
    # No -X, but a body: curl makes a POST by itself.
    assert blocked("curl https://api.resend.com/emails -d @letter.json")


def test_curl_form_blocked():
    assert blocked("curl -F from=a@example.com https://api.mailgun.net/v3/x/messages")


def test_curl_config_file_blocked():
    # -K reads options (including the body) from a file the guard cannot see.
    assert blocked("curl -K send.cfg https://api.postmarkapp.com/email")


def test_curl_sendgrid_send_endpoint_blocked():
    assert blocked("curl https://api.sendgrid.com/v3/mail/send")


def test_curl_brevo_blocked():
    assert blocked("curl -X POST https://api.brevo.com/v3/smtp/email --json @m.json")


def test_curl_sparkpost_blocked():
    assert blocked("curl https://api.sparkpost.com/api/v1/transmissions -d @t.json")


def test_python_requests_post_blocked():
    assert blocked("python3 -c \"import requests; requests.post('https://api.resend.com/emails', "
                   "json={'to': 'b@example.com'})\"")


def test_ses_http_api_blocked():
    assert blocked("curl https://email.us-east-1.amazonaws.com/ -d Action=SendEmail")


def test_aws_ses_send_email_blocked():
    assert blocked("aws ses send-email --from a@example.com "
                   "--destination ToAddresses=b@example.com --message file://m.json")


def test_aws_sesv2_send_email_blocked():
    assert blocked("aws sesv2 send-email --from-email-address a@example.com "
                   "--destination ToAddresses=b@example.com --content file://c.json")


def test_gmail_api_send_blocked():
    assert blocked("curl -X POST https://gmail.googleapis.com/gmail/v1/users/me/messages/send "
                   "-d @raw.json")


def test_graph_send_mail_blocked():
    assert blocked("curl -X POST https://graph.microsoft.com/v1.0/me/sendMail --json @m.json")


def test_curl_smtp_blocked():
    assert blocked("curl --url smtps://203.0.113.5:465 --mail-from a@example.com "
                   "--mail-rcpt b@example.com -T m.txt")


def test_sendmail_binary_blocked():
    assert blocked("/usr/sbin/sendmail -t < letter.txt")


def test_msmtp_blocked():
    assert blocked("msmtp b@example.com < letter.txt")


def test_mutt_blocked():
    assert blocked("mutt -s 'report' b@example.com < body.txt")


def test_mailx_blocked():
    assert blocked("echo hi | mailx -s hi b@example.com")


def test_mail_cli_blocked():
    assert blocked("mail -s x a@example.com < f")


def test_mail_cli_address_only_blocked():
    assert blocked("echo hi | mail b@example.com")


def test_mail_cli_inside_sh_c_blocked():
    assert blocked("ssh 203.0.113.9 'mail -s report b@example.com < /tmp/r.txt'")


def test_git_send_email_blocked():
    assert blocked("git send-email --to=b@example.com 0001.patch")


def test_generic_send_email_call_blocked():
    assert blocked("python3 -c 'from app.mail import send_email; send_email(to, subject, body)'")


def test_django_send_mail_blocked():
    assert blocked("python3 manage.py shell -c \"from django.core.mail import send_mail; "
                   "send_mail('s', 'b', 'a@example.com', ['b@example.com'])\"")


def test_nodemailer_blocked():
    assert blocked("node -e \"t.sendMail({to: 'b@example.com', text: 'hi'})\"")


def test_draft_word_does_not_cover_smtplib():
    assert blocked("python3 -c \"import imaplib, smtplib; smtplib.SMTP('h')\"")


def test_dry_run_in_one_part_does_not_cover_another():
    assert blocked("python3 x.py --dry-run && curl -X POST https://api.resend.com/emails "
                   "-d @l.json")


# -- Block: a send hidden behind a read ------------------------------------------------
# The dangerous spot of the read relaxation: a command that STARTS with a read and ends
# with a send. Every part of the command is checked, not the first word.

def test_search_then_send_blocked():
    assert blocked("grep -l draft *.txt && python3 -c 'import smtplib'")


def test_read_piped_into_interpreter_blocked():
    assert blocked("grep -h smtplib sender.py | python3 -")


def test_grep_results_fed_to_interpreter_via_xargs_blocked():
    assert blocked("grep -l smtplib jobs/*.py | xargs -n1 python3")


def test_ls_then_curl_post_blocked():
    assert blocked("ls letters/ ; curl -X POST https://api.resend.com/emails -d @l.json")


def test_subshell_inside_read_blocked():
    assert blocked("echo $(python3 -c 'import smtplib; smtplib.SMTP(\"h\")')")


# -- The block message -----------------------------------------------------------------

def test_block_message_explains_and_goes_to_stderr():
    p = run("mail -s x a@example.com < f")
    assert p.returncode == 2
    assert p.stdout == ""
    msg = p.stderr
    assert "BLOCKED" in msg
    assert "mail" in msg.lower()
    assert "recalled" in msg
    assert "SEND_APPROVED=1" in msg
    assert "draft" in msg.lower()
    assert "that one message only" in msg
    for word in ("text", "sender", "recipient"):
        assert word in msg.lower()
    assert "mail -s x a@example.com" in msg


def test_block_message_names_what_was_detected():
    p = run("aws ses send-email --from a@example.com")
    assert p.returncode == 2
    assert "SES" in p.stderr


def test_hook_source_and_output_are_ascii_and_neutral():
    src = open(HOOK, encoding="utf-8").read()
    out = run("curl -X POST https://api.resend.com/emails -d @l.json").stderr
    for text in (src, out):
        assert not NON_ASCII.search(text)
        low = text.lower()
        for word in ("immappeal", OWNER_DOMAIN, "/home/ubuntu", "_send_mail", "invite-mail"):
            assert word not in low, word

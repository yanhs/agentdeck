"""hooks/guard_secrets.py -- block a git commit / git push that would publish a secret.

The hook is run the way Claude Code runs it: PreToolUse JSON on stdin, in a subprocess.
Every repository is a throwaway one under tmp_path; push tests use a local bare repository
as "origin". All secret-looking values are made up and assembled at run time, so this file
itself contains nothing a secret scanner (or the guard) would flag.
"""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO, "hooks", "guard_secrets.py")

# Made-up values, built at run time.
TG_TOKEN = "123456789" + ":" + "A" * 35
PRIVATE_KEY = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
KEY_BODY = "b3BlbnNzaC1rZXktdjEAAAAA" + "Q" * 40
AWS_KEY = "AK" + "IA" + "Z" * 16
ANTHROPIC_KEY = "sk-" + "ant-" + "x" * 30
GH_TOKEN = "gh" + "p_" + "b" * 36
GH_PAT = "github" + "_pat_" + "c" * 30
SLACK_TOKEN = "xo" + "xb-" + "1234567890-abcdef"
IP = ".".join(["203", "0", "113", "7"])   # documentation range, never a real host
ELLIPSIS = "\u2026"


# -- helpers -------------------------------------------------------------------------------
def git(cwd, *args):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                       timeout=30)
    assert p.returncode == 0, f"git {args} failed: {p.stderr}"
    return p.stdout


def make_repo(path, origin=None):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "master")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    git(path, "config", "commit.gpgsign", "false")
    (path / "README").write_text("hello\n")
    git(path, "add", "README")
    git(path, "commit", "-q", "-m", "init")
    if origin is not None:
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True,
                       capture_output=True)
        git(path, "remote", "add", "origin", str(origin))
        git(path, "push", "-q", "-u", "origin", "master")
    return path


def commit_file(repo, name, text, msg="change"):
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", msg)


def run_hook(command, cwd, tmp_path, tool="Bash", raw=None, **env_extra):
    env = dict(os.environ)
    env["AGENTKENNEL_PUBLIC_IPS"] = IP
    env["AGENTKENNEL_SECRETS_CACHE"] = str(tmp_path / "cache" / "public-ipv4")
    env["AGENTKENNEL_PUBLIC_IPS_FILE"] = str(tmp_path / "no-known-ips")
    env.update({k: str(v) for k, v in env_extra.items()})
    payload = {"session_id": "s1", "tool_name": tool, "cwd": str(cwd),
               "tool_input": {"command": command}}
    return subprocess.run([sys.executable, HOOK],
                          input=raw if raw is not None else json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=60)


def blocked(p):
    return p.returncode == 2


def load_module():
    spec = importlib.util.spec_from_file_location("guard_secrets", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "work")


@pytest.fixture
def pushed_repo(tmp_path):
    return make_repo(tmp_path / "work", origin=tmp_path / "origin.git")


# -- pass-through --------------------------------------------------------------------------
def test_non_bash_tool_passes(repo, tmp_path):
    p = run_hook("git commit -m x", repo, tmp_path, tool="Edit")
    assert p.returncode == 0 and p.stderr == ""


def test_fast_path_non_git_command(repo, tmp_path):
    (repo / "a.txt").write_text(TG_TOKEN + "\n")
    git(repo, "add", "a.txt")
    for cmd in ("ls -la", "echo commit && echo push", "cat github.txt | grep push"):
        p = run_hook(cmd, repo, tmp_path)
        assert p.returncode == 0 and p.stderr == "", cmd


def test_git_words_inside_an_argument_are_not_an_invocation(repo, tmp_path):
    (repo / "a.txt").write_text(TG_TOKEN + "\n")
    git(repo, "add", "a.txt")
    p = run_hook('grep -rn "git commit" . && echo "git push"', repo, tmp_path)
    assert p.returncode == 0, p.stderr


def test_bad_json_passes(repo, tmp_path):
    p = run_hook("", repo, tmp_path, raw="{not json")
    assert p.returncode == 0


def test_fast_path_helper():
    mod = load_module()
    assert not mod.mentions_commit_or_push("ls -la")
    assert not mod.mentions_commit_or_push("github push")
    assert mod.mentions_commit_or_push("cd x && git commit -m y")
    assert mod.mentions_commit_or_push("git -C d push")


# -- commit --------------------------------------------------------------------------------
def test_clean_commit_passes(repo, tmp_path):
    (repo / "app.py").write_text("print('hello')\n")
    git(repo, "add", "app.py")
    p = run_hook("git commit -m 'add app'", repo, tmp_path)
    assert p.returncode == 0, p.stderr


def test_staged_token_blocked_with_location_and_mask(repo, tmp_path):
    (repo / "app.py").write_text(f"a = 1\nb = 2\nTOKEN = '{TG_TOKEN}'\n")
    git(repo, "add", "app.py")
    p = run_hook("git commit -m 'add app'", repo, tmp_path)
    assert blocked(p), p.stderr
    assert "BLOCKED" in p.stderr and "guard_secrets" in p.stderr
    assert "app.py:3" in p.stderr
    assert TG_TOKEN[:4] + ELLIPSIS + TG_TOKEN[-2:] in p.stderr
    assert TG_TOKEN not in p.stderr + p.stdout
    assert "Telegram" in p.stderr
    assert "git restore --staged" in p.stderr
    assert "SECRETS_OK=1" in p.stderr
    assert "rotate" in p.stderr


def test_unstaged_change_not_counted_without_all_flag(repo, tmp_path):
    (repo / "README").write_text(f"hello\n{AWS_KEY}\n")    # tracked, modified, not staged
    (repo / "app.py").write_text("x = 1\n")
    git(repo, "add", "app.py")
    p = run_hook("git commit -m x", repo, tmp_path)
    assert p.returncode == 0, p.stderr


def test_commit_all_flag_scans_tracked_unstaged(repo, tmp_path):
    (repo / "README").write_text(f"hello\nkey = {AWS_KEY}\n")
    for cmd in ("git commit -am 'update'", "git commit --all -m update",
                "git commit -a -m update"):
        p = run_hook(cmd, repo, tmp_path)
        assert blocked(p), cmd
        assert "README:2" in p.stderr
        assert AWS_KEY not in p.stderr


def test_commit_with_pathspec_scans_those_files(repo, tmp_path):
    (repo / "README").write_text(f"hello\n{AWS_KEY}\n")
    p = run_hook("git commit -m x README", repo, tmp_path)
    assert blocked(p), p.stderr


def test_add_then_commit_in_one_command_scans_untracked_file(repo, tmp_path):
    (repo / "id_test").write_text(PRIVATE_KEY + "\n" + KEY_BODY + "\n")
    p = run_hook("git add id_test && git commit -m 'add key'", repo, tmp_path)
    assert blocked(p), p.stderr
    assert "id_test:1" in p.stderr
    assert "private key" in p.stderr.lower()
    assert KEY_BODY not in p.stderr


def test_add_then_commit_scans_tracked_modified_file(repo, tmp_path):
    (repo / "README").write_text(f"hello\n{GH_TOKEN}\n")
    p = run_hook("git add README; git commit -m x", repo, tmp_path)
    assert blocked(p), p.stderr
    assert "README:2" in p.stderr


@pytest.mark.parametrize("add", ["git add -A", "git add .", "git add --all"])
def test_add_all_then_commit_scans_everything(repo, tmp_path, add):
    (repo / "sub").mkdir()
    (repo / "sub" / "conf.env").write_text(f"KEY={ANTHROPIC_KEY}\n")
    p = run_hook(f"{add} && git commit -m x", repo, tmp_path)
    assert blocked(p), p.stderr
    assert "sub/conf.env:1" in p.stderr


def test_add_all_respects_gitignore(repo, tmp_path):
    (repo / ".gitignore").write_text("secret.env\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore")
    (repo / "secret.env").write_text(f"KEY={ANTHROPIC_KEY}\n")
    p = run_hook("git add -A && git commit -m x", repo, tmp_path)
    assert p.returncode == 0, p.stderr


def test_binary_untracked_file_skipped(repo, tmp_path):
    (repo / "blob.bin").write_bytes(b"\x00\x01" + TG_TOKEN.encode() + b"\x00")
    p = run_hook("git add blob.bin && git commit -m x", repo, tmp_path)
    assert p.returncode == 0, p.stderr


def test_removed_lines_not_flagged(repo, tmp_path):
    commit_file(repo, "app.py", f"x = 1\nTOKEN = '{TG_TOKEN}'\n")
    (repo / "app.py").write_text("x = 1\nTOKEN = 'placeholder'\n")
    git(repo, "add", "app.py")
    p = run_hook("git commit -m 'drop token'", repo, tmp_path)
    assert p.returncode == 0, p.stderr


def test_first_commit_in_empty_repo(tmp_path):
    r = tmp_path / "fresh"
    r.mkdir()
    git(r, "init", "-q", "-b", "master")
    git(r, "config", "user.email", "test@example.com")
    (r / "a.py").write_text(f"T = '{TG_TOKEN}'\n")
    git(r, "add", "a.py")
    assert blocked(run_hook("git commit -m first", r, tmp_path))
    assert blocked(run_hook("git commit -am first", r, tmp_path))


def test_every_kind_detected_and_never_printed_in_full(repo, tmp_path):
    values = [TG_TOKEN, AWS_KEY, ANTHROPIC_KEY, GH_TOKEN, GH_PAT, SLACK_TOKEN, IP]
    lines = [f"v{i} = '{v}'" for i, v in enumerate(values)]
    lines += [PRIVATE_KEY, KEY_BODY]
    (repo / "all.txt").write_text("\n".join(lines) + "\n")
    git(repo, "add", "all.txt")
    p = run_hook("git commit -m all", repo, tmp_path)
    assert blocked(p)
    out = p.stdout + p.stderr
    for v in values + [KEY_BODY]:
        assert v not in out, f"full value printed: {v[:6]}"
    for n in range(1, len(values) + 2):
        assert f"all.txt:{n}" in out, n
    assert "203.0.x.x" in out


def test_ip_matched_as_whole_token_only(repo, tmp_path):
    (repo / "a.txt").write_text(f"v = 1{IP}\nw = {IP}5\nver = {IP}.1\n")
    git(repo, "add", "a.txt")
    p = run_hook("git commit -m x", repo, tmp_path)
    assert p.returncode == 0, p.stderr


def test_no_public_ip_known_skips_ip_check(repo, tmp_path):
    (repo / "a.txt").write_text(f"host = {IP}\n")
    git(repo, "add", "a.txt")
    p = run_hook("git commit -m x", repo, tmp_path, AGENTKENNEL_PUBLIC_IPS="")
    assert p.returncode == 0, p.stderr


# -- override and failure modes ------------------------------------------------------------
def test_secrets_ok_override(repo, tmp_path):
    (repo / "a.txt").write_text(f"{TG_TOKEN}\n")
    git(repo, "add", "a.txt")
    p = run_hook("SECRETS_OK=1 git commit -m x", repo, tmp_path)
    assert p.returncode == 0, p.stderr


def test_not_a_repo_passes(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "a.txt").write_text(f"{TG_TOKEN}\n")
    p = run_hook("git add a.txt && git commit -m x && git push", d, tmp_path)
    assert p.returncode == 0, p.stderr


def test_git_missing_passes(repo, tmp_path):
    (repo / "a.txt").write_text(f"{TG_TOKEN}\n")
    git(repo, "add", "a.txt")
    p = run_hook("git commit -m x", repo, tmp_path, PATH=str(tmp_path / "nothing"))
    assert p.returncode == 0, p.stderr


# -- locating the repository ---------------------------------------------------------------
def test_git_dash_C_resolves_dir(repo, tmp_path):
    (repo / "a.txt").write_text(f"{TG_TOKEN}\n")
    git(repo, "add", "a.txt")
    p = run_hook("git -C work commit -m x", tmp_path, tmp_path)
    assert blocked(p), p.stderr
    p = run_hook(f"git -C {repo} -c core.pager=cat commit -m x", "/", tmp_path)
    assert blocked(p), p.stderr


def test_cd_before_commit_resolves_dir(repo, tmp_path):
    (repo / "a.txt").write_text(f"{TG_TOKEN}\n")
    git(repo, "add", "a.txt")
    for cmd in ("cd work && git commit -m x", "cd /nonexistent; cd work\ngit commit -m x",
                "echo hi || cd work && git -c user.name=x commit -m 'a; b'"):
        p = run_hook(cmd, tmp_path, tmp_path)
        assert blocked(p), cmd


def test_cd_elsewhere_overrides_cwd(repo, tmp_path):
    (repo / "a.txt").write_text(f"{TG_TOKEN}\n")
    git(repo, "add", "a.txt")
    other = tmp_path / "plain"
    other.mkdir()
    p = run_hook(f"cd {other} && git commit -m x", repo, tmp_path)
    assert p.returncode == 0, p.stderr


# -- push ----------------------------------------------------------------------------------
def test_push_of_unpushed_commit_with_ip_blocked(pushed_repo, tmp_path):
    commit_file(pushed_repo, "deploy.sh", f"#!/bin/sh\nssh root@{IP}\n")
    p = run_hook("git push", pushed_repo, tmp_path)
    assert blocked(p), p.stderr
    assert "deploy.sh:2" in p.stderr
    assert "203.0.x.x" in p.stderr
    assert IP not in p.stderr + p.stdout
    assert "git reset --soft origin/master" in p.stderr
    assert "push" in p.stderr


def test_push_when_secret_commit_already_on_remote_passes(pushed_repo, tmp_path):
    commit_file(pushed_repo, "deploy.sh", f"ssh root@{IP}\n")
    git(pushed_repo, "push", "-q", "origin", "master")
    commit_file(pushed_repo, "notes.txt", "clean\n")
    for cmd in ("git push", "git push origin master", "git push -u origin HEAD"):
        p = run_hook(cmd, pushed_repo, tmp_path)
        assert p.returncode == 0, (cmd, p.stderr)


def test_push_named_branch_refspec(pushed_repo, tmp_path):
    git(pushed_repo, "checkout", "-q", "-b", "feature")
    commit_file(pushed_repo, "a.txt", f"{SLACK_TOKEN}\n")
    git(pushed_repo, "checkout", "-q", "master")
    for cmd in ("git push origin feature", "git push -u origin feature:refs/heads/x",
                "git push origin +feature", "git push --all origin"):
        p = run_hook(cmd, pushed_repo, tmp_path)
        assert blocked(p), cmd
    p = run_hook("git push origin master", pushed_repo, tmp_path)
    assert p.returncode == 0, p.stderr
    p = run_hook("git push origin :feature", pushed_repo, tmp_path)   # delete: nothing new
    assert p.returncode == 0, p.stderr


def test_push_tags(pushed_repo, tmp_path):
    git(pushed_repo, "checkout", "-q", "-b", "side")
    commit_file(pushed_repo, "a.txt", f"{GH_TOKEN}\n")
    git(pushed_repo, "tag", "-a", "v1.0", "-m", "release")
    git(pushed_repo, "checkout", "-q", "master")
    git(pushed_repo, "branch", "-q", "-D", "side")
    assert blocked(run_hook("git push origin v1.0", pushed_repo, tmp_path))
    assert blocked(run_hook("git push --tags", pushed_repo, tmp_path))
    assert blocked(run_hook("git push origin --tags", pushed_repo, tmp_path))


def test_push_to_empty_remote_scans_history(tmp_path):
    r = make_repo(tmp_path / "work")
    commit_file(r, "a.txt", f"{TG_TOKEN}\n")
    subprocess.run(["git", "init", "-q", "--bare", str(tmp_path / "o.git")], check=True)
    git(r, "remote", "add", "origin", str(tmp_path / "o.git"))
    p = run_hook("git push -u origin master", r, tmp_path)
    assert blocked(p), p.stderr


def test_commit_and_push_in_one_command(pushed_repo, tmp_path):
    (pushed_repo / "a.txt").write_text(f"{TG_TOKEN}\n")
    p = run_hook("git add a.txt && git commit -m x && git push", pushed_repo, tmp_path)
    assert blocked(p), p.stderr


# -- public IP discovery (unit) ------------------------------------------------------------
def test_keep_public_filters_non_global():
    mod = load_module()
    addrs = ["10.0.0.5", "172.16.3.4", "192.168.1.10", "127.0.0.1", "169.254.1.1",
             "100.64.0.1", "100.127.255.254", "192.0.2.1", "198.51.100.2", IP,
             "8.8.8.8", "1.2.3.4", "not-an-ip", "8.8.8.8"]
    assert mod.keep_public(addrs) == ["8.8.8.8", "1.2.3.4"]


def test_parse_ip_output():
    mod = load_module()
    text = ("2: eth0    inet 8.8.4.4/24 brd 8.8.4.255 scope global eth0\\    valid_lft\n"
            "3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\n")
    assert mod.parse_ip_output(text) == ["8.8.4.4", "172.17.0.1"]
    assert mod.parse_ip_output("10.0.0.2 8.8.4.4 fe80::1\n") == ["10.0.0.2", "8.8.4.4"]


def test_discovered_ips_cached_on_second_call(tmp_path):
    mod = load_module()
    cache = tmp_path / "c" / "public-ipv4"
    calls = []

    def probe():
        calls.append(1)
        return ["192.168.0.2", "8.8.4.4"]

    assert mod.discover_global_ipv4(str(cache), probe=probe) == {"8.8.4.4"}
    assert mod.discover_global_ipv4(str(cache), probe=probe) == {"8.8.4.4"}
    assert len(calls) == 1
    assert cache.read_text().split() == ["8.8.4.4"]
    old = cache.stat().st_mtime - 2 * 86400
    os.utime(cache, (old, old))
    mod.discover_global_ipv4(str(cache), probe=probe)
    assert len(calls) == 2


def test_public_ips_is_union_of_env_file_and_discovery(tmp_path, monkeypatch):
    mod = load_module()
    ip_file = tmp_path / "known-ips"
    ip_file.write_text("# host address, written by the host\n198.51.100.9  # vps\n\n"
                       "junk-line\n")
    monkeypatch.setenv("AGENTKENNEL_PUBLIC_IPS", f"{IP}, 8.8.8.8")
    monkeypatch.setenv("AGENTKENNEL_PUBLIC_IPS_FILE", str(ip_file))
    monkeypatch.setenv("AGENTKENNEL_SECRETS_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(mod, "probe_interfaces", lambda: ["10.1.1.1", "8.8.4.4"])
    assert mod.public_ips() == {IP, "8.8.8.8", "198.51.100.9", "8.8.4.4"}


def test_public_ips_missing_file_is_fine(tmp_path, monkeypatch):
    mod = load_module()
    monkeypatch.delenv("AGENTKENNEL_PUBLIC_IPS", raising=False)
    monkeypatch.setenv("AGENTKENNEL_PUBLIC_IPS_FILE", str(tmp_path / "no-such-file"))
    monkeypatch.setenv("AGENTKENNEL_SECRETS_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(mod, "probe_interfaces", lambda: ["10.1.1.1"])
    assert mod.public_ips() == set()


def test_ip_from_known_ips_file_blocks_commit(repo, tmp_path):
    ip_file = tmp_path / "known-ips"
    ip_file.write_text(f"# this server\n{IP}\n")
    (repo / "a.txt").write_text(f"host = {IP}\n")
    git(repo, "add", "a.txt")
    p = run_hook("git commit -m x", repo, tmp_path, AGENTKENNEL_PUBLIC_IPS="",
                 AGENTKENNEL_PUBLIC_IPS_FILE=ip_file)
    assert blocked(p), p.stderr
    assert "public IP" in p.stderr and "203.0.x.x" in p.stderr


def test_mask_helpers():
    mod = load_module()
    assert mod.mask_ip(IP) == "203.0.x.x"
    assert mod.mask(TG_TOKEN) == "1234" + ELLIPSIS + "AA"


def test_hook_file_is_ascii():
    with open(HOOK, "rb") as f:
        data = f.read()
    data.decode("ascii")
    with open(__file__, "rb") as f:
        f.read().decode("ascii")

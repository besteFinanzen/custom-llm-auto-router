"""The optional git hook must stay stdlib-only and degrade gracefully.

These tests drive a real temporary git repository and a threaded stdlib HTTP
server, so they exercise the same code path as ``post-commit`` in a repo:
config parsing, payload building, queueing during outages and flushing.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "integrations" / "git" / "worktracker_hook.py"


def _load_hook():
    spec = importlib.util.spec_from_file_location("worktracker_hook", HOOK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["worktracker_hook"] = module
    spec.loader.exec_module(module)
    return module


hook = _load_hook()


def git(*args: str, cwd: pathlib.Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    git("config", "user.name", "Ada Lovelace", cwd=root)
    git("config", "user.email", "ada@example.com", cwd=root)
    git("remote", "add", "origin", "https://github.com/example/demo.git", cwd=root)
    return root


def commit(repo: pathlib.Path, name: str = "a.txt", content: str = "hello\n") -> str:
    (repo / name).write_text(content)
    git("add", name, cwd=repo)
    git("commit", "-q", "-m", f"edit {name}", cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


class Collector(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _handle(self):
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        self.server.calls.append({  # type: ignore[attr-defined]
            "method": self.command, "path": self.path, "body": body,
            "auth": self.headers.get("authorization")})
        status = self.server.responses.pop(0) if self.server.responses else 200  # type: ignore[attr-defined]
        data = json.dumps({"ok": status == 200, "scope": "hook"}).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _handle
    do_POST = _handle


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    httpd.calls = []  # type: ignore[attr-defined]
    httpd.responses = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    thread.join()
    httpd.server_close()


def write_env(repo: pathlib.Path, url: str, token: str = "hook-token",
              device: str = "laptop") -> pathlib.Path:
    path = hook.git_dir(str(repo)) / "worktracker.env"
    path.write_text(f"WORKTRACKER_URL={url}\nWORKTRACKER_TOKEN={token}\n"
                    f"WORKTRACKER_DEVICE={device}\n")
    return path


@pytest.fixture(autouse=True)
def isolated_git_env(monkeypatch, tmp_path):
    """Never read the developer's real env: WORKTRACKER_* would override the
    per-test ``.git/worktracker.env``."""
    for name in ("WORKTRACKER_URL", "WORKTRACKER_TOKEN", "WORKTRACKER_DEVICE",
                 "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
        monkeypatch.delenv(name, raising=False)
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")


def test_api_url_join_and_config_parsing(repo, tmp_path, monkeypatch):
    assert hook.api_url("http://h:1/", "/commits") == "http://h:1/worktracker/api/commits"
    env = write_env(repo, "http://h:1/")
    values = hook.parse_env(env)
    assert values["WORKTRACKER_TOKEN"] == "hook-token"
    monkeypatch.delenv("WORKTRACKER_TOKEN", raising=False)
    config = hook.tracker_config(str(repo))
    assert config == {"url": "http://h:1", "token": "hook-token", "device": "laptop"}
    monkeypatch.setenv("WORKTRACKER_TOKEN", "from-env")
    assert hook.tracker_config(str(repo))["token"] == "from-env"
    monkeypatch.delenv("WORKTRACKER_TOKEN")
    assert hook.tracker_config(str(tmp_path))["url"] == ""


def test_commit_payload_captures_metadata(repo):
    sha = commit(repo, "src.py", "print(1)\n")
    payload = hook.commit_payload(sha, str(repo))
    assert payload["sha"] == sha
    assert payload["branch"] == "main"
    assert payload["author_email"] == "ada@example.com"
    assert payload["subject"] == "edit src.py"
    assert payload["files"] == [{"path": "src.py", "additions": 1, "deletions": 0,
                                 "binary": False}]
    assert payload["additions"] == 1 and payload["deletions"] == 0
    assert payload["project"] == "github.com/example/demo"
    assert payload["source"] == "hook"
    assert payload["repo_root"] == str(repo)


def test_binary_files_and_path_key_without_remote(repo):
    git("remote", "remove", "origin", cwd=repo)
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02")
    git("add", "blob.bin", cwd=repo)
    git("commit", "-q", "-m", "binary", cwd=repo)
    payload = hook.commit_payload("HEAD", str(repo))
    assert payload["files"][0]["binary"] is True
    assert payload["files"][0]["additions"] == 0
    assert payload["project"] == f"path:{repo}"
    assert payload["repo_remote"] is None


def test_post_commit_delivers_and_uses_token(repo, server):
    write_env(repo, f"http://127.0.0.1:{server.server_port}")
    sha = commit(repo)
    assert hook.post_commit(str(repo)) == 0
    assert len(server.calls) == 1  # type: ignore[attr-defined]
    call = server.calls[0]  # type: ignore[attr-defined]
    assert call["path"] == "/worktracker/api/commits"
    assert call["auth"] == "Bearer hook-token"
    assert call["body"]["sha"] == sha
    assert not hook.queue_file(str(repo)).exists()


def test_outage_queues_then_flush_delivers(repo, server):
    write_env(repo, f"http://127.0.0.1:{server.server_port}")
    server.responses = [500]  # type: ignore[attr-defined]
    sha = commit(repo)
    hook.post_commit(str(repo))
    queue = hook.queue_file(str(repo))
    assert queue.exists() and len(queue.read_text().splitlines()) == 1

    # Second commit also fails; both stay queued for the next flush.
    server.responses = [503, 503]  # type: ignore[attr-defined]
    newer = commit(repo, "b.txt")
    hook.post_commit(str(repo))
    assert len(queue.read_text().splitlines()) == 2

    server.responses = []  # type: ignore[attr-defined]  # the tracker recovered
    server.calls.clear()  # type: ignore[attr-defined]
    assert hook.flush(str(repo)) == 2
    paths = [c["body"]["sha"] for c in server.calls]  # type: ignore[attr-defined]
    assert newer in paths
    assert sha in paths
    assert not queue.exists()


def test_unreachable_tracker_is_not_an_error(repo):
    write_env(repo, "http://127.0.0.1:9")  # discard port: connection refused
    commit(repo)
    assert hook.post_commit(str(repo)) == 0
    assert hook.queue_file(str(repo)).exists()
    assert hook.flush(str(repo)) == 0


def test_flush_skips_garbage_lines_and_without_url(repo):
    write_env(repo, "http://127.0.0.1:4141")
    queue = hook.queue_file(str(repo))
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text("not json\n")
    assert hook.flush(str(repo)) == 0
    assert not hook.queue_file(str(repo)).exists()


def test_install_writes_env_and_chain_executable(repo):
    target = hook.install(str(repo), url="http://tracker:4141/", token="abc",
                          device="desk", prompt=False)
    assert target.exists() and target.name == "post-commit"
    assert os.access(target, os.X_OK)
    text = target.read_text()
    assert "worktracker_hook.py" in text and "post-commit" in text
    env = hook.parse_env(hook.git_dir(str(repo)) / "worktracker.env")
    assert env["WORKTRACKER_URL"] == "http://tracker:4141"
    assert env["WORKTRACKER_TOKEN"] == "abc"
    assert env["WORKTRACKER_DEVICE"] == "desk"
    # Re-install must not chain itself twice.
    hook.install(str(repo), url="http://tracker:4141/", token="abc", prompt=False)
    assert target.read_text().count("worktracker_hook.py") == 1


def test_install_preserves_existing_post_commit(repo):
    hooks = hook.git_dir(str(repo)) / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    existing = hooks / "post-commit"
    existing.write_text("#!/bin/sh\necho existing\n")
    existing.chmod(0o755)
    hook.install(str(repo), url="http://tracker:4141", token="t", prompt=False)
    text = existing.read_text()
    assert "echo existing" in text
    assert text.index("echo existing") < text.index("worktracker_hook.py")


def test_installer_script_runs_without_tty_when_credentials_supplied(repo):
    """CI/containers have neither a tty on stdin nor /dev/tty.

    Supplying WORKTRACKER_URL/WORKTRACKER_TOKEN must skip the prompt path;
    an unreachable tracker only downgrades credential validation to a warning.
    """
    script = ROOT / "integrations" / "git" / "install-hook.sh"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(repo),
           "WORKTRACKER_URL": "http://127.0.0.1:9", "WORKTRACKER_TOKEN": "abc"}
    result = subprocess.run(["sh", str(script)], cwd=repo, env=env,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr
    gd = hook.git_dir(str(repo))
    assert hook.parse_env(gd / "worktracker.env")["WORKTRACKER_TOKEN"] == "abc"
    assert (gd / "hooks" / "post-commit.d" / "post_commit.py").exists()


def test_install_falls_back_to_env_and_prompt(repo, monkeypatch):
    monkeypatch.setenv("WORKTRACKER_URL", "http://env-host/")
    monkeypatch.setenv("WORKTRACKER_TOKEN", "env-token")
    hook.install(str(repo), prompt=False)
    env = hook.parse_env(hook.git_dir(str(repo)) / "worktracker.env")
    assert env["WORKTRACKER_URL"] == "http://env-host"
    assert env["WORKTRACKER_TOKEN"] == "env-token"


def test_prompt_without_tty_exits(repo, monkeypatch):
    monkeypatch.delenv("WORKTRACKER_URL", raising=False)
    monkeypatch.delenv("WORKTRACKER_TOKEN", raising=False)
    monkeypatch.setattr(hook, "open", lambda *a, **k: (_ for _ in ()).throw(OSError()),
                        raising=False)
    with pytest.raises(SystemExit, match="no terminal"):
        hook.install(str(repo), prompt=True)


def test_main_commands(repo, server, capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(repo)
    assert hook.main(["hook", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["url"] == ""
    assert hook.main(["hook", "version"]) == 0
    assert "worktracker hook" in capsys.readouterr().out
    assert hook.main(["hook", "bogus-command"]) == 2  # prints usage
    url = f"http://127.0.0.1:{server.server_port}"
    assert hook.main(["hook", "install", "--url", url, "--token", "tok",
                      "--no-prompt"]) == 0
    out = capsys.readouterr().out
    assert "hook installed" in out and "authenticated as hook" in out

    sha = commit(repo)
    assert hook.main(["hook", "post-commit"]) == 0
    assert server.calls[-1]["body"]["sha"] == sha  # type: ignore[attr-defined]

    assert hook.main(["hook", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status == {"url": url, "token": "set", "queue": 0}

    # A rejected token is reported but installation still succeeds locally.
    server.responses = [401]  # type: ignore[attr-defined]
    assert hook.main(["hook", "install", "--url", url, "--token", "bad",
                      "--no-prompt"]) == 0
    assert "credential check failed" in capsys.readouterr().out

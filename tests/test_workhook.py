"""The optional git hook must stay stdlib-only and degrade gracefully.

These tests drive a real temporary git repository and a threaded stdlib HTTP
server, so they exercise the same code path as ``post-commit`` in a repo:
config parsing, payload building, queueing during outages and flushing.
"""
from __future__ import annotations

import importlib.util
import io
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


class FakeTerminal:
    """Enough of /dev/tty for the wizard: queued answers and captured output."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.written: list[str] = []
        self.closed = False

    def write(self, text: str) -> None:
        self.written.append(text)

    def flush(self) -> None:
        pass

    def readline(self) -> str:
        return self.answers.pop(0) + "\n" if self.answers else ""

    def close(self) -> None:
        self.closed = True

    @property
    def output(self) -> str:
        return "".join(self.written)


def attach_terminal(monkeypatch, answers: list[str],
                    secrets: list[str] | None = None) -> FakeTerminal:
    """Patch the wizard's terminal and its hidden-input prompt."""
    terminal = FakeTerminal(answers)
    pending = list(secrets or [])

    def read_secret(_label: str, _stream) -> str:
        assert pending, "the wizard asked for more secrets than the test supplied"
        return pending.pop(0)

    monkeypatch.setattr(hook, "open", lambda *a, **k: terminal, raising=False)
    monkeypatch.setattr(hook, "_read_secret", read_secret)
    return terminal


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


def test_installer_script_accepts_install_word(repo):
    """`sh install-hook.sh install` is a natural way to invoke it; accept it."""
    script = ROOT / "integrations" / "git" / "install-hook.sh"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(repo),
           "WORKTRACKER_URL": "http://127.0.0.1:9", "WORKTRACKER_TOKEN": "abc"}
    result = subprocess.run(["sh", str(script), "install"], cwd=repo, env=env,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr
    gd = hook.git_dir(str(repo))
    assert (gd / "hooks" / "post-commit.d" / "post_commit.py").exists()


def test_install_falls_back_to_env_and_prompt(repo, monkeypatch):
    monkeypatch.setenv("WORKTRACKER_URL", "http://env-host/")
    monkeypatch.setenv("WORKTRACKER_TOKEN", "env-token")
    hook.install(str(repo), prompt=False)
    env = hook.parse_env(hook.git_dir(str(repo)) / "worktracker.env")
    assert env["WORKTRACKER_URL"] == "http://env-host"
    assert env["WORKTRACKER_TOKEN"] == "env-token"


def deny_terminal(monkeypatch) -> None:
    """Simulate a shell with neither /dev/tty nor a terminal on stdin."""
    class NoStdin:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(hook, "open", lambda *a, **k: (_ for _ in ()).throw(OSError()),
                        raising=False)
    monkeypatch.setattr(hook.sys, "stdin", NoStdin())


def test_prompt_without_tty_exits(repo, monkeypatch):
    monkeypatch.delenv("WORKTRACKER_URL", raising=False)
    monkeypatch.delenv("WORKTRACKER_TOKEN", raising=False)
    deny_terminal(monkeypatch)
    with pytest.raises(SystemExit, match="no terminal"):
        hook.install(str(repo), prompt=True)


def test_install_without_tty_reuses_saved_credentials(repo, monkeypatch):
    write_env(repo, "http://tracker:4141", token="saved-token", device="desk")
    deny_terminal(monkeypatch)

    target = hook.install(str(repo), prompt=True)

    assert target.exists()
    env = hook.parse_env(hook.git_dir(str(repo)) / "worktracker.env")
    assert env["WORKTRACKER_TOKEN"] == "saved-token"
    assert env["WORKTRACKER_DEVICE"] == "desk"


def test_interactive_without_tty_still_exits(repo, monkeypatch):
    write_env(repo, "http://tracker:4141", token="saved-token")
    deny_terminal(monkeypatch)
    with pytest.raises(SystemExit, match="no terminal"):
        hook.install(str(repo), prompt=True, interactive=True)


def test_console_falls_back_to_stdin_when_dev_tty_is_missing(monkeypatch):
    """IDEs and containers can hand us a terminal on stdin without /dev/tty."""
    class TerminalStdin(io.StringIO):
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return 0

    out = io.StringIO()
    monkeypatch.setattr(hook, "open", lambda *a, **k: (_ for _ in ()).throw(OSError()),
                        raising=False)
    monkeypatch.setattr(hook.sys, "stdin", TerminalStdin("mac\n"))
    monkeypatch.setattr(hook.sys, "stdout", out)

    with hook._open_console() as console:
        assert console.ask("device") == "mac"

    assert out.getvalue() == "device: "
    assert not out.closed


def test_normalize_url_accepts_hosts_and_strips_api_paths():
    assert hook._normalize_url("ai.example.com/") == "https://ai.example.com"
    assert hook._normalize_url("https://ai.example.com/worktracker/api/") == \
        "https://ai.example.com"
    assert hook._normalize_url("https://ai.example.com/tracker/worktracker") == \
        "https://ai.example.com/tracker"
    assert hook._normalize_url("http://ai.example.com:4141/ai") == \
        "http://ai.example.com:4141/ai"
    for bad in ("", "ftp://tracker", "https://", "https://x/?q=1", "not a url"):
        with pytest.raises(ValueError):
            hook._normalize_url(bad)


def test_wizard_prefills_saved_values_and_confirms(repo, server, monkeypatch):
    url = f"http://127.0.0.1:{server.server_port}"
    write_env(repo, url, token="saved-token", device="desk")
    terminal = attach_terminal(monkeypatch, answers=["", "", "yes"], secrets=[""])

    target = hook.install(str(repo), prompt=True)

    assert "Worktracker git hook setup" in terminal.output
    assert "Install summary" in terminal.output
    assert "saved-token" not in terminal.output  # the token stays masked
    assert target.exists()
    env = hook.parse_env(hook.git_dir(str(repo)) / "worktracker.env")
    assert env == {"WORKTRACKER_URL": url, "WORKTRACKER_TOKEN": "saved-token",
                   "WORKTRACKER_DEVICE": "desk"}


def test_wizard_reprompts_invalid_url(repo, server, monkeypatch):
    url = f"http://127.0.0.1:{server.server_port}"
    terminal = attach_terminal(monkeypatch, answers=["ftp://tracker", url, "", "y"],
                               secrets=["tok"])

    hook.install(str(repo), prompt=True)

    assert "start with http:// or https://" in terminal.output
    assert hook.parse_env(hook.git_dir(str(repo)) / "worktracker.env")[
        "WORKTRACKER_URL"] == url
    assert server.calls[0]["auth"] == "Bearer tok"  # type: ignore[attr-defined]


def test_wizard_retries_rejected_token(repo, server, monkeypatch):
    url = f"http://127.0.0.1:{server.server_port}"
    server.responses = [401, 200]  # type: ignore[attr-defined]
    terminal = attach_terminal(monkeypatch, answers=[url, "", "y"],
                               secrets=["bad", "good"])

    hook.install(str(repo), prompt=True)

    assert "token rejected (HTTP 401)" in terminal.output
    env = hook.parse_env(hook.git_dir(str(repo)) / "worktracker.env")
    assert env["WORKTRACKER_TOKEN"] == "good"
    whoami = [c for c in server.calls if c["path"].endswith("/whoami")]  # type: ignore[attr-defined]
    assert [c["auth"] for c in whoami] == ["Bearer bad", "Bearer good"]


def test_wizard_aborts_after_three_rejections(repo, server, monkeypatch):
    url = f"http://127.0.0.1:{server.server_port}"
    server.responses = [401, 401, 401]  # type: ignore[attr-defined]
    attach_terminal(monkeypatch, answers=[url], secrets=["a", "b", "c"])

    with pytest.raises(SystemExit, match="credential rejected"):
        hook.install(str(repo), prompt=True)

    gd = hook.git_dir(str(repo))
    assert not (gd / "worktracker.env").exists()
    assert not (gd / "hooks" / "post-commit").exists()


def test_wizard_can_continue_when_tracker_is_unreachable(repo, monkeypatch):
    attach_terminal(monkeypatch, answers=["http://127.0.0.1:9", "n"], secrets=["tok"])
    with pytest.raises(SystemExit, match="tracker unreachable"):
        hook.install(str(repo), prompt=True)
    assert not (hook.git_dir(str(repo)) / "worktracker.env").exists()

    attach_terminal(monkeypatch, answers=["http://127.0.0.1:9", "y", "", "y"],
                    secrets=["tok"])
    hook.install(str(repo), prompt=True)
    assert hook.parse_env(hook.git_dir(str(repo)) / "worktracker.env")[
        "WORKTRACKER_DEVICE"] == hook.default_device()


def test_wizard_declining_confirmation_writes_nothing(repo, server, monkeypatch):
    url = f"http://127.0.0.1:{server.server_port}"
    hooks = hook.git_dir(str(repo)) / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    existing = hooks / "post-commit"
    existing.write_text("#!/bin/sh\necho keep\n")
    attach_terminal(monkeypatch, answers=[url, "", "n"], secrets=[""])

    with pytest.raises(SystemExit, match="nothing installed"):
        hook.install(str(repo), prompt=True)

    assert existing.read_text() == "#!/bin/sh\necho keep\n"
    assert not (hook.git_dir(str(repo)) / "worktracker.env").exists()
    assert not pathlib.Path(f"{existing}.d").exists()


def test_interactive_flag_reviews_saved_config(repo, server, monkeypatch):
    url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("WORKTRACKER_URL", url)
    monkeypatch.setenv("WORKTRACKER_TOKEN", "env-token")
    terminal = attach_terminal(monkeypatch, answers=["", "", "", "y"], secrets=[""])

    hook.install(str(repo), prompt=True, interactive=True)

    assert "Install summary" in terminal.output
    assert hook.parse_env(hook.git_dir(str(repo)) / "worktracker.env")[
        "WORKTRACKER_TOKEN"] == "env-token"


def test_main_install_no_prompt_requires_credentials(repo, capsys, monkeypatch):
    monkeypatch.chdir(repo)
    assert hook.main(["hook", "install", "--no-prompt"]) == 2
    assert "missing credentials" in capsys.readouterr().err


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

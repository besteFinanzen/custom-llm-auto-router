# Git hook (optional)

The OpenCode plugin already reports commits it observes. The hook covers the
rest: commits made from a terminal, another IDE, a script or a CI job.

```bash
curl -fsSL https://raw.githubusercontent.com/besteFinanzen/custom-llm-auto-router/feat/worktracker/integrations/git/install-hook.sh | sh
```

The installer asks on `/dev/tty` (so it works through `curl | sh`) for:

1. the tracker URL, e.g. `https://ai.example.com`
2. a hook token — create one with `TRACKER_HOOK_TOKEN` in the router's `.env`

It validates the credential against `POST /worktracker/api/whoami`, then writes:

- `.git/worktracker.env` (mode `0600`) with the URL and token — never committed
- `.git/hooks/post-commit` — a marker-delimited block appended to any existing hook
- `.git/hooks/post-commit.d/post_commit.py` — the stdlib-only reporter

Non-interactive install:

```bash
WORKTRACKER_URL=https://ai.example.com WORKTRACKER_TOKEN=wt_hook_... \
  sh integrations/git/install-hook.sh
```

## Reliability

A commit is never blocked: the reporter posts `{sha, parents, branch, files,
additions, deletions, subject, committed_at, repo_remote}` to
`/worktracker/api/commits` with an 8 s timeout. Failures are appended to
`.git/worktracker-queue.jsonl` and flushed on the next commit.

```bash
python3 .git/hooks/post-commit.d/post_commit.py status   # url/token/queue
python3 .git/hooks/post-commit.d/post_commit.py flush    # retry now
```

Remove the marker block from `.git/hooks/post-commit` to disable.

# Git hook (optional)

The OpenCode plugin already reports commits it observes. The hook covers the
rest: commits made from a terminal, another IDE, a script or a CI job.

```bash
curl -fsSL https://raw.githubusercontent.com/besteFinanzen/custom-llm-auto-router/feat/worktracker/integrations/git/install-hook.sh | sh
```

The installer opens a guided setup on `/dev/tty` (so it also works through
`curl | sh`):

1. shows the repository and project the hook will credit, plus whether an
   existing `post-commit` will be appended to or its Worktracker block updated
2. asks for the tracker URL, pre-filled with the saved value — Enter keeps it;
   a bare host gets `https://`, and a pasted `/worktracker/api` suffix is removed
3. asks for a hook token (input hidden) — Enter keeps the saved token; generate
   one with scope `hook` (or `editor`/`admin`) in the tracker UI → API Keys;
   `TRACKER_HOOK_TOKEN` in `.env` still works
4. validates the credential against `GET /worktracker/api/whoami` **before
   writing anything**: `401`/`403` re-prompts up to three times, an unreachable
   tracker asks whether to install anyway (commits will queue)
5. asks for a device name, defaulting to the saved one or the hostname
6. prints a summary with the token masked and asks `Install these?`

Declining, Ctrl-C or a failed credential check leaves the repository untouched.
After confirmation it writes:

- `.git/worktracker.env` (mode `0600`) with the URL, token and device — never
  committed
- `.git/hooks/post-commit` — a marker-delimited block appended to any existing
  hook
- `.git/hooks/post-commit.d/post_commit.py` — the stdlib-only reporter

Re-running updates the env file and the marker block in place (the device name
is preserved when you accept the default).

Non-interactive install:

```bash
WORKTRACKER_URL=https://ai.example.com WORKTRACKER_TOKEN=wt_hook_... \
  sh integrations/git/install-hook.sh
```

`--url` and `--token` skip the corresponding prompt, `--no-prompt` never
prompts (and exits with an error if credentials are missing), and
`--interactive` forces the wizard even when saved values already exist:

```bash
sh integrations/git/install-hook.sh --interactive
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

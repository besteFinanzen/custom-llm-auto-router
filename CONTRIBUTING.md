# Contributing

Thanks for considering a contribution. This project is licensed under the
[GNU Affero General Public License v3.0 or later](LICENSE) — an OSI-approved
copyleft license. By contributing you agree that:

- your contributions are licensed under the same terms (AGPL-3.0-or-later), and
- you accept the [Code of Conduct](CODE_OF_CONDUCT.md).

The project also states its expectations in the README's
[Acceptable Use](README.md#acceptable-use) section (no war/military use, illegal
drugs, human-rights violations, or terrorism). That statement is a
non-binding community commitment rather than a license condition — it keeps
the license OSI-approved — but we do expect contributors to share it.

## Development setup

```bash
git clone <your-fork-url> llm-auto-router
cd llm-auto-router
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env     # fill in whichever provider keys you have; all optional
```

You do **not** need real provider keys to work on most of the code: the test
suite runs against scriptable in-process mock upstreams.

## Tests

```bash
.venv/bin/python -m pytest -q
```

Please keep the suite green and add tests for new behavior. Tests must stay
hermetic — do not depend on network access, real API keys, or the local OpenCode
background service (the suite forces `ROUTER_TITLES_CMD=""` for this reason).

## Coding guidelines

- Match the existing style: small modules, docstrings explaining *why*, type
  hints on public functions, no new runtime dependencies without discussion.
- Keep the routing policy data-driven — configuration belongs in `catalog.yaml`
  or `.env`, not hardcoded.
- Never commit secrets, personal data, or `state/` contents.

## Pull requests

1. Fork and create a topic branch.
2. Make focused changes with tests and, if user-facing, update `README.md`.
3. Run `pytest -q` and make sure it passes.
4. Open a PR describing the motivation, the change, and how you tested it.

For anything large or design-level, please open an issue first so we can agree on
the approach.

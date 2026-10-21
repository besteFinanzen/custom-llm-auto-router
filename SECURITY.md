# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Instead, report them
privately — e.g. via GitHub's "Report a vulnerability" (Security → Advisories) if
the repository is on GitHub, or by contacting the maintainer directly. Include:

- what the issue is and where (file, endpoint, version/commit);
- how to reproduce it;
- the impact you think it has.

You can expect an initial response within a few days. Please give a reasonable
window to fix and release before any public disclosure.

## Scope

llm-auto-router is a local gateway that proxies to third-party model providers. It
has **no authentication of its own** and is intended to run on a trusted host /
network (by default it binds `127.0.0.1:4141`). Exposing it to an untrusted
network lets anyone spend your provider credit and use your keys — put it behind
a reverse proxy with auth, or bind it to a private interface, if you need remote
access.

In scope: credential leakage, path traversal, injection, denial of service via the
gateway, and unsafe handling of upstream responses.

Out of scope: vulnerabilities in third-party providers or in the models
themselves; misconfiguration that deliberately exposes the gateway.

## Handling secrets

- Real API keys belong **only** in `.env`, which is gitignored and should be
  `chmod 600`. Copy `.env.example` to get started; never edit `.env.example` to
  contain real values.
- `.env` is loaded at startup; unset keys disable the corresponding provider.
- If you believe a key has been committed or otherwise leaked, **rotate it** at
  the provider immediately — assume it is compromised.
- Runtime state under `state/` (usage, balances, decision log, cached session
  titles) is gitignored and may contain usage information; treat it as private.

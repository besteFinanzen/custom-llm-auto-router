"""Self-hosted AI work tracker.

The package is intentionally independent from the router internals: it exposes a
FastAPI router and a synchronous PostgreSQL store, so it can run inside the
single ``all`` image, as a standalone ``tracker`` service, or from tests.
"""

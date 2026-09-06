# Agent notes

- The HTTP client used in tests is `httpx`; install dev extras before running pytest.
- Long builds are expected; do not retry `docker build` on timeout.
- Install Python deps with `poetry install --no-root`; the repo root is not a package.

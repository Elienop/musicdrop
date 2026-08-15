"""Build/dev scripts. A package so `scripts.*` is the one module path mypy and
the tests both see — without it, a module listed in mypy's `files` AND imported
by a test resolves under two names and mypy refuses to check either."""

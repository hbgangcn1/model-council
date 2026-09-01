# Contributing to Model Council

Thanks for your interest in contributing! Model Council is a small, focused
project; we welcome bug reports, documentation improvements, and well-scoped
code changes.

## Code of conduct

This project follows the [Contributor Covenant](https://www.contributor-covenant.org/).
By participating, you agree to uphold it.

## Reporting bugs

Open a [GitHub Issue](https://github.com/hbgangcn1/model-council/issues) with:
- A minimal reproduction (task text, expected vs actual behavior)
- Your environment (`python --version`, `model-council.__version__`)
- The capability archive version (`revision` field in `capabilities.json`)
- Relevant log output (use `--tb=short` for pytest, `--verbose` for CLI)

## Proposing changes

Open an Issue first to discuss non-trivial changes. For small fixes (typos,
doc improvements, bug fixes), a PR is fine directly.

## Development setup

```bash
git clone https://github.com/hbgangcn1/model-council.git
cd model-council
pip install -e ".[dev,test]"
```

Run tests:
```bash
pytest                          # full suite
pytest orchestrator/test_*.py   # subset
pytest -k test_selector         # by keyword
```

Run lints:
```bash
ruff check .
ruff format .
mypy orchestrator benchmark
```

## Code style

- Python 3.10+ (uses `from __future__ import annotations` is not required; we
  target 3.10 baseline)
- Line length: 100 chars (configured in `pyproject.toml`)
- Use type hints for new public functions
- Docstrings: Google style for public APIs, terse for internal

## What we accept

- Bug fixes with a regression test
- New capability dimensions (with rationale: why a model differs on this)
- New provider adapters (with config docs)
- Documentation improvements
- Test coverage improvements
- Performance improvements (with benchmarks before/after)

## What we may push back on

- Changes to the core scoring formula without empirical evidence
- Breaking changes to the capability archive schema (v1 → v2 needs migration
  tooling)
- Removing existing guardrails (without a replacement)

## Architecture decisions

Before proposing changes to the core orchestrator or selector, please read
[`docs/architecture.md`](docs/architecture.md) and the existing ADRs in
[`docs/adr/`](docs/adr/). New significant design decisions should be captured
as new ADRs.

## License

By contributing, you agree that your contributions will be licensed under the
project's MIT License.
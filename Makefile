.PHONY: verify live test lint fmt fmt-check typecheck mdlint

verify: lint fmt-check typecheck test mdlint

test:
	uv run pytest

# The live suite talks to real forges with the maintainer's credentials. It is never run in CI
# and never by `verify`; see docs/live-testing.md. Tests skip when their variables are unset.
live:
	uv run pytest -m live tests/live -rs

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

fmt-check:
	uv run ruff format --check .

typecheck:
	uv run pyright

mdlint:
	uv run pymarkdown --config .pymarkdown.json scan -r --respect-gitignore .

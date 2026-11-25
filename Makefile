.PHONY: lint test

lint:
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	uv run --locked pyright
	actionlint

test:
	uv run --locked pytest

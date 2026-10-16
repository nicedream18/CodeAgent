.PHONY: check lint test protocol
check:
	uv sync --frozen
	uv run ruff check src tests scripts examples
	uv run mypy
	uv run python scripts/gen_protocol_doc.py --check
	uv run pytest -q
	git diff --check
lint:
	uv run ruff check src tests scripts examples
	uv run mypy
test:
	uv run pytest -q
protocol:
	uv run python scripts/gen_protocol_doc.py

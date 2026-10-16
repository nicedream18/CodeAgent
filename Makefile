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

.PHONY: check-docker
check-docker:
	MOKIO_TEST_DOCKER=1 uv run pytest -q tests/test_execution_backend.py

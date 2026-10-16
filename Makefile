.PHONY: install test test-safety lint eval clean

PY ?= .venv/bin/python

install:
	uv venv .venv || python3 -m venv .venv
	uv pip install -e ".[dev]" || $(PY) -m pip install -e ".[dev]"

test:            ## fast suite: unit + loop + safety + e2e, no model, no network
	$(PY) -m pytest -q

test-safety:     ## red-team suite only
	$(PY) -m pytest -q -m safety

lint:
	.venv/bin/ruff check src tests
	.venv/bin/mypy src

eval:            ## real-model golden tasks (needs gca.toml + API key; costs money)
	.venv/bin/gca -c gca.toml eval

clean:
	rm -rf .gca .pytest_cache build *.egg-info src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

.PHONY: help install lint format typecheck test test-integration cov playground backend eval docker-build docker-run clean

UV ?= uv
PY := $(UV) run

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Create venv and install all deps (incl. dev)
	$(UV) sync --all-groups
	$(PY) pre-commit install

lint: ## Ruff lint + format check
	$(PY) ruff check src tests
	$(PY) ruff format --check src tests

format: ## Auto-format
	$(PY) ruff check --fix src tests
	$(PY) ruff format src tests

typecheck: ## mypy (strict)
	$(PY) mypy

test: ## Unit + integration tests with a fake model (no network)
	$(PY) pytest -q -m "not integration"

test-integration: ## Tests that call Gemini (needs GOOGLE_API_KEY)
	$(PY) pytest -q -m integration

cov: ## Tests with coverage report
	$(PY) pytest -q -m "not integration" --cov --cov-report=term-missing --cov-report=xml

playground: ## ADK dev UI (adk web) on :8000
	$(PY) adk web src --port 8000

backend: ## FastAPI + A2A server on :8080
	$(PY) python -m veggie_planner

eval: ## adk eval over the eval set (needs GOOGLE_API_KEY)
	$(PY) adk eval src/veggie_planner tests/evals/veggie_planner.evalset.json --config_file_path tests/evals/test_config.json

docker-build: ## Build the container image
	docker build -t veggie-meal-planner:local .

docker-run: ## Run the image with your .env
	docker run --rm -p 8080:8080 --env-file .env veggie-meal-planner:local

deploy-agent-engine: ## Deploy to Vertex AI Agent Engine (needs GOOGLE_CLOUD_PROJECT)
	$(PY) python deployment/deploy_agent_engine.py

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov coverage.xml dist build data/*.db

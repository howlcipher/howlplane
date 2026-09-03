.PHONY: install test test-changed test-fast test-fast-python test-unit test-integration test-acceptance test-slow test-live test-full test-coverage lint format clean coverage build docs sync

# Environment and Setup
install:
	@echo "Installing Go and Python dependencies and global launchers..."
	go mod tidy
	go build -o build/howlplane ./cmd/howlplane
	pip install -e ".[dev]"
	mkdir -p $(HOME)/.config/howlplane
	printf '[control_plane]\npath = "%s"\n' "$$(pwd)" > $(HOME)/.config/howlplane/config.toml
	mkdir -p $(HOME)/.config/ai-control-plane
	printf '[control_plane]\npath = "%s"\n' "$$(pwd)" > $(HOME)/.config/ai-control-plane/config.toml
	mkdir -p $(HOME)/.local/bin
	ln -sf $$(pwd)/bin/howlplane $(HOME)/.local/bin/howlplane
	ln -sf $$(pwd)/bin/ai $(HOME)/.local/bin/ai

PYTEST ?= $(shell if [ -f /run/media/system/tallgeese/dev/.ci_verify_venv/bin/pytest ]; then echo /run/media/system/tallgeese/dev/.ci_verify_venv/bin/pytest; elif [ -f venv/bin/pytest ]; then echo venv/bin/pytest; else echo pytest; fi)
PYTHON ?= $(shell if [ -f /run/media/system/tallgeese/dev/.ci_verify_venv/bin/python3 ]; then echo /run/media/system/tallgeese/dev/.ci_verify_venv/bin/python3; elif [ -f venv/bin/python3 ]; then echo venv/bin/python3; else echo python3; fi)
FLAKE8 ?= $(shell if [ -f /run/media/system/tallgeese/dev/.ci_verify_venv/bin/flake8 ]; then echo /run/media/system/tallgeese/dev/.ci_verify_venv/bin/flake8; elif [ -f venv/bin/flake8 ]; then echo venv/bin/flake8; else echo flake8; fi)
BANDIT ?= $(shell if [ -f /run/media/system/tallgeese/dev/.ci_verify_venv/bin/bandit ]; then echo /run/media/system/tallgeese/dev/.ci_verify_venv/bin/bandit; elif [ -f venv/bin/bandit ]; then echo venv/bin/bandit; else echo bandit; fi)
PDOC ?= $(shell if [ -f /run/media/system/tallgeese/dev/.ci_verify_venv/bin/pdoc ]; then echo /run/media/system/tallgeese/dev/.ci_verify_venv/bin/pdoc; elif [ -f venv/bin/pdoc ]; then echo venv/bin/pdoc; else echo pdoc; fi)

# Testing and Coverage
test:
	@$(MAKE) test-full

test-full:
	@echo "Running Python tests..."
	PYTHONPATH=. $(PYTEST) tests/ -v
	@echo "Running Go tests..."
	go test -v ./...

test-changed:
	@echo "Selecting tests relevant to the current change set..."
	PYTHONPATH=. $(PYTHON) scripts/select_relevant_tests.py

test-fast:
	@echo "Running fast deterministic Python tests (all tiers except measured slow/live)..."
	PYTHONPATH=. $(PYTEST) tests/ -v -m "not slow and not live"
	@echo "Running Go tests..."
	go test ./...

test-fast-python:
	@echo "Running fast deterministic Python tests only..."
	PYTHONPATH=. $(PYTEST) tests/ -v -m "not slow and not live"

test-unit:
	PYTHONPATH=. $(PYTEST) tests/ -v -m unit

test-integration:
	PYTHONPATH=. $(PYTEST) tests/ -v -m integration

test-acceptance:
	PYTHONPATH=. $(PYTEST) tests/ -v -m acceptance

test-slow:
	PYTHONPATH=. $(PYTEST) tests/ -v -m slow

test-live:
	PYTHONPATH=. $(PYTEST) tests/ -v -m live || [ $$? -eq 5 ]

coverage-python:
	@echo "Generating Python coverage..."
	PYTHONPATH=. $(PYTEST) tests/ -v --cov=src --cov=scripts --cov-branch --cov-report=term-missing --cov-fail-under=42

test-coverage: coverage-python

coverage-go:
	@echo "Generating Go coverage..."
	go test -v -coverprofile=coverage.out ./...
	go tool cover -func=coverage.out

# Linting and Quality Checks
lint:
	@echo "Running Python linting (flake8)..."
	$(FLAKE8) src/ scripts/ tests/ --count --select=E9,F63,F7,F82 --show-source --statistics
	@echo "Running Python SAST (bandit)..."
	$(BANDIT) -r src/ scripts/ tests/ -ll -ii
	@echo "Running Go linting..."
	if command -v golangci-lint >/dev/null 2>&1; then golangci-lint run; else echo "golangci-lint not installed, skipping..."; fi
	@echo "Running Go SAST (gosec)..."
	if command -v gosec >/dev/null 2>&1; then gosec ./...; else echo "gosec not installed, skipping..."; fi



format:
	@echo "Formatting Python code (black)..."
	black src/ scripts/ tests/
	@echo "Formatting Go code (gofmt)..."
	gofmt -w .

clean:
	@echo "Cleaning build artifacts and cache..."
	rm -f ai_installer coverage.out
	rm -rf __pycache__ .pytest_cache docs/
	find . -type d -name "__pycache__" -exec rm -r {} +

# Build
build:
	@echo "Building Go binary installer..."
	go build -o ai_installer ./cmd/installer
	@echo "Build complete: ./ai_installer"

# Documentation

docs:
	@echo "Generating API documentation via pdoc..."
	rm -rf docs/.build-tmp
	mkdir -p docs/.build-tmp/api
	$(PDOC) ./src ./scripts -o docs/.build-tmp/api # pdoc staging build
	cp -r documentation docs/.build-tmp/documentation
	cp -r assets docs/.build-tmp/assets
	cp -r .agents docs/.build-tmp/.agents
	@echo "Swapping in freshly built docs subtrees..."
	rm -rf docs/api docs/documentation docs/assets docs/.agents
	mv docs/.build-tmp/api docs/api
	mv docs/.build-tmp/documentation docs/documentation
	mv docs/.build-tmp/assets docs/assets
	mv docs/.build-tmp/.agents docs/.agents
	rm -rf docs/.build-tmp
	@echo "Setting up GitHub Pages frontpage..."
	cp README.md docs/index.md
	cp AGENTS.md docs/
	cp GEMINI.md docs/
	cp CLAUDE.md docs/
	cp change_log.md docs/
	cp -r docs_theme/_layouts docs/
	echo "include: [\".agents\"]" > docs/_config.yml
	echo "exclude: [\"Makefile\"]" >> docs/_config.yml
	echo "defaults:" >> docs/_config.yml
	echo "  -" >> docs/_config.yml
	echo "    scope:" >> docs/_config.yml
	echo "      path: \"documentation\"" >> docs/_config.yml
	echo "    values:" >> docs/_config.yml
	echo "      layout: \"default\"" >> docs/_config.yml
	echo "  -" >> docs/_config.yml
	echo "    scope:" >> docs/_config.yml
	echo "      path: \".agents\"" >> docs/_config.yml
	echo "    values:" >> docs/_config.yml
	echo "      layout: \"default\"" >> docs/_config.yml
	echo "  -" >> docs/_config.yml
	echo "    scope:" >> docs/_config.yml
	echo "      path: \"index.md\"" >> docs/_config.yml
	echo "    values:" >> docs/_config.yml
	echo "      layout: \"default\"" >> docs/_config.yml
	echo "  -" >> docs/_config.yml
	echo "    scope:" >> docs/_config.yml
	echo "      path: \"GEMINI.md\"" >> docs/_config.yml
	echo "    values:" >> docs/_config.yml
	echo "      layout: \"default\"" >> docs/_config.yml
	echo "  -" >> docs/_config.yml
	echo "    scope:" >> docs/_config.yml
	echo "      path: \"AGENTS.md\"" >> docs/_config.yml
	echo "    values:" >> docs/_config.yml
	echo "      layout: \"default\"" >> docs/_config.yml
	echo "  -" >> docs/_config.yml
	echo "    scope:" >> docs/_config.yml
	echo "      path: \"CLAUDE.md\"" >> docs/_config.yml
	echo "    values:" >> docs/_config.yml
	echo "      layout: \"default\"" >> docs/_config.yml
	echo "  -" >> docs/_config.yml
	echo "    scope:" >> docs/_config.yml
	echo "      path: \"change_log.md\"" >> docs/_config.yml
	echo "    values:" >> docs/_config.yml
	echo "      layout: \"default\"" >> docs/_config.yml

# Sync and Data
sync:
	@echo "Syncing context and pulling remote docs..."
	python3 scripts/sync_context.py
	python3 scripts/pull_from_docs.py

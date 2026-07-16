PROJECT_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
FRONTEND_DIR := $(PROJECT_ROOT)/frontend
BACKEND_DIR := $(PROJECT_ROOT)/backend
TAURI_DIR := $(PROJECT_ROOT)/tauri
DATA_DIR := $(PROJECT_ROOT)/.data
TAURI_CLI := $(FRONTEND_DIR)/node_modules/.bin/tauri

.PHONY: setup backend frontend dev icons check check-backend check-frontend check-tauri build-frontend build-tauri help

setup:
	uv sync --directory $(BACKEND_DIR) --frozen
	npm --prefix $(FRONTEND_DIR) ci

backend:
	PAGEFERRY_DATA_DIR="$(DATA_DIR)" uv run --directory $(BACKEND_DIR) uvicorn main:app --reload --host 127.0.0.1 --port 8765

frontend:
	npm --prefix $(FRONTEND_DIR) run dev

dev:
	cd $(TAURI_DIR) && $(TAURI_CLI) dev

icons:
	$(TAURI_CLI) icon $(FRONTEND_DIR)/src/assets/logo.svg --output $(TAURI_DIR)/icons
	rm -rf $(TAURI_DIR)/icons/android $(TAURI_DIR)/icons/ios
	rm -f $(TAURI_DIR)/icons/64x64.png $(TAURI_DIR)/icons/StoreLogo.png $(TAURI_DIR)/icons/Square*Logo.png

check: check-backend check-frontend check-tauri

check-backend:
	uv run --directory $(BACKEND_DIR) pytest
	uv run --directory $(BACKEND_DIR) ruff check .
	uv run --directory $(BACKEND_DIR) ruff format --check .

check-frontend:
	npm --prefix $(FRONTEND_DIR) run test
	npm --prefix $(FRONTEND_DIR) run typecheck
	npm --prefix $(FRONTEND_DIR) run lint
	npm --prefix $(FRONTEND_DIR) run format:check
	npm --prefix $(FRONTEND_DIR) run build

check-tauri:
	cd $(TAURI_DIR) && cargo fmt --check
	cd $(TAURI_DIR) && cargo clippy --all-targets -- -D warnings
	cd $(TAURI_DIR) && cargo check

build-frontend:
	npm --prefix $(FRONTEND_DIR) run build

build-tauri:
	cd $(TAURI_DIR) && $(TAURI_CLI) build --debug --no-bundle

help:
	@printf '%s\n' \
		'make setup          Install locked Python and Node dependencies' \
		'make backend        Run the FastAPI service with repo-local dev data' \
		'make frontend       Run the React UI in a browser' \
		'make dev            Run the Tauri shell (start make backend separately)' \
		'make icons          Regenerate desktop icons from the product SVG' \
		'make check          Run backend, frontend, and Rust checks' \
		'make build-tauri    Build the current debug shell without an installer'

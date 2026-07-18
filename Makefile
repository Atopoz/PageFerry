PROJECT_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
FRONTEND_DIR := $(PROJECT_ROOT)/frontend
BACKEND_DIR := $(PROJECT_ROOT)/backend
TAURI_DIR := $(PROJECT_ROOT)/tauri
DATA_DIR := $(PROJECT_ROOT)/.data
TAURI_CLI := $(FRONTEND_DIR)/node_modules/.bin/tauri
TAURI_VERSION = $(shell node -e "process.stdout.write(require('$(TAURI_DIR)/tauri.conf.json').version)")
MACOS_DMG_ARCH = $(shell if [ "$$(uname -m)" = "arm64" ]; then printf 'aarch64'; elif [ "$$(uname -m)" = "x86_64" ]; then printf 'x64'; fi)
MACOS_DMG = $(TAURI_DIR)/target/release/bundle/dmg/PageFerry_$(TAURI_VERSION)_$(MACOS_DMG_ARCH).dmg
DMG_SIGN_IDENTITY ?=

.PHONY: setup backend frontend dev icons check check-backend check-frontend check-tauri build-frontend build-sidecar build-tauri finalize-macos-dmg build-macos-smoke build-macos-beta build-macos-release help

setup:
	uv sync --directory $(BACKEND_DIR) --frozen
	npm --prefix $(FRONTEND_DIR) ci

backend:
	PAGEFERRY_DATA_DIR="$(DATA_DIR)" PAGEFERRY_SECRET_SERVICE_NAME="com.pageferry.provider-secrets.dev" uv run --directory $(BACKEND_DIR) uvicorn main:create_app --factory --reload --host 127.0.0.1 --port 8765

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

build-sidecar:
	uv run --directory $(BACKEND_DIR) python ../scripts/build-sidecar.py

build-tauri:
	cd $(TAURI_DIR) && $(TAURI_CLI) build --debug --no-bundle

finalize-macos-dmg:
	@test "$$(uname -s)" = "Darwin" || { printf '%s\n' 'finalize-macos-dmg only supports macOS' >&2; exit 1; }
	@test -n "$(TAURI_VERSION)" -a -n "$(MACOS_DMG_ARCH)" || { printf '%s\n' 'unable to resolve macOS DMG version or architecture' >&2; exit 1; }
	uv run --directory $(BACKEND_DIR) python ../scripts/finalize-macos-dmg.py --dmg "$(MACOS_DMG)" $(if $(DMG_SIGN_IDENTITY),--sign-identity="$(DMG_SIGN_IDENTITY)")

build-macos-smoke: build-sidecar
	cd $(TAURI_DIR) && $(TAURI_CLI) build --bundles app,dmg --config tauri.macos-smoke.conf.json
	$(MAKE) finalize-macos-dmg DMG_SIGN_IDENTITY=-

build-macos-beta: build-macos-smoke

build-macos-release:
	@test "$$(uname -s)" = "Darwin" || { printf '%s\n' 'build-macos-release only supports macOS' >&2; exit 1; }
	@test -n "$(APPLE_SIGNING_IDENTITY)" || { printf '%s\n' 'APPLE_SIGNING_IDENTITY is required' >&2; exit 1; }
	@test -n "$(DMG_SIGN_IDENTITY)" || { printf '%s\n' 'DMG_SIGN_IDENTITY is required' >&2; exit 1; }
	APPLE_SIGNING_IDENTITY="$(APPLE_SIGNING_IDENTITY)" $(MAKE) build-sidecar
	cd $(TAURI_DIR) && env -u APPLE_ID -u APPLE_PASSWORD -u APPLE_TEAM_ID -u APPLE_API_ISSUER -u APPLE_API_KEY -u APPLE_API_KEY_PATH APPLE_SIGNING_IDENTITY="$(APPLE_SIGNING_IDENTITY)" $(TAURI_CLI) build --bundles app,dmg --config tauri.release.conf.json
	$(MAKE) finalize-macos-dmg DMG_SIGN_IDENTITY="$(DMG_SIGN_IDENTITY)"

help:
	@printf '%s\n' \
		'make setup          Install locked Python and Node dependencies' \
		'make backend        Run the FastAPI service with repo-local dev data' \
		'make frontend       Run the React UI in a browser' \
		'make dev            Run the Tauri shell (start make backend separately)' \
		'make icons          Regenerate desktop icons from the product SVG' \
		'make check          Run backend, frontend, and Rust checks' \
		'make build-sidecar  Freeze the backend for the current Tauri target' \
		'make build-tauri    Build the current debug shell without an installer' \
		'make finalize-macos-dmg  Remove the DMG volume icon and preserve Finder layout' \
		'make build-macos-smoke  Build an ad-hoc signed macOS app and DMG' \
		'make build-macos-beta  Build the same ad-hoc artifact for an unsigned public beta' \
		'make build-macos-release  Build a signed, unnotarized macOS release candidate'

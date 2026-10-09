.PHONY: setup dev check test css migrate revision i18n-extract i18n-update i18n-compile

UV := uv
DEV_HOST ?= 127.0.0.1
DEV_PORT ?= 8000

setup:
	$(UV) sync --all-groups
	$(UV) run pre-commit install
	$(MAKE) css i18n-compile

check: i18n-compile
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run pyright
	$(UV) run pytest
	@temp_dir=$$(mktemp -d); \
	trap 'rm -rf "$$temp_dir"' EXIT; \
	ASSETBOARD_DATA_DIR="$$temp_dir" ASSETBOARD_DATABASE_URL="sqlite:///$$temp_dir/check.db" $(UV) run assetboard migrate && \
	ASSETBOARD_DATA_DIR="$$temp_dir" ASSETBOARD_DATABASE_URL="sqlite:///$$temp_dir/check.db" $(UV) run assetboard check-migrations
	$(UV) run pre-commit run --all-files

test:
	$(UV) run pytest

css:
	@mkdir -p src/assetboard/static/css .cache
	@set -eu; \
	case "$$(uname -s)-$$(uname -m)" in \
		Linux-x86_64) asset=tailwindcss-linux-x64 ;; \
		Linux-aarch64|Linux-arm64) asset=tailwindcss-linux-arm64 ;; \
		Darwin-x86_64) asset=tailwindcss-macos-x64 ;; \
		Darwin-arm64) asset=tailwindcss-macos-arm64 ;; \
		*) echo "Unsupported Tailwind CLI platform: $$(uname -s)-$$(uname -m)" >&2; exit 1 ;; \
	esac; \
	if [ ! -x ".cache/$$asset" ]; then \
		curl --fail --location --silent --show-error \
			"https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/$$asset" \
			-o ".cache/$$asset"; \
		chmod +x ".cache/$$asset"; \
	fi; \
	".cache/$$asset" --config tailwind.config.js \
		--input src/assetboard/static/css/input.css \
		--output src/assetboard/static/css/app.css --minify

migrate:
	$(UV) run assetboard migrate

revision:
	$(UV) run assetboard revision --message "$(m)"

i18n-extract:
	mkdir -p locales
	$(UV) run pybabel extract -F babel.cfg -k _ -k gettext \
		-o locales/messages.pot src/assetboard

i18n-update:
	$(MAKE) i18n-extract
	$(UV) run pybabel update -i locales/messages.pot -d src/assetboard/locales -l en
	$(UV) run pybabel update -i locales/messages.pot -d src/assetboard/locales -l pl

i18n-compile:
	$(UV) run pybabel compile -d src/assetboard/locales

dev:
	$(MAKE) css i18n-compile
	$(UV) run uvicorn assetboard.app:create_app --factory --reload --host $(DEV_HOST) --port $(DEV_PORT)

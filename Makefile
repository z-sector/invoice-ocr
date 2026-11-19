IMAGE         ?= invoice-ocr
TAG           ?= latest
CONTAINER     ?= invoice-ocr
PORT          ?= 8741
DOCKER        ?= docker
URL           ?= http://localhost:$(PORT)
WAIT_TIMEOUT  ?= 300
# Cache lifetime passed into the container, in hours. Empty in the app means 24 (one day).
PARSE_CACHE_TTL_HOURS ?= 24
# Survives `make restart`. Removed only by `docker volume rm`.
CACHE_VOLUME  ?= $(CONTAINER)-cache
# `make parse` argument
FILE          ?=

IMAGE_REF      := $(IMAGE):$(TAG)
CONTAINER_PORT := 8741

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} /^##@/ {printf "\n%s\n", substr($$0, 5)} /^[a-zA-Z_-]+:.*## / {printf "  %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

##@ Docker

.PHONY: build
build: ## Build the image (downloads the OCR models into it)
	$(DOCKER) build -t $(IMAGE_REF) .

.PHONY: rebuild
rebuild: ## Build the image without the layer cache
	$(DOCKER) build --no-cache --pull -t $(IMAGE_REF) .

.PHONY: run
run: ## (Re)start the container; keep the parse cache in a volume
	@$(DOCKER) image inspect $(IMAGE_REF) >/dev/null 2>&1 || { echo "Image $(IMAGE_REF) not found - run 'make build' first (or 'make up')." >&2; exit 1; }
	@$(DOCKER) stop $(CONTAINER) >/dev/null 2>&1; $(DOCKER) rm $(CONTAINER) >/dev/null 2>&1 || true
	$(DOCKER) run -d --name $(CONTAINER) --restart unless-stopped \
		-p $(PORT):$(CONTAINER_PORT) \
		-e PARSE_CACHE_TTL_HOURS="$(PARSE_CACHE_TTL_HOURS)" \
		-v $(CACHE_VOLUME):/home/app/.cache/invoice-ocr \
		$(IMAGE_REF)
	@echo "Starting on $(URL) - run 'make wait' to block until it is ready, 'make logs' to follow the output."

.PHONY: up
up: build run ## Build the image, then (re)start the container

.PHONY: stop
stop: ## Stop and remove the container
	@$(DOCKER) stop $(CONTAINER) >/dev/null 2>&1; $(DOCKER) rm $(CONTAINER) >/dev/null 2>&1 && echo "Removed $(CONTAINER)." || echo "No $(CONTAINER) container."

.PHONY: restart
restart: stop run ## Recreate the container

.PHONY: logs
logs: ## Follow the container logs
	$(DOCKER) logs -f $(CONTAINER)

.PHONY: status
status: ## Show the container state and health
	@$(DOCKER) ps -a --filter name=^$(CONTAINER)$$ --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'

.PHONY: shell
shell: ## Open a shell inside the running container
	$(DOCKER) exec -it $(CONTAINER) bash

.PHONY: clean
clean: stop ## Remove the container and the image
	-$(DOCKER) rmi $(IMAGE_REF)

##@ API (against URL, default http://localhost:PORT)

.PHONY: health
health: ## GET /health
	@curl -sS --fail-with-body $(URL)/health && echo

.PHONY: wait
wait: ## Block until /health answers (WAIT_TIMEOUT seconds, default 300)
	@echo "Waiting for $(URL)/health ..."
	@end=$$(( $$(date +%s) + $(WAIT_TIMEOUT) )); \
	until curl -fsS -o /dev/null $(URL)/health 2>/dev/null; do \
		if [ $$(date +%s) -ge $$end ]; then echo "Not ready after $(WAIT_TIMEOUT)s - check 'make logs'." >&2; exit 1; fi; \
		sleep 2; \
	done; \
	echo "Ready."

.PHONY: parse
parse: ## POST /api/parse: make parse FILE=invoice.pdf
	@test -n "$(FILE)" || { echo "Usage: make parse FILE=path/to/invoice.pdf" >&2; exit 2; }
	@curl -sS --fail-with-body -F "file=@$(FILE)" $(URL)/api/parse && echo

##@ Local development (uv, no Docker)

.PHONY: test
test: ## Run the unit tests
	uv run python -m unittest discover -s tests -v

.PHONY: install
install: ## Create .venv from uv.lock
	uv sync --locked

.PHONY: dev
dev: ## Run the API locally with auto-reload
	uv run uvicorn app.main:app --reload --host 0.0.0.0 --port $(PORT)

.PHONY: lock
lock: ## Re-resolve uv.lock after editing pyproject.toml
	uv lock

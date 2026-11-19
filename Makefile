IMAGE         ?= invoice-ocr
TAG           ?= latest
CONTAINER     ?= invoice-ocr
PORT          ?= 8741
MODELS_VOLUME ?= invoice-ocr-models
# Languages whose models are downloaded into the image at build time, e.g. MODEL_LANGS=en,ru
MODEL_LANGS   ?=
# Passed to the container with --env-file when the file exists
ENV_FILE      ?= .env
DOCKER        ?= docker
URL           ?= http://localhost:$(PORT)
WAIT_TIMEOUT  ?= 300
# `make parse` arguments
FILE          ?=
DOC_LANG      ?=

IMAGE_REF      := $(IMAGE):$(TAG)
CONTAINER_PORT := 8741
MODELS_DIR     := /home/app/.paddlex

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} /^##@/ {printf "\n%s\n", substr($$0, 5)} /^[a-zA-Z_-]+:.*## / {printf "  %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

##@ Docker

.PHONY: build
build: ## Build the image (MODEL_LANGS=en,ru bakes models in)
	$(DOCKER) build --build-arg MODEL_LANGS=$(MODEL_LANGS) -t $(IMAGE_REF) .

.PHONY: rebuild
rebuild: ## Build the image without the layer cache
	$(DOCKER) build --no-cache --pull --build-arg MODEL_LANGS=$(MODEL_LANGS) -t $(IMAGE_REF) .

.PHONY: run
run: ## (Re)start the container in the background from the built image
	@$(DOCKER) image inspect $(IMAGE_REF) >/dev/null 2>&1 || { echo "Image $(IMAGE_REF) not found - run 'make build' first (or 'make up')." >&2; exit 1; }
	@$(DOCKER) stop $(CONTAINER) >/dev/null 2>&1; $(DOCKER) rm $(CONTAINER) >/dev/null 2>&1 || true
	$(DOCKER) run -d --name $(CONTAINER) --restart unless-stopped \
		-p $(PORT):$(CONTAINER_PORT) \
		-v $(MODELS_VOLUME):$(MODELS_DIR) \
		$(if $(wildcard $(ENV_FILE)),--env-file $(ENV_FILE)) \
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

.PHONY: clean-models
clean-models: stop ## Remove the container and the downloaded-models volume
	-$(DOCKER) volume rm $(MODELS_VOLUME)

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
parse: ## POST /api/parse: make parse FILE=invoice.pdf [DOC_LANG=ru]
	@test -n "$(FILE)" || { echo "Usage: make parse FILE=path/to/invoice.pdf [DOC_LANG=ru]" >&2; exit 2; }
	@curl -sS --fail-with-body -F "file=@$(FILE)" $(if $(DOC_LANG),-F "lang=$(DOC_LANG)") $(URL)/api/parse && echo

##@ Local development (uv, no Docker)

.PHONY: install
install: ## Create .venv from uv.lock
	uv sync --locked

.PHONY: dev
dev: ## Run the API locally with auto-reload
	uv run uvicorn app.main:app --reload --host 0.0.0.0 --port $(PORT)

.PHONY: lock
lock: ## Re-resolve uv.lock after editing pyproject.toml
	uv lock

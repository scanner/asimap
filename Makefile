# -*- Mode: Makefile -*-
ROOT_DIR := $(shell git rev-parse --show-toplevel)
include $(ROOT_DIR)/Make.rules
DOCKER_BUILDKIT := 1
LATEST_TAG := $(shell git describe --abbrev=0)

.PHONY: clean lint test mypy logs shell restart delete down up build dirs help

test: venv
	$(ACTIVATE) pytest

build: requirements/production.txt requirements/development.txt	## `docker build` for both `prod` and `dev` targets
	@COMPOSE_DOCKER_CLI_BUILD=1 DOCKER_BUILDKIT=1 docker build --target prod
	@COMPOSE_DOCKER_CLI_BUILD=1 DOCKER_BUILDKIT=1 docker build --target dev

package:  ## build python package

release: package  ## Make a releases.

help:	## Show this help.
	@grep -hE '^[A-Za-z0-9_ \-]*?:.*##.*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'

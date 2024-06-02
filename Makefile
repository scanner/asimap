# -*- Mode: Makefile -*-
ROOT_DIR := $(shell git rev-parse --show-toplevel)
include $(ROOT_DIR)/Make.rules
DOCKER_BUILDKIT := 1
LATEST_TAG := $(shell git describe --abbrev=0)

.PHONY: clean lint test test_units test_integrations mypy logs shell restart delete down up build dirs help package publish

test_integrations: venv
	PYTHONPATH=`pwd` $(ACTIVATE) pytest -m integration

test_units: venv
	PYTHONPATH=`pwd` $(ACTIVATE) pytest -m "not integration"

test: venv
	PYTHONPATH=`pwd` $(ACTIVATE) pytest

coverage: venv
	PYTHONPATH=`pwd` $(ACTIVATE) coverage run -m pytest
	coverage html
	open 'htmlcov/index.html'

build: requirements/build.txt requirements/development.txt	## `docker build` for both `prod` and `dev` targets
	COMPOSE_DOCKER_CLI_BUILD=1 DOCKER_BUILDKIT=1 docker build --build-arg VERSION=$(LATEST_TAG) --target prod --tag asimap:$(LATEST_TAG) --tag asimap:prod .
	COMPOSE_DOCKER_CLI_BUILD=1 DOCKER_BUILDKIT=1 docker build --build-arg VERSION=$(LATEST_TAG) --target dev --tag asimap:$(LATEST_TAG)-dev --tag asimap:dev .

asimap_test_dir:   ## Create directory running local development
	@mkdir -p $(ROOT_DIR)/asimap_test_dir

traces_dir:   ## Create traces directory for running local development
	@mkdir -p $(ROOT_DIR)/asimap_test_dir/traces

ssl:     ## Creates the ssl directory used to hold development ssl cert and key
	@mkdir -p $(ROOT_DIR)/asimap_test_dir/ssl

dirs: asimap_test_dir ssl traces_dir

# XXX Should we have an option to NOT use certs/mkcert (either just make
#     self-signed ourself) in case a developer does not want to go through the
#     potential risks associated with mkcert?
#
asimap_test_dir/ssl/ssl_key.pem asimap_test_dir/ssl/ssl_crt.pem:
	@mkcert -key-file $(ROOT_DIR)/asimap_test_dir/ssl/ssl_key.pem \
                -cert-file $(ROOT_DIR)/asimap_test_dir/ssl/ssl_crt.pem \
                `hostname` localhost 127.0.0.1 ::1

certs: ssl asimap_test_dir/ssl/ssl_key.pem asimap_test_dir/ssl/ssl_crt.pem	## uses `mkcert` to create certificates for local development.

up: build dirs certs	## build and then `docker compose up` for the `dev` profile. Use this to rebuild restart services that have changed.
	@docker compose --profile dev up --remove-orphans --detach

down:	## `docker compose down` for the `dev` profile
	@docker compose --profile dev down --remove-orphans

delete: clean	## docker compose down for `dev` and `prod` and `make clean`.
	@docker compose --profile dev down --remove-orphans
	@docker compose --profile prod down --remove-orphans

restart:	## docker compose restart for the `dev` profile
	@docker compose --profile dev restart

shell:	## Make a bash shell an ephemeral devweb container
	@docker compose run --rm devweb /bin/bash

exec_shell: ## Make a bash shell in the docker-compose running devweb container
	@docker compose exec devweb /bin/bash

.package: venv $(PY_FILES) pyproject.toml README.md LICENSE Makefile
	PYTHONPATH=`pwd` $(ACTIVATE) python -m build
	@touch .package

package: .package ## build python package (.tar.gz and .whl)

install: package  ## Install asimap via pip install of the package wheel
	pip install --force-reinstall -U $(ROOT_DIR)/dist/asimap-$(LATEST_TAG)-py3-none-any.whl

release: package  ## Make a release. Tag based on the version.

publish: package  ## Publish the package to pypi

help:	## Show this help.
	@grep -hE '^[A-Za-z0-9_ \-]*?:.*##.*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'

clean::	## Swab the decks! Does not touch docker images or volumes.
	@rm -rf $(ROOT_DIR)/asimap_test_dir

logs:	## Tail the logs for devweb, worker, devsmtpd, mailhog
	@docker compose logs -f imap-dev

# -*- Mode: Makefile -*-
ROOT_DIR := $(shell git rev-parse --show-toplevel)
include $(ROOT_DIR)/Make.rules
DOCKER_BUILDKIT := 1
LATEST_TAG := $(shell git describe --abbrev=0)

.PHONY: clean lint test test_units test_integrations mypy logs shell restart delete down up build dirs help

test_integrations: venv
	$(ACTIVATE) pytest -m integration

test_units: venv
	$(ACTIVATE) pytest -m "not integration"

test: venv test_units test_integrations

build: requirements/production.txt requirements/development.txt	## `docker build` for both `prod` and `dev` targets
	@COMPOSE_DOCKER_CLI_BUILD=1 DOCKER_BUILDKIT=1 docker build --target prod
	@COMPOSE_DOCKER_CLI_BUILD=1 DOCKER_BUILDKIT=1 docker build --target dev

ssl:
	@mkdir $(ROOT_DIR)/ssl

# XXX Should we have an option to NOT use certs/mkcert (either just make
#     self-signed ourself) in case a developer does not want to go through the
#     potential risks associated with mkcert?
#
ssl/ssl_key.pem ssl/ssl_crt.pem:
	@mkcert -key-file $(ROOT_DIR)/ssl/ssl_key.pem \
                -cert-file $(ROOT_DIR)/ssl/ssl_crt.pem \
                `hostname` localhost 127.0.0.1 ::1

certs: ssl ssl/ssl_key.pem ssl/ssl_crt.pem	## uses `mkcert` to create certificates for local development.

package:  ## build python package

release: package  ## Make a releases.

help:	## Show this help.
	@grep -hE '^[A-Za-z0-9_ \-]*?:.*##.*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'

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

build: build_pkg requirements/production.txt requirements/development.txt	## `docker build` for both `prod` and `dev` targets
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


.package: venv $(PY_FILES) pyproject.toml README.md LICENSE Makefile
	PYTHONPATH=`pwd` $(ACTIVATE) python -m build
	@touch .package

package: .package ## build python package (.tar.gz and .whl)

install: package  ## Install asimap via pip install of the package wheel
	pip install -U $(ROOT_DIR)/dist/asimap-$(VERSION)-py3-none-any.whl

release: package  ## Make a releases.

publish: package  ## Publish the package to pypi.

help:	## Show this help.
	@grep -hE '^[A-Za-z0-9_ \-]*?:.*##.*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'

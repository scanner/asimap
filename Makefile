# -*- Mode: Makefile -*-
ROOT_DIR := $(shell git rev-parse --show-toplevel)
include $(ROOT_DIR)/Make.rules
DOCKER_BUILDKIT := 1

.PHONY: clean lint test test-units test-integrations mypy logs shell restart delete down up build dirs help package publish tag publish-tag

test-integrations: venv
	PYTHONPATH=`pwd` $(ACTIVATE) pytest -m integration

test-units: venv
	PYTHONPATH=`pwd` $(ACTIVATE) pytest -m "not integration"

test: venv
	PYTHONPATH=`pwd` $(ACTIVATE) pytest

coverage: venv
	PYTHONPATH=`pwd` $(ACTIVATE) coverage run -m pytest
	coverage html
	open 'htmlcov/index.html'

build: version requirements/build.txt requirements/development.txt	## `docker build` for both `prod` and `dev` targets
	COMPOSE_DOCKER_CLI_BUILD=1 DOCKER_BUILDKIT=1 docker build --build-arg VERSION="$(VERSION)" --target prod --tag asimap:$(VERSION) --tag asimap:prod .
	COMPOSE_DOCKER_CLI_BUILD=1 DOCKER_BUILDKIT=1 docker build --build-arg VERSION=$(VERSION) --target dev --tag asimap:$(VERSION)-dev --tag asimap:dev .

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

shell:	## Make a bash shell an ephemeral dev container
	@docker compose exec -ti asimap-dev /bin/bash

exec_shell: ## Make a bash shell in the docker-compose running imap-dev container
	@docker compose exec imap-dev /bin/bash

.package: version venv $(PY_FILES) pyproject.toml README.md LICENSE Makefile requirements/build.txt requirements/production.txt
	@PYTHONPATH=`pwd` $(ACTIVATE) python -m build
	@touch .package

package: .package ## build python package (.tar.gz and .whl)

install: version package  ## Install asimap via pip install of the package wheel
	pip install --force-reinstall -U $(ROOT_DIR)/dist/asimap-$(VERSION)-py3-none-any.whl

# Should mark the published tag as a release on github
release: package  ## Make a release. Tag based on the version.

publish: package  ## Publish the package to pypi

tag: version    ## Tag the git repo with the current version of asimapd.
	@if git rev-parse "$(VERSION)" >/dev/null 2>&1; then \
	    echo "Tag '$(VERSION)' already exists (skipping 'git tag')" ; \
        else \
	    git tag --sign "$(VERSION)" -m "Version $(VERSION)"; \
            echo "Tagged with '$(VERSION)'" ; \
        fi

publish-tag: version tag   ## Tag (if not already tagged) and publish the tag of the current version to git `origin` branch
	@git push origin tag $(VERSION)

help:	## Show this help.
	@grep -hE '^[A-Za-z0-9_ \-]*?:.*##.*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'

clean::	## Swab the decks! Does not touch docker images or volumes.
	@rm -rf $(ROOT_DIR)/asimap_test_dir $(ROOT_DIR)/.package $(ROOT_DIR)/dist

logs:	## Tail the logs for imap-dev container
	@docker compose logs -f imap-dev

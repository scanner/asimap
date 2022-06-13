# -*- Mode: Makefile -*-
ROOT_DIR := $(shell git rev-parse --show-toplevel)
ACTIVATE := source $(ROOT_DIR)/venv/bin/activate &&
REQ_ACTIVATE := source $(ROOT_DIR)/requirements/venv_req/bin/activate &&

.PHONY: clean lint test mypy

.git/hooks/pre-commit .git/hooks/pre-push:
	@$(ACTIVATE) pre-commit install
	@echo "pre-commit hooks installed!"
	@touch .git/hooks/pre-commit .git/hooks/pre-push


$(ROOT_DIR)/requirements/venv_req:
	@python -m venv "$@" ; \
	$(REQ_ACTIVATE) pip install -U pip ; \
	$(REQ_ACTIVATE) pip install pip-tools ; \
	touch $@

venv_req: $(ROOT_DIR)/requirements/venv_req

$(ROOT_DIR)/venv: $(ROOT_DIR)/requirements/local.txt
	@if [ -d "$@" ] ; then \
	  $(ACTIVATE) pip-sync $(ROOT_DIR)/requirements/local.txt ; \
        else \
	  python -m venv $@ ; \
	  $(ACTIVATE) pip install -U pip ; \
	  $(ACTIVATE) pip install -r $(ROOT_DIR)/requirements/local.txt ; \
        fi
	@touch $@

venv:: $(ROOT_DIR)/venv
venv:: .git/hooks/pre-commit
venv:: .git/hooks/pre-push


squeegee: venv isort black
	@$(ACTIVATE) flake8 asimap/

# @$(ACTIVATE) mypy --install-types --non-interactive asimap/

lint: venv
	@$(ACTIVATE) pre-commit run -a

PY_FILES=$(shell find asimap/ -type f -name '*.py')
.blackened: $(PY_FILES) venv
	@$(ACTIVATE) black asimap/
	@touch .blackened

.isorted: $(PY_FILES) venv
	@$(ACTIVATE) isort asimap/
	@touch .isorted

formatting: isort black
isort: .isorted
black: .blackened

mypy: venv
	$(ACTIVATE) mypy --install-types --non-interactive .

$(ROOT_DIR)/requirements/base.txt: $(ROOT_DIR)/requirements/venv_req $(ROOT_DIR)/requirements/base.in
	@$(REQ_ACTIVATE) pip-compile $(ROOT_DIR)/requirements/base.in -o $(ROOT_DIR)/requirements/base.txt

$(ROOT_DIR)/requirements/production.txt: $(ROOT_DIR)/requirements/venv_req $(ROOT_DIR)/requirements/base.txt $(ROOT_DIR)/requirements/production.in
	@$(REQ_ACTIVATE) pip-compile $(ROOT_DIR)/requirements/production.in -o $(ROOT_DIR)/requirements/production.txt

$(ROOT_DIR)/requirements/local.txt: $(ROOT_DIR)/requirements/venv_req $(ROOT_DIR)/requirements/local.in  $(ROOT_DIR)/requirements/base.txt
	@$(REQ_ACTIVATE) pip-compile $(ROOT_DIR)/requirements/local.in -o $(ROOT_DIR)/requirements/local.txt

requirements: $(ROOT_DIR)/requirements/local.txt $(ROOT_DIR)/requirements/production.txt

test: venv
	$(ACTIVATE) pytest

clean::
	@find $(ROOT_DIR) -name \*~ -exec rm '{}' +
	@find $(ROOT_DIR) -name \*.pyc -exec rm '{}' +
	@find $(ROOT_DIR) -name __pycache__ -prune -exec rm -vfr '{}' +
	@rm -rf build bdist cover dist sdist distribute-* *.egg *.egg-info
	@rm -rf *.tar.gz junit.xml coverage.xml .cache
	@rm -rf .tox .eggs .blackened .isorted
	@rm -rf venv*
	@rm -rf requirements/venv_req
	@find $(ROOT_DIR) \( -name \*.orig -o -name \*.bak -o -name \*.rej \) -exec rm '{}' +

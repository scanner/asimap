# -*- Mode: Makefile -*-
#
ACTIVATE := . venv_req/bin/activate &&

objects := $(wildcard *.in)
outputs := $(objects:.in=.txt)

all: $(outputs)

venv_req:
	@python -m venv "$@"
	@$(ACTIVATE) pip install -U pip pip-tools
	@touch "$@"

%.txt: %.in venv_req
	$(ACTIVATE) pip-compile --resolver=backtracking --strip-extras --quiet --output-file "$@" "$<"

development.txt: production.txt lint.txt

clean:
	@rm -rf venv_req

.PHONY: all clean

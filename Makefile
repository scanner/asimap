ROOT_DIR := $(shell git rev-parse --show-toplevel)
include $(ROOT_DIR)/Make.rules

.PHONY: clean lint test mypy

test: venv
	$(ACTIVATE) pytest

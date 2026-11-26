# The web UI build goes through loadgov like any compile (ui/README.md).
PROJECT ?= infrastructure-load-management

.DEFAULT_GOAL := help
.PHONY: help ui ui-install ui-test ui-lint

help:
	@echo "targets: ui (build), ui-install (npm ci), ui-test, ui-lint; all run through loadctl"

ui-install:
	loadctl run --kind compile --project $(PROJECT) --label ui-install -- npm --prefix ui ci

ui:
	loadctl run --kind compile --project $(PROJECT) --label ui-build -- npm --prefix ui run build

ui-test:
	loadctl run --kind light --project $(PROJECT) --label ui-test -- npm --prefix ui test

ui-lint:
	loadctl run --kind light --project $(PROJECT) --label ui-lint -- npm --prefix ui run lint

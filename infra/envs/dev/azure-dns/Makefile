ROOT ?= $(abspath ../../../..)
include ../../../mk/network.mk

CAPTURE_DATE ?= 2026-10-23
START_UTC ?=
.PHONY: proposal
proposal:
	cd $(ROOT)
	source scripts/dev-env.sh
	python3 scripts/dns_proposal.py --plan "$(CURDIR)/$(PLAN)" --start-utc "$(START_UTC)" --capture-date "$(CAPTURE_DATE)" --out "$(CURDIR)/$(PROPOSAL)"

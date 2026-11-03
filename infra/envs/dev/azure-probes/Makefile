ROOT ?= $(abspath ../../../..)
include ../../../mk/network.mk

METER_MANIFEST ?= meters.json
PREFLIGHT ?= preflight.json
START_UTC ?=
.PHONY: proposal
proposal:
	cd $(ROOT)
	source scripts/dev-env.sh
	python3 scripts/probe_proposal.py --plan "$(CURDIR)/$(PLAN)" --meter-manifest "$(abspath $(METER_MANIFEST))" --preflight "$(abspath $(PREFLIGHT))" --start-utc "$(START_UTC)" --out "$(CURDIR)/$(PROPOSAL)"

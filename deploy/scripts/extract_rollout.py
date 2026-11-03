"""Print the Rollout and AnalysisTemplate from a rendered ingest chart (stdin path arg)."""
import sys

import yaml

def block(dumper, s):
    return dumper.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None)


yaml.SafeDumper.add_representer(str, block)
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d.get("kind") in ("Rollout", "AnalysisTemplate")]
print("# Rendered from deploy/charts/ingest by `make deploy-check UPDATE=1`; do not edit by hand.")
print("# Image tags are the check placeholders; real tags come from each desired-state revision.")
sys.stdout.write(yaml.safe_dump_all(docs, sort_keys=False, allow_unicode=True))

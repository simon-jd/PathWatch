"""Write kubeconform JSON schemas for every CRD in the given manifests.

Usage: python -I crd_schemas.py OUT_DIR manifest.yaml...
Output layout matches kubeconform's -schema-location
'OUT_DIR/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'.
"""
import json
import os
import sys

import yaml


def strict(schema):
    """Reject unknown fields like kubeconform's -strict does for built-in kinds."""
    if isinstance(schema, dict):
        if schema.get("type") == "object" and "properties" in schema and not schema.get("x-kubernetes-preserve-unknown-fields"):
            schema.setdefault("additionalProperties", False)
        for v in schema.values():
            strict(v)
    elif isinstance(schema, list):
        for v in schema:
            strict(v)
    return schema


def main(out, paths):
    n = 0
    for path in paths:
        with open(path) as f:
            for doc in yaml.safe_load_all(f):
                if not doc or doc.get("kind") != "CustomResourceDefinition":
                    continue
                spec = doc["spec"]
                group, kind = spec["group"], spec["names"]["kind"].lower()
                for v in spec["versions"]:
                    schema = strict(v["schema"]["openAPIV3Schema"])
                    os.makedirs(os.path.join(out, group), exist_ok=True)
                    with open(os.path.join(out, group, f"{kind}_{v['name']}.json"), "w") as w:
                        json.dump(schema, w)
                    n += 1
    print(f"wrote {n} CRD schemas to {out}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])

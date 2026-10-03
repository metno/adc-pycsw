"""
Build tests/fixtures/stac_fixtures.json from the live STAC endpoint.

Usage:
    STAC_URL=https://test.wps.met.no/stac \
    .venv/bin/python tests/build_stac_fixtures.py
"""

import json
import os
import sys
from pathlib import Path

import requests

STAC_URL = os.environ.get("STAC_URL", "https://test.wps.met.no/stac").rstrip("/")
OUT_PATH = Path(__file__).parent / "fixtures" / "stac_fixtures.json"


def fetch(path: str, **params) -> dict:
    url = f"{STAC_URL}{path}"
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def build():
    print(f"Connecting to {STAC_URL} …")

    # Landing page — capture stac_version and required link rels
    landing = fetch("/")
    print(f"  stac_version={landing.get('stac_version')}  id={landing.get('id')}")

    # Conformance
    conformance = fetch("/conformance")
    conforms_to = conformance.get("conformsTo", [])

    # Collections
    colls_resp = fetch("/collections")
    raw_collections = colls_resp.get("collections", [])
    print(f"  {len(raw_collections)} collections found")

    collections = []
    for coll in raw_collections:
        cid = coll["id"]
        title = coll.get("title", "")
        extent = coll.get("extent", {})
        spatial = extent.get("spatial", {}).get("bbox", [[]])[0]
        temporal_interval = extent.get("temporal", {}).get("interval", [[None, None]])[0]

        # Fetch first item for this collection
        items_resp = fetch(f"/collections/{cid}/items", limit=1)
        features = items_resp.get("features", [])
        sample_item_id = features[0]["id"] if features else None
        item_count = items_resp.get("numberMatched", len(features))

        record = {
            "id": cid,
            "title": title,
            "bbox": spatial,
            "temporal_start": temporal_interval[0],
            "temporal_end": temporal_interval[1] if len(temporal_interval) > 1 else None,
            "sample_item_id": sample_item_id,
            "item_count": item_count,
        }
        collections.append(record)
        print(f"    {cid[:50]}  items={item_count}  sample={sample_item_id}")

    fixture = {
        "stac_url": STAC_URL,
        "stac_version": landing.get("stac_version"),
        "conforms_to": conforms_to,
        "collections": collections,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(fixture, indent=2, ensure_ascii=False))
    print(f"\nWrote {len(collections)} collections → {OUT_PATH}")


if __name__ == "__main__":
    try:
        build()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

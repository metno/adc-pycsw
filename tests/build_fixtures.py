#!/usr/bin/env python3
"""
Extract ground-truth records from Solr and write tests/fixtures/test_records.json.

Each record in the fixture has a known spatial extent, time range, and keywords,
so CSW tests can query for them and assert their presence in results.

Usage:
    SOLR_URL=https://metsis-solr.met.no/solr/devcore \
    SOLR_USER=drupal \
    SOLR_PASS=secret \
    .venv/bin/python tests/build_fixtures.py

Optional flags:
    --target N            number of records to collect (default 50)
    --collections A,B,C   comma-separated Solr collections (default: ADC)
    --out PATH            output path (default tests/fixtures/test_records.json)
"""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth
from shapely import wkt as shapely_wkt


FETCH_FIELDS = [
    "metadata_identifier",
    "title",
    "abstract",
    "geometry_wkt",
    "temporal_extent_start_date",
    "temporal_extent_end_date",
    "collection",
    "keywords_vocabulary",
    "keywords_gcmd",
    "keywords_cfstdn",
    "keywords_gemet",
    "keywords_northemes",
    "keywords_gcmdprov",
    "keywords_gcmdloc",
    "keywords_gcmdplt",
    "keywords_gcmdinst",
]

# Fields that must be present and non-empty for a record to be usable.
# temporal_extent_end_date is optional — many ongoing datasets omit it.
REQUIRED = [
    "metadata_identifier",
    "geometry_wkt",
    "temporal_extent_start_date",
    "title",
]

_STOPWORDS = {
    "a", "an", "the", "of", "in", "for", "on", "and", "to", "with",
    "by", "at", "from", "is", "are", "was", "be", "this", "that", "it",
    "as", "or", "data", "dataset", "measurements", "observations",
}

# Minimum bbox side length in degrees; below this we treat the geometry as a point
_MIN_SIDE = 0.001
# Buffer applied to point/near-point geometries for query bbox
_POINT_BUFFER = 0.5


def _bbox_from_wkt(wkt_str: str) -> tuple[list[float], bool] | tuple[None, None]:
    """
    Return ([west, south, east, north], is_point) or (None, None) on parse failure.
    Points are buffered so they remain usable in spatial CSW queries.
    """
    try:
        geom = shapely_wkt.loads(wkt_str)
        west, south, east, north = geom.bounds
        is_point = (east - west) < _MIN_SIDE or (north - south) < _MIN_SIDE
        if is_point:
            west -= _POINT_BUFFER
            south -= _POINT_BUFFER
            east += _POINT_BUFFER
            north += _POINT_BUFFER
            # Clamp to valid coordinate ranges
            west = max(west, -180.0)
            south = max(south, -90.0)
            east = min(east, 180.0)
            north = min(north, 90.0)
        return [round(west, 6), round(south, 6), round(east, 6), round(north, 6)], is_point
    except Exception:
        return None, None


def _collect_keywords(doc: dict) -> list[str]:
    """Flatten all keywords_* list fields into a deduplicated list."""
    kws: list[str] = []
    vocab_fields = [
        "keywords_gcmd", "keywords_cfstdn", "keywords_gemet",
        "keywords_northemes", "keywords_gcmdprov", "keywords_gcmdloc",
        "keywords_gcmdplt", "keywords_gcmdinst",
    ]
    for field in vocab_fields:
        for kw in doc.get(field, []):
            if kw and kw not in kws:
                kws.append(kw)
    return kws


def _anytext_sample(doc: dict) -> str:
    """
    Pick the most distinctive ASCII word from the title (or abstract) that
    is likely to appear in the Solr full_text copy field.

    full_text is a copyField destination — indexed but not stored, so it is
    never returned in Solr results.  We derive candidates from the stored
    title and abstract fields, which are both source fields for full_text.
    """
    title = doc.get("title", "")
    abstract = doc.get("abstract", "") or ""

    candidates = [
        w for w in re.split(r"[\s,;:()/\-_\[\]]+", title)
        if w.isascii() and w.isalpha() and len(w) > 5 and w.lower() not in _STOPWORDS
    ]
    if not candidates:
        candidates = [w for w in re.split(r"\W+", title) if w.isascii() and len(w) > 3]

    if not candidates:
        return ""

    # Prefer words that also appear in the abstract (stronger evidence they're
    # indexed in full_text and specific enough to be useful as a search term).
    abstract_lower = abstract.lower()
    in_abstract = [w for w in candidates if w.lower() in abstract_lower]
    if in_abstract:
        return max(in_abstract, key=len)
    return max(candidates, key=len)


def _has_required_fields(doc: dict) -> bool:
    for field in REQUIRED:
        val = doc.get(field)
        if not val:
            return False
        if isinstance(val, list) and not val[0]:
            return False
    return True


def _normalise_date(val) -> str:
    v = val[0] if isinstance(val, list) else val
    return v[:10] if len(v) > 10 else v


def fetch_from_collection(
    solr_select: str,
    auth: HTTPBasicAuth,
    collection: str,
    limit: int,
    seen_ids: set[str],
) -> list[dict]:
    """Query Solr for up to `limit` usable, previously-unseen records from one collection."""
    seed = int(hashlib.md5(collection.encode()).hexdigest()[:8], 16) % 1_000_000
    params = {
        "q": "*:*",
        "q.op": "OR",
        "fq": [
            "metadata_status:Active",
            f"collection:{collection}",
        ],
        "fl": ",".join(FETCH_FIELDS),
        "rows": max(limit * 15, 500),
        "sort": f"random_{seed} asc",
    }

    try:
        resp = requests.get(solr_select, params=params, auth=auth, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"  [WARN] Solr request failed for collection {collection}: {exc}")
        return []

    docs = resp.json()["response"]["docs"]
    results = []
    for doc in docs:
        if doc.get("metadata_identifier") in seen_ids:
            continue
        if not _has_required_fields(doc):
            continue
        bbox, is_point = _bbox_from_wkt(doc["geometry_wkt"])
        if bbox is None:
            continue

        t_begin_raw = doc.get("temporal_extent_start_date")
        if not t_begin_raw:
            continue
        t_end_raw = doc.get("temporal_extent_end_date")

        record = {
            "identifier": doc["metadata_identifier"],
            "title": doc.get("title", ""),
            "collection": collection,
            "bbox": bbox,
            "bbox_is_point": is_point,
            "time_begin": _normalise_date(t_begin_raw),
            "time_end": _normalise_date(t_end_raw) if t_end_raw else None,
            "keywords": _collect_keywords(doc),
            "anytext_sample": _anytext_sample(doc),
        }
        results.append(record)
        if len(results) >= limit:
            break

    return results


def build_fixtures(
    solr_url: str,
    user: str,
    password: str,
    collections: list[str],
    target: int,
    out_path: Path,
    skip_ids: set[str] | None = None,
) -> None:
    solr_select = solr_url.rstrip("/") + "/select"
    auth = HTTPBasicAuth(user, password)

    all_records: list[dict] = []
    seen_ids: set[str] = set(skip_ids or [])

    # Distribute target evenly across collections; remainder goes to first collection
    per_coll = max(1, target // len(collections))
    remainder = target - per_coll * len(collections)

    for i, collection in enumerate(collections):
        coll_limit = per_coll + (remainder if i == 0 else 0)
        print(f"Fetching collection {collection} (want {coll_limit}) ...")
        records = fetch_from_collection(solr_select, auth, collection, coll_limit, seen_ids)
        for rec in records:
            seen_ids.add(rec["identifier"])
            all_records.append(rec)
        print(f"  -> {len(records)} records added (total: {len(all_records)})")

    if not all_records:
        print("ERROR: no records collected. Check SOLR_URL and credentials.")
        sys.exit(1)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(all_records, indent=2, ensure_ascii=False))
    print(f"\nWrote {len(all_records)} records to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build CSW test fixture from Solr")
    parser.add_argument("--target", type=int, default=50)
    parser.add_argument(
        "--collections",
        type=lambda s: [c.strip() for c in s.split(",")],
        default=["ADC"],
        help="Comma-separated Solr collection names (default: ADC)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent / "fixtures" / "test_records.json",
    )
    parser.add_argument(
        "--skip-ids",
        type=lambda s: {i.strip() for i in s.split(",") if i.strip()},
        default=set(),
        help="Comma-separated identifiers to exclude from the fixture",
    )
    args = parser.parse_args()

    solr_url = os.environ.get("SOLR_URL", "").strip()
    user = os.environ.get("SOLR_USER", "").strip()
    password = os.environ.get("SOLR_PASS", "").strip()

    if not solr_url:
        print("ERROR: set SOLR_URL environment variable")
        sys.exit(1)

    build_fixtures(
        solr_url=solr_url,
        user=user,
        password=password,
        collections=args.collections,
        target=args.target,
        out_path=args.out,
        skip_ids=args.skip_ids,
    )


if __name__ == "__main__":
    main()

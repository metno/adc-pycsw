"""
STAC API — Collections tests.

Covers:
  GET /stac/collections              — collection list
  GET /stac/collections/{id}         — single collection
  GET /stac/collections/{nonexistent} — invalid ID → 4xx

Spec reference: https://api.stacspec.org/v1.0.0/ogcapi-features
"""

import pytest
from .conftest import STAC_URL

REQUIRED_COLLECTION_FIELDS = {"id", "type", "links", "extent"}
REQUIRED_EXTENT_KEYS = {"spatial", "temporal"}


# ---------------------------------------------------------------------------
# Collection list
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def collections_response(stac_session):
    resp = stac_session.get(f"{STAC_URL}/collections")
    resp.raise_for_status()
    return resp.json()


def test_collections_status(stac_session):
    resp = stac_session.get(f"{STAC_URL}/collections")
    assert resp.status_code == 200


def test_collections_is_nonempty_list(collections_response):
    colls = collections_response.get("collections", [])
    assert isinstance(colls, list) and len(colls) > 0


def test_collections_count_matches_fixture(collections_response, stac_collections):
    assert len(collections_response["collections"]) == len(stac_collections)


# ---------------------------------------------------------------------------
# Per-collection structural tests (parametrised)
# ---------------------------------------------------------------------------

@pytest.fixture
def collection_record(stac_collections, collection_index):
    return stac_collections[collection_index]


@pytest.fixture
def single_collection(stac_session, collection_record):
    cid = collection_record["id"]
    resp = stac_session.get(f"{STAC_URL}/collections/{cid}")
    resp.raise_for_status()
    return resp.json()


def test_single_collection_status(stac_session, collection_record):
    cid = collection_record["id"]
    resp = stac_session.get(f"{STAC_URL}/collections/{cid}")
    assert resp.status_code == 200


def test_single_collection_required_fields(single_collection):
    missing = REQUIRED_COLLECTION_FIELDS - single_collection.keys()
    assert not missing, f"Missing fields: {missing}"


def test_single_collection_id_matches(single_collection, collection_record):
    assert single_collection["id"] == collection_record["id"]


def test_single_collection_has_extent(single_collection):
    extent = single_collection.get("extent", {})
    missing = REQUIRED_EXTENT_KEYS - extent.keys()
    assert not missing, f"Missing extent keys: {missing}"


def test_single_collection_spatial_bbox(single_collection):
    bbox = single_collection["extent"]["spatial"].get("bbox", [])
    assert len(bbox) > 0 and len(bbox[0]) == 4, "spatial.bbox must be [[minx,miny,maxx,maxy]]"


def test_single_collection_temporal_interval(single_collection):
    interval = single_collection["extent"]["temporal"].get("interval", [])
    assert len(interval) > 0 and len(interval[0]) == 2


def test_single_collection_has_items_link(single_collection):
    rels = {link["rel"] for link in single_collection.get("links", [])}
    assert "items" in rels, "Collection must have a link with rel=items"


def test_single_collection_has_self_link(single_collection):
    rels = {link["rel"] for link in single_collection.get("links", [])}
    assert "self" in rels


# ---------------------------------------------------------------------------
# Invalid collection
# ---------------------------------------------------------------------------

def test_invalid_collection_returns_4xx(stac_session):
    resp = stac_session.get(f"{STAC_URL}/collections/does-not-exist-xyz")
    assert resp.status_code in (400, 404), (
        f"Expected 4xx for unknown collection, got {resp.status_code}"
    )

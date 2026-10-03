"""
STAC API — Items tests.

Covers:
  GET /stac/collections/{id}/items          — item list (FeatureCollection)
  GET /stac/collections/{id}/items?limit=N  — pagination
  GET /stac/collections/{id}/items/{itemid} — single item

Spec reference: https://api.stacspec.org/v1.0.0/ogcapi-features
"""

import pytest
from .conftest import STAC_URL

REQUIRED_ITEM_FIELDS = {"id", "type", "stac_version", "geometry", "bbox", "properties", "links"}
REQUIRED_ITEM_LINK_RELS = {"self", "root", "parent", "collection"}


# ---------------------------------------------------------------------------
# Helpers / parametrised fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def collection_record(stac_collections, collection_index):
    return stac_collections[collection_index]


@pytest.fixture
def items_response(stac_session, collection_record):
    cid = collection_record["id"]
    resp = stac_session.get(f"{STAC_URL}/collections/{cid}/items", params={"limit": 5})
    resp.raise_for_status()
    return resp.json()


def _skip_if_only_subcollections(collection_record):
    # Items are non-parent records (isParent:false). A collection whose
    # children are all sub-collections (isParent and isChild, e.g. in adc2)
    # has no items of its own; the fixture records item_count 0 for it.
    if collection_record["item_count"] == 0:
        pytest.skip(
            f"Collection {collection_record['id']} has no items of its own "
            "(only sub-collections)"
        )


@pytest.fixture
def first_item(items_response, collection_record):
    _skip_if_only_subcollections(collection_record)
    features = items_response.get("features", [])
    assert features, "No items returned — cannot run item-level tests"
    return features[0]


# ---------------------------------------------------------------------------
# Items list
# ---------------------------------------------------------------------------

def test_items_status(stac_session, collection_record):
    cid = collection_record["id"]
    resp = stac_session.get(f"{STAC_URL}/collections/{cid}/items")
    assert resp.status_code == 200


def test_items_is_feature_collection(items_response):
    assert items_response.get("type") == "FeatureCollection"


def test_items_features_is_list(items_response):
    assert isinstance(items_response.get("features"), list)


def test_items_returns_nonzero(items_response, collection_record):
    _skip_if_only_subcollections(collection_record)
    assert len(items_response["features"]) > 0, (
        f"Collection {collection_record['id']} returned 0 items"
    )


def test_items_number_matched(items_response, collection_record):
    """numberMatched must equal the fixture item count (collection hasn't changed)."""
    matched = items_response.get("numberMatched")
    if matched is None:
        pytest.skip("numberMatched not present in response")
    assert matched == collection_record["item_count"], (
        f"Expected {collection_record['item_count']} items, got {matched}"
    )


def test_items_limit_respected(stac_session, collection_record):
    cid = collection_record["id"]
    resp = stac_session.get(
        f"{STAC_URL}/collections/{cid}/items", params={"limit": 2}
    )
    resp.raise_for_status()
    features = resp.json().get("features", [])
    assert len(features) <= 2


# ---------------------------------------------------------------------------
# Item structure (first item of each collection)
# ---------------------------------------------------------------------------

def test_item_required_fields(first_item):
    missing = REQUIRED_ITEM_FIELDS - first_item.keys()
    assert not missing, f"Missing STAC item fields: {missing}"


def test_item_type_is_feature(first_item):
    assert first_item["type"] == "Feature"


def test_item_stac_version(first_item):
    assert first_item.get("stac_version"), "stac_version must be non-empty"


def test_item_geometry_is_geojson(first_item):
    geom = first_item.get("geometry")
    assert geom is not None, "geometry must not be null"
    assert "type" in geom and "coordinates" in geom


def test_item_bbox_is_four_numbers(first_item):
    bbox = first_item.get("bbox", [])
    assert len(bbox) == 4, f"bbox must have 4 elements, got {bbox}"
    assert all(isinstance(v, (int, float)) for v in bbox)


def test_item_has_required_link_rels(first_item):
    rels = {link["rel"] for link in first_item.get("links", [])}
    missing = REQUIRED_ITEM_LINK_RELS - rels
    assert not missing, f"Missing link rels: {missing}"


def test_item_properties_has_temporal(first_item):
    props = first_item.get("properties", {})
    has_datetime = "datetime" in props
    has_interval = "start_datetime" in props or "end_datetime" in props
    assert has_datetime or has_interval, (
        "Item properties must include datetime or start_datetime/end_datetime"
    )


def test_item_properties_has_title(first_item):
    assert first_item.get("properties", {}).get("title"), "Item must have a non-empty title"


# ---------------------------------------------------------------------------
# Single item fetch
# ---------------------------------------------------------------------------

def test_single_item_status(stac_session, collection_record):
    cid = collection_record["id"]
    item_id = collection_record["sample_item_id"]
    if not item_id:
        pytest.skip("No sample_item_id in fixture")
    resp = stac_session.get(f"{STAC_URL}/collections/{cid}/items/{item_id}")
    assert resp.status_code == 200


def test_single_item_type(stac_session, collection_record):
    cid = collection_record["id"]
    item_id = collection_record["sample_item_id"]
    if not item_id:
        pytest.skip("No sample_item_id in fixture")
    resp = stac_session.get(f"{STAC_URL}/collections/{cid}/items/{item_id}")
    resp.raise_for_status()
    assert resp.json().get("type") == "Feature"


def test_single_item_id_matches(stac_session, collection_record):
    cid = collection_record["id"]
    item_id = collection_record["sample_item_id"]
    if not item_id:
        pytest.skip("No sample_item_id in fixture")
    resp = stac_session.get(f"{STAC_URL}/collections/{cid}/items/{item_id}")
    resp.raise_for_status()
    assert resp.json().get("id") == item_id


def test_invalid_item_returns_4xx(stac_session, collection_record):
    cid = collection_record["id"]
    resp = stac_session.get(f"{STAC_URL}/collections/{cid}/items/does-not-exist-xyz")
    assert resp.status_code in (400, 404), (
        f"Expected 4xx for unknown item, got {resp.status_code}"
    )

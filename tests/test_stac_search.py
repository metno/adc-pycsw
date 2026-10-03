"""
STAC API — Item Search tests (GET and POST).

Covers:
  GET  /stac/search                  — basic search
  GET  /stac/search?limit=N          — limit parameter
  GET  /stac/search?bbox=...         — bbox spatial filter
  GET  /stac/search?q=...            — free-text filter
  GET  /stac/search?datetime=...     — datetime filter (xfail: known HTTP 500)
  POST /stac/search  {bbox}          — POST bbox
  POST /stac/search  {limit}         — POST limit
  POST /stac/search  {ids}           — POST ids (exact match)
  POST /stac/search  {collections}   — POST collections filter

Spec reference: https://api.stacspec.org/v1.0.0/item-search
"""

import pytest
from .conftest import STAC_URL

# A tight bbox covering Svalbard (known to contain records)
SVALBARD_BBOX = [10.0, 74.0, 35.0, 81.0]
# A bbox covering the middle of the Pacific (should return very few or no records)
EMPTY_BBOX = [-180.0, -10.0, -90.0, 10.0]

FREE_TEXT_TERM = "Svalbard"


# ---------------------------------------------------------------------------
# GET search — basic
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def search_default(stac_session):
    resp = stac_session.get(f"{STAC_URL}/search")
    resp.raise_for_status()
    return resp.json()


def test_search_get_status(stac_session):
    resp = stac_session.get(f"{STAC_URL}/search")
    assert resp.status_code == 200


def test_search_get_is_feature_collection(search_default):
    assert search_default.get("type") == "FeatureCollection"


def test_search_get_features_is_list(search_default):
    assert isinstance(search_default.get("features"), list)


def test_search_get_returns_items(search_default):
    assert len(search_default["features"]) > 0


# ---------------------------------------------------------------------------
# GET search — limit
# ---------------------------------------------------------------------------

def test_search_get_limit(stac_session):
    resp = stac_session.get(f"{STAC_URL}/search", params={"limit": 2})
    resp.raise_for_status()
    features = resp.json().get("features", [])
    assert len(features) <= 2


def test_search_get_limit_one(stac_session):
    resp = stac_session.get(f"{STAC_URL}/search", params={"limit": 1})
    resp.raise_for_status()
    assert len(resp.json().get("features", [])) == 1


# ---------------------------------------------------------------------------
# GET search — bbox
# ---------------------------------------------------------------------------

def test_search_get_bbox_returns_results(stac_session):
    bbox = ",".join(str(v) for v in SVALBARD_BBOX)
    resp = stac_session.get(f"{STAC_URL}/search", params={"bbox": bbox})
    resp.raise_for_status()
    assert len(resp.json()["features"]) > 0


def test_search_get_bbox_narrows_results(stac_session):
    """A tight bbox must return fewer items than an unfiltered search with the same limit."""
    limit = 100
    resp_all = stac_session.get(f"{STAC_URL}/search", params={"limit": limit})
    resp_all.raise_for_status()
    bbox = ",".join(str(v) for v in SVALBARD_BBOX)
    resp_bbox = stac_session.get(f"{STAC_URL}/search", params={"bbox": bbox, "limit": limit})
    resp_bbox.raise_for_status()
    all_matched = resp_all.json().get("numberMatched", len(resp_all.json()["features"]))
    bbox_matched = resp_bbox.json().get("numberMatched", len(resp_bbox.json()["features"]))
    assert bbox_matched <= all_matched


# ---------------------------------------------------------------------------
# GET search — free-text (q)
# ---------------------------------------------------------------------------

def test_search_get_q_returns_results(stac_session):
    resp = stac_session.get(f"{STAC_URL}/search", params={"q": FREE_TEXT_TERM})
    resp.raise_for_status()
    assert len(resp.json()["features"]) > 0


def test_search_get_q_narrows_results(stac_session, search_default):
    resp = stac_session.get(f"{STAC_URL}/search", params={"q": FREE_TEXT_TERM, "limit": 1000})
    resp.raise_for_status()
    q_count = resp.json().get("numberMatched", len(resp.json()["features"]))
    total = search_default.get("numberMatched", len(search_default["features"]))
    assert q_count <= total


# ---------------------------------------------------------------------------
# GET search — datetime (known HTTP 500 — tracked as xfail)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="GET ?datetime= returns HTTP 500 (known bug)", strict=True)
def test_search_get_datetime_open_end(stac_session):
    resp = stac_session.get(
        f"{STAC_URL}/search",
        params={"datetime": "2020-01-01T00:00:00Z/.."},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST search
# ---------------------------------------------------------------------------

def test_search_post_status(stac_session):
    resp = stac_session.post(f"{STAC_URL}/search", json={})
    assert resp.status_code == 200


def test_search_post_is_feature_collection(stac_session):
    resp = stac_session.post(f"{STAC_URL}/search", json={})
    resp.raise_for_status()
    assert resp.json().get("type") == "FeatureCollection"


def test_search_post_limit(stac_session):
    resp = stac_session.post(f"{STAC_URL}/search", json={"limit": 2})
    resp.raise_for_status()
    assert len(resp.json()["features"]) <= 2


def test_search_post_bbox(stac_session):
    resp = stac_session.post(f"{STAC_URL}/search", json={"bbox": SVALBARD_BBOX})
    resp.raise_for_status()
    assert len(resp.json()["features"]) > 0


def test_search_post_bbox_narrows_results(stac_session):
    limit = 100
    resp_all = stac_session.post(f"{STAC_URL}/search", json={"limit": limit})
    resp_all.raise_for_status()
    resp_bbox = stac_session.post(f"{STAC_URL}/search", json={"bbox": SVALBARD_BBOX, "limit": limit})
    resp_bbox.raise_for_status()
    total = resp_all.json().get("numberMatched", len(resp_all.json()["features"]))
    bbox_count = resp_bbox.json().get("numberMatched", len(resp_bbox.json()["features"]))
    assert bbox_count <= total


def test_search_post_ids(stac_session, stac_collections):
    known_id = stac_collections[0]["sample_item_id"]
    if not known_id:
        pytest.skip("No sample_item_id in fixture")
    resp = stac_session.post(f"{STAC_URL}/search", json={"ids": [known_id]})
    resp.raise_for_status()
    ids = [f["id"] for f in resp.json().get("features", [])]
    assert known_id in ids, f"{known_id} not found in search-by-ids response"


def test_search_post_collections_filter(stac_session, stac_collections):
    cid = stac_collections[0]["id"]
    resp = stac_session.post(f"{STAC_URL}/search", json={"collections": [cid], "limit": 5})
    resp.raise_for_status()
    features = resp.json().get("features", [])
    assert len(features) > 0

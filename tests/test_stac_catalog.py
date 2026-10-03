"""
STAC API — Catalog / landing page and conformance tests.

Covers:
  GET /stac/          — STAC catalog object
  GET /stac/conformance — conformance classes

Spec reference: https://api.stacspec.org/v1.0.0/core
"""

import re
import pytest
from .conftest import STAC_URL

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+")

REQUIRED_LANDING_LINKS = {"self", "root", "conformance", "data", "search"}

REQUIRED_CONFORMANCE_CLASSES = [
    "https://api.stacspec.org/v1.0.0/core",
    "https://api.stacspec.org/v1.0.0/item-search",
    "https://api.stacspec.org/v1.0.0/ogcapi-features",
]


# ---------------------------------------------------------------------------
# Landing page
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def landing(stac_session):
    resp = stac_session.get(f"{STAC_URL}/")
    resp.raise_for_status()
    return resp.json()


def test_landing_page_status(stac_session):
    resp = stac_session.get(f"{STAC_URL}/")
    assert resp.status_code == 200


def test_landing_page_type(landing):
    assert landing.get("type") == "Catalog"


def test_landing_page_stac_version(landing):
    version = landing.get("stac_version", "")
    assert SEMVER_RE.match(version), f"stac_version not semver: {version!r}"


def test_landing_page_has_id(landing):
    assert "id" in landing and landing["id"]


def test_landing_page_required_links(landing):
    rels = {link["rel"] for link in landing.get("links", [])}
    missing = REQUIRED_LANDING_LINKS - rels
    assert not missing, f"Missing link rels: {missing}"


def test_landing_page_conforms_to(landing):
    conforms_to = landing.get("conformsTo", [])
    assert isinstance(conforms_to, list)
    assert len(conforms_to) > 0


# ---------------------------------------------------------------------------
# Conformance
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def conformance(stac_session):
    resp = stac_session.get(f"{STAC_URL}/conformance")
    resp.raise_for_status()
    return resp.json()


def test_conformance_status(stac_session):
    resp = stac_session.get(f"{STAC_URL}/conformance")
    assert resp.status_code == 200


def test_conformance_is_list(conformance):
    assert isinstance(conformance.get("conformsTo"), list)


@pytest.mark.parametrize("cls", REQUIRED_CONFORMANCE_CLASSES)
def test_conformance_contains_required_class(conformance, cls):
    assert cls in conformance["conformsTo"], (
        f"Missing conformance class: {cls}"
    )


def test_conformance_matches_landing(landing, conformance):
    """conformsTo in the landing page must equal /conformance."""
    assert set(landing.get("conformsTo", [])) == set(conformance["conformsTo"])

"""
Shared fixtures and helpers for the CSW and STAC test suites.

Run against the live test endpoint:
    .venv/bin/python -m pytest tests/ -v

Known limitations in the current plugin (pycsw repo-abstract + SolrMETNORepository):
  - And(comparison, comparison) fails — only And(BBox, text-filter) is accepted
  - Some AnyText literals cause "Invalid query syntax" (upstream pygeofilter issue)
  - STAC datetime search (GET ?datetime=) returns HTTP 500 (tracked as xfail)
"""

import json
import os
import pytest
import requests as requests_lib
from pathlib import Path
from owslib.csw import CatalogueServiceWeb

# Endpoint URLs — env-overridable so the same test suite can hit the live
# MET test deployment, a sibling CI container, or a localhost dev server
# without rebuilding the image.
CSW_URL = os.environ.get("CSW_URL", "https://test.wps.met.no/csw")
STAC_URL = os.environ.get("STAC_URL", "https://test.wps.met.no/stac")

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "test_records.json"
STAC_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "stac_fixtures.json"

# How many records to paginate through before giving up on finding an identifier
MAX_SEARCH_RECORDS = 100
PAGE_SIZE = 20


@pytest.fixture(scope="session")
def csw():
    """Shared OWSLib CSW 2.0.2 client for the entire test session."""
    try:
        client = CatalogueServiceWeb(CSW_URL, version="2.0.2", timeout=30)
    except Exception as exc:
        pytest.skip(f"CSW endpoint unreachable at {CSW_URL}: {exc}")
    return client


@pytest.fixture(scope="session")
def stac_session():
    """Shared requests.Session for STAC tests. Skips if endpoint is unreachable."""
    session = requests_lib.Session()
    session.headers["Accept"] = "application/json"
    try:
        resp = session.get(f"{STAC_URL}/", timeout=10)
        resp.raise_for_status()
    except Exception as exc:
        pytest.skip(f"STAC endpoint unreachable at {STAC_URL}: {exc}")
    return session


@pytest.fixture(scope="session")
def stac_fixture() -> dict:
    """Full STAC fixture document (stac_fixtures.json)."""
    return json.loads(STAC_FIXTURE_PATH.read_text())


@pytest.fixture(scope="session")
def stac_collections(stac_fixture) -> list[dict]:
    """List of collection records from the STAC fixture."""
    return stac_fixture["collections"]


def pytest_generate_tests(metafunc):
    """Dynamically parametrise test indices from fixture files."""
    # CSW parametrisation
    records = json.loads(FIXTURE_PATH.read_text())
    if "record_index" in metafunc.fixturenames:
        metafunc.parametrize(
            "record_index", range(len(records)), ids=lambda i: f"rec{i:02d}"
        )
    if "polygon_index" in metafunc.fixturenames:
        n = sum(1 for r in records if not r["bbox_is_point"])
        metafunc.parametrize(
            "polygon_index", range(n), ids=lambda i: f"polygon{i:02d}"
        )

    # STAC parametrisation
    if "collection_index" in metafunc.fixturenames:
        stac = json.loads(STAC_FIXTURE_PATH.read_text())
        n = len(stac["collections"])
        metafunc.parametrize(
            "collection_index", range(n), ids=lambda i: f"coll{i:02d}"
        )


@pytest.fixture(scope="session")
def all_records() -> list[dict]:
    """All ground-truth records from the fixture file."""
    return json.loads(FIXTURE_PATH.read_text())


@pytest.fixture(scope="session")
def polygon_records(all_records) -> list[dict]:
    """Subset of records whose geometry is a true polygon (not a buffered point)."""
    return [r for r in all_records if not r["bbox_is_point"]]


@pytest.fixture(scope="session")
def records_with_time_end(all_records) -> list[dict]:
    """Subset of records that have a known time_end (bounded temporal extent)."""
    return [r for r in all_records if r["time_end"] is not None]


def find_identifier(
    csw_client,
    constraints: list,
    identifier: str,
    max_records: int = MAX_SEARCH_RECORDS,
    page_size: int = PAGE_SIZE,
) -> tuple[bool, int]:
    """
    Paginate through CSW results looking for a specific record identifier.

    Returns:
        (found, total) — whether the identifier appeared and total matched records.
    """
    start = 1
    total = 0
    while start <= max_records:
        csw_client.getrecords2(
            constraints=constraints,
            maxrecords=page_size,
            startposition=start,
            esn="summary",
        )
        total = csw_client.results.get("matches", 0)
        if identifier in csw_client.records:
            return True, total
        returned = csw_client.results.get("returned", 0)
        if returned == 0:
            break
        # Advance by the actual returned count (server may cap below page_size)
        start += returned
    return False, total

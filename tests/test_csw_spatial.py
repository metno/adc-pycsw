"""
Spatial CSW query tests — BBox filter (OGC BBOX / INTERSECTS).

Each test queries with a record's known bounding box and asserts the record
appears within the first MAX_SEARCH_RECORDS results.  The test is marked slow
because it may paginate through several pages to find the target.

Known behaviour: global/large datasets also INTERSECT any specific bbox, so
result sets can be large.  The plugin uses INTERSECTS semantics, not CONTAINS,
so all overlapping datasets are returned.

Fixes applied:
  - evaluate.py: spatial {!field ...} queries now go in Solr filter[] (not query
    field) — Geo3D fields return wrong counts when the spatial predicate is placed
    in the main query position.
  - evaluate.py: rectangular polygons converted to ENVELOPE format to avoid
    Geo3D "coplanar" errors for global/antimeridian-crossing bboxes.
  - evaluate.py + solr_metno.py: and_() properly merges filter arrays so that
    And([BBox, text]) places the spatial part in filter and the text part in query.
"""

import pytest
from owslib.fes import BBox
from .conftest import find_identifier, MAX_SEARCH_RECORDS



@pytest.mark.slow
def test_spatial_bbox_finds_record(csw, polygon_records, polygon_index):
    """
    GetRecords with the record's own BBox finds the record within
    the first MAX_SEARCH_RECORDS results.

    Skips when the bbox returns too many results to paginate within the limit
    (e.g. dense satellite-imagery areas with hundreds of thousands of records).
    """
    rec = polygon_records[polygon_index]
    west, south, east, north = rec["bbox"]

    # Quick count check before paginating
    csw.getrecords2(
        constraints=[BBox([west, south, east, north])],
        maxrecords=1, esn="brief",
    )
    total = csw.results["matches"]
    if total > MAX_SEARCH_RECORDS:
        pytest.skip(
            f"BBox returns {total} results (> {MAX_SEARCH_RECORDS}); "
            "too many to paginate — spatial filter is active but result set too large."
        )

    found, total = find_identifier(
        csw,
        constraints=[BBox([west, south, east, north])],
        identifier=rec["identifier"],
        max_records=MAX_SEARCH_RECORDS,
    )

    assert found, (
        f"Record '{rec['identifier']}' not found in first {MAX_SEARCH_RECORDS} "
        f"spatial results (bbox={rec['bbox']}, total matches={total}).\n"
        f"Title: {rec['title']}"
    )


def test_spatial_bbox_returns_nonzero(csw, polygon_records, polygon_index):
    """
    GetRecords with the record's BBox returns at least one match.
    This verifies the spatial filter is accepted and evaluated.
    """
    rec = polygon_records[polygon_index]
    west, south, east, north = rec["bbox"]

    csw.getrecords2(
        constraints=[BBox([west, south, east, north])],
        maxrecords=1,
        esn="brief",
    )
    total = csw.results["matches"]

    assert total > 0, (
        f"BBox query for '{rec['identifier']}' returned 0 results "
        f"(bbox={rec['bbox']}).  Spatial filter may be broken."
    )


def test_spatial_filter_is_active(csw):
    """
    Different bboxes return different result counts, confirming the spatial filter
    is being evaluated.
    """
    csw.getrecords2(constraints=[BBox([-180, -90, 180, 90])], maxrecords=1, esn="brief")
    total_global = csw.results["matches"]

    csw.getrecords2(constraints=[BBox([3.0, -55.0, 4.0, -54.0])], maxrecords=1, esn="brief")
    total_small = csw.results["matches"]

    csw.getrecords2(maxrecords=1, esn="brief")
    total_all = csw.results["matches"]

    # At least one bbox query must differ from the unfiltered total
    assert total_global != total_all or total_small != total_all, (
        f"All bbox queries return the same count as no filter ({total_all}). "
        "Spatial filter appears completely inactive."
    )
    assert total_global != total_small, (
        f"Global bbox ({total_global}) and small bbox ({total_small}) return same count. "
        "Spatial filter has no discriminating power."
    )


def test_spatial_selectivity_correct_direction(csw):
    """Global bbox returns more results than a small bbox."""
    csw.getrecords2(constraints=[BBox([-180, -90, 180, 90])], maxrecords=1, esn="brief")
    total_global = csw.results["matches"]

    csw.getrecords2(constraints=[BBox([3.0, -55.0, 4.0, -54.0])], maxrecords=1, esn="brief")
    total_small = csw.results["matches"]

    assert total_small < total_global, (
        f"Small bbox ({total_small}) >= global bbox ({total_global})."
    )

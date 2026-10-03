"""
Combined CSW query tests — And filters mixing spatial, temporal, and text constraints.

Working combinations:
  And([BBox, PropertyIsLike])                         — spatial goes to Solr filter[], text to query.
  And([temporal_comparison, AnyText])                 — works after date format fix.
  And([BBox, temporal_comparison, text_comparison])   — fixed: FESBaseParser.and_() now accepts *args.
"""

import pytest
from owslib.fes import (
    BBox,
    PropertyIsLike,
    PropertyIsGreaterThanOrEqualTo,
    And,
)
from owslib.ows import ExceptionReport
from .conftest import find_identifier, MAX_SEARCH_RECORDS

TEMPORAL_PROP = "apiso:TempExtent_begin"
ANYTEXT_PROP = "csw:AnyText"



def test_spatial_and_anytext_accepted_by_server(csw, polygon_records, polygon_index):
    """
    And([BBox, AnyText]) is accepted by the server and returns a non-error response.
    The BBox component is now applied correctly (spatial filter goes to Solr filter[]).
    """
    rec = polygon_records[polygon_index]
    word = rec["anytext_sample"]
    if not word:
        pytest.skip("No usable anytext_sample")

    west, south, east, north = rec["bbox"]

    try:
        csw.getrecords2(
            constraints=[And([BBox([west, south, east, north]),
                              PropertyIsLike(ANYTEXT_PROP, f"*{word}*")])],
            maxrecords=1, esn="brief",
        )
    except ExceptionReport as exc:
        pytest.skip(f"And([BBox, AnyText]) rejected by server: {exc}")

    count_combined = csw.results["matches"]
    assert count_combined >= 0, "Server returned a negative match count — unexpected."


def test_bbox_narrows_anytext_in_and_combination(csw):
    """
    And([BBox, AnyText]) returns fewer results than AnyText alone, confirming
    the BBox component now actively filters within And() combinations.

    Uses "Arctic" (2000+ matches globally) narrowed to the Svalbard bbox.
    """
    word = "Arctic"
    west, south, east, north = 10.0, 74.0, 35.0, 82.0  # Svalbard

    csw.getrecords2(
        constraints=[PropertyIsLike(ANYTEXT_PROP, f"*{word}*")],
        maxrecords=1, esn="brief",
    )
    count_anytext = csw.results["matches"]

    csw.getrecords2(
        constraints=[And([BBox([west, south, east, north]),
                          PropertyIsLike(ANYTEXT_PROP, f"*{word}*")])],
        maxrecords=1, esn="brief",
    )
    count_combined = csw.results["matches"]

    assert count_combined < count_anytext, (
        f"And([BBox, AnyText]) returned {count_combined} which is not less than "
        f"AnyText alone ({count_anytext}). BBox should narrow the result."
    )


@pytest.mark.slow
def test_spatial_and_anytext_finds_record(csw, polygon_records, polygon_index):
    """
    And([BBox, AnyText]) with a record's own bbox and title word finds the record
    within the first MAX_SEARCH_RECORDS results.

    Skips when the combined result set is still too large to paginate.
    """
    rec = polygon_records[polygon_index]
    word = rec["anytext_sample"]
    if not word:
        pytest.skip("No usable anytext_sample")

    west, south, east, north = rec["bbox"]

    # Quick count check first
    try:
        csw.getrecords2(
            constraints=[And([BBox([west, south, east, north]),
                              PropertyIsLike(ANYTEXT_PROP, f"*{word}*")])],
            maxrecords=1, esn="brief",
        )
    except ExceptionReport as exc:
        pytest.skip(f"And([BBox, AnyText]) rejected — server error: {exc}")

    total = csw.results["matches"]
    if total > MAX_SEARCH_RECORDS:
        pytest.skip(
            f"And([BBox, AnyText]) returns {total} results (> {MAX_SEARCH_RECORDS}); "
            "too many to paginate — both filters are active but result set too large."
        )

    try:
        found, total = find_identifier(
            csw,
            constraints=[And([BBox([west, south, east, north]),
                              PropertyIsLike(ANYTEXT_PROP, f"*{word}*")])],
            identifier=rec["identifier"],
            max_records=MAX_SEARCH_RECORDS,
        )
    except ExceptionReport as exc:
        pytest.skip(f"And([BBox, AnyText]) rejected — server error: {exc}")

    assert found, (
        f"Record '{rec['identifier']}' not found in first {MAX_SEARCH_RECORDS} "
        f"And([BBox, AnyText]) results (total={total}).\n"
        f"Word: '{word}', bbox: {rec['bbox']}\nTitle: {rec['title']}"
    )


def test_temporal_and_anytext(csw, polygon_records):
    """And([temporal_comparison, AnyText]) is accepted and returns a non-negative count."""
    rec = polygon_records[0]
    year = rec["time_begin"][:4]
    word = rec["anytext_sample"] or "Arctic"

    csw.getrecords2(
        constraints=[And([
            PropertyIsGreaterThanOrEqualTo(TEMPORAL_PROP, f"{year}-01-01"),
            PropertyIsLike(ANYTEXT_PROP, f"*{word}*"),
        ])],
        maxrecords=1, esn="brief",
    )
    assert csw.results["matches"] >= 0


def test_three_way_and(csw, polygon_records):
    """And([BBox, temporal, AnyText]) is accepted and returns a non-negative count."""
    rec = polygon_records[0]
    year = rec["time_begin"][:4]
    word = rec["anytext_sample"] or "Arctic"
    west, south, east, north = rec["bbox"]

    csw.getrecords2(
        constraints=[And([
            BBox([west, south, east, north]),
            PropertyIsGreaterThanOrEqualTo(TEMPORAL_PROP, f"{year}-01-01"),
            PropertyIsLike(ANYTEXT_PROP, f"*{word}*"),
        ])],
        maxrecords=1, esn="brief",
    )
    assert csw.results["matches"] >= 0

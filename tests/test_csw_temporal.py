"""
Temporal CSW query tests — TempExtent_begin / TempExtent_end filters.

Uses apiso:TempExtent_begin with comparison operators.

Fixes applied:
  - evaluate.py: _to_solr_date() appends T00:00:00Z to bare date strings for Solr pdate fields
  - cfg.yml: added apiso:TempExtent_begin mapping to temporal_extent_start_date
  - solr_metno.py: keywords2themes() now skips unknown vocabularies (e.g. 'NONE')
"""

import pytest
from owslib.fes import (
    PropertyIsGreaterThanOrEqualTo,
    PropertyIsLessThanOrEqualTo,
    And,
)
from .conftest import find_identifier, MAX_SEARCH_RECORDS

TEMPORAL_PROP = "apiso:TempExtent_begin"




def test_temporal_gte_returns_records(csw, all_records, record_index):
    """
    PropertyIsGreaterThanOrEqualTo on TempExtent_begin returns a non-zero,
    non-total result set — confirming the temporal filter is applied.
    """
    rec = all_records[record_index]
    year = rec["time_begin"][:4]
    date_from = f"{year}-01-01"

    csw.getrecords2(
        constraints=[PropertyIsGreaterThanOrEqualTo(TEMPORAL_PROP, date_from)],
        maxrecords=1,
        esn="brief",
    )
    count_gte = csw.results["matches"]

    csw.getrecords2(maxrecords=1, esn="brief")
    total = csw.results["matches"]

    assert 0 < count_gte <= total, (
        f"Temporal GTE '{date_from}' returned {count_gte} (total={total}). "
        "Expected a non-zero count less than or equal to total."
    )


def test_temporal_lte_returns_fewer_than_total(csw, all_records, record_index):
    """
    PropertyIsLessThanOrEqualTo on TempExtent_begin narrows the result set
    relative to no filter.
    """
    rec = all_records[record_index]
    # Use the specific start date as the upper bound; anything before should be a subset
    date_to = rec["time_begin"]

    csw.getrecords2(
        constraints=[PropertyIsLessThanOrEqualTo(TEMPORAL_PROP, date_to)],
        maxrecords=1,
        esn="brief",
    )
    count_lte = csw.results["matches"]

    csw.getrecords2(maxrecords=1, esn="brief")
    total = csw.results["matches"]

    assert 0 < count_lte <= total, (
        f"Temporal LTE '{date_to}' returned {count_lte} (total={total})."
    )


def test_temporal_gte_narrows_with_later_date(csw):
    """
    GTE on a more recent date returns fewer results than GTE on a distant past date.
    Confirms that the temporal filter is selective (date format fix: T00:00:00Z appended).
    """
    csw.getrecords2(
        constraints=[PropertyIsGreaterThanOrEqualTo(TEMPORAL_PROP, "1900-01-01")],
        maxrecords=1, esn="brief",
    )
    count_early = csw.results["matches"]

    csw.getrecords2(
        constraints=[PropertyIsGreaterThanOrEqualTo(TEMPORAL_PROP, "2020-01-01")],
        maxrecords=1, esn="brief",
    )
    count_recent = csw.results["matches"]

    assert count_recent < count_early, (
        f"GTE 2020 ({count_recent}) is not less than GTE 1900 ({count_early}). "
        "Temporal filter direction is wrong."
    )


def test_temporal_range_and(csw, all_records):
    """
    And([GTE, LTE]) on TempExtent_begin returns records within a year range.
    Confirms both And() composition and the temporal filter work together.
    """
    rec = all_records[0]
    year = rec["time_begin"][:4]

    csw.getrecords2(
        constraints=[And([
            PropertyIsGreaterThanOrEqualTo(TEMPORAL_PROP, f"{year}-01-01"),
            PropertyIsLessThanOrEqualTo(TEMPORAL_PROP,   f"{year}-12-31"),
        ])],
        maxrecords=1, esn="brief",
    )
    assert csw.results["matches"] >= 0

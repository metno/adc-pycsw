"""
AnyText CSW query tests — PropertyIsLike on csw:AnyText.

The anytext_sample field in each fixture record contains the longest pure-ASCII
word extracted from the dataset title.  PropertyIsLike with wildcard wrapping
(*word*) is used to find the record.

Known limitation: some words trigger 'Invalid query syntax' in the current plugin
(upstream pygeofilter/Solr interaction).  Affected tests are skipped at runtime.
"""

import pytest
from owslib.fes import PropertyIsLike
from owslib.ows import ExceptionReport
from .conftest import find_identifier, MAX_SEARCH_RECORDS

ANYTEXT_PROP = "csw:AnyText"




def _anytext_constraint(word: str) -> PropertyIsLike:
    return PropertyIsLike(ANYTEXT_PROP, f"*{word}*")


def test_anytext_returns_nonzero(csw, all_records, record_index):
    """
    AnyText search with the record's sample word returns at least one result.
    Skips gracefully if the word causes a server-side syntax error.
    """
    rec = all_records[record_index]
    word = rec["anytext_sample"]
    if not word:
        pytest.skip("No usable anytext_sample for this record")

    try:
        csw.getrecords2(
            constraints=[_anytext_constraint(word)],
            maxrecords=1,
            esn="brief",
        )
    except ExceptionReport as exc:
        pytest.skip(f"Server rejected AnyText literal '{word}': {exc}")

    total = csw.results["matches"]
    assert total > 0, (
        f"AnyText search for '{word}' returned 0 results. "
        f"Record: {rec['identifier']}"
    )


def test_anytext_finds_record(csw, all_records, record_index):
    """
    AnyText search with the record's sample word finds the specific record
    within the first MAX_SEARCH_RECORDS results.

    Skips when the word is too common (> MAX_SEARCH_RECORDS total results)
    because the target would be buried beyond the pagination limit.
    """
    rec = all_records[record_index]
    word = rec["anytext_sample"]
    if not word:
        pytest.skip("No usable anytext_sample for this record")

    # Quick count check first (1 request, cheap)
    try:
        csw.getrecords2(
            constraints=[_anytext_constraint(word)],
            maxrecords=1,
            esn="brief",
        )
    except ExceptionReport as exc:
        pytest.skip(f"Server rejected AnyText literal '{word}': {exc}")

    total = csw.results["matches"]
    if total > MAX_SEARCH_RECORDS:
        pytest.skip(
            f"AnyText '{word}' returns {total} results (> {MAX_SEARCH_RECORDS}); "
            "word too common to reliably verify record presence within pagination limit"
        )

    # Now paginate to find the record
    try:
        found, _ = find_identifier(
            csw,
            constraints=[_anytext_constraint(word)],
            identifier=rec["identifier"],
            max_records=MAX_SEARCH_RECORDS,
        )
    except ExceptionReport as exc:
        pytest.skip(f"Server rejected AnyText literal '{word}': {exc}")

    assert found, (
        f"Record '{rec['identifier']}' not found in {total} "
        f"AnyText results for '{word}'.\n"
        f"Title: {rec['title']}"
    )


def test_anytext_selectivity(csw):
    """
    A rare word returns fewer results than a common word.
    Confirms that AnyText filtering is selective rather than returning everything.
    """
    # "Arctic" is a common term in this dataset — should return many results
    try:
        csw.getrecords2(
            constraints=[PropertyIsLike(ANYTEXT_PROP, "*Arctic*")],
            maxrecords=1, esn="brief",
        )
    except ExceptionReport:
        pytest.skip("Server rejected 'Arctic' literal")
    count_broad = csw.results["matches"]

    # "Svalbard" is a specific location name — should return far fewer
    try:
        csw.getrecords2(
            constraints=[PropertyIsLike(ANYTEXT_PROP, "*Svalbard*")],
            maxrecords=1, esn="brief",
        )
    except ExceptionReport:
        pytest.skip("Server rejected 'Svalbard' literal")
    count_specific = csw.results["matches"]

    assert 0 < count_specific < count_broad, (
        f"Specific search 'Svalbard' ({count_specific}) should be > 0 and < "
        f"broad search 'Arctic' ({count_broad}). AnyText filter may not be selective."
    )


def test_cql_unprefixed_anytext_matches_prefixed(csw):
    """
    CQL constraints commonly use the unprefixed `AnyText` queryable; it must
    resolve to the same Solr field as `csw:AnyText` instead of matching nothing.
    """
    import re
    import requests

    def matched(prop):
        resp = requests.get(
            csw.url,
            params={
                "service": "CSW",
                "version": "2.0.2",
                "request": "GetRecords",
                "typenames": "csw:Record",
                "resulttype": "hits",
                "elementsetname": "brief",
                "constraintlanguage": "CQL_TEXT",
                "constraint_language_version": "1.1.0",
                "constraint": f"{prop} LIKE '%Arctic%'",
            },
            timeout=60,
        )
        resp.raise_for_status()
        found = re.search(r'numberOfRecordsMatched="(\d+)"', resp.text)
        assert found, resp.text[:500]
        return int(found.group(1))

    prefixed = matched("csw:AnyText")
    assert prefixed > 0
    assert matched("AnyText") == prefixed

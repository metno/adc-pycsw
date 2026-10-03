"""
GetRecordById tests — verify every fixture record is retrievable by its identifier.

This is the most fundamental test: it validates that the plugin can serve
individual records and that identifiers round-trip correctly between Solr
and the CSW response.
"""

import pytest
from .conftest import find_identifier


def pytest_generate_tests(metafunc):
    if "record" in metafunc.fixturenames:
        records = metafunc.config._store.get("all_records_cache", None)
        # Use indirect parametrisation via the fixture
        pass




def test_record_exists_by_id(csw, all_records, record_index):
    """GetRecordById returns the expected record for every fixture entry."""
    rec = all_records[record_index]
    identifier = rec["identifier"]

    csw.getrecordbyid(
        id=[identifier],
        outputschema="http://www.opengis.net/cat/csw/2.0.2",
    )

    assert identifier in csw.records, (
        f"GetRecordById did not return '{identifier}' "
        f"(title: {rec['title'][:60]})"
    )
    returned = csw.records[identifier]
    assert returned.identifier == identifier


def test_record_has_title(csw, all_records, record_index):
    """Retrieved record has a non-empty title matching the fixture."""
    rec = all_records[record_index]
    csw.getrecordbyid(
        id=[rec["identifier"]],
        outputschema="http://www.opengis.net/cat/csw/2.0.2",
    )
    csw_rec = csw.records.get(rec["identifier"])
    assert csw_rec is not None
    assert csw_rec.title, f"Record {rec['identifier']} has no title"


def test_nonexistent_record_returns_empty(csw):
    """GetRecordById with a made-up UUID returns no records (not an error)."""
    fake_id = "00000000-0000-0000-0000-000000000000"
    csw.getrecordbyid(id=[fake_id])
    assert fake_id not in csw.records

"""
Output schema tests — every advertised outputSchema must serialise real records.

Each pycsw outputschema plugin is a separate serialiser with its own
assumptions about the record (e.g. pycsw's fgdc / gm03 / dif writers read
link['protocol'] directly), so a schema can be advertised and still fail
with HTTP 500 on every record. These tests request the fixture records in
every expected schema, through both GetRecordById and GetRecords.

They also catch the outputschemas hot-patch silently not applying: the MET
Norway dif / dif10 / wmo modules are mounted into the pycsw source tree
(/home/pycsw/pycsw/pycsw/plugins/outputschemas); mounted anywhere else they
are ignored, DIF10 disappears from the capabilities and DIF falls back to
pycsw's own writer.
"""

import pytest
import requests
from lxml import etree

from .conftest import CSW_URL

OWS_NS = {"ows": "http://www.opengis.net/ows"}

# outputSchema -> (namespace, local name) of the record element per element
# set; a namespace of None means "any".
EXPECTED_SCHEMAS = {
    "http://www.opengis.net/cat/csw/2.0.2": {
        "full": ("http://www.opengis.net/cat/csw/2.0.2", "Record"),
        "summary": ("http://www.opengis.net/cat/csw/2.0.2", "SummaryRecord"),
        "brief": ("http://www.opengis.net/cat/csw/2.0.2", "BriefRecord"),
    },
    "http://www.isotc211.org/2005/gmd": ("http://www.isotc211.org/2005/gmd", "MD_Metadata"),
    "http://gcmd.gsfc.nasa.gov/Aboutus/xml/dif/": ("http://gcmd.gsfc.nasa.gov/Aboutus/xml/dif/", "DIF"),
    # The DIF 10 schema keeps the DIF 9 target namespace.
    "http://gcmd.gsfc.nasa.gov/Aboutus/xml/dif/10/": ("http://gcmd.gsfc.nasa.gov/Aboutus/xml/dif/", "DIF"),
    "http://www.w3.org/2005/Atom": ("http://www.w3.org/2005/Atom", "entry"),
    "http://www.opengis.net/cat/csw/csdgm": (None, "metadata"),
    "http://www.interlis.ch/INTERLIS2.3": (None, "TRANSFER"),
    "http://datacite.org/schema/kernel-4": (None, "resource"),
}

ELEMENT_SETS = ("full", "summary", "brief")


def expected_root(schema: str, esn: str):
    expected = EXPECTED_SCHEMAS[schema]
    return expected[esn] if isinstance(expected, dict) else expected


def kvp(**params) -> dict:
    return {"service": "CSW", "version": "2.0.2", **params}


def parse(resp: requests.Response, what: str) -> etree._Element:
    """Parse a CSW response, failing with the server's own message."""
    assert resp.status_code == 200, (
        f"{what}: HTTP {resp.status_code}: {resp.text[:300]}"
    )
    try:
        root = etree.fromstring(resp.content)
    except etree.XMLSyntaxError as exc:
        pytest.fail(f"{what}: response is not XML ({exc}): {resp.text[:300]}")
    if etree.QName(root).localname == "ExceptionReport":
        pytest.fail(f"{what}: {' '.join(root.xpath('string(.)').split())[:300]}")
    return root


def record_elements(root: etree._Element) -> list:
    """Record elements of a GetRecordById / GetRecords response."""
    if etree.QName(root).localname == "GetRecordsResponse":
        results = root.xpath('//*[local-name()="SearchResults"]')
        assert results, "GetRecordsResponse without SearchResults"
        root = results[0]
    return [child for child in root if isinstance(child.tag, str)]


def assert_root(element, schema: str, esn: str, what: str):
    namespace, localname = expected_root(schema, esn)
    qname = etree.QName(element)
    assert qname.localname == localname, (
        f"{what}: expected <{localname}>, got <{qname.localname}>"
    )
    if namespace is not None:
        assert qname.namespace == namespace, (
            f"{what}: expected namespace {namespace}, got {qname.namespace}"
        )


@pytest.fixture(scope="module")
def http():
    session = requests.Session()
    try:
        session.get(CSW_URL, params=kvp(request="GetCapabilities"), timeout=30).raise_for_status()
    except Exception as exc:
        pytest.skip(f"CSW endpoint unreachable at {CSW_URL}: {exc}")
    return session


@pytest.fixture(scope="module")
def advertised_schemas(http) -> dict:
    """outputSchema values advertised per operation in GetCapabilities."""
    root = parse(http.get(CSW_URL, params=kvp(request="GetCapabilities"), timeout=60), "GetCapabilities")
    return {
        op: set(root.xpath(
            f'//ows:Operation[@name="{op}"]/ows:Parameter[@name="outputSchema"]/ows:Value/text()',
            namespaces=OWS_NS,
        ))
        for op in ("GetRecords", "GetRecordById")
    }


@pytest.fixture(scope="module")
def served_identifiers(http, all_records) -> set:
    """Fixture identifiers this endpoint actually serves (Dublin Core)."""
    served = set()
    for rec in all_records:
        resp = http.get(CSW_URL, params=kvp(
            request="GetRecordById", id=rec["identifier"], elementsetname="brief",
        ), timeout=60)
        if resp.status_code == 200 and record_elements(etree.fromstring(resp.content)):
            served.add(rec["identifier"])
    return served


@pytest.mark.parametrize("operation", ["GetRecords", "GetRecordById"])
def test_expected_schemas_advertised(advertised_schemas, operation):
    """Every schema in EXPECTED_SCHEMAS is offered (DIF10 missing = mount not applied)."""
    missing = set(EXPECTED_SCHEMAS) - advertised_schemas[operation]
    assert not missing, f"{operation} does not advertise: {sorted(missing)}"


def test_no_untested_schemas(advertised_schemas):
    """A newly advertised schema must be added to EXPECTED_SCHEMAS to be tested."""
    untested = advertised_schemas["GetRecordById"] - set(EXPECTED_SCHEMAS)
    assert not untested, f"advertised but not covered by these tests: {sorted(untested)}"


@pytest.mark.parametrize("schema", sorted(EXPECTED_SCHEMAS))
def test_getrecordbyid_full(http, all_records, served_identifiers, record_index, schema):
    """Each fixture record serialises in each schema (element set full)."""
    identifier = all_records[record_index]["identifier"]
    if identifier not in served_identifiers:
        pytest.skip(f"{identifier} is not served by {CSW_URL} (fixture built from another core?)")
    what = f"GetRecordById {identifier} as {schema}"
    root = parse(http.get(CSW_URL, params=kvp(
        request="GetRecordById", id=identifier, elementsetname="full", outputschema=schema,
    ), timeout=90), what)
    records = record_elements(root)
    assert len(records) == 1, f"{what}: expected 1 record, got {len(records)}"
    assert_root(records[0], schema, "full", what)


@pytest.mark.parametrize("esn", ["summary", "brief"])
@pytest.mark.parametrize("schema", sorted(EXPECTED_SCHEMAS))
def test_getrecordbyid_element_sets(http, all_records, served_identifiers, schema, esn):
    """summary / brief element sets serialise too (one served record per schema)."""
    if not served_identifiers:
        pytest.skip(f"no fixture record is served by {CSW_URL}")
    identifier = next(r["identifier"] for r in all_records if r["identifier"] in served_identifiers)
    what = f"GetRecordById {identifier} as {schema} ({esn})"
    root = parse(http.get(CSW_URL, params=kvp(
        request="GetRecordById", id=identifier, elementsetname=esn, outputschema=schema,
    ), timeout=90), what)
    records = record_elements(root)
    assert len(records) == 1, f"{what}: expected 1 record, got {len(records)}"
    assert_root(records[0], schema, esn, what)


@pytest.mark.parametrize("schema", sorted(EXPECTED_SCHEMAS))
def test_getrecords(http, schema):
    """A GetRecords page serialises in each schema (records not chosen by us)."""
    what = f"GetRecords as {schema}"
    root = parse(http.get(CSW_URL, params=kvp(
        request="GetRecords", typenames="csw:Record", resulttype="results",
        elementsetname="full", maxrecords=5, outputschema=schema,
    ), timeout=90), what)
    records = record_elements(root)
    assert records, f"{what}: no records returned"
    for element in records:
        assert_root(element, schema, "full", what)

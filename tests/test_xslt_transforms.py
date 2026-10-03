"""
XSLT outputschema transformation tests.

Tests that each mmd-to-<format>.xsl:
  1. Parses without error (valid XSLT).
  2. Transforms sample MMD documents without runtime errors.
  3. Produces a root element with the expected tag / namespace.
  4. Validates against the bundled XSD schema where one is available.

Schema availability:
  - DIF 9.x  → mmd/xsd/dif/9.x/dif_v9.9.3.xsd          (present, validation passes)
  - DIF 10.x → mmd/xsd/dif/10.x/dif.xsd                  (present, but Use_Constraints
                element mismatch in XSLT output — marked xfail)
  - ISO 19139 / WMO → no local XSD bundled; well-formedness + root tag checked only

pycsw is only available inside the container, so outputschemas that call
pycsw.core.util (atom, fgdc, gm03) are not tested here.  Those are covered
by live CSW tests with outputSchema= parameters (future work).
"""

import pathlib
import pytest
from lxml import etree

REPO = pathlib.Path(__file__).parent.parent
XSLT_DIR = REPO / "mmd" / "xslt"
XSD_DIR = REPO / "mmd" / "xsd"

MMD_SAMPLES = [
    pytest.param(
        REPO / "mmd" / "tests" / "data" / "precipitation_amount_st_92350.xml",
        id="precipitation",
    ),
    pytest.param(
        REPO / "mmd" / "input-examples" / "foo.xml",
        id="foo",
    ),
]

# foo.xml uses vocabulary="MyOwnVocab" which is not in the MMD schema enumeration.
# It is an intentionally simplified example; only real production records are
# used for XSD output-schema validation.
MMD_SAMPLES_VALID = [
    pytest.param(
        REPO / "mmd" / "tests" / "data" / "precipitation_amount_st_92350.xml",
        id="precipitation",
    ),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_xslt(name: str) -> etree.XSLT:
    return etree.XSLT(etree.parse(str(XSLT_DIR / f"mmd-to-{name}.xsl")))


def _transform(xslt: etree.XSLT, mmd_path: pathlib.Path, **params) -> etree._ElementTree:
    doc = etree.parse(str(mmd_path))
    result = xslt(doc, **params)
    assert not xslt.error_log, f"XSLT runtime errors: {list(xslt.error_log)}"
    return result


def _validate(result_tree: etree._ElementTree, xsd_path: pathlib.Path) -> list:
    schema = etree.XMLSchema(etree.parse(str(xsd_path)))
    schema.validate(result_tree)
    return [str(e) for e in schema.error_log]


# ---------------------------------------------------------------------------
# XSLT load tests — verify the stylesheet itself is valid XSLT
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["dif", "dif10", "iso", "wmo"])
def test_xslt_loads(name):
    """Each mmd-to-<name>.xsl parses as valid XSLT without error."""
    xslt = _load_xslt(name)
    assert isinstance(xslt, etree.XSLT)


# ---------------------------------------------------------------------------
# DIF 9.x  (mmd-to-dif.xsl)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mmd_path", MMD_SAMPLES)
def test_dif9_transforms(mmd_path):
    """mmd-to-dif.xsl produces a dif:DIF root element."""
    xslt = _load_xslt("dif")
    result = _transform(xslt, mmd_path)
    root = result.getroot()
    assert root is not None
    assert "DIF" in root.tag
    assert "gcmd.gsfc.nasa.gov" in root.tag or root.tag.endswith("DIF")


@pytest.mark.parametrize("mmd_path", MMD_SAMPLES_VALID)
def test_dif9_schema_valid(mmd_path):
    """mmd-to-dif.xsl output validates against DIF 9.9.3 XSD (valid MMD input only)."""
    xslt = _load_xslt("dif")
    result = _transform(xslt, mmd_path)
    errors = _validate(result, XSD_DIR / "dif" / "9.x" / "dif_v9.9.3.xsd")
    assert not errors, "DIF9 schema errors:\n" + "\n".join(errors)


# ---------------------------------------------------------------------------
# DIF 10.x  (mmd-to-dif10.xsl)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mmd_path", MMD_SAMPLES)
def test_dif10_transforms(mmd_path):
    """mmd-to-dif10.xsl produces a dif:DIF root element."""
    xslt = _load_xslt("dif10")
    result = _transform(xslt, mmd_path)
    root = result.getroot()
    assert root is not None
    assert "DIF" in root.tag


@pytest.mark.xfail(
    reason=(
        "DIF10 XSD rejects the Use_Constraints element produced by the XSLT: "
        "'Element content is not allowed, because the content type is a simple "
        "type definition.'  The XSLT generates a child element where the schema "
        "expects a text node.  Fix mmd-to-dif10.xsl to resolve."
    ),
    strict=True,
)
@pytest.mark.parametrize("mmd_path", MMD_SAMPLES_VALID)
def test_dif10_schema_valid(mmd_path):
    """mmd-to-dif10.xsl output validates against DIF 10 XSD (known XSLT bug)."""
    xslt = _load_xslt("dif10")
    result = _transform(xslt, mmd_path)
    errors = _validate(result, XSD_DIR / "dif" / "10.x" / "dif.xsd")
    assert not errors, "DIF10 schema errors:\n" + "\n".join(errors)


# ---------------------------------------------------------------------------
# ISO 19139  (mmd-to-iso.xsl)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mmd_path", MMD_SAMPLES)
def test_iso_transforms(mmd_path):
    """mmd-to-iso.xsl produces a gmd:MD_Metadata root element."""
    xslt = _load_xslt("iso")
    result = _transform(xslt, mmd_path)
    root = result.getroot()
    assert root is not None
    assert "MD_Metadata" in root.tag
    assert "isotc211.org" in root.tag


@pytest.mark.parametrize("mmd_path", MMD_SAMPLES)
def test_iso_well_formed(mmd_path):
    """mmd-to-iso.xsl output serialises to well-formed XML."""
    xslt = _load_xslt("iso")
    result = _transform(xslt, mmd_path)
    xml_bytes = etree.tostring(result, xml_declaration=True, encoding="UTF-8")
    reparsed = etree.fromstring(xml_bytes)
    assert reparsed is not None


# ---------------------------------------------------------------------------
# WMO (mmd-to-wmo.xsl)  — ISO profile, also produces gmd:MD_Metadata
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mmd_path", MMD_SAMPLES)
def test_wmo_transforms(mmd_path):
    """mmd-to-wmo.xsl produces a gmd:MD_Metadata root element."""
    xslt = _load_xslt("wmo")
    # wmo XSLT accepts an optional path_to_parent_list param; pass empty string
    result = _transform(
        xslt, mmd_path,
        path_to_parent_list=etree.XSLT.strparam(""),
    )
    root = result.getroot()
    assert root is not None
    assert "MD_Metadata" in root.tag


@pytest.mark.parametrize("mmd_path", MMD_SAMPLES)
def test_wmo_well_formed(mmd_path):
    """mmd-to-wmo.xsl output serialises to well-formed XML."""
    xslt = _load_xslt("wmo")
    result = _transform(
        xslt, mmd_path,
        path_to_parent_list=etree.XSLT.strparam(""),
    )
    xml_bytes = etree.tostring(result, xml_declaration=True, encoding="UTF-8")
    reparsed = etree.fromstring(xml_bytes)
    assert reparsed is not None


# ---------------------------------------------------------------------------
# MMD input schema validation (sanity-check the test fixtures themselves)
# ---------------------------------------------------------------------------

def test_mmd_input_valid_precipitation():
    """The production MMD sample validates against mmd.xsd."""
    mmd_path = REPO / "mmd" / "tests" / "data" / "precipitation_amount_st_92350.xml"
    schema = etree.XMLSchema(etree.parse(str(XSD_DIR / "mmd.xsd")))
    doc = etree.parse(str(mmd_path))
    schema.validate(doc)
    errors = [str(e) for e in schema.error_log]
    assert not errors, "MMD schema errors:\n" + "\n".join(errors)


@pytest.mark.xfail(
    reason=(
        "foo.xml uses vocabulary='MyOwnVocab' which is not in the MMD schema "
        "enumeration.  It is an intentionally simplified example file, not a "
        "conforming production record."
    ),
    strict=True,
)
def test_mmd_input_valid_foo():
    """foo.xml is a non-conforming example; its MMD schema failure is documented."""
    mmd_path = REPO / "mmd" / "input-examples" / "foo.xml"
    schema = etree.XMLSchema(etree.parse(str(XSD_DIR / "mmd.xsd")))
    doc = etree.parse(str(mmd_path))
    schema.validate(doc)
    errors = [str(e) for e in schema.error_log]
    assert not errors, "MMD schema errors:\n" + "\n".join(errors)

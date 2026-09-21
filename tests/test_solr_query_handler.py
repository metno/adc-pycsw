"""
Regression tests for the OGC filter -> SOLR translation.

Run with the plugins directory on the path, e.g. from the repository root:

    python3 -m pytest tests/test_solr_query_handler.py

The cases below are the ones that broke real NBS clients after the September
2026 query handler rewrite; see test_sentinel1_daily_harvest for the exact
filter that stopped returning records.
"""

import importlib.util
import os

import pytest
import xmltodict

HANDLER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "plugins",
    "repository",
    "solr_query_handler.py",
)
_spec = importlib.util.spec_from_file_location("solr_query_handler", HANDLER)
qh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qh)


def translate(filter_xml, collection="NBS"):
    """Filter query built from an ogc:Filter document, without the base filters"""
    constraint = {"_dict": xmltodict.parse(filter_xml)}
    params = qh.QueryHandler(collection).query(constraint)
    return params["fq"][-1]


def like(literal, prop="apiso:AnyText", wildcard="*", single_char="?", escape="\\"):
    return (
        '<ogc:Filter xmlns:ogc="http://www.opengis.net/ogc">'
        f'<ogc:PropertyIsLike wildCard="{wildcard}" singleChar="{single_char}"'
        f' escapeChar="{escape}">'
        f"<ogc:PropertyName>{prop}</ogc:PropertyName>"
        f"<ogc:Literal>{literal}</ogc:Literal>"
        "</ogc:PropertyIsLike></ogc:Filter>"
    )


def envelope(lower, upper, srs_name=None):
    srs = f' srsName="{srs_name}"' if srs_name else ""
    return (
        '<ogc:Filter xmlns:ogc="http://www.opengis.net/ogc"'
        ' xmlns:gml="http://www.opengis.net/gml"><ogc:BBOX>'
        "<ogc:PropertyName>ows:BoundingBox</ogc:PropertyName>"
        f"<gml:Envelope{srs}>"
        f"<gml:lowerCorner>{lower}</gml:lowerCorner>"
        f"<gml:upperCorner>{upper}</gml:upperCorner>"
        "</gml:Envelope></ogc:BBOX></ogc:Filter>"
    )


class TestPropertyIsLike:
    def test_declared_wildcards_are_translated(self):
        assert translate(like("S1%foo_bar", wildcard="%", single_char="_")) == (
            "full_text:(S1*foo?bar)"
        )

    def test_solr_wildcards_are_kept_when_other_chars_are_declared(self):
        # OWSLib clients routinely declare singleChar="." or "_" and still type
        # "?" and "*"; escaping those into literals matches nothing
        assert translate(like("S1?_EW*", single_char=".")) == "full_text:(S1?_EW*)"

    def test_escape_char_makes_wildcards_literal(self):
        # an escaped wildcard searches for the character itself; only the ones
        # SOLR treats as special need a backslash in the pattern
        assert translate(like(r"50\% a\?b", wildcard="%")) == r"full_text:(50% a\?b)"

    def test_underscore_is_literal_when_not_declared(self):
        assert translate(like("S1A_EW*", single_char=".")) == "full_text:(S1A_EW*)"

    def test_title_maps_to_the_title_field(self):
        assert translate(like("S1*", prop="dc:title")) == "title:(S1*)"

    def test_empty_literal_is_rejected(self):
        with pytest.raises(qh.UnsupportedFilterError):
            translate(like(""))


class TestBoundingBox:
    ARCTIC = 'bbox:"Intersects(ENVELOPE(-25.0,50.0,83.0,70.0))"'

    @pytest.mark.parametrize(
        "srs_name",
        [
            None,
            "EPSG:4326",
            "urn:x-ogc:def:crs:EPSG:6.18:4326",
            "urn:ogc:def:crs:EPSG::4326",
            "http://www.opengis.net/def/crs/EPSG/0/4326",
            "urn:ogc:def:crs:OGC:1.3:CRS84",
        ],
    )
    def test_corners_are_read_as_lon_lat(self, srs_name):
        # every srsName form clients send here carries lon/lat corners, the URN
        # forms included, so none of them may transpose the envelope
        assert translate(envelope("-25 70", "50 83", srs_name)) == self.ARCTIC

    def test_unknown_srs_falls_back_to_lon_lat(self):
        assert translate(envelope("-25 70", "50 83", "EPSG:32633")) == self.ARCTIC

    def test_invalid_envelope_is_rejected(self):
        with pytest.raises(qh.UnsupportedFilterError):
            translate(envelope("-25", "50 83"))


class TestTemporalExtent:
    def test_overlap_query(self):
        filter_xml = (
            '<ogc:Filter xmlns:ogc="http://www.opengis.net/ogc"><ogc:And>'
            "<ogc:PropertyIsLessThanOrEqualTo>"
            "<ogc:PropertyName>apiso:TempExtent_begin</ogc:PropertyName>"
            "<ogc:Literal>2026-09-21 23:59</ogc:Literal>"
            "</ogc:PropertyIsLessThanOrEqualTo>"
            "<ogc:PropertyIsGreaterThanOrEqualTo>"
            "<ogc:PropertyName>apiso:TempExtent_end</ogc:PropertyName>"
            "<ogc:Literal>2026-09-21 00:00</ogc:Literal>"
            "</ogc:PropertyIsGreaterThanOrEqualTo>"
            "</ogc:And></ogc:Filter>"
        )
        assert translate(filter_xml) == (
            "(temporal_extent_start_date:[* TO 2026-09-21T23:59:00Z]"
            " AND temporal_extent_end_date:[2026-09-21T00:00:00Z TO *])"
        )


def test_sentinel1_daily_harvest():
    """
    The NBS Sentinel-1 harvesting filter, as sent by OWSLib.

    Two separate regressions each reduced this to zero matches: the "?" in the
    literal was escaped because the client declares singleChar=".", and the
    URN srsName transposed the bounding box into the Gulf of Guinea.
    """
    filter_xml = (
        '<ogc:Filter xmlns:ogc="http://www.opengis.net/ogc"'
        ' xmlns:gml="http://www.opengis.net/gml"><ogc:And>'
        '<ogc:PropertyIsLike wildCard="*" singleChar="." escapeChar="\\">'
        "<ogc:PropertyName>apiso:AnyText</ogc:PropertyName>"
        "<ogc:Literal>S1?_EW_GRDM_1SDH*</ogc:Literal></ogc:PropertyIsLike>"
        "<ogc:PropertyIsLessThanOrEqualTo>"
        "<ogc:PropertyName>apiso:TempExtent_begin</ogc:PropertyName>"
        "<ogc:Literal>2026-09-21 23:59</ogc:Literal>"
        "</ogc:PropertyIsLessThanOrEqualTo>"
        "<ogc:PropertyIsGreaterThanOrEqualTo>"
        "<ogc:PropertyName>apiso:TempExtent_end</ogc:PropertyName>"
        "<ogc:Literal>2026-09-21 00:00</ogc:Literal>"
        "</ogc:PropertyIsGreaterThanOrEqualTo>"
        "<ogc:BBOX><ogc:PropertyName>ows:BoundingBox</ogc:PropertyName>"
        '<gml:Envelope srsName="urn:x-ogc:def:crs:EPSG:6.18:4326">'
        "<gml:lowerCorner>-25 70</gml:lowerCorner>"
        "<gml:upperCorner>50 83</gml:upperCorner>"
        "</gml:Envelope></ogc:BBOX></ogc:And></ogc:Filter>"
    )
    assert translate(filter_xml) == (
        "(full_text:(S1?_EW_GRDM_1SDH*)"
        " AND temporal_extent_start_date:[* TO 2026-09-21T23:59:00Z]"
        " AND temporal_extent_end_date:[2026-09-21T00:00:00Z TO *]"
        ' AND bbox:"Intersects(ENVELOPE(-25.0,50.0,83.0,70.0))")'
    )

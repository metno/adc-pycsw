"""
Translate OGC filters (FES 1.1 and FES 2.0) into SOLR query parameters.

pycsw hands the repository the filter as a dictionary produced by xmltodict,
e.g. {"ogc:Filter": {"ogc:And": {"ogc:BBOX": {...}, "ogc:PropertyIsLike": [...]}}}
for CSW 2.0.2 and {"fes20:Filter": {...}} for CSW 3.0.0 and OpenSearch.

The whole filter is translated recursively into one boolean SOLR expression
that is added as a filter query (fq). Operators or properties that cannot be
translated raise UnsupportedFilterError: ignoring them would silently return
unfiltered results.
"""

import logging
import re
from datetime import datetime, timezone

from dateutil import parser as dparser

LOGGER = logging.getLogger(__name__)


class UnsupportedFilterError(ValueError):
    """Raised when a filter cannot be translated into a SOLR query"""


TEXT, STRING, DATE = "text", "string", "date"

# CSW queryable (local name, lower case) -> (SOLR fields, field kind)
FIELDS = {
    "title": (["title"], TEXT),
    "alternatetitle": (["title"], TEXT),
    "abstract": (["abstract"], TEXT),
    "subject": (["keywords_keyword"], STRING),
    "creator": (["personnel_investigator_name"], TEXT),
    "contributor": (["personnel_technical_name", "personnel_metadata_author_name"], TEXT),
    "source": (["related_url_landing_page"], STRING),
    "format": (["storage_information_file_format"], STRING),
    "language": (["dataset_language"], STRING),
    "resourcelanguage": (["dataset_language"], STRING),
    "publisher": (["dataset_citation_publisher"], TEXT),
    "rights": (["use_constraint_identifier", "use_constraint_license_text"], STRING),
    "identifier": (["metadata_identifier"], STRING),
    "parentidentifier": (["related_dataset"], STRING),
    "topiccategory": (["iso_topic_category"], STRING),
    "anytext": (["full_text"], TEXT),
    "modified": (["last_metadata_update_datetime"], DATE),
    "date": (["last_metadata_update_datetime"], DATE),
    "tempextent_begin": (["temporal_extent_start_date"], DATE),
    "tempextent_end": (["temporal_extent_end_date"], DATE),
}

# Multi-valued date fields compared by their latest value, which is the value
# reported as the record modification date (e.g. the OAI-PMH datestamp)
LATEST_VALUE_FIELDS = {"last_metadata_update_datetime"}

# pycsw database column used in SortBy -> SOLR sort expression
SORT_FIELDS = {
    "date_modified": "field(last_metadata_update_datetime,max)",
    "date": "field(last_metadata_update_datetime,max)",
    "time_begin": "field(temporal_extent_start_date,min)",
    "time_end": "field(temporal_extent_end_date,max)",
    "insert_date": "timestamp",
}

# OGC spatial operator -> SOLR spatial predicate
SPATIAL_OPERATORS = {
    "BBOX": "Intersects",
    "Intersects": "Intersects",
    "Within": "IsWithin",
    "Contains": "Contains",
    "Disjoint": "IsDisjointTo",
}

# comparison operator -> (lower bound inclusive, upper bound inclusive) for ranges
RANGE_OPERATORS = {
    "PropertyIsGreaterThan": (False, None),
    "PropertyIsGreaterThanOrEqualTo": (True, None),
    "PropertyIsLessThan": (None, False),
    "PropertyIsLessThanOrEqualTo": (None, True),
}

TYPE_QUERIES = {"dataset": "isParent:false", "series": "isParent:true"}

MATCH_NOTHING = "(*:* -*:*)"

SOLR_SPECIAL_CHARS = set('+-&|!(){}[]^"~*?:\\/')


def base_filters(adc_collection_filter):
    """Filter queries applied to every SOLR request"""
    filters = ["metadata_status:Active"]
    if adc_collection_filter:
        filters.append(f"collection:({adc_collection_filter})")
    return filters


def _local(key):
    """Local name of an xmltodict key, e.g. 'ogc:PropertyName' -> 'PropertyName'"""
    return key.split(":")[-1]


def _children(node):
    """(local name, value) pairs of the child elements of an xmltodict node"""
    if not isinstance(node, dict):
        return []
    children = []
    for key, value in node.items():
        if key.startswith(("@", "#")):
            continue
        for item in value if isinstance(value, list) else [value]:
            children.append((_local(key), item))
    return children


def _child(node, name):
    """First child element of an xmltodict node with the given local name"""
    for child_name, value in _children(node):
        if child_name == name:
            return value
    return None


def _text(value):
    """Text content of an xmltodict element"""
    if isinstance(value, dict):
        value = value.get("#text")
    return "" if value is None else str(value)


def _join(clauses, operator):
    if len(clauses) == 1:
        return clauses[0]
    return "(" + f" {operator} ".join(clauses) + ")"


def _negate(clause):
    # SOLR matches nothing for a purely negative clause nested in a boolean query
    return f"(*:* -{clause})"


def _quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _escape(char, keep_whitespace):
    if char in SOLR_SPECIAL_CHARS or (char.isspace() and not keep_whitespace):
        return "\\" + char
    return char


def _like_pattern(literal, wildcard, single_char, escape_char, keep_whitespace):
    """Convert a PropertyIsLike literal into a SOLR wildcard pattern"""
    pattern = []
    escaped = False
    for char in literal:
        if escaped:
            pattern.append(_escape(char, keep_whitespace))
            escaped = False
        elif escape_char and char == escape_char:
            escaped = True
        elif char == wildcard:
            pattern.append("*")
        elif char == single_char:
            pattern.append("?")
        else:
            pattern.append(_escape(char, keep_whitespace))
    return "".join(pattern)


def _parse_date(literal):
    try:
        value = dparser.parse(literal, default=datetime(1900, 1, 1))
    except (ValueError, OverflowError) as err:
        raise UnsupportedFilterError(f"invalid date: {literal}") from err
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _axis_order(srs_name):
    """
    Axis order of envelope corners.

    Without srsName, and for EPSG:4326 and CRS84, corners are read as lon/lat
    (x y), as this plugin always did. The URN and URI forms of EPSG:4326
    define lat/lon (y x).
    """
    if not srs_name:
        return "xy"
    srs = srs_name.strip().lower()
    if srs.endswith("crs84"):
        return "xy"
    if re.search(r"(^|[^0-9])4326$", srs):
        return "yx" if srs.startswith("urn:") or "/def/crs/" in srs else "xy"
    raise UnsupportedFilterError(f"unsupported srsName: {srs_name}")


class QueryHandler:
    def __init__(self, adc_collection_filter="NBS"):
        self.adc_collection_filter = adc_collection_filter
        # full text clauses used as the main query so results are ranked by relevance
        self.scoring_clauses = []
        self.params = {
            "q": "*:*",
            "q.op": "AND",
            "start": 0,
            "rows": 10,
            "fq": base_filters(adc_collection_filter),
        }

    def query(
        self, constraint, sortby=None, typenames=None, maxrecords=10, startposition=0
    ):
        """
        Build SOLR query parameters for a CSW query.

        :param constraint: pycsw constraint, with the filter as a dictionary in "_dict".
        :param sortby: pycsw SortBy ({"propertyname": <db column>, "order": "ASC"|"DESC"}).
        :return: SOLR query parameters.
        """
        if constraint and constraint.get("_dict"):
            filters = [node for name, node in _children(constraint["_dict"]) if name == "Filter"]
            if not filters:
                raise UnsupportedFilterError("constraint without a Filter")
            clauses = [
                self._translate(name, node, scoring=True)
                for name, node in _children(filters[0])
            ]
            if clauses:
                self.params["fq"].append(_join(clauses, "AND"))
            if self.scoring_clauses:
                self.params["q"] = " AND ".join(self.scoring_clauses)

        sort = self._sort(sortby)
        if sort:
            self.params["sort"] = sort

        LOGGER.debug("SOLR query parameters: %s", self.params)
        return self.params

    def _translate(self, name, node, scoring=False):
        """
        Translate one filter element into a SOLR boolean expression.

        :param scoring: True when the element must match for the whole filter to
            match (not below Or/Not), so full text clauses can drive relevance.
        """
        if name in ("And", "Or"):
            clauses = [
                self._translate(child_name, child, scoring and name == "And")
                for child_name, child in _children(node)
            ]
            if not clauses:
                raise UnsupportedFilterError(f"empty {name}")
            return _join(clauses, name.upper())
        if name == "Not":
            children = _children(node)
            if len(children) != 1:
                raise UnsupportedFilterError("Not must contain exactly one operator")
            return _negate(self._translate(*children[0]))
        if name in SPATIAL_OPERATORS:
            return self._spatial(name, node)
        if name.startswith("PropertyIs"):
            return self._comparison(name, node, scoring)
        raise UnsupportedFilterError(f"unsupported filter operator: {name}")

    def _spatial(self, name, node):
        envelope = _child(node, "Envelope")
        if not isinstance(envelope, dict):
            raise UnsupportedFilterError(f"{name} is only supported with a gml:Envelope")
        try:
            lower = [float(v) for v in _text(_child(envelope, "lowerCorner")).split()]
            upper = [float(v) for v in _text(_child(envelope, "upperCorner")).split()]
        except ValueError as err:
            raise UnsupportedFilterError(f"invalid envelope in {name}") from err
        if len(lower) < 2 or len(upper) < 2:
            raise UnsupportedFilterError(f"invalid envelope in {name}")

        if _axis_order(envelope.get("@srsName")) == "yx":
            min_x, min_y, max_x, max_y = lower[1], lower[0], upper[1], upper[0]
        else:
            min_x, min_y, max_x, max_y = lower[0], lower[1], upper[0], upper[1]
        return f'bbox:"{SPATIAL_OPERATORS[name]}(ENVELOPE({min_x},{max_x},{max_y},{min_y}))"'

    def _comparison(self, name, node, scoring):
        if _child(node, "Function") is not None:
            raise UnsupportedFilterError("filter functions are not supported")
        prop = _text(_child(node, "PropertyName") or _child(node, "ValueReference")).strip()
        key = _local(prop).lower()
        literal = _text(_child(node, "Literal"))

        if key == "type":
            return self._type(name, literal)
        if key not in FIELDS:
            raise UnsupportedFilterError(f"unsupported property for {name}: {prop}")
        fields, kind = FIELDS[key]

        if name == "PropertyIsNull":
            return _negate(_join([f"{field}:[* TO *]" for field in fields], "OR"))

        if name == "PropertyIsLike":
            if kind == DATE:
                raise UnsupportedFilterError(f"PropertyIsLike is not supported for {prop}")
            pattern = _like_pattern(
                literal,
                node.get("@wildCard", "%"),
                node.get("@singleChar", "_"),
                node.get("@escapeChar"),
                keep_whitespace=kind == TEXT,
            )
            if not pattern.strip():
                raise UnsupportedFilterError(f"empty PropertyIsLike literal for {prop}")
            clause = _join([f"{field}:({pattern})" for field in fields], "OR")

        elif name in ("PropertyIsEqualTo", "PropertyIsNotEqualTo"):
            if kind == DATE:
                clause = _join([self._range(field, literal, True, literal, True) for field in fields], "OR")
            else:
                clause = _join([f"{field}:{_quote(literal)}" for field in fields], "OR")
            if name == "PropertyIsNotEqualTo":
                return _negate(clause)

        elif name in RANGE_OPERATORS or name == "PropertyIsBetween":
            if kind == TEXT:
                raise UnsupportedFilterError(f"{name} is not supported for {prop}")
            if name == "PropertyIsBetween":
                lower = _text(_child(_child(node, "LowerBoundary"), "Literal"))
                upper = _text(_child(_child(node, "UpperBoundary"), "Literal"))
                bounds = (lower, True, upper, True)
            else:
                lower_inclusive, upper_inclusive = RANGE_OPERATORS[name]
                bounds = (
                    literal if lower_inclusive is not None else None, lower_inclusive,
                    literal if upper_inclusive is not None else None, upper_inclusive,
                )
            if kind == DATE:
                clause = _join([self._range(field, *bounds) for field in fields], "OR")
            else:
                clause = _join([self._string_range(field, *bounds) for field in fields], "OR")

        else:
            raise UnsupportedFilterError(f"unsupported filter operator: {name}")

        if key == "anytext" and scoring and name in ("PropertyIsLike", "PropertyIsEqualTo"):
            self.scoring_clauses.append(clause)
        return clause

    @staticmethod
    def _type(name, literal):
        if name not in ("PropertyIsEqualTo", "PropertyIsNotEqualTo", "PropertyIsLike"):
            raise UnsupportedFilterError(f"{name} is not supported for the type property")
        clause = TYPE_QUERIES.get(literal.strip().lower(), MATCH_NOTHING)
        return _negate(clause) if name == "PropertyIsNotEqualTo" else clause

    @staticmethod
    def _range(field, lower, lower_inclusive, upper, upper_inclusive):
        """Date range query; bounds of None are open"""
        lower = _parse_date(lower) if lower is not None else None
        upper = _parse_date(upper) if upper is not None else None

        if field in LATEST_VALUE_FIELDS:
            args = []
            if lower is not None:
                args += [f"l={int(lower.timestamp() * 1000)}", f"incl={str(lower_inclusive).lower()}"]
            if upper is not None:
                args += [f"u={int(upper.timestamp() * 1000)}", f"incu={str(upper_inclusive).lower()}"]
            return '_query_:"{!frange ' + " ".join(args) + "}" + f'field({field},max)"'

        solr_format = "%Y-%m-%dT%H:%M:%SZ"
        start = lower.strftime(solr_format) if lower is not None else "*"
        end = upper.strftime(solr_format) if upper is not None else "*"
        return (
            f"{field}:{'[' if lower is None or lower_inclusive else '{'}{start} TO "
            f"{end}{']' if upper is None or upper_inclusive else '}'}"
        )

    @staticmethod
    def _string_range(field, lower, lower_inclusive, upper, upper_inclusive):
        start = _quote(lower) if lower is not None else "*"
        end = _quote(upper) if upper is not None else "*"
        return (
            f"{field}:{'[' if lower is None or lower_inclusive else '{'}{start} TO "
            f"{end}{']' if upper is None or upper_inclusive else '}'}"
        )

    @staticmethod
    def _sort(sortby):
        if not sortby:
            return None
        expression = SORT_FIELDS.get(sortby.get("propertyname"))
        if expression is None:
            LOGGER.warning(
                "SortBy %s is not supported by the SOLR backend, using default order",
                sortby.get("propertyname"),
            )
            return None
        order = "desc" if str(sortby.get("order", "ASC")).upper().startswith("D") else "asc"
        return f"{expression} {order}"

# =================================================================
#
# Authors: Massimo Di Stefano <massimods@met.no>
#          Magnar Martinsen <magnarem@met.no>
#          Tom Kralidis <tomkralidis@gmail.com>
#
# Copyright (c) 2025 Massimo Di Stefano
# Copyright (c) 2025 Magnar Martinsen
# Copyright (c) 2025 Tom Kralidis
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# =================================================================

import json
import logging
import os
from datetime import datetime
from operator import itemgetter
from typing import Any, Optional, cast
from urllib.parse import urlencode

import dateutil.parser as dparser
import niquests as requests
import validators
from lxml import etree as lxml_etree
from niquests.auth import HTTPBasicAuth
from pycsw.core.repository import Repository
from pygeofilter import ast
from pygeofilter.backends.solr.evaluate import SolrDSLQuery, to_filter

LOGGER = logging.getLogger(__name__)


class SolrMETNORepository(Repository):
    """
    Class to interact with underlying METNO Solr backend repository
    """

    def __init__(self, repo_object: dict, context):
        """
        Initialize repository
        """
        LOGGER.debug("Initializing SorMETNORepoistory")
        self.database = None
        self.filter = repo_object.get("filter", "")
        if not validators.url(self.filter):
            raise RuntimeError(f"Invalid Solr url filter provided in config: {self.filter}")
        self.xslt_iso_transformer = repo_object.get("xslt_iso_transformer")
        self.xslt = repo_object.get("xslt")
        self.mmd_to_iso_xslt_path = repo_object.get("mmd_to_iso_xslt_path", None)
        self.parent_list_path = repo_object.get("parent_list_path", "")
        self.context = context
        self.fts = False
        self.label = "MetNO/Solr"
        self.local_ingest = True
        self.solr_select_url = f"{self.filter.rstrip('/')}/select"
        self.dbtype = "Solr"
        self.user_agent = repo_object.get("user_agent_string")
        self.username = repo_object.get("username")
        self.password = repo_object.get("password")
        self.authentication = None

        # Setup requests session and connection pool
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.user_agent or "PyCSW SolrMETNORepository/4.0"
            }
        )
        if self.username is not None and self.password is not None:
            self.authentication = HTTPBasicAuth(self.username, self.password)
            self.session.auth = self.authentication
        LOGGER.info("Connected to solr backend: %s", self.filter)
        # End setup connection pooling

        self.stable_sort = repo_object.get("stable_sort", False)
        self.adc_collections = repo_object.get("adc_collections", [])
        # get the Solr mappings for main queryables
        self.query_mappings = repo_object.get("solr_mappings", {})
        self.facets = repo_object.get("facets", [])

        # APISO clients (the apiso profile is the one enabled here) send the
        # spatial property as `apiso:BoundingBox`, but solr_mappings only
        # declares `ows:BoundingBox` / `geometry`. Without a mapping entry
        # pygeofilter passes the property name through to Solr verbatim as a
        # field name, and Solr rejects it ("undefined field: apiso:BoundingBox"),
        # surfaced to the client as a generic "Invalid query syntax". Alias the
        # apiso spatial queryable onto whatever Solr field the existing spatial
        # queryables resolve to, so the field name is derived (not hardcoded).
        if "apiso:BoundingBox" not in self.query_mappings:
            spatial_field = (
                self.query_mappings.get("ows:BoundingBox")
                or self.query_mappings.get("geometry")
            )
            if spatial_field:
                self.query_mappings["apiso:BoundingBox"] = spatial_field
                LOGGER.debug(
                    "Aliased apiso:BoundingBox -> Solr field '%s'", spatial_field
                )

        # CQL clients commonly use unprefixed queryables (`AnyText LIKE ...`).
        # Unmapped, the name reaches Solr as a field that does not exist, so
        # alias each prefixed mapping under its local name unless that name
        # is already mapped explicitly.
        for key, field in list(self.query_mappings.items()):
            _, sep, local_name = key.partition(":")
            if sep and local_name not in self.query_mappings:
                self.query_mappings[local_name] = field

        # generate core queryables db and obj bindings
        self.queryables = {}

        for tname in self.context.model["typenames"]:
            for qname in self.context.model["typenames"][tname]["queryables"]:
                self.queryables[qname] = {}
                items = self.context.model["typenames"][tname]["queryables"][qname].items()

                for qkey, qvalue in items:
                    self.queryables[qname][qkey] = qvalue

        # flatten all queryables
        self.queryables["_all"] = {}
        for qbl in self.queryables:
            self.queryables["_all"].update(self.queryables[qbl])
        self.queryables["_all"].update(self.context.md_core_model["mappings"])
        LOGGER.debug("Querables: %s", self.queryables)
        LOGGER.info("SolrMETNORepository initialized.")

    def __del__(self):
        self.session.close()

    def _raise_solr_request_error(
        self,
        err: requests.exceptions.RequestException,
        action: str,
        url: str,
    ) -> None:
        """Normalize requests failures into concise RuntimeError messages."""
        response_text = None
        if isinstance(err, requests.exceptions.HTTPError) and err.response is not None:
            response_text = err.response.text

        reason = response_text or str(err)
        msg = f"Solr {action} failed for {url}: {reason}"
        LOGGER.error(msg)
        raise RuntimeError(msg) from err

    def describe(self) -> dict:
        """Derive table columns and types"""

        type_mappings = {
            "TEXT": "string",
            "VARCHAR": "string",
            "text_en": "string",
            "text_nb": "string",
            "text_und": "string",
            "text_general": "string",
            "pdate": "string",
            "date_range": "string",
            "bbox": "string",
            "geospatial_bounds3d": "string",
            "string": "string",
            "type": "string",
            "pdouble": "number",
            "pint": "integer",
            "text_gen_sort": "string",
            "platform_ancillary_cloud_coverage": "string",
            "platform_name": "string",
            "platform_instrument_name": "string",
            }

        try:
            url = f"{self.filter}/schema/fields"
            response = self.session.get(url)
            response.raise_for_status()
            payload = cast(dict[str, Any], response.json())
            if not isinstance(payload, dict):
                raise RuntimeError(
                    f"Unexpected schema response from {url}: expected object"
                )
        except requests.exceptions.RequestException as err:
            self._raise_solr_request_error(err, "schema fields query", url)

        properties = {
            "geometry": {
                "$ref": "https://geojson.org/schema/Polygon.json",
                "x-ogc-role": "primary-geometry",
            }
        }

        for field in payload.get("fields", []):
            LOGGER.debug(f"Processing field: {field['name']} of type {field['type']}")
            if field["name"] in self.query_mappings.values():
                pname = dict((v, k) for k, v in self.query_mappings.items()).get(field["name"])
                reverse_map: dict[str, str] = {v: k for k, v in self.query_mappings.items()}
                pname = reverse_map.get(field["name"])
                if pname is None:
                    continue
                LOGGER.debug(f"Mapping Solr field '{field['name']}' to queryable '{pname}'")
                properties[pname] = {"title": pname}
                LOGGER.debug(f"Field '{field['name']}' mapped to queryable '{pname}'")
                if field["type"] in type_mappings:
                    LOGGER.debug(f"Mapping Solr field type '{field['type']}' to '{type_mappings[field['type']]}'")
                    properties[pname]["type"] = type_mappings[field["type"]]
                    if field["type"] == "pdate":
                        properties[pname]["format"] = "date-time"
                    if field["type"] == "date_range":
                        properties[pname]["format"] = "interval"
                if pname == "identifier":
                    properties[pname]["x-ogc-role"] = "id"

        properties["type"] = {"type": "string"}
        LOGGER.debug("Properties/describe: %s", properties)
        return properties

    def dataset(self, record):
        """
        Stub to mock a pycsw dataset object for Transactions
        """

        return type("dataset", (object,), record)

    def query_ids(self, ids: list) -> list:
        """
        Query by list of identifiers
        """
        all_ids = '" OR "'.join(ids)
        query = SolrDSLQuery(filters=f'metadata_identifier:("{all_ids}")')

        if self.adc_collections not in ["", None]:
            query.add_filter(f"collection:({self._collection_filter()})")

        resp = self.do_query(query)

        return resp.get("results", [])

    def query_collections(self, collection=None, filters=None, limit=25) -> list:
        """Query for parent collections"""

        results = []
        if collection is not None:
            LOGGER.debug("query_collection collection argument %s", collection)
            query = SolrDSLQuery(f"metadata_identifier:\"{collection}\"", filters="isParent:true")

        else:
            query = SolrDSLQuery(filters="isParent:true")
        query["limit"] = 200
        if self.adc_collections not in ["", None]:
            query.add_filter(f"collection:({self._collection_filter()})")
        LOGGER.debug(f"Query collections with filters: {filters}")
        resp = self.do_query(query)
        results = resp.get("results", [])
        LOGGER.debug("Collection returned %s of total %s results with limit: %s", len(results), resp.get("total", 0), query["limit"])
        return results

    def query_domain(self, domain, typenames, domainquerytype="list", count=False) -> list:
        """
        Query by property domain values
        """

        results = []

        params = {
            "q": "*:*",
            "rows": 0,
            "facet": "true",
            "facet.query": "distinct",
            "facet.type": "terms",
            "facet.field": domain,
            "fq": ["metadata_status:Active"],
        }

        if self.adc_collections not in ["", None]:
            params["fq"].append(f"collection:({self._collection_filter()})")

        try:
            url = f"{self.filter}/select"
            response = self.session.get(url, params=params)
            response.raise_for_status()
            response_json = response.json()
            payload = cast(dict[str, Any], response_json)
            if not isinstance(payload, dict):
                raise RuntimeError(
                    f"Unexpected schema response from {url}: expected object"
                )
        except requests.exceptions.RequestException as err:
            self._raise_solr_request_error(err, "domain query", url)

        counts = payload["facet_counts"]["facet_fields"][domain]

        for term in zip(*([iter(counts)] * 2), strict=False):
            LOGGER.debug(f"Term: {term}")
            results.append(term)

        return results

    def query_insert(self, direction="max") -> str:
        """
        Query to get latest (default) or earliest update to repository
        """

        if direction == "min":
            sort_order = "asc"
        else:
            sort_order = "desc"

        params = {
            "q": "*:*",
            "q.op": "OR",
            "fl": "timestamp",
            "sort": f"timestamp {sort_order}",
            "fq": ["metadata_status:Active"],
        }

        if self.adc_collections not in ["", None, []]:
            params["fq"].append(f"collection:({self._collection_filter()})")

        try:
            url = f"{self.filter}/select"
            response = self.session.get(url, params=params)
            response.raise_for_status()
            response_json = response.json()
            payload = cast(dict[str, Any], response_json)
            if not isinstance(payload, dict):
                raise RuntimeError(
                    f"Unexpected schema response from {url}: expected object"
                )
        except requests.exceptions.RequestException as err:
            self._raise_solr_request_error(err, "insert-date query", url)

        try:
            timestamp = datetime.strptime(payload["response"]["docs"][0]["timestamp"], "%Y-%m-%dT%H:%M:%S.%fZ")
        except IndexError:
            timestamp = datetime.now()

        return timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")

    def query_source(self, source):
        """
        Query by source
        """

        return NotImplementedError()

    def query(self, constraint=None, sortby=None, typenames=None, maxrecords=10, startposition=0) -> tuple:
        """
        Query records from underlying repository
        """

        solr_query = {}

        """Handle Constraints"""
        solr_query = cast(SolrDSLQuery, SolrDSLQuery())
        if constraint is not None and constraint.get("ast") is not None:
            constraint = constraint.get("ast")
            # ask pygeofilter to convert AST to Solr query
            LOGGER.debug(constraint)
            # LOGGER.debug(ast.get_repr(constraint))

            """rewrite Not GeometryDisjoint ast to GeometryIntersects.
            """
            constraint = handleNotGeometryDisjoint(constraint)

            LOGGER.debug(f"AST Query: {ast.get_repr(cast(ast.Node, constraint))}")
            """ generate solr filter and do csw to solr fields query mappings"""
            filtered = to_filter(constraint, self.query_mappings)
            #solr_query = self._remove_type_item(solr_query)
            solr_query = cast(SolrDSLQuery, self._rewrite_type_terms(filtered))
            LOGGER.debug(f"SOLR Query from to_filter: {solr_query}")
        else:
            # DO NOT ask pygeofilter to convert AST to Solr query
            solr_query = SolrDSLQuery()

        # add handle sortby, maxrecords, startposition
        solr_query["offset"] = startposition
        # Respect the per-request maxrecords from the CSW client. A previous
        # refactor wired this to METNO_PYCSW_COLLECTIONS_LIMIT, which both
        # ignored the client's pagination size and (with the test container's
        # env value of 1) silently capped every GetRecords response at one
        # record while still reporting numberOfRecordsMatched as the true
        # Solr hit count — producing matched-vs-returned mismatches downstream.
        solr_query["limit"] = maxrecords

        LOGGER.debug(f"Sortby: {sortby}")
        if sortby is not None:
            solr_query["sort"] = f"{self.query_mappings[sortby['propertyname']]} {sortby['order']}"
            LOGGER.debug(f"Sort: {solr_query['sort']}")

        LOGGER.debug(f"Final Solr query: {solr_query}")
        resp = self.do_query(solr_query)
        total = resp.get("total", 0)
        results = resp.get("results", [])
        return total, results


    # pycsw/STAC record types, as produced by to_filter() through the
    # `type: isChild` mapping, onto the boolean flags that _doc2record() uses
    # to type records: isParent -> "series", everything else -> "dataset".
    # Left as-is, Solr parses the string values on the boolean isChild field
    # as false.
    TYPE_TERM_REWRITES = {
        'isChild:"item"': "isParent:false",
        'isChild:"dataset"': "isParent:false",
        'isChild:"series"': "isParent:true",
        'typename:"stac:Collection"': "isParent:true",
    }

    def _rewrite_type_terms(self, data):
        """
        Recursively rewrite record type terms (see TYPE_TERM_REWRITES) in a
        Solr query: the top-level query string, bool clauses and filters.

        :param data: The query string, dictionary or list to traverse.
        :return: The query with type terms rewritten.
        """
        if isinstance(data, str):
            negated = data.startswith("-")
            term = data[1:] if negated else data
            if term in self.TYPE_TERM_REWRITES:
                return ("-" if negated else "") + self.TYPE_TERM_REWRITES[term]
            return data

        if isinstance(data, dict):
            for key, value in data.items():
                data[key] = self._rewrite_type_terms(value)
        elif isinstance(data, list):
            data = [self._rewrite_type_terms(item) for item in data]

        return data


    def _collection_filter(self):
        """
        Get the collections to filter from config and generate solr collection filter string
        """
        collections_filter = ' '.join(self.adc_collections)
        LOGGER.debug(f"MMD Collections filter: {collections_filter}")
        return collections_filter


    def _doc2record(self, doc: dict):
        """
        Transform a Solr doc into a pycsw dataset object
        """
        # LOGGER.debug("Transforming Solr doc to pycsw dataset object: %s", doc)
        record = {}

        record["identifier"] = doc["metadata_identifier"]
        record["metadata_type"] = "application/xml"
        record["schema"] = "http://www.isotc211.org/2005/gmd"
        # typename is set to 'gmd:MD_Metadata' only when we have ISO XML in record['xml'];
        # APISO write_record() fast-paths esn='full' only when typename=='gmd:MD_Metadata',
        # calling etree.fromstring(xml_blob) — if xml_blob is None that crashes with memoryview error.

        if "isParent" in doc and doc["isParent"]:
            record["type"] = "series"
        else:
            record["type"] = "dataset"

        if "isChild" in doc and doc["isChild"]:
            record["parentidentifier"] = doc["related_dataset"][0]
        else:
            record["parentidentifier"] = None

        # BBox for geometry
        record["wkt_geometry"] = None
        if "geometry_wkt" in doc:
            record["wkt_geometry"] = doc["geometry_wkt"]

        record["title"] = None
        record["title"] = doc["title"]
        record["abstract"] = None
        record["abstract"] = doc["abstract"]

        if "iso_topic_category" in doc:
            filtered_iso_topic_category = [item for item in doc["iso_topic_category"] if item != "Not available"]
            if filtered_iso_topic_category:
                record["topicategory"] = ",".join(filtered_iso_topic_category)
            else:
                record["topicategory"] = None
        else:
            record["topicategory"] = None


        record["source"] = None

        record["language"] = doc.get("dataset_language", "en")

        # Transform the indexed time as insert_data
        insert = dparser.parse(doc["timestamp"][0])
        record["insert_date"] = insert.isoformat()
        record["date"] = insert.isoformat()

        # Transform the last metadata update datetime as modified
        record["date_creation"] = None
        record["date_modified"] = None
        record["date_publication"] = None

        if "last_metadata_updated_date" in doc:
            last_metadata_updated = doc["last_metadata_updated_date"]
            #modified = dparser.parse(last_metadata_updated)
            #record["date_modified"] = modified.isoformat()
            record["date_modified"] = last_metadata_updated

        if "last_metadata_created_date" in doc:
            #record["date_creation"] = modified.isoformat()
            #created = dparser.parse(doc["last_metadata_created_date"])
            created = doc["last_metadata_created_date"]
            #record["date_creation"] = created.isoformat()
            record["date_creation"] = created
            #record["date_publication"] = created.isoformat()
            record["date_publication"] = created

        if "dataset_citation_publication_date" in doc:
            publication_date = dparser.parse(doc["dataset_citation_publication_date"][0])
            record["date_publication"] =  publication_date.isoformat()


        # Transform temporal extent values from potentially multi-valued arrays.
        # We expose one interval using earliest start and latest end.
        record["time_begin"] = None
        record["time_end"] = None
        time_begin, time_end = extract_temporal_extent(
            doc.get("temporal_extent_start_date"),
            doc.get("temporal_extent_end_date"),
        )
        record["time_begin"] = time_begin
        record["time_end"] = time_end

        links = []
        assets = {}
        # LOGGER.debug(f"Processing record properties: {list(record.keys())}")
        record["relation"] = None

        # name, description, protocol, url
        if "data_access_url_opendap" in doc:
            assets['opendap-data'] = {
                    "name": "OPeNDAP access",
                    "description": "OPeNDAP access",
                    "rel": "OPeNDAP:OPeNDAP",
                    "url": doc["data_access_url_opendap"][0],
                    "roles": ["data"]
                }
            links.append(
                {
                    "name": "OPeNDAP:OPeNDAP",
                    "description": "OPeNDAP access",
                    #"rel": "OPeNDAP:OPeNDAP",
                    "rel": "enclosure",
                    #"type": "OPeNDAP:OPeNDAP",
                    "protocol": "OPeNDAP:OPeNDAP",
                    "url": doc["data_access_url_opendap"][0],
                }
            )
        if "data_access_url_ogc_wms" in doc:
            links.append(
                {
                    "name": "OGC:WMS",
                    "description": "OGC-WMS Web Map Service",
                    "rel": "enclosure",
                    "protocol": "OGC:WMS",
                    #"rel": "describes",
                    "url": doc["data_access_url_ogc_wms"][0],
                }
            )
        if "data_access_url_http" in doc:
            assets['download-data'] = {
                    "name": "File for download",
                    "description": "Direct HTTP download",
                    "rel": "WWW:DOWNLOAD-1.0-http--download",
                    "url": doc["data_access_url_http"][0],
                    "roles": ["data"]
                }
            links.append(
                {
                    "name": "WWW:DOWNLOAD-1.0-http--download",
                    "description": "Direct HTTP download",
                    #"rel": "WWW:DOWNLOAD-1.0-http--download",
                    "protocol": "WWW:DOWNLOAD-1.0-http--download",
                    "rel": "enclosure",
                    #"type": "WWW:DOWNLOAD-1.0-http--download",
                    "url": doc["data_access_url_http"][0],
                }
            )
        if "data_access_url_ftp" in doc:
            links.append(
                {
                    "name": "ftp",
                    "description": "Direct FTP download",
                    "protocol": "ftp",
                    "rel": "ftp",
                    #"type": "ftp",
                    "url": doc["data_access_url_ftp"][0],
                }
            )
        if "related_url_landing_page" in doc:
            links.append(
                {
                    "name": "Dataset landing page",
                    "description": "URL of the dataset landing page",
                    "rel": "about",
                    "url": doc["related_url_landing_page"],
                }
            )
        if "use_constraint_resource" in doc and doc['use_constraint_resource'] != 'Not provided':
            links.append(
                {
                    "name": "License",
                    "description": "URL of the license",
                    "rel": "license",
                    "url": doc["use_constraint_resource"],
                }
            )
        if "dataset_citation_doi" in doc:
            # dataset_citation_doi is multiValued in Solr; pycsw csw2._write_record
            # expects link['url'] to be a string and will TypeError on a list.
            doi = doc["dataset_citation_doi"]
            if isinstance(doi, list):
                doi = doi[0] if doi else None
            if doi:
                links.append(
                    {
                        #"name": "Citation DOI",
                        "title": "Citation DOI",
                        "description": "Citation DOI",
                        "rel": "cite-as",
                        "url": doi,
                    }
                )
        # pycsw's own outputschemas (fgdc, gm03, upstream dif) read
        # link['name'] / link['protocol'] directly and fail with KeyError
        # when they are missing; fgdc also writes protocol as an XML
        # attribute, so it must be a string.
        for link in links:
            link.setdefault("name", link.get("title") or link["description"])
            link.setdefault("protocol", "WWW:LINK")
        record["links"] = json.dumps(links)
        record["assets"] = json.dumps(assets)
        personnel = doc.get("personnel_json", [])
        # LOGGER.debug(f"assets: {record['assets']}")

        creators = []
        contributors = []
        contacts = []


        for entry in personnel:
            if not isinstance(entry, dict):
                continue

            role_raw = str(entry.get("role", "")).strip()
            name = str(entry.get("name", "")).strip()
            org = str(entry.get("organisation", "")).strip()
            email = str(entry.get("email", "")).strip()

            if not name:
                continue

            role_norm = normalize_contact_role(role_raw)

            if role_norm == "creator":
                creators.append(name)
            elif role_norm == "contributor":
                contributors.append(name)

            contact = {
                "name": name,
                "organization": org,
                "role": role_norm,
                "email": email,
            }
            contacts.append(contact)

        # Keep same output fields as before
        if creators:
            record["creator"] = ",".join(creators)
        if contributors:
            record["contributor"] = ",".join(contributors)

        record["contacts"] = json.dumps(contacts)
        record["providers"] = json.dumps(contacts)
        #LOGGER.debug(f"Record contacts is of type: {type(contacts)}")
        #LOGGER.debug(f"Parsed contacts for record {record['identifier']}: {contacts}")
        #LOGGER.debug("Jsonified contacts: %s", str(json.dumps(contacts)))
        #record["contacts"] = json.dumps([contacts])# if contacts else None
        # record["contacts"] = json.dumps([{'role': 'creator', 'name': 'satan'}])
        # record['themes'] = keywords2themes(doc)
        record["themes"], record["keywords"] = keywords2themes(doc)

        if "platform_name" in doc:
            record["platform"] = doc["platform_name"][0]
        else:
            record["platform"] = None
        if "platform_instrument_name" in doc:
            record["instrument"] = doc["platform_instrument_name"][0]
        else:
            record["instrument"] = None
        if "platform_ancillary_cloud_coverage" in doc:
            record["cloudcover"] = doc["platform_ancillary_cloud_coverage"][0]
        else:
            record["cloudcover"] = None
        # TODO: rights is mapped to accessconstraint, although we provide this
        # info in the use constraint.
        # we should use dc:license instead, but it is not mapped in csw.
        if "use_constraint_license_text" in doc:
            record["otherconstraints"] = doc.get("use_constraint_license_text")
        else:
            record["otherconstraints"] = doc.get("use_constraint_identifier")

        # this is mapped to rights. We do not have it
        record["conditionapplyingtoaccessanduse"] = None

#        if "dataset_citation_publisher" in doc:
#            record["publisher"] = doc["dataset_citation_publisher"][0]

        if "storage_information_file_format" in doc:
            record["format"] = doc["storage_information_file_format"]
        else:
            record["format"] = "Not provided"

        mmd_xml_str = doc.get("mmd_xml_file")
        record["mmd_xml_file"] = mmd_xml_str  # always set; None when absent from Solr doc
        if mmd_xml_str and self.mmd_to_iso_xslt_path is not None:
            try:
                xslt_path = self.mmd_to_iso_xslt_path
                mmd_doc = lxml_etree.fromstring(mmd_xml_str.encode("utf-8"))
                transform = lxml_etree.XSLT(lxml_etree.parse(xslt_path))
                pl = self.parent_list_path
                kw = {}
                if os.path.exists(pl):
                    kw["path_to_parent_list"] = lxml_etree.XSLT.strparam(pl)
                result_tree = transform(mmd_doc, **kw).getroot()
                record["xml"] = lxml_etree.tostring(result_tree, encoding="unicode").encode("utf-8")
                record["typename"] = "gmd:MD_Metadata"
            except Exception as exc:
                LOGGER.warning("MMD→ISO transform failed for %s: %s",
                               doc.get("metadata_identifier"), exc)
        params = {"q.op": "OR", "q": f"metadata_identifier:{doc['metadata_identifier']}"}

        mdsource_url = self.solr_select_url + urlencode(params)
        record["mdsource"] = mdsource_url
        # LOGGER.debug(f"RECORD PROPERTIES: {list(record.keys())}")
        # LOGGER.debug(f"RECORD PROPERTIES: {dir(record)}")
        # LOGGER.debug("recor\n %s",record)
        return self.dataset(record)


    def get_facets(self, ast=None) -> dict:
        """
        Gets all facets for a given query

        :returns: `dict` of facets
        """

        facets_results = {}

        # Generate solr facet queries
        facets_query = SolrDSLQuery()
        facets_query['facet'] = {}
        for facet in self.facets:
            LOGGER.debug(f'Running facet query for {facet}')
            facets_query['facet'][facet] = {
                        "type": "terms",
                        "field": facet,
                        "limit": 20
            }
            facets_results[facet] = {
                'type': 'terms',
                'property': facet,
                'buckets': []
            }
        LOGGER.debug(f"FINAL facetq is: {facets_query}")

        # Send facet query to Solr
        resp = self.do_query(facets_query, return_results=False)
        LOGGER.debug(f"SOLR FACETS RESULTS: {resp['facets']}")

        # Process facets solr results and generate pycsw facets_results dict
        for facet in self.facets:
            bucket = resp['facets'][facet]['buckets']
            for fq in bucket:
                facets_results[facet]['buckets'].append({
                    'value': fq['val'],
                    'count': fq['count']
                })
        facets_results[facet]['buckets'].sort(key=itemgetter('count'), reverse=True)
        LOGGER.debug(f"PYCSW facet results: {facets_results}")
        return facets_results

    def ping(self):
        """
        Ping the Solr service
        """
        try:
            url = f"{self.filter}/admin/ping"
            response = self.session.get(url, auth=self.authentication)
            response.raise_for_status()
            payload = cast(dict[str, Any], response.json())
            if not isinstance(payload, dict):
                raise RuntimeError(
                    f"Unexpected schema response from {url}: expected object"
                )
        except requests.exceptions.RequestException as err:
            self._raise_solr_request_error(err, "ping", url)
        status = payload["status"]
        LOGGER.info(f"Solr Ping {self.filter}: {status}")

    def do_query(self, solr_query, return_results=True) -> dict:
        results = []

        # Make sure we have default filtes
        filters = solr_query.get('filter', None)
        if filters is not None:
            str_filters = [f for f in filters if isinstance(f, str)]
            contains_collection = any("collection:" in f for f in str_filters)
            if not contains_collection:
                solr_query['filter'].append(f"collection:({self._collection_filter()})")

            contains_metadata_status = any("metadata_status:" in f for f in str_filters)
            if not contains_metadata_status:
                solr_query['filter'].append("metadata_status:Active")
        else:
            solr_query.add_filter(f"collection:({self._collection_filter()})")
            solr_query.add_filter("metadata_status:Active")
        # Set fields
        solr_fields = [
            "id",
            "title",
            "abstract",
            "related_dataset_id",
            "personnel_organisation",
            "project_long_name",
            "project_short_name",
            "project_name",
            "temporal_extent_start_date",
            "temporal_extent_end_date",
            "last_metadata_updated_date",
            "last_metadata_created_date",
            "geographic_extent_*",
            "abstract",
            "related_url*",
            "related_dataset",
            "bbox",
            "geometry_wkt",
            "isParent",
            "isChild",
            "data_access_url_opendap",
            "feature_type",
            "data_access_url_http",
            "use_constraint_identifier",
            "use_constraint_resource",
            "use_constraint_license_text",
            "iso_topic_category",
            "activity_type",
            "dataset_production_status",
            "metadata_status",
            "data_center_name",
            "geographic_extent_rectangle_*",
            "last_metadata_updated_date",
            "last_metadata_created_date",
            "platform_name",
            "platform_instrument_name",
            "platform_ancillary_cloud_coverage",
            "data_center_url",
            "personnel_name",
            "metadata_identifier",
            "collection",
            "timestamp",
            "keywords_*",
            "dataset_citation_doi",
            "data_access_url_ftp",
            "data_access_url_http",
            "data_access_url_ogc_wms",
            "data_access_wms_layers",
            "storage_information_file_format",
            "personnel_json:[json]",
            "data_access_json:[json]",
            "platform_json:[json]",
            "related_information_json:[json]",
            "last_metadata_update_json:[json]",
            "dataset_citation_json:[json]",
            "mmd_xml_file",
        ]
        # The /select handler may default to edismax, which treats local
        # params in the query string (e.g. {!complexphrase} from pygeofilter)
        # as plain text. The queries built here are Lucene syntax.
        solr_query['params'] = {
            "defType": "lucene",
            "fl": solr_fields,
        }
        LOGGER.debug(f"Solr filter queries: {filters}")
        LOGGER.debug(f"Do Solr query with query: {solr_query}")
        try:
            #url = f"{self.filter}/select?fl={','.join(solr_fields)}"
            url = self.solr_select_url
            response = self.session.post(url, json=solr_query, auth=self.authentication)
            response.raise_for_status()
            payload = cast(dict[str, Any], response.json())
            if not isinstance(payload, dict):
                raise RuntimeError(
                    f"Unexpected schema response from {url}: expected object"
                )
        except requests.exceptions.RequestException as err:
            self._raise_solr_request_error(err, "record query", url)

        total = payload["response"]["numFound"]
        LOGGER.debug(f"Found: {total}")
        if return_results:
            for doc in payload["response"]["docs"]:
                results.append(self._doc2record(doc))

            return {
                "total": total,
                "results": results,
            }
        else:
            return payload


def keywords2themes(doc: dict) -> tuple:
    themes = []
    kvoctothesaurus = {
        "GCMDSK": "https://gcmd.earthdata.nasa.gov/kms/concepts/concept_scheme/sciencekeywords",
        "CFSTDN": "https://vocab.nerc.ac.uk/standard_name/",
        "GEMET": "http://inspire.ec.europa.eu/theme",
        "NORTHEMES": "https://register.geonorge.no/metadata-kodelister/nasjonal-temainndeling",
        "GCMDPROV": "https://gcmd.earthdata.nasa.gov/kms/concepts/concept_scheme/providers",
        "GCMDLOC": "https://gcmd.earthdata.nasa.gov/kms/concepts/concept_scheme/locations",
        "GCMDPLT": "https://gcmd.earthdata.nasa.gov/kms/concepts/concept_scheme/platforms",
        "GCMDINST": "https://gcmd.earthdata.nasa.gov/kms/concepts/concept_scheme/instruments",
    }
    keywords = ""
    for vocab in set(doc.get("keywords_vocabulary", [])):
        voc = "gcmd" if vocab == "GCMDSK" else vocab
        vocab_keywords = list(doc.get(f"keywords_{voc.lower()}", []))
        if vocab in kvoctothesaurus:
            themes.append(
                {
                    "keywords": [{"name": v} for v in vocab_keywords],
                    #'scheme': key,
                    "thesaurus": {"title": vocab, "url": kvoctothesaurus[vocab]},
                }
            )
        else:
            keywords = ",".join(vocab_keywords)

    pltthesaurus = {
        "oscar": "https://space.oscar.wmo.int/satellites",
        "vocab.nerc.ac.uk/collection/C17": "https://vocab.nerc.ac.uk/collection/C17/current/",
        }

    if "platform_long_name" in doc:
        freeplt = []
        schemes_plt = {}
        for k in pltthesaurus:
            schemes_plt[k] = []
            for index, value in enumerate(doc["platform_long_name"]):
                if 'platform_resource' in doc:
                    if k in doc["platform_resource"][index]:
                        schemes_plt[k].append(value)
                    else:
                        if value not in freeplt:
                            freeplt.append(value)


        for key,value in schemes_plt.items():
            if value:
                themes.append(
                       {
                           "keywords": [{"name": v} for v in value],
                           "thesaurus": {"url": pltthesaurus[key]},
                       }
                   )
        if keywords != "":
            keywords += ','+','.join(freeplt)
        else:
            keywords = ','.join(freeplt)

    instthesaurus = {
        "oscar": "https://space.oscar.wmo.int/instruments",
        "vocab.nerc.ac.uk/collection/L22": "http://vocab.nerc.ac.uk/collection/L22/current/",
        "vocab.nerc.ac.uk/collection/L05": "http://vocab.nerc.ac.uk/collection/L05/current/",
        }

    if "platform_instrument_long_name" in doc:
        freeinst = []
        schemes_inst = {}
        for k in instthesaurus:
            schemes_inst[k] = []
            for index, value in enumerate(doc["platform_instrument_long_name"]):
                if 'platform_instrument_resource' in doc:
                    if k in doc["platform_instrument_resource"][index]:
                        schemes_inst[k].append(value)
                    else:
                        if value not in freeinst:
                            freeinst.append(value)


        for key,value in schemes_inst.items():
            if value:
                themes.append(
                       {
                           "keywords": [{"name": v} for v in value],
                           "thesaurus": {"url": instthesaurus[key]},
                       }
                   )
        if keywords != "":
            keywords += ','+','.join(freeinst)
        else:
            keywords = ','.join(freeinst)

    return json.dumps(themes), keywords


def personnel2contact(doc: dict, ct: str, index: int = 0) -> dict:
    contact = {}

    mmdrole2roles = {
        "metadata_author": "contributor",
        "technical": "contributor",
        "investigator": "creator",
        "datacenter": "publisher",
    }

    if f"personnel_{ct}_name" in doc:
        contact = {
            "name": doc[f"personnel_{ct}_name"][index],
            "organization": doc[f"personnel_{ct}_organisation"][index],
            "role": mmdrole2roles[f"{ct}"],
            "email": doc[f"personnel_{ct}_email"][index],
        }

    return contact


def handleNotGeometryDisjoint(
    node: ast.AstType,
    parent: Optional[Any] = None,
    parent_attr: Optional[str] = None,
) -> ast.AstType:
    """
    Traverse the AST and replace Not(GeometryDisjoint) with GeometryIntersects.

    :param node: The current AST node being processed.
    :param parent: The parent node of the current node (used for replacement).
    :param parent_attr: The attribute name in the parent node that references the current node.
    :return: The updated AST node.
    """
    # LOGGER.debug(f"Processing node of type: {type(node)}")

    # Check if the current node is an ast.Not
    if isinstance(node, ast.Not):
        # LOGGER.debug(f"Original Node: {ast.get_repr(node)}")

        # Check if the sub_node is a GeometryDisjoint
        sub_node = getattr(node, "sub_node", None)
        if isinstance(sub_node, ast.GeometryDisjoint):
            lhs, rhs = sub_node.get_sub_nodes()  # Get sub-nodes directly from sub_node
            new_node = ast.GeometryIntersects(cast(Any, lhs), cast(Any, rhs))
            LOGGER.debug(f"Replacing Not(GeometryDisjoint) with GeometryIntersects: {ast.get_repr(new_node)}")

            # Replace the ast.Not node in the parent
            if parent is not None and parent_attr:
                setattr(parent, parent_attr, new_node)  # Replace the node in the parent
            return new_node  # Return the new node to stop further traversal
    # Handle And/Combination nodes
    if isinstance(node, ast.Combination):
        # LOGGER.debug(f"Processing Combination node: {ast.get_repr(node)}")

        # Recursively process lhs and rhs
        if node.lhs:
            node.lhs = cast(ast.Node, handleNotGeometryDisjoint(node.lhs, parent=node, parent_attr="lhs"))
        if node.rhs:
            node.rhs = cast(ast.Node, handleNotGeometryDisjoint(node.rhs, parent=node, parent_attr="rhs"))

    # Recursively process sub-nodes
    get_sub_nodes = getattr(node, "get_sub_nodes", None)
    if callable(get_sub_nodes):
        for sub_node in cast(list[Any], get_sub_nodes()):
            # Pass the current node as the parent and the attribute name (if applicable)
            handleNotGeometryDisjoint(cast(ast.AstType, sub_node), parent=node, parent_attr="sub_node")

    return node


def normalize_contact_role(role: str) -> str:
    r = role.strip().lower()
    if r in {"investigator", "principal investigator"}:
        return "producer"
    if r in {"data center contact", "datacenter", "publisher"}:
        return "host"
    # metadata author, technical contact, and unknown roles as contributor
    return "producer"

def handleTypeItem(
    node: ast.AstType,
    parent: Optional[ast.AstType] = None,
    parent_attr: Optional[str] = None,
) -> ast.AstType:
    """
    Traverse the AST and remove ATTRIBUTE type = 'item'.

    :param node: The current AST node being processed.
    :param parent: The parent node of the current node (used for replacement).
    :param parent_attr: The attribute name in the parent node that references the current node.
    :return: The updated AST node.
    """
    # Keep backward-compatible behavior while delegating traversal logic
    # to the actively used helper with broader ast type handling.
    return handleNotGeometryDisjoint(node, parent=parent, parent_attr=parent_attr)

def _as_list(value) -> list:
    """Normalize scalar/list temporal field values to a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def extract_temporal_extent(start_values, end_values) -> tuple[Optional[str], Optional[str]]:
    """Extract (earliest start, latest end) from Solr temporal extent arrays.

    Open-ended intervals are encoded as one more start than end element.
    In that case, ``time_end`` must be ``None``.
    """
    starts_raw = [v for v in _as_list(start_values) if v not in [None, ""]]
    ends_raw = [v for v in _as_list(end_values) if v not in [None, ""]]

    parsed_starts = []
    for value in starts_raw:
        try:
            parsed_starts.append(dparser.parse(str(value)))
        except Exception:
            LOGGER.warning("Could not parse temporal_extent_start_date value: %s", value)

    parsed_ends = []
    for value in ends_raw:
        try:
            parsed_ends.append(dparser.parse(str(value)))
        except Exception:
            LOGGER.warning("Could not parse temporal_extent_end_date value: %s", value)

    time_begin = min(parsed_starts).isoformat() if parsed_starts else None

    # Open interval rule: one extra start value means end is open.
    if len(starts_raw) > len(ends_raw):
        time_end = None
    else:
        time_end = max(parsed_ends).isoformat() if parsed_ends else None

    return time_begin, time_end

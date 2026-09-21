# -*- coding: iso-8859-15 -*-
# =================================================================
#
# Authors: Tom Kralidis <tomkralidis@gmail.com>
#          Massimo Di Stefano <massimods@met.no>
#          Magnar Martinsen <magnarem@met.no>
#
# Copyright (c) 2022 Tom Kralidis
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

import base64
import configparser
from datetime import datetime, timezone
import dateutil.parser as dparser
import logging
from urllib.parse import urlencode

import requests

from pycsw.core import util
from pycsw.core.etree import etree
import os

from http.client import HTTPConnection  # py3

import json
from pycsw.plugins.repository.solr_query_handler import QueryHandler, base_filters

LOGGER = logging.getLogger(__name__)
# HTTPConnection.debuglevel = 1

from requests.auth import HTTPBasicAuth

from pycsw.plugins.repository.solr_helper import (
    get_collection_filter,
    get_config_parser,
    parse_time_query,
    parse_field_query,
    parse_field_OR_query,
    parse_bbox_OR_query,
    parse_bbox_query,
    parse_apiso_query,
    get_iso_transformer,
    get_solr_connection,
)

# (connect, read) timeouts in seconds; the read timeout stays below the
# default gunicorn worker timeout (30s) so SOLR errors are reported, not killed
SOLR_TIMEOUT = (5, 25)


def _first(value):
    """First value of a multi-valued SOLR field, or the value of a single-valued one"""
    return value[0] if isinstance(value, list) else value


class SOLRMETNORepository(object):
    """
    Class to interact with underlying METNO SOLR backend repository
    """

    def __init__(self, context, repo_filter=None):
        """
        Initialize repository
        """
        self.context = context
        self.filter = repo_filter
        self.fts = False
        self.label = "MetNO/SOLR"
        self.local_ingest = True
        self.solr_select_url = "%s/select" % self.filter
        self.dbtype = "SOLR"

        self.username, self.password = get_solr_connection()
        self.authentication = HTTPBasicAuth(self.username, self.password)
        self.adc_collection_filter = get_collection_filter()

        # XSLT used to transform MMD records to ISO, compiled on first use
        self.iso_transformer_file = get_iso_transformer()
        self._iso_transform = None

        # generate core queryables db and obj bindings
        self.queryables = {}

        self.query_handler = QueryHandler(self.adc_collection_filter)

        for tname in self.context.model["typenames"]:
            for qname in self.context.model["typenames"][tname]["queryables"]:
                self.queryables[qname] = {}
                items = self.context.model["typenames"][tname]["queryables"][
                    qname
                ].items()

                for qkey, qvalue in items:
                    self.queryables[qname][qkey] = qvalue

        # flatten all queryables
        self.queryables["_all"] = {}
        for qbl in self.queryables:
            self.queryables["_all"].update(self.queryables[qbl])
        self.queryables["_all"].update(self.context.md_core_model["mappings"])

    def dataset(self, record):
        """
        Stub to mock a pycsw dataset object for Transactions
        """
        return type("dataset", (object,), record)

    def _select(self, params):
        """
        Run a SOLR select request and return the decoded JSON response
        """
        response = requests.get(
            self.solr_select_url,
            params=params,
            auth=self.authentication,
            timeout=SOLR_TIMEOUT,
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not response.ok or "response" not in body:
            message = body.get("error", {}).get("msg") or response.text[:500]
            LOGGER.error("SOLR request failed (HTTP %s): %s", response.status_code, message)
            raise RuntimeError(
                "SOLR request failed (HTTP %s): %s" % (response.status_code, message)
            )
        return body

    def query_ids(self, ids):
        """
        Query by list of identifiers
        """
        if not ids:
            return []

        quoted_ids = [
            '"%s"' % i.replace("\\", "\\\\").replace('"', '\\"') for i in ids
        ]
        params = {
            "fq": base_filters(self.adc_collection_filter)
            + ["metadata_identifier:(%s)" % " OR ".join(quoted_ids)],
            "q": "*:*",
            "rows": len(ids),
        }
        LOGGER.debug("query_ids params: %s", params)

        response = self._select(params)
        return [self._doc2record(doc) for doc in response["response"]["docs"]]

    def query_domain(self, domain, typenames, domainquerytype="list", count=False):
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
            "fq": base_filters(self.adc_collection_filter),
        }
        LOGGER.debug("query_domain params: %s", params)

        response = self._select(params)

        counts = response["facet_counts"]["facet_fields"][domain]

        for term in zip(*([iter(counts)] * 2)):
            LOGGER.debug("Term: %s", term)
            results.append(term)

        return results

    def query_insert(self, direction="max"):
        """
        Query to get latest (default) or earliest update to repository
        """
        if direction == "min":
            sort_order = "asc"
        else:
            sort_order = "desc"

        params = {
            "q": "*:*",
            "fl": "timestamp",
            "rows": 1,
            "sort": "timestamp %s" % sort_order,
            "fq": base_filters(self.adc_collection_filter),
        }

        docs = self._select(params)["response"]["docs"]
        if docs and "timestamp" in docs[0]:
            timestamp = dparser.parse(_first(docs[0]["timestamp"]))
        else:
            timestamp = datetime.now(timezone.utc)

        return timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")

    def query_source(self, source):
        """
        Query by source
        """
        return NotImplementedError()

    def query(
        self, constraint, sortby=None, typenames=None, maxrecords=10, startposition=0
    ):
        """
        Query records from underlying repository
        """
        params = self.query_handler.query(constraint, sortby=sortby)
        params["rows"] = maxrecords
        params["start"] = startposition
        LOGGER.info("QUERY PARAMETERS: %s", params)

        response = self._select(params)

        total = response["response"]["numFound"]
        LOGGER.debug("Found: %s", total)
        results = [self._doc2record(doc) for doc in response["response"]["docs"]]

        return str(total), results

    def _transform_to_iso(self, doc_):
        if self._iso_transform is None:
            self._iso_transform = etree.XSLT(etree.parse(self.iso_transformer_file))
        pl = '/usr/local/share/parent_list.xml'
        return self._iso_transform(doc_, path_to_parent_list=etree.XSLT.strparam(pl)).getroot()

    def _doc2record(self, doc):
        """
        Transform a SOLR doc into a pycsw dataset object
        """

        record = {}

        record["identifier"] = doc["metadata_identifier"]
        record["typename"] = "gmd:MD_Metadata"
        record["schema"] = "http://www.isotc211.org/2005/gmd"
        # check for parent-child relationship
        if 'isParent' in doc and doc["isParent"]:
            record["type"] = "series"
        else:
            record["type"] = "dataset"
        #
        if 'isChild' in doc and doc["isChild"] and doc.get("related_dataset"):
            record["parentidentifier"] = doc["related_dataset"][0]
        if "bbox" in doc:
            record["wkt_geometry"] = doc["bbox"]
        record["title"] = _first(doc.get("title", ""))
        record["abstract"] = _first(doc.get("abstract", ""))
        if "iso_topic_category" in doc:
            record["topicategory"] = ",".join(doc["iso_topic_category"])
        if "keywords_keyword" in doc:
            record["keywords"] = ",".join(doc["keywords_keyword"])
        # record['source'] = doc['related_url_landing_page'][0]
        if "data_access_url_opendap" in doc:
            record["source"] = doc["data_access_url_opendap"][0]
        elif "data_access_url_http" in doc:
            record["source"] = doc["data_access_url_http"][0]
        elif "related_url_landing_page" in doc:
            record["source"] = doc["related_url_landing_page"][0]

        if "dataset_language" in doc:
            record["language"] = doc["dataset_language"]

        # Transform the indexed time as insert_data
        if "timestamp" in doc:
            insert = dparser.parse(_first(doc["timestamp"]))
            record["insert_date"] = insert.isoformat()

        # Transform the latest metadata update datetime as modified
        if doc.get("last_metadata_update_datetime"):
            updates = doc["last_metadata_update_datetime"]
            modified = max(dparser.parse(u) for u in (updates if isinstance(updates, list) else [updates]))
            record["date_modified"] = modified.isoformat()

        # Transform temporal extendt start and end dates
        if "temporal_extent_start_date" in doc:
            time_begin = dparser.parse(_first(doc["temporal_extent_start_date"]))
            record["time_begin"] = time_begin.isoformat()
        if "temporal_extent_end_date" in doc:
            time_end = dparser.parse(_first(doc["temporal_extent_end_date"]))
            record["time_end"] = time_end.isoformat()

        links = []
        if "data_access_url_opendap" in doc:
            links.append(
                {
                    "name": "OPeNDAP access",
                    "description": "OPeNDAP access",
                    "protocol": "OPeNDAP:OPeNDAP",
                    "url": doc["data_access_url_opendap"][0],
                }
            )
        if "data_access_url_ogc_wms" in doc:
            links.append(
                {
                    "name": "OGC-WMS Web Map Service",
                    "description": "OGC-WMS Web Map Service",
                    "protocol": "OGC:WMS",
                    "url": doc["data_access_url_ogc_wms"][0],
                }
            )
        if "data_access_url_http" in doc:
            links.append(
                {
                    "name": "File for download",
                    "description": "Direct HTTP download",
                    "protocol": "WWW:DOWNLOAD-1.0-http--download",
                    "url": doc["data_access_url_http"][0],
                }
            )
        if "data_access_url_ftp" in doc:
            links.append(
                {
                    "name": "File for download",
                    "description": "Direct FTP download",
                    "protocol": "ftp",
                    "url": doc["data_access_url_ftp"][0],
                }
            )
        record["links"] = json.dumps(links)

        # Transform the first investigator as creator.
        if "personnel_investigator_name" in doc:
            record["creator"] = ",".join(doc["personnel_investigator_name"])

        if "personnel_technical_name" in doc:
            record["contributor"] = ",".join(doc["personnel_technical_name"])

        if "personnel_metadata_author_name" in doc:
            if "contributor" in record:
                record["contributor"] += "," + ",".join(
                    doc["personnel_metadata_author_name"]
                )
            else:
                record["contributor"] = ",".join(doc["personnel_metadata_author_name"])

        # rights is mapped to accessconstraint, although we provide this info in the use constraint.
        # we should use dc:license instead, but it is not mapped in csw.
        if "use_constraint_license_text" in doc:
            record["rights"] = doc["use_constraint_license_text"]
            record["accessconstraints"] = doc["use_constraint_license_text"]
        if (
            "use_constraint_identifier" in doc
            and "use_constraint_license_text" not in doc
        ):
            record["rights"] = doc["use_constraint_identifier"]
            record["accessconstraints"] = doc["use_constraint_identifier"]

        if "dataset_citation_publisher" in doc:
            record["publisher"] = doc["dataset_citation_publisher"][0]

        if "storage_information_file_format" in doc:
            record["format"] = doc["storage_information_file_format"]

        xml_ = base64.b64decode(doc["mmd_xml_file"])
        doc_ = etree.fromstring(xml_, self.context.parser)
        record["xml"] = etree.tostring(self._transform_to_iso(doc_))
        record["mmd_xml_file"] = doc["mmd_xml_file"]

        params = {
            "q.op": "OR",
            "q": "metadata_identifier:(%s)" % doc["metadata_identifier"],
        }

        mdsource_url = self.solr_select_url + "?" + urlencode(params)
        record["mdsource"] = mdsource_url

        return self.dataset(record)

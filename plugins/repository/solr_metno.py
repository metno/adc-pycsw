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
from pycsw.plugins.repository.solr_query_handler import QueryHandler

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

# I removed parse_bbox_OR_query by calling it internally via the OR flag in parse_bbox_query
# and I should do the same for parse_field_OR_query
# I should also remove parse_bbox_OR_query from solr_helper.py
# and do the same for parse_field_OR_query

class SOLRMETNORepository(object):
    """
    Class to interact with underlying METNO SOLR backend repository
    """

    def __init__(self, context, repo_filter=None):
        """
        Initialize repository
        """
        # print('SOLRMETNORepository __init__')
        self.context = context
        self.filter = repo_filter
        self.fts = False
        self.label = "MetNO/SOLR"
        self.local_ingest = True
        self.solr_select_url = "%s/select" % self.filter
        self.dbtype = "SOLR"
        
        self.username, self.password = get_solr_connection()
        self.authentication = HTTPBasicAuth(self.username, self.password)
        # self.config_obj = get_config()
        self.adc_collection_filter = get_collection_filter()
        
        # print(self.adc_collection_filter)

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

        # self.dataset = type('dataset', (object,), {})

    def dataset(self, record):
        """
        Stub to mock a pycsw dataset object for Transactions
        """
        # print('dataset stub')
        return type("dataset", (object,), record)

    def query_ids(self, ids):
        """
        Query by list of identifiers
        """

        results = []

        params = {
            "fq": ['metadata_identifier:("%s")' % '" OR "'.join(ids)],
            "q.op": "OR",
            "q": "*:*",
        }
        params["fq"].append("metadata_status:%s" % "Active")
        if self.adc_collection_filter != "" or self.adc_collection_filter != None:
            params["fq"].append("collection:(%s)" % self.adc_collection_filter)

        print(params)

        response = requests.get(self.solr_select_url, params=params, auth=self.authentication)

        response = response.json()

        for doc in response["response"]["docs"]:
            results.append(self._doc2record(doc))
        # print("query by ID \n")
        return results

    def query_domain(self, domain, typenames, domainquerytype="list", count=False):
        """
        Query by property domain values
        """
        # print('Query domain')
        results = []

        params = {
            "q": "*:*",
            "rows": 0,
            "facet": "true",
            "facet.query": "distinct",
            "facet.type": "terms",
            "facet.field": domain,
            "fq": [],
        }
        params["fq"].append("metadata_status:%s" % "Active")
        if self.adc_collection_filter != "" or self.adc_collection_filter != None:
            params["fq"].append("collection:(%s)" % self.adc_collection_filter)

        print(params)

        response = requests.get("%s/select" % self.filter, params=params, auth=self.authentication).json()

        counts = response["facet_counts"]["facet_fields"][domain]

        for term in zip(*([iter(counts)] * 2)):
            LOGGER.debug("Term: %s", term)
            results.append(term)

        return results

    def query_insert(self, direction="max"):
        """
        Query to get latest (default) or earliest update to repository
        """
        # print('query_insert')
        if direction == "min":
            sort_order = "asc"
        else:
            sort_order = "desc"

        params = {
            "q": "*:*",
            "q.op": "OR",
            "fl": "timestamp",
            "sort": "timestamp %s" % sort_order,
            "fq": [],
        }
        params["fq"].append("metadata_status:%s" % "Active")
        if self.adc_collection_filter != "" or self.adc_collection_filter != None:
            params["fq"].append("collection:(%s)" % self.adc_collection_filter)

        response = requests.get("%s/select" % self.filter, params=params, auth=self.authentication).json()

        # TODO
        # check if any record available if none (length <= 0) add time.now
        #
        try:
            timestamp = datetime.strptime(
                response["response"]["docs"][0]["timestamp"], "%Y-%m-%dT%H:%M:%S.%fZ"
            )
        except IndexError:
            timestamp = datetime.now()

        return timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")

    def query_source(self, source):
        """
        Query by source
        """
        # print('Query_source')
        return NotImplementedError()

    def query(
        self, constraint, sortby=None, typenames=None, maxrecords=10, startposition=0
    ):
        """
        Query records from underlying repository
        """
        # DEBUG:
        # if "_dict" in constraint:
        #     print("constraint: ", constraint['_dict'])
        print(" #####  get_iso_transformer #####", "\n", get_iso_transformer(), "\n", "#####  get_iso_transformer #####")
        # mmd_to_NOiso
        print(json.dumps(constraint, indent=2, default=str))
        results = []

        # # print(('%s/select' % self.filter, params=params).json())
        params = self.query_handler.query(constraint)
        LOGGER.info("QUERY PARAMETERS: %s", params)
        print(" easking for the following query params:", params)        
        response = requests.get("%s/select" % self.filter, params=params, auth=self.authentication).json()

        # print("######################  ---  ###################################\n")
        # print('%s/select' % self.filter)
        # print(params)
        # print(response)
        # print(len(response['response']['docs']))
        # for i in response['response']['docs']:
        #    print(i['metadata_identifier'])
        # print("######################  ---  ###################################\n")

        total = response["response"]["numFound"]
        # response = response.json()
        print("Found: %s" % total)
        for doc in response["response"]["docs"]:
            results.append(self._doc2record(doc))
            # print(doc['metadata_identifier'])
        # print(total)

        return str(total), results

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
        if 'isChild' in doc and doc["isChild"]:
            record["parentidentifier"] = doc["related_dataset"][0]    
            # print(doc.keys())        
        # record["type"] = "dataset"
        record["wkt_geometry"] = doc["bbox"]
        record["title"] = doc["title"][0]
        record["abstract"] = doc["abstract"][0]
        if "iso_topic_category" in doc:
            record["topicategory"] = ",".join(doc["iso_topic_category"])
        if "keywords_keyword" in doc:
            record["keywords"] = ",".join(doc["keywords_keyword"])
        # record['source'] = doc['related_url_landing_page'][0]
        if "related_url_landing_page" in doc:
            record["source"] = doc["related_url_landing_page"][0]
        if "dataset_language" in doc:
            record["language"] = doc["dataset_language"]

        # Transform the indexed time as insert_data
        insert = dparser.parse(doc["timestamp"][0])
        record["insert_date"] = insert.isoformat()

        # Transform the last metadata update datetime as modified
        if "last_metadata_update_datetime" in doc:
            modified = dparser.parse(doc["last_metadata_update_datetime"][0])
            record["date_modified"] = modified.isoformat()

        # Transform temporal extendt start and end dates
        if "temporal_extent_start_date" in doc:
            time_begin = dparser.parse(doc["temporal_extent_start_date"][0])
            record["time_begin"] = time_begin.isoformat()
        if "temporal_extent_end_date" in doc:
            time_end = dparser.parse(doc["temporal_extent_end_date"][0])
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
            # record['creator'] = doc['personnel_investigator_name'][0] +" (" + doc['personnel_investigator_email'][0] + "), " + doc['personnel_investigator_organisation'][0]
            record["creator"] = ",".join(
                doc["personnel_investigator_name"]
            )  # +" (" + doc['personnel_investigator_email'][0] + "), " + doc['personnel_investigator_organisation'][0]

        if "personnel_technical_name" in doc:
            # for i in doc['personnel_technical_name']:
            # record['contributor'] = doc['personnel_technical_name'][i]
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

        # xslt = os.environ.get('MMD_TO_ISO')
        xslt_file = get_iso_transformer()
        # xslt_file = get_config_parser("xslt", "mmd_to_iso")

        transform = etree.XSLT(etree.parse(xslt_file))
        xml_ = base64.b64decode(doc["mmd_xml_file"])
        # print("xml_: ", xml_)

        doc_ = etree.fromstring(xml_, self.context.parser)
        # print("doc_:", doc_)
        pl = '/usr/local/share/parent_list.xml'
        result_tree = transform(doc_, path_to_parent_list=etree.XSLT.strparam(pl)).getroot()
        # result_tree = transform(doc_).getroot()
        record["xml"] = etree.tostring(result_tree)
        record["mmd_xml_file"] = doc["mmd_xml_file"]

        # print(record['xml'])
        params = {
            #'fq': doc['metadata_identifier'],
            "q.op": "OR",
            "q": "metadata_identifier:(%s)" % doc["metadata_identifier"],
        }

        mdsource_url = self.solr_select_url + urlencode(params)
        record["mdsource"] = mdsource_url

        return self.dataset(record)
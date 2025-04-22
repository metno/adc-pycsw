# -*- coding: utf-8 -*-
# =================================================================
#
# Authors: Tom Kralidis <tomkralidis@gmail.com>
#
# Copyright (c) 2015 Tom Kralidis
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

from pycsw.core.etree import etree
import base64
from pycsw.plugins.repository.solr_helper import get_config_parser


NAMESPACE = 'https://wis.wmo.int/2011/schemata/iso19139_2007/schema/gmd/gmd.xsd'
NAMESPACES = {'wmo': NAMESPACE}

def write_record(result, esn, context, url=None):
    ''' Return csw:SearchResults child as lxml.etree.Element '''

    # run lxml XSLT transformation and return against
    # result.mmd_xml_file (which needs to base64 decoded)
    # https://lxml.de/xpathxslt.html#xslt
    xslt_file = get_config_parser("xslt", "wmo")
    transform = etree.XSLT(etree.parse(xslt_file))
    mmd = base64.b64decode(result.mmd_xml_file)
    doc = etree.fromstring(mmd, context.parser)
    pl = '/usr/local/share/parent_list.xml'
    result_tree = transform(doc, path_to_parent_list=etree.XSLT.strparam(pl)).getroot()
    # result_tree = transform(doc).getroot()

    return result_tree

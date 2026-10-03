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

NAMESPACE = 'http://gcmd.gsfc.nasa.gov/Aboutus/xml/dif/10/'
NAMESPACES = {'dif': NAMESPACE}

XSLT_PATH = '/usr/local/share/mmd/xslt/mmd-to-dif10.xsl'


def write_record(result, esn, context, url=None):
    ''' Return csw:SearchResults child as lxml.etree.Element '''
    mmd_xml = getattr(result, 'mmd_xml_file', None)
    if not mmd_xml:
        node = etree.Element('{%s}DIF' % NAMESPACE)
        node.append(etree.Comment('mmd_xml_file not available for this record'))
        return node
    mmd_bytes = mmd_xml.encode('utf-8') if isinstance(mmd_xml, str) else mmd_xml
    doc = etree.fromstring(mmd_bytes, context.parser)
    transform = etree.XSLT(etree.parse(XSLT_PATH))
    return transform(doc).getroot()

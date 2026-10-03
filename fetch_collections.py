"""
Build parent_list.xml: the metadata_identifier of every parent dataset in Solr.

The MMD -> ISO/DIF/WMO XSLT transforms use this list to populate parent-child
relationships; the file is bind-mounted into every pycsw container.

Usage (credentials and default URL come from the environment, see .env.example):
        set -a; . ./.env; set +a
        python3 fetch_collections.py
        python3 fetch_collections.py --solr-url https://solr.example.org/solr/core \\
                                     --output /usr/local/share/csw/parent_list.xml

Only the standard library is used, so it can run from cron with the system
python, e.g. daily at 04:00:
        0 4 * * * cd /path/to/pycsw_solr_metno && set -a && . ./.env && set +a && /usr/bin/python3 fetch_collections.py >> artifacts/fetch_collections.log 2>&1
"""

import argparse
import base64
import json
import logging
import os
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.dom import minidom

DEFAULT_OUTPUT = "/usr/local/share/csw/parent_list.xml"

REQUEST_PARAMS = {
    'fl': 'metadata_identifier',
    'fq': 'isParent:true',
    'q.op': 'OR',
    'q': '*:*',
    'rows': 70000,
}


def make_solr_request(endpoint, params, user=None, password=None):
    url = f"{endpoint.rstrip('/')}/select?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url)
    if user and password:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def create_xml_file(data, output_file):
    root = ET.Element("parent")

    for doc in data.get("response", {}).get("docs", []):
        metadata_identifier = doc.get("metadata_identifier")
        if metadata_identifier:
            child = ET.SubElement(root, "id")
            child.text = metadata_identifier

    xml_str = minidom.parseString(ET.tostring(root)).toprettyxml(indent="    ")

    # Written in place on purpose: the file is bind-mounted into running
    # containers, and replacing it (write + rename) would leave them looking
    # at the old inode.
    with open(output_file, "w", encoding="utf-8") as file:
        file.write(xml_str)
    return len(root)


def main():
    parser = argparse.ArgumentParser(description="Build parent_list.xml from the Solr parent records.")
    parser.add_argument(
        '--solr-url',
        default=os.environ.get("PARENT_LIST_SOLR_URL") or os.environ.get("SOLR_URL"),
        help='Solr core URL (default: $PARENT_LIST_SOLR_URL, then $SOLR_URL)'
    )
    parser.add_argument('--output', default=DEFAULT_OUTPUT, help=f'Output file (default: {DEFAULT_OUTPUT})')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    if not args.solr_url:
        sys.exit("ERROR: no Solr URL: pass --solr-url or set PARENT_LIST_SOLR_URL")

    # Check before querying Solr: the default location needs root to create.
    output_dir = os.path.dirname(os.path.abspath(args.output))
    if not os.path.isdir(output_dir):
        sys.exit(
            f"ERROR: {output_dir} does not exist. Create it with:\n"
            f"    sudo mkdir -p {output_dir}\n"
            f'    sudo chown "$(id -u):$(id -g)" {output_dir}'
        )
    if not os.access(output_dir, os.W_OK):
        sys.exit(f"ERROR: {output_dir} is not writable by this user; fix its ownership or pass --output")

    try:
        data = make_solr_request(
            args.solr_url, REQUEST_PARAMS,
            user=os.environ.get("SOLR_USER"), password=os.environ.get("SOLR_PASS"),
        )
    except (OSError, ValueError) as err:
        # Leave the existing file untouched rather than emptying it.
        logging.error("Failed to fetch Solr data from %s: %s", args.solr_url, err)
        sys.exit(1)

    count = create_xml_file(data, args.output)
    logging.info("Wrote %d parent identifiers to %s", count, args.output)


if __name__ == "__main__":
    main()

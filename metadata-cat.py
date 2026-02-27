"""
Metadata catalogue: harvest Zenodo community (BioEcoOcean) and export records as JSON-LD.

Uses Zenodo REST API to list records in a community, then fetches each record's
schema.org JSON-LD export and writes a combined catalogue file (ODIS/schema.org aligned).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    print("Install dependencies: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

try:
    import xml.etree.ElementTree as ET
except Exception as exc:  # pragma: no cover
    print(f"Could not import ElementTree: {exc}", file=sys.stderr)
    sys.exit(1)

# Zenodo API (no auth required for public records)
ZENODO_API = "https://zenodo.org/api"
RECORDS_URL = f"{ZENODO_API}/records"
# Schema.org JSON-LD export (record id in path)
EXPORT_TEMPLATE = "https://zenodo.org/record/{record_id}/export/schemaorg_jsonld"

# OBIS IPT BioEcoOcean RSS
OBIS_IPT_RSS = "https://ipt.obis.org/bioecoocean/rss.do"

# Default: BioEcoOcean community
DEFAULT_COMMUNITY = "bioecoocean"
# Be nice to external services: ~1 request per second
REQUEST_DELAY_S = 1.2


def list_community_records(community: str, size: int = 25, max_pages: int | None = None) -> list[dict]:
    """List all record IDs (and minimal metadata) for a Zenodo community via search API."""
    params: dict = {
        "communities": community,
        "size": size,
        "sort": "mostrecent",
        "page": 1,
    }
    headers = {"Accept": "application/json"}
    all_hits: list[dict] = []

    while True:
        r = requests.get(RECORDS_URL, params=params, headers=headers, timeout=90)
        r.raise_for_status()
        data = r.json()
        hits = data.get("hits", {}).get("hits", [])
        total = data.get("hits", {}).get("total", 0)

        for h in hits:
            rec_id = h.get("id")
            if rec_id is not None:
                all_hits.append({"id": rec_id, "metadata": h.get("metadata", {})})

        if not hits:
            break
        if max_pages is not None and params["page"] >= max_pages:
            break
        if len(all_hits) >= total:
            break

        params["page"] += 1
        time.sleep(REQUEST_DELAY_S)

    return all_hits


def fetch_record_jsonld(record_id: int | str) -> dict | None:
    """Fetch schema.org JSON-LD for a single Zenodo record."""
    url = EXPORT_TEMPLATE.format(record_id=record_id)
    headers = {"Accept": "application/ld+json, application/json"}
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code != 200:
        return None
    ct = (r.headers.get("Content-Type") or "").lower()
    if "json" not in ct and "ld+json" not in ct:
        # Some setups return HTML; try to parse as JSON anyway
        try:
            return r.json()
        except Exception:
            return None
    try:
        return r.json()
    except Exception:
        return None


def _metadata_to_schema_stub(rec_id: int | str, metadata: dict) -> dict:
    """Build a schema.org-style JSON-LD stub from Zenodo record metadata."""
    url = f"https://zenodo.org/record/{rec_id}"
    doi = metadata.get("doi")
    identifier = f"https://doi.org/{doi}" if doi else url
    stub = {
        "@context": "https://schema.org/",
        "@type": "CreativeWork",
        "@id": url,
        "name": metadata.get("title") or f"Zenodo record {rec_id}",
        "identifier": identifier,
        "url": url,
    }
    if metadata.get("description"):
        stub["description"] = metadata["description"]
    if metadata.get("publication_date"):
        stub["datePublished"] = metadata["publication_date"]
    if metadata.get("creators"):
        stub["creator"] = [
            {"@type": "Person", "name": c.get("name", "")}
            for c in metadata["creators"]
        ]
    if metadata.get("keywords"):
        stub["keywords"] = metadata["keywords"]
    if metadata.get("license"):
        stub["license"] = metadata.get("license")
    return stub


def _strip_html(text: str) -> str:
    """Very simple HTML tag stripper for descriptions."""
    no_tags = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", no_tags).strip()


def harvest_obis_rss(rss_url: str = OBIS_IPT_RSS) -> list[dict]:
    """Harvest BioEcoOcean OBIS IPT RSS and map to schema.org Dataset stubs."""
    try:
        resp = requests.get(rss_url, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"Warning: could not harvest OBIS IPT RSS ({exc})", file=sys.stderr)
        return []

    try:
        root = ET.fromstring(resp.text)
    except Exception as exc:
        print(f"Warning: could not parse OBIS IPT RSS XML ({exc})", file=sys.stderr)
        return []

    channel = root.find("channel")
    if channel is None:
        return []

    datasets: list[dict] = []
    ns = {"ipt": "http://ipt.gbif.org/"}

    for item in channel.findall("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = item.find("description")
        pub_el = item.find("pubDate")
        eml_el = item.find("ipt:eml", ns)
        dwca_el = item.find("ipt:dwca", ns)

        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        link = link_el.text.strip() if link_el is not None and link_el.text else ""
        desc = desc_el.text or ""
        desc = _strip_html(desc)

        date_published = ""
        if pub_el is not None and pub_el.text:
            try:
                dt = parsedate_to_datetime(pub_el.text.strip())
                date_published = dt.date().isoformat()
            except Exception:
                date_published = pub_el.text.strip()

        identifier_val: str | None = None
        if eml_el is not None and eml_el.text:
            identifier_val = eml_el.text.strip()
        elif link:
            identifier_val = link

        identifier_block: dict[str, Any] | None = None
        if identifier_val:
            identifier_block = {
                "@type": "PropertyValue",
                "description": "OBIS IPT resource",
                "propertyID": "url",
                "url": identifier_val,
                "value": identifier_val,
            }

        dataset: dict[str, Any] = {
            "@context": "https://schema.org/",
            "@type": "Dataset",
            "name": title or "OBIS IPT resource",
            "description": desc,
            "url": link or identifier_val or "",
        }
        if identifier_block:
            dataset["identifier"] = identifier_block
        if date_published:
            dataset["datePublished"] = date_published

        # Distributions: add links to EML and DwC-A where present
        distributions: list[dict[str, Any]] = []
        if eml_el is not None and eml_el.text:
            distributions.append(
                {
                    "@type": "DataDownload",
                    "name": "EML metadata",
                    "encodingFormat": "application/xml",
                    "contentUrl": eml_el.text.strip(),
                }
            )
        if dwca_el is not None and dwca_el.text:
            distributions.append(
                {
                    "@type": "DataDownload",
                    "name": "Darwin Core Archive",
                    "encodingFormat": "application/zip",
                    "contentUrl": dwca_el.text.strip(),
                }
            )
        if distributions:
            dataset["distribution"] = distributions

        datasets.append(dataset)

    return datasets


def build_catalogue(community: str, max_pages: int | None = None) -> list[dict]:
    """List community records and fetch JSON-LD for each; return list of JSON-LD objects."""
    hits = list_community_records(community, size=25, max_pages=max_pages)
    catalogue: list[dict] = []
    for hit in hits:
        rec_id = hit["id"]
        meta = hit.get("metadata", {})
        ld = fetch_record_jsonld(rec_id)
        if ld is not None and ld.get("@type"):
            catalogue.append(ld)
        else:
            catalogue.append(_metadata_to_schema_stub(rec_id, meta))
        time.sleep(REQUEST_DELAY_S)

    # Also harvest BioEcoOcean OBIS IPT datasets and add them as schema.org Datasets
    if community == DEFAULT_COMMUNITY:
        obis_datasets = harvest_obis_rss(OBIS_IPT_RSS)
        if obis_datasets:
            print(f"Adding {len(obis_datasets)} OBIS IPT dataset(s) from {OBIS_IPT_RSS}", file=sys.stderr)
            catalogue.extend(obis_datasets)

    return catalogue


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Harvest Zenodo community metadata and export as JSON-LD catalogue."
    )
    parser.add_argument(
        "--community",
        default=DEFAULT_COMMUNITY,
        help=f"Zenodo community identifier (default: {DEFAULT_COMMUNITY})",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=Path("bioecoocean-catalogue.jsonld"),
        help="Output catalogue file (default: bioecoocean-catalogue.jsonld)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Limit number of record list pages (for testing)",
    )
    args = parser.parse_args()

    print(f"Harvesting Zenodo community: {args.community}", file=sys.stderr)
    catalogue = build_catalogue(args.community, max_pages=args.max_pages)
    print(f"Collected {len(catalogue)} records", file=sys.stderr)

    # JSON-LD catalogue: @graph is a common pattern for multiple entities
    out = {
        "@context": [
            "https://schema.org/",
            {"@vocab": "https://schema.org/"},
        ],
        "@graph": catalogue,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print(f"Wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

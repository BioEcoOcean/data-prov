#!/usr/bin/env python3
"""
Transform a catalogue JSON-LD file (from metadata-cat.py) into
per-record JSON-LD files in a richer "Project" style structure,
and optionally build a simple sitemap over those JSON files.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable, List, Dict, Any
import html


# Simple BioEcoOcean funding block reused for all project entries coming
# from the BioEcoOcean Zenodo community.
BIOECOOCEAN_FUNDING: Dict[str, Any] = {
    "@type": "MonetaryGrant",
    "name": "BioEcoOcean (Horizon Europe)",
    "identifier": "101136748",
    "funder": {
        "@type": "FundingAgency",
        "name": "European Commission",
        "legalName": "European Commission",
        "url": "https://commission.europa.eu/index_en",
    },
}


LICENSE_MAP = {
    # Zenodo license id  ->  (label, URL)
    "cc-by-4.0": (
        "CC-BY: Creative Commons Attribution 4.0",
        "https://creativecommons.org/licenses/by/4.0/",
    ),
}


def load_graph(path: Path) -> List[Dict[str, Any]]:
    """Load @graph array from a JSON-LD catalogue file."""
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        graph = data.get("@graph")
        if isinstance(graph, list):
            return graph
    if isinstance(data, list):
        return data  # already a list of records
    return []


def parse_zenodo_rec_id(entry: Dict[str, Any]) -> str | None:
    """Extract the Zenodo record id from @id or url if possible."""
    url = entry.get("url") or entry.get("@id") or ""
    marker = "zenodo.org/record/"
    if marker in url:
        tail = url.split(marker, 1)[1]
        rec_id = tail.split("/", 1)[0].split("?", 1)[0]
        return rec_id or None
    return None


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "record"


def extract_doi(identifier: Any) -> tuple[str | None, str | None]:
    """
    From an identifier field, try to extract (doi_value, doi_url).
    """
    doi_val: str | None = None
    doi_url: str | None = None

    if isinstance(identifier, str):
        if "doi.org/" in identifier:
            doi_url = identifier
            doi_val = identifier.split("doi.org/", 1)[1]
        else:
            doi_val = identifier
    elif isinstance(identifier, dict):
        value = identifier.get("value")
        if isinstance(value, str):
            doi_val = value
        url = identifier.get("url")
        if isinstance(url, str) and "doi.org/" in url:
            doi_url = url

    if doi_val and not doi_url:
        doi_url = f"https://doi.org/{doi_val}"
    return doi_val, doi_url


def dataset_to_project(
    entry: Dict[str, Any],
    base_url: str | None,
    rel_path: Path,
) -> Dict[str, Any]:
    """Map a catalogue entry (Dataset-style) to a Project-style JSON-LD object."""
    rec_id = parse_zenodo_rec_id(entry)
    url = entry.get("url") or entry.get("@id") or ""

    name = entry.get("name") or (f"Zenodo record {rec_id}" if rec_id else "Record")
    desc = entry.get("description", "")
    if isinstance(desc, dict) and "@value" in desc:
        desc = desc.get("@value") or ""

    doi_val, doi_url = extract_doi(entry.get("identifier"))

    identifier_block: Dict[str, Any] | None = None
    if doi_val:
        identifier_block = {
            "@type": "PropertyValue",
            "description": "DOI",
            "propertyID": "https://registry.identifiers.org/registry/doi",
            "url": doi_url or f"https://doi.org/{doi_val}",
            "value": doi_val,
        }
    elif url:
        identifier_block = {
            "@type": "PropertyValue",
            "description": "Record URL",
            "propertyID": "url",
            "url": url,
            "value": url,
        }

    project: Dict[str, Any] = {
        "@context": "https://schema.org/",
        "@type": "Project",
        "name": name,
        "description": desc or "",
        "url": url,
    }

    if identifier_block:
        project["identifier"] = identifier_block

    # publishingPrinciples from license (when mappable)
    lic = entry.get("license")
    lic_label: str | None = None
    lic_url: str | None = None
    if isinstance(lic, str):
        key = lic.lower()
        if key in LICENSE_MAP:
            lic_label, lic_url = LICENSE_MAP[key]
        elif "creativecommons.org/licenses/by/4.0" in key:
            lic_label, lic_url = LICENSE_MAP["cc-by-4.0"]
    if lic_url:
        project["publishingPrinciples"] = [
            {
                "@type": "CreativeWork",
                "name": lic_label or "Creative Commons Attribution 4.0",
                "url": lic_url,
            }
        ]

    # keywords -> simple DefinedTerm entries
    kws = entry.get("keywords") or []
    if isinstance(kws, list):
        terms: List[Dict[str, Any]] = []
        for kw in kws:
            if isinstance(kw, str):
                terms.append({"@type": "DefinedTerm", "name": kw})
        if terms:
            project["keywords"] = terms

    # Optional, constant funding block for BioEcoOcean
    if url and "zenodo.org/record/" in url:
        project["funding"] = [BIOECOOCEAN_FUNDING]

    # Leave contactPoint, areaServed, makesOffer, etc. to be curated manually.

    if base_url:
        rel_str = rel_path.as_posix().lstrip("./")
        project["@id"] = base_url.rstrip("/") + "/" + rel_str

    return project


def build_sitemap_xml(ids: Iterable[str]) -> str:
    """Build a simple XML sitemap over the given @id URLs."""
    id_list = [i for i in ids if i]
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc in id_list:
        esc = html.escape(loc, quote=True)
        lines.append(f"  <url><loc>{esc}</loc></url>")
    lines.append("</urlset>")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Export per-record Project JSON-LD files from a catalogue JSON-LD "
            "(e.g. bioecoocean-catalogue.jsonld) and optionally build a sitemap."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("bioecoocean-catalogue.jsonld"),
        help="Input catalogue JSON-LD file (default: bioecoocean-catalogue.jsonld).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("jsonFiles/zenodo"),
        help="Directory to write per-record JSON files (default: jsonFiles/zenodo).",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=None,
        help=(
            "Base URL prefix for @id of each JSON file, e.g. "
            "https://raw.githubusercontent.com/ORG/REPO/BRANCH "
            "(the file's repo-relative path will be appended)."
        ),
    )
    parser.add_argument(
        "--sitemap",
        type=Path,
        default=None,
        help=(
            "Optional path to write an XML sitemap listing all per-record @id URLs "
            "(e.g. sitemap.xml)."
        ),
    )

    args = parser.parse_args()

    records = load_graph(args.input)
    if not records:
        print(f"No records found in {args.input}", file=sys.stderr)
        return 1

    cwd = Path.cwd().resolve()
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    ids: List[str] = []

    for idx, entry in enumerate(records, start=1):
        rec_id = parse_zenodo_rec_id(entry)
        base_name = entry.get("name") or (rec_id or f"record-{idx}")
        slug = slugify(str(base_name))
        if rec_id and rec_id not in slug:
            filename = f"{slug}-{rec_id}.json"
        else:
            filename = f"{slug}.json"

        out_path = out_dir / filename
        try:
            rel_for_id = out_path.resolve().relative_to(cwd)
        except ValueError:
            rel_for_id = Path(filename)

        project_obj = dataset_to_project(entry, args.base_url, rel_for_id)

        with out_path.open("w", encoding="utf-8") as f:
            json.dump(project_obj, f, indent=2, ensure_ascii=False)

        entry_id = project_obj.get("@id")
        if isinstance(entry_id, str) and entry_id:
            ids.append(entry_id)

    print(f"Wrote {len(records)} project JSON file(s) under {out_dir}", file=sys.stderr)

    if args.sitemap:
        sitemap_xml = build_sitemap_xml(ids)
        args.sitemap.parent.mkdir(parents=True, exist_ok=True)
        with args.sitemap.open("w", encoding="utf-8") as f:
            f.write(sitemap_xml)
        print(f"Wrote sitemap with {len(ids)} entries to {args.sitemap}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())


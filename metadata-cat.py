"""
Metadata catalogue: harvest Zenodo (BioEcoOcean) and OBIS IPT, export JSON-LD.

Lists community records via Zenodo API, maps OBIS IPT RSS items, enriches each
entry (funding, DOI identifier, license) and writes a combined @graph catalogue
and optional per-record JSON files.
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
DEFAULT_ZENODO_DIR = Path("jsonFiles/zenodo")
DEFAULT_OBIS_DIR = Path("jsonFiles/OBIS")
DEFAULT_BASE_URL = "https://raw.githubusercontent.com/BioEcoOcean/data-prov/refs/heads/main"

BIOECOOCEAN_FUNDING: dict[str, Any] = {
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

LICENSE_MAP: dict[str, tuple[str, str]] = {
    "cc-by-4.0": (
        "CC-BY: Creative Commons Attribution 4.0",
        "https://creativecommons.org/licenses/by/4.0/",
    ),
    "cc-by": (
        "CC-BY: Creative Commons Attribution",
        "https://creativecommons.org/licenses/by/4.0/",
    ),
}


def _strip_html(text: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", no_tags).strip()


def slugify(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-") or "record"


def _zenodo_schema_type(metadata: dict) -> str:
    """Dataset for data uploads; otherwise CreativeWork (publications, posters, etc.)."""
    if (metadata.get("upload_type") or "").lower() == "dataset":
        return "Dataset"
    return "CreativeWork"


def _doi_property_value(doi: str) -> dict[str, Any]:
    doi = doi.strip().removeprefix("https://doi.org/").removeprefix("http://doi.org/")
    return {
        "@type": "PropertyValue",
        "description": "DOI",
        "propertyID": "https://registry.identifiers.org/registry/doi",
        "url": f"https://doi.org/{doi}",
        "value": doi,
    }


def _normalize_identifier(identifier: Any, fallback_url: str = "") -> Any:
    """Prefer PropertyValue for DOIs; leave other shapes unchanged."""
    if isinstance(identifier, dict) and identifier.get("@type") == "PropertyValue":
        return identifier
    if isinstance(identifier, str):
        if "doi.org/" in identifier:
            doi = identifier.split("doi.org/", 1)[1].split("?", 1)[0]
            return _doi_property_value(doi)
        if identifier.startswith("10."):
            return _doi_property_value(identifier)
        if identifier:
            return identifier
    if fallback_url:
        return fallback_url
    return identifier


def _normalize_keywords(keywords: Any) -> list[str] | None:
    if not keywords:
        return None
    if isinstance(keywords, str):
        return [keywords]
    if not isinstance(keywords, list):
        return None
    out: list[str] = []
    for kw in keywords:
        if isinstance(kw, str) and kw.strip():
            out.append(kw.strip())
        elif isinstance(kw, dict) and kw.get("name"):
            out.append(str(kw["name"]).strip())
    return out or None


def _publishing_principles_from_license(license_field: Any) -> list[dict[str, str]] | None:
    if not isinstance(license_field, str):
        return None
    key = license_field.lower().strip()
    if key in LICENSE_MAP:
        label, url = LICENSE_MAP[key]
        return [{"@type": "CreativeWork", "name": label, "url": url}]
    if "creativecommons.org/licenses/by/4.0" in key:
        label, url = LICENSE_MAP["cc-by-4.0"]
        return [{"@type": "CreativeWork", "name": label, "url": url}]
    return None


def enrich_record(record: dict[str, Any], *, add_funding: bool = True) -> dict[str, Any]:
    """
    Normalize a catalogue entry: strip HTML, DOI identifier, plain keywords,
    optional BioEcoOcean funding and license as publishingPrinciples.
    Preserves existing @type (CreativeWork, Dataset, etc.).
    """
    out = dict(record)

    desc = out.get("description")
    if isinstance(desc, str):
        out["description"] = _strip_html(desc)
    elif isinstance(desc, dict) and "@value" in desc:
        out["description"] = _strip_html(str(desc.get("@value") or ""))

    url = out.get("url") or out.get("@id") or ""
    out["identifier"] = _normalize_identifier(out.get("identifier"), str(url))

    kws = _normalize_keywords(out.get("keywords"))
    if kws:
        out["keywords"] = kws
    elif "keywords" in out:
        del out["keywords"]

    if "publishingPrinciples" not in out:
        principles = _publishing_principles_from_license(out.get("license"))
        if principles:
            out["publishingPrinciples"] = principles

    if add_funding and "funding" not in out:
        out["funding"] = [BIOECOOCEAN_FUNDING]

    if "@context" not in out:
        out["@context"] = "https://schema.org/"

    return out


def list_community_records(community: str, size: int = 25, max_pages: int | None = None) -> list[dict]:
    params: dict = {
        "communities": community,
        "size": size,
        "sort": "mostrecent",
        "page": 1,
    }
    headers = {"Accept": "application/json"}
    all_hits: list[dict] = []

    while True:
        try:
            r = requests.get(RECORDS_URL, params=params, headers=headers, timeout=90)
            r.raise_for_status()
        except requests.RequestException as exc:
            print(f"Warning: Zenodo API failed on page {params['page']} ({exc})", file=sys.stderr)
            break

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
    url = EXPORT_TEMPLATE.format(record_id=record_id)
    headers = {"Accept": "application/ld+json, application/json"}
    try:
        r = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None


def _metadata_to_schema_stub(rec_id: int | str, metadata: dict) -> dict:
    url = f"https://zenodo.org/record/{rec_id}"
    doi = metadata.get("doi")
    stub: dict[str, Any] = {
        "@type": _zenodo_schema_type(metadata),
        "name": metadata.get("title") or f"Zenodo record {rec_id}",
        "url": url,
    }
    if doi:
        stub["identifier"] = _doi_property_value(doi)
    else:
        stub["identifier"] = url

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
        stub["license"] = metadata["license"]
    return stub


def harvest_obis_rss(rss_url: str = OBIS_IPT_RSS) -> list[dict]:
    try:
        resp = requests.get(rss_url, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"Warning: could not harvest OBIS IPT RSS ({exc})", file=sys.stderr)
        return []

    try:
        root = ET.fromstring(resp.text)
    except Exception as exc:
        print(f"Warning: could not parse OBIS IPT RSS ({exc})", file=sys.stderr)
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
        desc = _strip_html(desc_el.text or "") if desc_el is not None else ""

        date_published = ""
        if pub_el is not None and pub_el.text:
            try:
                date_published = parsedate_to_datetime(pub_el.text.strip()).date().isoformat()
            except Exception:
                date_published = pub_el.text.strip()

        identifier_val = ""
        if eml_el is not None and eml_el.text:
            identifier_val = eml_el.text.strip()
        elif link:
            identifier_val = link

        dataset: dict[str, Any] = {
            "@type": "Dataset",
            "name": title or "OBIS IPT resource",
            "description": desc,
            "url": link or identifier_val,
        }
        if identifier_val:
            dataset["identifier"] = {
                "@type": "PropertyValue",
                "description": "OBIS IPT resource",
                "propertyID": "url",
                "url": identifier_val,
                "value": identifier_val,
            }
        if date_published:
            dataset["datePublished"] = date_published

        distributions: list[dict[str, Any]] = []
        if eml_el is not None and eml_el.text:
            distributions.append({
                "@type": "DataDownload",
                "name": "EML metadata",
                "encodingFormat": "application/xml",
                "contentUrl": eml_el.text.strip(),
            })
        if dwca_el is not None and dwca_el.text:
            distributions.append({
                "@type": "DataDownload",
                "name": "Darwin Core Archive",
                "encodingFormat": "application/zip",
                "contentUrl": dwca_el.text.strip(),
            })
        if distributions:
            dataset["distribution"] = distributions

        datasets.append(dataset)

    return datasets


def _obis_resource_slug(url: str) -> str | None:
    if "resource?r=" in url:
        return url.split("resource?r=", 1)[1].split("&", 1)[0]
    return None


def _zenodo_rec_id_from_record(record: dict) -> str | None:
    src = record.get("url") or record.get("@id") or ""
    if isinstance(src, str) and "zenodo.org/record/" in src:
        return src.split("zenodo.org/record/", 1)[1].split("/", 1)[0].split("?", 1)[0]
    return None


def _stable_record_key(record: dict) -> str | None:
    rec_id = _zenodo_rec_id_from_record(record)
    if rec_id:
        return f"zenodo:{rec_id}"
    url = str(record.get("url") or record.get("@id") or "")
    obis_slug = _obis_resource_slug(url)
    if obis_slug:
        return f"obis:{obis_slug}"
    return None


def _find_existing_path(record_dir: Path, record: dict) -> Path | None:
    """Locate an existing JSON file for this record (by Zenodo id or OBIS resource slug)."""
    if not record_dir.is_dir():
        return None
    rec_id = _zenodo_rec_id_from_record(record)
    if rec_id:
        matches = sorted(record_dir.glob(f"*-{rec_id}.json"))
        if matches:
            if len(matches) > 1:
                print(f"Warning: multiple files for Zenodo {rec_id}, using {matches[0].name}", file=sys.stderr)
            return matches[0]
        return None
    url = str(record.get("url") or record.get("@id") or "")
    obis_slug = _obis_resource_slug(url)
    if obis_slug:
        suffix = slugify(obis_slug)
        matches = sorted(record_dir.glob(f"*-{suffix}.json"))
        if matches:
            return matches[0]
    return None


def _canonical_record_json(record: dict) -> str:
    """Stable JSON representation for equality checks (ignores @id)."""
    body = {k: v for k, v in record.items() if k != "@id"}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _records_equal(a: dict, b: dict) -> bool:
    return _canonical_record_json(a) == _canonical_record_json(b)


def _json_file_id(base_url: str, out_path: Path, cwd: Path) -> str:
    """Canonical @id: raw URL of this JSON file on GitHub."""
    try:
        rel = out_path.resolve().relative_to(cwd)
    except ValueError:
        rel = Path(out_path.name)
    return base_url.rstrip("/") + "/" + rel.as_posix()


def _prepare_record_for_path(
    record: dict,
    out_path: Path,
    *,
    base_url: str | None,
    cwd: Path,
) -> dict:
    prepared = dict(record)
    if base_url:
        prepared["@id"] = _json_file_id(base_url, out_path, cwd)
    elif not prepared.get("@id"):
        prepared["@id"] = prepared.get("url") or ""
    return prepared


def _record_filename(record: dict) -> str:
    name = record.get("name") or record.get("@id") or "record"
    base_slug = slugify(str(name))
    src = record.get("@id") or record.get("url") or ""
    if isinstance(src, str) and "zenodo.org/record/" in src:
        rec_id = src.split("zenodo.org/record/", 1)[1].split("/", 1)[0].split("?", 1)[0]
        return f"{base_slug}-{rec_id}.json"
    if isinstance(src, str):
        obis_slug = _obis_resource_slug(src)
        if obis_slug:
            return f"{base_slug}-{slugify(obis_slug)}.json"
    return f"{base_slug}.json"


def _sync_record_file(
    record: dict,
    record_dir: Path,
    *,
    base_url: str | None,
    cwd: Path,
    source_label: str,
) -> tuple[dict, str]:
    """
    Create or update a per-record JSON file when content changed; otherwise skip.
    Returns (record for catalogue, action: created|updated|skipped).
    """
    record_dir.mkdir(parents=True, exist_ok=True)
    existing_path = _find_existing_path(record_dir, record)
    out_path = existing_path if existing_path is not None else record_dir / _record_filename(record)
    prepared = _prepare_record_for_path(record, out_path, base_url=base_url, cwd=cwd)

    if existing_path is not None and existing_path.is_file():
        try:
            with existing_path.open(encoding="utf-8") as f:
                on_disk = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Warning: could not read {existing_path} ({exc}); will rewrite", file=sys.stderr)
            on_disk = None
        if on_disk is not None and _records_equal(prepared, on_disk):
            if prepared.get("@id") != on_disk.get("@id"):
                with out_path.open("w", encoding="utf-8") as f:
                    json.dump(prepared, f, indent=2, ensure_ascii=False)
                print(f"updated ({source_label}, @id): {out_path}", file=sys.stderr)
                return prepared, "updated"
            print(f"skipped ({source_label}): {out_path}", file=sys.stderr)
            return prepared, "skipped"

    action = "updated" if existing_path is not None else "created"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(prepared, f, indent=2, ensure_ascii=False)
    print(f"{action} ({source_label}): {out_path}", file=sys.stderr)
    return prepared, action


def build_catalogue(
    community: str,
    max_pages: int | None = None,
    *,
    zenodo_dir: Path | None = DEFAULT_ZENODO_DIR,
    obis_dir: Path | None = DEFAULT_OBIS_DIR,
    base_url: str | None = None,
    cwd: Path | None = None,
    write_json: bool = True,
    add_funding: bool = True,
) -> tuple[list[dict], dict[str, int]]:
    cwd = cwd or Path.cwd().resolve()
    hits = list_community_records(community, size=25, max_pages=max_pages)
    catalogue: list[dict] = []
    stats = {"created": 0, "updated": 0, "skipped": 0}

    for hit in hits:
        rec_id = hit["id"]
        meta = hit.get("metadata", {})
        raw = fetch_record_jsonld(rec_id)
        if raw is not None and raw.get("@type"):
            record = raw
            if not record.get("name") and meta.get("title"):
                record["name"] = meta["title"]
        else:
            record = _metadata_to_schema_stub(rec_id, meta)

        record = enrich_record(record, add_funding=add_funding)
        if write_json and zenodo_dir is not None:
            record, action = _sync_record_file(
                record, zenodo_dir, base_url=base_url, cwd=cwd, source_label="zenodo"
            )
            stats[action] += 1
        catalogue.append(record)
        time.sleep(REQUEST_DELAY_S)

    if community == DEFAULT_COMMUNITY:
        obis_records = harvest_obis_rss(OBIS_IPT_RSS)
        for dataset in obis_records:
            record = enrich_record(dataset, add_funding=add_funding)
            if write_json and obis_dir is not None:
                record, action = _sync_record_file(
                    record, obis_dir, base_url=base_url, cwd=cwd, source_label="OBIS"
                )
                stats[action] += 1
            catalogue.append(record)
        if obis_records:
            print(f"Processed {len(obis_records)} OBIS IPT dataset(s)", file=sys.stderr)

    if write_json:
        print(
            f"JSON files: {stats['created']} created, {stats['updated']} updated, "
            f"{stats['skipped']} skipped",
            file=sys.stderr,
        )

    return catalogue, stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Harvest Zenodo + OBIS IPT metadata and export as JSON-LD catalogue."
    )
    parser.add_argument("--community", default=DEFAULT_COMMUNITY)
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=Path("bioecoocean-catalogue.jsonld"),
    )
    parser.add_argument(
        "--zenodo-dir",
        type=Path,
        default=DEFAULT_ZENODO_DIR,
        help=f"Directory for Zenodo JSON files (default: {DEFAULT_ZENODO_DIR}).",
    )
    parser.add_argument(
        "--obis-dir",
        type=Path,
        default=DEFAULT_OBIS_DIR,
        help=f"Directory for OBIS IPT JSON files (default: {DEFAULT_OBIS_DIR}).",
    )
    parser.add_argument(
        "--no-json-files",
        action="store_true",
        help="Skip writing per-record JSON files under jsonFiles/.",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=DEFAULT_BASE_URL,
        help=(
            f"Base URL for each record @id (raw JSON file URL; default: {DEFAULT_BASE_URL}). "
            "Pass empty string to use Zenodo/IPT page URLs instead."
        ),
    )
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument(
        "--no-funding",
        action="store_true",
        help="Do not add the BioEcoOcean funding block to entries.",
    )
    args = parser.parse_args()

    base_url = args.base_url.strip() or None

    print(f"Harvesting Zenodo community: {args.community}", file=sys.stderr)
    catalogue, _stats = build_catalogue(
        args.community,
        max_pages=args.max_pages,
        zenodo_dir=None if args.no_json_files else args.zenodo_dir,
        obis_dir=None if args.no_json_files else args.obis_dir,
        base_url=base_url,
        write_json=not args.no_json_files,
        add_funding=not args.no_funding,
    )
    print(f"Collected {len(catalogue)} records", file=sys.stderr)

    out = {
        "@context": {"@vocab": "https://schema.org/"},
        "@graph": catalogue,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"Wrote {args.output}", file=sys.stderr)

    if not args.no_json_files:
        print(f"Zenodo JSON: {args.zenodo_dir}", file=sys.stderr)
        print(f"OBIS JSON: {args.obis_dir}", file=sys.stderr)
        print("Run update_sitemap.py after harvest to refresh sitemap.xml.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

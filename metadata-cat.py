"""
Metadata catalogue: harvest Zenodo (BioEcoOcean), OBIS IPT, PANGAEA and
GitHub, export JSON-LD.

Lists community records via Zenodo API, maps OBIS IPT RSS items, PANGAEA
datasets and GitHub repositories tagged with the BioEcoOcean output topic, enriches each
entry (funding, DOI identifier, license) and writes a combined @graph catalogue
and optional per-record JSON files.
"""

from __future__ import annotations

import argparse
import html
import json
import os
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
EXPORT_TEMPLATE = "https://zenodo.org/records/{record_id}/export/json-ld"
# OBIS IPT BioEcoOcean RSS
OBIS_IPT_RSS = "https://ipt.obis.org/bioecoocean/rss.do"
# Default: BioEcoOcean community
DEFAULT_COMMUNITY = "bioecoocean"
# Be nice to external services: ~1 request per second
REQUEST_DELAY_S = 1.2
DEFAULT_ZENODO_DIR = Path("jsonFiles/zenodo")
DEFAULT_OBIS_DIR = Path("jsonFiles/OBIS")
DEFAULT_PANGAEA_DIR = Path("jsonFiles/pangaea")
DEFAULT_GITHUB_DIR = Path("jsonFiles/github")
DEFAULT_BASE_URL = "https://raw.githubusercontent.com/BioEcoOcean/data-prov/refs/heads/main"
PANGAEA_QUERY = "BioEcoOcean"
# GitHub repositories (any owner) carrying this topic are catalogued as code outputs
GITHUB_TOPIC = "bioecoocean-output"
GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"

# Source catalogues, attached to each record as schema.org includedInDataCatalog
ZENODO_CATALOG: dict[str, Any] = {
    "@type": "DataCatalog",
    "name": "Zenodo",
    "url": "https://zenodo.org/communities/bioecoocean",
}
OBIS_CATALOG: dict[str, Any] = {
    "@type": "DataCatalog",
    "name": "OBIS",
    "url": "https://ipt.obis.org/bioecoocean/",
}
PANGAEA_CATALOG: dict[str, Any] = {
    "@type": "DataCatalog",
    "name": "PANGAEA",
    "url": "https://www.pangaea.de/",
}
GITHUB_CATALOG: dict[str, Any] = {
    "@type": "DataCatalog",
    "name": "GitHub",
    "url": f"https://github.com/topics/{GITHUB_TOPIC}",
}

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
    no_tags = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", no_tags).strip()


def slugify(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-") or "record"


def _zenodo_resource_type(metadata: dict) -> dict:
    """Zenodo resource_type, e.g. {"type": "publication", "subtype": "article", "title": "Journal article"}."""
    rt = metadata.get("resource_type")
    return rt if isinstance(rt, dict) else {}


def _zenodo_schema_type(metadata: dict) -> str:
    """Dataset for data uploads; otherwise CreativeWork (publications, posters, etc.)."""
    if (_zenodo_resource_type(metadata).get("type") or "").lower() == "dataset":
        return "Dataset"
    return "CreativeWork"


def _short_schema_type(value: Any) -> Any:
    """'https://schema.org/ScholarlyArticle' -> 'ScholarlyArticle'."""
    if isinstance(value, str):
        return re.sub(r"^https?://schema\.org/", "", value)
    return value


def _apply_zenodo_metadata(record: dict, metadata: dict) -> dict:
    """Fill fields from the Zenodo API metadata that the JSON-LD export omits or flattens."""
    out = dict(record)
    out["@type"] = _short_schema_type(out.get("@type"))
    # Export joins keywords into one comma-separated string; the API keeps the list
    if metadata.get("keywords"):
        out["keywords"] = metadata["keywords"]
    rt_title = _zenodo_resource_type(metadata).get("title")
    if rt_title:
        out["additionalType"] = rt_title
    out["includedInDataCatalog"] = ZENODO_CATALOG
    return out


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

    if add_funding:
        funding = out.get("funding") or []
        if isinstance(funding, dict):
            funding = [funding]
        # Replace Zenodo's own entry for the BioEcoOcean grant (e.g. identifier
        # "00k4n6c32::101136748") with the canonical block; keep co-funders.
        grant_id = BIOECOOCEAN_FUNDING["identifier"]
        others = [
            f for f in funding
            if not (isinstance(f, dict) and (
                str(f.get("identifier") or "").endswith(grant_id)
                or grant_id in str(f.get("name") or "")
            ))
        ]
        out["funding"] = [BIOECOOCEAN_FUNDING, *others]

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
    url = f"https://zenodo.org/records/{rec_id}"
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


def _eml_text(el: Any) -> str:
    """All text inside an EML element (e.g. <abstract><para>…</para></abstract>), whitespace-collapsed."""
    if el is None:
        return ""
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def fetch_obis_eml(eml_url: str) -> dict[str, Any]:
    """Read dataset-level fields from an IPT EML document; empty dict on failure."""
    try:
        resp = requests.get(eml_url, timeout=30)
        resp.raise_for_status()
        dataset_el = ET.fromstring(resp.content).find("dataset")
    except Exception as exc:
        print(f"Warning: could not read OBIS EML {eml_url} ({exc})", file=sys.stderr)
        return {}
    if dataset_el is None:
        return {}

    fields: dict[str, Any] = {}

    title = _eml_text(dataset_el.find("title"))
    if title:
        fields["name"] = title

    abstract_el = dataset_el.find("abstract")
    if abstract_el is not None:
        paras = [_eml_text(p) for p in abstract_el.findall("para")] or [_eml_text(abstract_el)]
        abstract = " ".join(p for p in paras if p)
        if abstract:
            fields["description"] = abstract

    creators: list[dict[str, Any]] = []
    for c in dataset_el.findall("creator"):
        given = _eml_text(c.find("individualName/givenName"))
        sur = _eml_text(c.find("individualName/surName"))
        name = ", ".join(p for p in (sur, given) if p) or _eml_text(c.find("organizationName"))
        if not name:
            continue
        creator: dict[str, Any] = {"@type": "Person" if sur else "Organization", "name": name}
        if given and sur:
            creator["givenName"] = given
            creator["familyName"] = sur
        orcid = _eml_text(c.find("userId"))
        if "orcid.org" in orcid:
            creator["@id"] = orcid
        org = _eml_text(c.find("organizationName"))
        if sur and org:
            creator["affiliation"] = {"@type": "Organization", "name": org}
        creators.append(creator)
    if creators:
        fields["creator"] = creators

    keywords: list[str] = []
    for ks in dataset_el.findall("keywordSet"):
        # Skip the GBIF dataset-type vocabulary (e.g. "Samplingevent")
        if "GBIF Dataset Type" in _eml_text(ks.find("keywordThesaurus")):
            continue
        for kw in ks.findall("keyword"):
            # NERC terms come as "Label: http://vocab…"
            label = _eml_text(kw).split(": http", 1)[0].strip()
            if label and label not in keywords:
                keywords.append(label)
    if keywords:
        fields["keywords"] = keywords

    ulink = dataset_el.find("intellectualRights/para/ulink")
    if ulink is not None and ulink.get("url"):
        fields["license"] = ulink.get("url")

    return fields


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

        # RSS titles carry " - Version X.Y"; keep the version as its own field
        version_match = re.search(r"\s+-\s+Version\s+(\S+)$", title)

        dataset: dict[str, Any] = {
            "@type": "Dataset",
            "name": title or "OBIS IPT resource",
            "description": desc,
            "url": link or identifier_val,
            "additionalType": "Dataset",
            "includedInDataCatalog": OBIS_CATALOG,
        }
        if version_match:
            dataset["version"] = version_match.group(1)
        # RSS <description> is the version change note; the dataset abstract,
        # title, creators, keywords and license live in the EML document
        if eml_el is not None and eml_el.text:
            dataset.update(fetch_obis_eml(eml_el.text.strip()))
            time.sleep(REQUEST_DELAY_S)
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


def _pangaea_dataset_to_schema(ds: Any) -> dict[str, Any]:
    """Map a PanDataSet object to a schema.org JSON-LD stub."""
    doi = getattr(ds, "doi", None) or ""
    uri = getattr(ds, "uri", None) or ""
    url = uri or (f"https://doi.pangaea.de/{doi}" if doi else "")

    record: dict[str, Any] = {
        "@type": "Dataset",
        "name": getattr(ds, "title", None) or f"PANGAEA {doi or 'dataset'}",
        "url": url,
        "additionalType": "Dataset",
        "includedInDataCatalog": PANGAEA_CATALOG,
    }

    if doi:
        record["identifier"] = _doi_property_value(doi)

    abstract = getattr(ds, "abstract", None)
    if abstract:
        record["description"] = abstract

    year = getattr(ds, "year", None)
    if year:
        record["datePublished"] = str(year)

    authors = getattr(ds, "authors", None) or []
    creators: list[dict[str, Any]] = []
    for a in authors:
        name = getattr(a, "fullname", "") or ""
        if not name:
            continue
        creator: dict[str, Any] = {"@type": "Person", "name": name}
        orcid = getattr(a, "ORCID", None)
        if orcid:
            creator["identifier"] = orcid
        creators.append(creator)
    if creators:
        record["creator"] = creators

    keywords = getattr(ds, "keywords", None)
    if keywords:
        record["keywords"] = list(keywords)

    licence = getattr(ds, "licence", None)
    if licence:
        lic_uri = getattr(licence, "URI", None)
        if lic_uri:
            record["license"] = lic_uri

    return record


def harvest_pangaea(query: str = PANGAEA_QUERY) -> list[dict]:
    """Search Pangaea for datasets matching query and return schema.org records."""
    try:
        from pangaeapy.panquery import PanQuery
        from pangaeapy.pandataset import PanDataSet
    except ImportError:
        print(
            "Warning: pangaeapy not installed; skipping Pangaea harvest. "
            "Run: pip install pangaeapy",
            file=sys.stderr,
        )
        return []

    all_uris: list[str] = []
    offset = 0
    per_page = 500

    while True:
        try:
            pq = PanQuery(query, limit=per_page, offset=offset)
        except Exception as exc:
            print(f"Warning: Pangaea search failed at offset {offset} ({exc})", file=sys.stderr)
            break

        results = pq.result or []
        if not results:
            break

        for r in results:
            uri = r.get("URI")
            if uri:
                all_uris.append(uri)

        total = int(getattr(pq, "totalcount", 0) or 0)
        offset += len(results)
        if offset >= total or len(results) < per_page:
            break
        time.sleep(REQUEST_DELAY_S)

    if not all_uris:
        print("Pangaea: no results found", file=sys.stderr)
        return []

    print(f"Pangaea: loading metadata for {len(all_uris)} record(s)", file=sys.stderr)

    datasets: list[dict] = []
    for i, uri in enumerate(all_uris, 1):
        try:
            ds = PanDataSet(uri, include_data=False)
        except Exception as exc:
            print(f"Warning: could not load Pangaea dataset {uri} ({exc})", file=sys.stderr)
            time.sleep(REQUEST_DELAY_S)
            continue
        datasets.append(_pangaea_dataset_to_schema(ds))
        if i < len(all_uris):
            time.sleep(REQUEST_DELAY_S)

    return datasets


def _github_headers() -> dict[str, str]:
    """GitHub API headers; GITHUB_TOKEN raises the search rate limit."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _github_repo_to_schema(repo: dict[str, Any], topic: str) -> dict[str, Any]:
    """Map a GitHub API repository object to a schema.org SoftwareSourceCode stub."""
    html_url = repo.get("html_url") or ""
    owner = repo.get("owner") or {}

    record: dict[str, Any] = {
        "@type": "SoftwareSourceCode",
        "name": repo.get("name") or repo.get("full_name") or "GitHub repository",
        "url": html_url,
        "codeRepository": html_url,
        "additionalType": "Software",
        "includedInDataCatalog": GITHUB_CATALOG,
    }

    if repo.get("description"):
        record["description"] = repo["description"]
    if owner.get("login"):
        record["creator"] = [{
            "@type": "Organization" if owner.get("type") == "Organization" else "Person",
            "name": owner["login"],
            "url": owner.get("html_url") or f"https://github.com/{owner['login']}",
        }]
    if repo.get("created_at"):
        record["datePublished"] = repo["created_at"][:10]
    if repo.get("pushed_at"):
        record["dateModified"] = repo["pushed_at"][:10]
    if repo.get("language"):
        record["programmingLanguage"] = repo["language"]

    # The harvest topic itself says nothing about the content
    keywords = [t for t in repo.get("topics") or [] if t != topic]
    if keywords:
        record["keywords"] = keywords

    spdx = (repo.get("license") or {}).get("spdx_id")
    if spdx and spdx != "NOASSERTION":
        record["license"] = f"https://spdx.org/licenses/{spdx}"

    parent = repo.get("parent") or {}
    if parent.get("html_url"):
        record["isBasedOn"] = {
            "@type": "SoftwareSourceCode",
            "name": parent.get("full_name") or parent.get("name") or "",
            "url": parent["html_url"],
            "codeRepository": parent["html_url"],
        }

    return record


def _github_fork_parent(repo: dict[str, Any]) -> dict[str, Any] | None:
    """Upstream repository of a fork (search results omit it); None if unavailable."""
    try:
        r = requests.get(repo["url"], headers=_github_headers(), timeout=30)
        r.raise_for_status()
        return r.json().get("parent")
    except (requests.RequestException, KeyError, ValueError) as exc:
        print(f"Warning: could not look up fork parent of {repo.get('full_name')} ({exc})", file=sys.stderr)
        return None


def _github_rate_limit_wait(r: requests.Response) -> float | None:
    """Seconds to wait before retrying a rate-limited GitHub response, or None if not rate-limited."""
    if r.status_code not in (403, 429):
        return None
    if r.headers.get("Retry-After"):
        return float(r.headers["Retry-After"])
    if r.headers.get("X-RateLimit-Remaining") == "0" and r.headers.get("X-RateLimit-Reset"):
        return max(0.0, float(r.headers["X-RateLimit-Reset"]) - time.time()) + 1
    return None


def _github_search_page(params: dict[str, Any], retries: int = 2) -> dict:
    """GET one page of GitHub search results, waiting out short rate limits."""
    for attempt in range(retries + 1):
        r = requests.get(GITHUB_SEARCH_URL, params=params, headers=_github_headers(), timeout=30)
        wait = _github_rate_limit_wait(r)
        # Search limits reset every minute; anything longer is not worth blocking on
        if wait is not None and wait <= 90 and attempt < retries:
            print(f"GitHub rate limit reached; retrying in {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise requests.HTTPError("GitHub rate limit still exceeded after retries")


def harvest_github(topic: str = GITHUB_TOPIC) -> list[dict] | None:
    """
    Search GitHub for public repositories tagged with topic and return schema.org records.
    Returns None if the search failed, so callers can keep previously harvested records.
    """
    # Search leaves out forks unless asked; project code is often a fork of a partner's repo
    params: dict[str, Any] = {"q": f"topic:{topic} fork:true", "per_page": 100, "page": 1}
    repos: list[dict] = []

    while True:
        try:
            data = _github_search_page(params)
        except requests.RequestException as exc:
            hint = "" if os.environ.get("GITHUB_TOKEN") else " Set GITHUB_TOKEN to raise the rate limit."
            print(f"Warning: GitHub search failed on page {params['page']} ({exc}).{hint}", file=sys.stderr)
            return None

        items = data.get("items") or []
        repos.extend(items)
        if not items or len(repos) >= int(data.get("total_count") or 0):
            break
        params["page"] += 1
        time.sleep(REQUEST_DELAY_S)

    if not repos:
        print(f"GitHub: no repositories found with topic '{topic}'", file=sys.stderr)
    for repo in repos:
        if repo.get("fork"):
            repo["parent"] = _github_fork_parent(repo)
            time.sleep(REQUEST_DELAY_S)
    return [_github_repo_to_schema(repo, topic) for repo in repos]


def _github_full_name_from_record(record: dict) -> str | None:
    """'owner/repo' from a GitHub repository record's codeRepository or url."""
    for field in ("codeRepository", "url"):
        m = re.match(r"https?://github\.com/([^/?#]+/[^/?#]+)", str(record.get(field) or ""))
        if m:
            return m.group(1).removesuffix(".git")
    return None


def _obis_resource_slug(url: str) -> str | None:
    if "resource?r=" in url:
        return url.split("resource?r=", 1)[1].split("&", 1)[0]
    return None


def _pangaea_id_from_record(record: dict) -> str | None:
    """Extract numeric PANGAEA dataset ID from a record's url, @id, or identifier."""
    for field in ("url", "@id", "identifier"):
        val = record.get(field)
        if isinstance(val, dict):
            val = val.get("value") or val.get("url") or ""
        m = re.search(r"PANGAEA\.(\d+)", str(val or ""))
        if m:
            return m.group(1)
    return None


def _zenodo_rec_id_from_record(record: dict) -> str | None:
    src = record.get("url") or record.get("@id") or ""
    m = re.search(r"zenodo\.org/records?/(\d+)", src) if isinstance(src, str) else None
    return m.group(1) if m else None


def _stable_record_key(record: dict) -> str | None:
    rec_id = _zenodo_rec_id_from_record(record)
    if rec_id:
        return f"zenodo:{rec_id}"
    url = str(record.get("url") or record.get("@id") or "")
    obis_slug = _obis_resource_slug(url)
    if obis_slug:
        return f"obis:{obis_slug}"
    pangaea_id = _pangaea_id_from_record(record)
    if pangaea_id:
        return f"pangaea:{pangaea_id}"
    gh_name = _github_full_name_from_record(record)
    if gh_name:
        return f"github:{gh_name}"
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
    pangaea_id = _pangaea_id_from_record(record)
    if pangaea_id:
        matches = sorted(record_dir.glob(f"*-pangaea{pangaea_id}.json"))
        if matches:
            return matches[0]
    gh_name = _github_full_name_from_record(record)
    if gh_name:
        path = record_dir / f"{slugify(gh_name)}.json"
        if path.is_file():
            return path
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
    rec_id = _zenodo_rec_id_from_record(record)
    if rec_id:
        return f"{base_slug}-{rec_id}.json"
    src = record.get("@id") or record.get("url") or ""
    if isinstance(src, str):
        obis_slug = _obis_resource_slug(src)
        if obis_slug:
            return f"{base_slug}-{slugify(obis_slug)}.json"
    pangaea_id = _pangaea_id_from_record(record)
    if pangaea_id:
        return f"{base_slug}-pangaea{pangaea_id}.json"
    gh_name = _github_full_name_from_record(record)
    if gh_name:
        # owner-repo, since the same repo name can exist under different owners
        return f"{slugify(gh_name)}.json"
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
    pangaea_dir: Path | None = DEFAULT_PANGAEA_DIR,
    github_dir: Path | None = DEFAULT_GITHUB_DIR,
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
        record = _apply_zenodo_metadata(record, meta)

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

        pangaea_records = harvest_pangaea(PANGAEA_QUERY)
        for dataset in pangaea_records:
            record = enrich_record(dataset, add_funding=add_funding)
            if write_json and pangaea_dir is not None:
                record, action = _sync_record_file(
                    record, pangaea_dir, base_url=base_url, cwd=cwd, source_label="pangaea"
                )
                stats[action] += 1
            catalogue.append(record)
        if pangaea_records:
            print(f"Processed {len(pangaea_records)} Pangaea dataset(s)", file=sys.stderr)

        github_records = harvest_github(GITHUB_TOPIC)
        if github_records is None:
            # Search failed: keep last run's records in the catalogue rather than dropping them
            github_records = []
            if github_dir is not None and github_dir.is_dir():
                kept = sorted(github_dir.glob("*.json"))
                for path in kept:
                    with path.open(encoding="utf-8") as f:
                        catalogue.append(json.load(f))
                print(f"GitHub: kept {len(kept)} previously harvested record(s)", file=sys.stderr)
        for repo in github_records:
            record = enrich_record(repo, add_funding=add_funding)
            if write_json and github_dir is not None:
                record, action = _sync_record_file(
                    record, github_dir, base_url=base_url, cwd=cwd, source_label="github"
                )
                stats[action] += 1
            catalogue.append(record)
        if github_records:
            print(f"Processed {len(github_records)} GitHub repositories", file=sys.stderr)

    if write_json:
        print(
            f"JSON files: {stats['created']} created, {stats['updated']} updated, "
            f"{stats['skipped']} skipped",
            file=sys.stderr,
        )

    return catalogue, stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Harvest Zenodo, OBIS IPT, PANGAEA and GitHub metadata and export as JSON-LD catalogue."
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
        "--pangaea-dir",
        type=Path,
        default=DEFAULT_PANGAEA_DIR,
        help=f"Directory for Pangaea JSON files (default: {DEFAULT_PANGAEA_DIR}).",
    )
    parser.add_argument(
        "--github-dir",
        type=Path,
        default=DEFAULT_GITHUB_DIR,
        help=f"Directory for GitHub repository JSON files (default: {DEFAULT_GITHUB_DIR}).",
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
        pangaea_dir=None if args.no_json_files else args.pangaea_dir,
        github_dir=None if args.no_json_files else args.github_dir,
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
        print(f"Pangaea JSON: {args.pangaea_dir}", file=sys.stderr)
        print(f"GitHub JSON: {args.github_dir}", file=sys.stderr)
        print("Run update_sitemap.py after harvest to refresh sitemap.xml.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

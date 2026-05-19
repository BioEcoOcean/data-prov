#!/usr/bin/env python3
"""
Build sitemap.xml from per-record JSON-LD files under jsonFiles/.

Each <loc> is the raw GitHub (or other) URL to the JSON file, e.g.
https://raw.githubusercontent.com/BioEcoOcean/data-prov/refs/heads/main/jsonFiles/zenodo/....json
"""

from __future__ import annotations

import argparse
import html
import sys
from pathlib import Path
from typing import Iterable

DEFAULT_ZENODO_DIR = Path("jsonFiles/zenodo")
DEFAULT_OBIS_DIR = Path("jsonFiles/OBIS")
DEFAULT_OUTPUT = Path("sitemap.xml")


def collect_json_file_urls(base_url: str, directories: list[Path], cwd: Path) -> list[str]:
    """List <loc> URLs for every .json file under the given directories."""
    base = base_url.rstrip("/")
    urls: list[str] = []
    for directory in directories:
        if not directory.is_dir():
            print(f"Warning: directory not found, skipping: {directory}", file=sys.stderr)
            continue
        for path in sorted(directory.rglob("*.json")):
            if not path.is_file():
                continue
            try:
                rel = path.resolve().relative_to(cwd)
            except ValueError:
                rel = path
            urls.append(f"{base}/{rel.as_posix()}")
    return urls


def build_sitemap_xml(urls: Iterable[str]) -> str:
    url_list = sorted({u for u in urls if u})
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc in url_list:
        lines.append(f"  <url><loc>{html.escape(loc, quote=True)}</loc></url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build XML sitemap from jsonFiles/zenodo and jsonFiles/OBIS JSON records.",
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help=(
            "URL prefix for each JSON file, e.g. "
            "https://raw.githubusercontent.com/BioEcoOcean/data-prov/refs/heads/main"
        ),
    )
    parser.add_argument(
        "--zenodo-dir",
        type=Path,
        default=DEFAULT_ZENODO_DIR,
        help=f"Zenodo JSON directory (default: {DEFAULT_ZENODO_DIR}).",
    )
    parser.add_argument(
        "--obis-dir",
        type=Path,
        default=DEFAULT_OBIS_DIR,
        help=f"OBIS JSON directory (default: {DEFAULT_OBIS_DIR}).",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output sitemap path (default: {DEFAULT_OUTPUT}).",
    )
    args = parser.parse_args()

    cwd = Path.cwd().resolve()
    dirs = [args.zenodo_dir.resolve(), args.obis_dir.resolve()]
    urls = collect_json_file_urls(args.base_url, dirs, cwd)

    if not urls:
        print("No JSON files found; sitemap not written.", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    xml = build_sitemap_xml(urls)
    args.output.write_text(xml, encoding="utf-8")
    print(f"Wrote {len(urls)} URL(s) to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

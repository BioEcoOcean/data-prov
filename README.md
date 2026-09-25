# data-prov

Sandbox repo for tools to track (meta)data provenance and controlled vocabulary usage for data generated through the project. It contains:

* CSV file to document mapping of variables measured by partners to controlled vocabularies
* Development of a metadata catalogue that pulls metadata from Zenodo (BioEcoOcean community: [https://zenodo.org/communities/bioecoocean/](https://zenodo.org/communities/bioecoocean/))

## Metadata catalogue

This catalogue is meant to be a place where all BioEcoOcean outputs can be found in one place. Quoting the BioEcoOcean Data Management Plan:

> To increase findability of project data and outputs, we are exploring development of a metadata catalogue that harvests metadata directly from the BioEcoOcean Zenodo community and exposes it as JSON-LD aligned with the ODIS specification for schema.org (i.e. ODIS-Arch). Such a catalogue would also be able to display all project outputs in one place, including those not represented in Zenodo (e.g. FigShare datasets). Project partners are responsible for providing complete metadata and documentation for all outputs regardless of repository. Records associated with data published elsewhere should clearly document these sources by using identifiers pointing to the original source (e.g. DOI).

### Running the catalogue harvest

The script `metadata-cat.py` connects to the Zenodo REST API, lists all records in the **BioEcoOcean** community, and writes a single JSON-LD catalogue file (schema.org/ODIS-aligned).

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Harvest the full BioEcoOcean community (catalogue + per-record JSON):
   ```bash
   python metadata-cat.py --base-url "https://raw.githubusercontent.com/BioEcoOcean/data-prov/refs/heads/main"
   ```

   By default this writes:
   - `bioecoocean-catalogue.jsonld` — combined catalogue (rewritten each run)
   - `jsonFiles/zenodo/*.json` — one file per Zenodo record
   - `jsonFiles/OBIS/*.json` — one file per OBIS IPT dataset
   - `jsonFiles/pangaea/*.json` — one file per PANGAEA dataset matching "BioEcoOcean"

   Re-runs **update only changed** per-record JSON files (matched by Zenodo record id or OBIS resource slug). The terminal prints `created`, `updated`, or `skipped` for each file, plus a summary count.

3. Build the XML sitemap from those JSON files (raw GitHub URLs, not Zenodo links):
   ```bash
   python update_sitemap.py --base-url "https://raw.githubusercontent.com/BioEcoOcean/data-prov/refs/heads/main"
   ```
   Writes `sitemap.xml` with one `<loc>` per JSON file under `jsonFiles/zenodo/`, `jsonFiles/OBIS/` and `jsonFiles/pangaea/`.

4. Options (`metadata-cat.py`):
   - `--community ID` — Zenodo community identifier (default: `bioecoocean`)
   - `-o FILE` — Output catalogue path (default: `bioecoocean-catalogue.jsonld`)
   - `--zenodo-dir` / `--obis-dir` / `--pangaea-dir` — Output folders (defaults: `jsonFiles/zenodo`, `jsonFiles/OBIS`, `jsonFiles/pangaea`)
   - `--base-url URL` — Prefix for each record’s `@id` (defaults to raw GitHub URL of that JSON file; see `DEFAULT_BASE_URL` in `metadata-cat.py`)
   - `--no-json-files` — Skip writing per-record JSON files
   - `--max-pages N` — Limit Zenodo pages (testing; 25 records per page)
   - `--no-funding` — Omit the BioEcoOcean funding block

   Example (catalogue only, no JSON files):
   ```bash
   python metadata-cat.py --no-json-files
   ```

The output is a JSON-LD document with an `@graph` of schema.org entries. Zenodo entries come from Zenodo's JSON-LD export (`/records/{id}/export/json-ld`), so `@type` follows the upload (`ScholarlyArticle`, `PresentationDigitalDocument`, `CreativeWork`, `Dataset`, …); OBIS IPT and PANGAEA resources are `Dataset`. Each entry includes `@id`, `name`, `identifier` (DOI as `PropertyValue` when available), `url`, `additionalType` (the Zenodo resource type, e.g. "Poster", "Project deliverable", or "Dataset"), `includedInDataCatalog` (Zenodo, OBIS or PANGAEA), and when available `description`, `datePublished`, `creator` (with ORCID and affiliation from Zenodo), plain-string `keywords`, `publishingPrinciples` (from license), and `funding` (BioEcoOcean grant first, plus any co-funders listed on Zenodo). Use `--no-funding` to omit the funding block. No API token is required for public records.

### Landing page (list and search outputs)

`index.html` is a static landing page that loads `bioecoocean-catalogue.jsonld` and lists all outputs with client-side search (by title, description, keywords, creators).

1. Generate the catalogue (if needed):
   ```bash
   python metadata-cat.py
   ```

2. Serve the folder over HTTP (required so the page can load the JSON-LD file):
   ```bash
   python -m http.server 8000
   ```
   
   Then open [http://localhost:8000](http://localhost:8000) in your browser.

You can also deploy the repo (e.g. to GitHub Pages) so that `index.html` and `bioecoocean-catalogue.jsonld` are served from the same origin.

## Code Set up

```
python -m venv new_env
.\new_env\Scripts\Activate.ps1
```

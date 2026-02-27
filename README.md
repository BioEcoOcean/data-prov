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

2. Harvest the full BioEcoOcean community (writes `bioecoocean-catalogue.jsonld` by default):
   ```bash
   python metadata-cat.py
   ```

3. Options:
   - `--community ID` — Zenodo community identifier (default: `bioecoocean`)
   - `-o FILE` — Output path (default: `bioecoocean-catalogue.jsonld`)
   - `--max-pages N` — Limit to N pages of results (for testing; 25 records per page)

   Example for another community and custom output:
   ```bash
   python metadata-cat.py --community my-community -o my-catalogue.jsonld
   ```

The output is a JSON-LD document with an `@graph` of schema.org `Dataset` (or other) entities, each with `@id`, `name`, `identifier`, `url`, and when available from Zenodo, `description`, `datePublished`, `creator`, `keywords`, and `license`. No API token is required for public records.

4. Export json into schema format
  `python export_json.py \`
  `--input bioecoocean-catalogue.jsonld \`
  `--out-dir jsonFiles/zenodo \`
  `--base-url "https://raw.githubusercontent.com/BioEcoOcean/data-prov/refs/heads/main" \`
  `--sitemap sitemap.jsonld` 

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

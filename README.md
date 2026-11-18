# Matamna county scraper

Scrapers that collect **public-record** real-estate leads for Dallas, Tarrant, Collin and Denton counties (Texas):
foreclosure notices, probate records, and tax lawsuits / tax-delinquent accounts. Results are written to a
**private Google Sheet**, which a separate private web app (Matamna County Leads, Google Apps Script) reads.

This repository only holds the scrapers. It is public so GitHub Actions runs are free; it contains no keys,
no sheet IDs and no lead data. Everything private lives in GitHub **Secrets** and in the Google Sheet.

## Workflows

| Workflow | Script | When (Chicago time) | What it adds |
|---|---|---|---|
| Daily county scrape (`scrape.yml`) | `scraper/main.py` | 10 AM and 6 PM, plus **Fetch now** in the web app / *Run workflow* here | Foreclosure notices from the County Clerks |
| Daily probate (`probate.yml`) | `scraper/probate.py` | ~3:37 AM | Probate records (County Clerk records, Collin / Denton probate courts) |
| Weekly tax roll (`tax.yml`) | `scraper/tax.py` | Tuesday ~2 AM | Tax lawsuits and tax-delinquent accounts |
| Tests (`tests.yml`) | `tests/` | every push | Parser tests (pytest) |

GitHub fires two cron entries per slot (summer / winter time); the run that lands outside a Chicago slot stops in
its first step, in a few seconds. *Run workflow* inputs: `force`, `days_back` (extra days to look back) and
`budget_min` (minutes of scraping before it stops; 40 normal, up to 320 to clear a backlog).

## Secrets (Settings › Secrets and variables › Actions)

| Name | Value |
|---|---|
| `GOOGLE_SA_JSON` | The whole JSON key of the Google service account that can edit the sheet |
| `SHEET_ID` | ID of the County Leads Google Sheet (the part between `/d/` and `/edit` in its URL) |

The sheet must be shared (Editor) with the service account's email. Secrets are never shown in logs and are not
given to workflows started from forks.

## Rules the scrapers follow

- Dates come from official sources only.
- Records are never merged on a fuzzy match; unclear ones go to the **Scrape Queue** tab.
- Cells a person typed in the sheet are never overwritten.
- Every run writes **Scrape Log** and **Scrape Detail** rows.

## Pausing a county

In the web app, **Log › Scrape counties** turns each county on or off. The choice is kept in the **Scrape Counties**
tab (County | Scrape | Resume from | Changed at | Changed by) and read at the start of every run by all three
scrapers (`Sheet.county_on` / `county_since` / `county_keeps` in `scraper/sheet.py`):

- a paused county is skipped completely (Scrape Log row with status `paused`);
- turning a county back on saves that day as *Resume from*; from then on only records filed on or after that day
  are added. Tax-delinquent accounts have no filed date and are added as before.

## Run locally

```bash
pip install -r requirements.txt pytest
python -m playwright install --with-deps chromium     # county scrape only
sudo apt-get install -y tesseract-ocr                 # OCR for scanned notices
export GOOGLE_SA_JSON="$(cat service-account.json)" SHEET_ID=...
python -m scraper.main        # or scraper.probate / scraper.tax
python -m pytest -q tests
```

## Notes

- GitHub turns off scheduled workflows in a public repository after 60 days without a commit. If that happens,
  open **Actions** and press *Enable workflow* (or push any small change).
- The web app's **Fetch now** button starts `scrape.yml` here through the GitHub API (Script properties
  `GH_TOKEN` = fine-grained token with *Actions: read and write* on this repository, `GH_REPO` =
  `peaker23/matamna-county-scraper`).

#!/usr/bin/env python3
"""
Weekly Iowa Liquor Sales keyword report.

Downloads the current year's full Iowa Liquor Sales CSV from the Iowa Data
Hub (idh-be.iowa.gov) and filters locally for rows in the last 7 days where
the Store Name or Vendor Name matches any of the target keywords, then
writes an HTML + CSV report.

Note: Iowa's open-data platform migrated in 2026 from the old Socrata-based
data.iowa.gov API (which supported server-side filtering) to a new "Iowa
Data Hub" platform that only offers full-file downloads, split one dataset
per calendar year. This script downloads the whole year's file and filters
it in Python instead of filtering server-side.
"""

import csv
import datetime as dt
import html
import io
import os
import sys
import urllib.request
import urllib.error

# ---- Configuration -------------------------------------------------------

# Iowa's open-data platform (Iowa Data Hub) publishes this dataset as one
# full CSV per calendar year, at a numeric dataset ID that increments each
# year the dataset is re-published. Known IDs so far:
#   2024 -> 1261
#   2025 -> 1262
# We try the current year's likely ID first (guessed from the pattern), and
# fall back to nearby IDs / last year's if that guess is wrong, printing
# clear diagnostics either way so this is easy to fix if IDs shift again.
KNOWN_YEAR_DATASET_IDS = {
    2024: 1261,
    2025: 1262,
}

BASE_URL = "https://idh-be.iowa.gov/api/v1/datasets/{id}/rows.csv"

KEYWORDS = [
    "Westside Spirits",
    "Benz Distributing",
]

# How many days back to look. 7 covers "since last week's report."
DAYS_BACK = 7

OUTPUT_DIR = "reports"


def guess_dataset_id_for_year(year):
    if year in KNOWN_YEAR_DATASET_IDS:
        return KNOWN_YEAR_DATASET_IDS[year]
    # Extrapolate forward from the most recent known year, assuming IDs
    # increment by 1 per year (true for 2024->2025). This is a guess and
    # is verified by actually trying the download below.
    last_known_year = max(KNOWN_YEAR_DATASET_IDS)
    offset = year - last_known_year
    return KNOWN_YEAR_DATASET_IDS[last_known_year] + offset


def fetch_csv_text(dataset_id):
    url = BASE_URL.format(id=dataset_id)
    req = urllib.request.Request(url)
    req.add_header(
        "User-Agent",
        "Mozilla/5.0 (compatible; weekly-report-script/1.0; "
        "+https://github.com/)",
    )
    req.add_header("Accept", "text/csv")
    with urllib.request.urlopen(req, timeout=180) as resp:
        raw = resp.read()
    return raw.decode("utf-8-sig", errors="replace")


def fetch_current_year_csv():
    """Try the guessed dataset ID for the current year; fall back to a
    small range of nearby IDs if the guess is off, and print exactly what
    was tried so it's easy to fix if this breaks again."""
    year = dt.date.today().year
    candidates = []
    guessed = guess_dataset_id_for_year(year)
    candidates.append((year, guessed))
    # Also try a couple of IDs just above/below in case the guess is off
    # by one (e.g. if Iowa published this year's dataset out of strict
    # numeric order).
    for delta in (1, -1, 2, -2):
        candidates.append((year, guessed + delta))

    errors = []
    for yr, ds_id in candidates:
        url = BASE_URL.format(id=ds_id)
        try:
            print(f"Trying dataset id {ds_id} for year {yr}: {url}")
            text = fetch_csv_text(ds_id)
            print(f"Success: dataset id {ds_id} returned {len(text)} chars")
            return text, ds_id
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:300]
            errors.append(f"  id {ds_id}: HTTP {e.code} - {body}")
        except Exception as e:
            errors.append(f"  id {ds_id}: {e}")

    raise RuntimeError(
        "Could not find the current year's Iowa Liquor Sales dataset. "
        "Tried:\n" + "\n".join(errors) +
        "\n\nThe dataset ID may need to be updated by hand in "
        "KNOWN_YEAR_DATASET_IDS -- check "
        "https://data.iowa.gov/catalog?search=iowa+liquor+sales "
        "for the current year's dataset and its numeric ID."
    )


def row_matches_keywords(row, keywords):
    store = (row.get("Store Name") or "").upper()
    vendor = (row.get("Vendor Name") or "").upper()
    for kw in keywords:
        kw_upper = kw.upper()
        if kw_upper in store or kw_upper in vendor:
            return True
    return False


def parse_date(value):
    # Iowa liquor sales CSVs use MM/DD/YYYY dates.
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


def fetch_rows():
    csv_text, dataset_id = fetch_current_year_csv()
    reader = csv.DictReader(io.StringIO(csv_text))

    cutoff = dt.date.today() - dt.timedelta(days=DAYS_BACK)
    matches = []
    for row in reader:
        row_date = parse_date(row.get("Date", ""))
        if row_date is None or row_date < cutoff:
            continue
        if row_matches_keywords(row, KEYWORDS):
            matches.append(row)

    print(f"Scanned dataset {dataset_id}; {len(matches)} matching row(s) "
          f"since {cutoff.isoformat()}")
    return matches, cutoff.isoformat()


# ---- Report generation ------------------------------------------------------

REPORT_COLUMNS = [
    ("Date", "Date"),
    ("Store Name", "Store Name"),
    ("City", "City"),
    ("Vendor Name", "Vendor Name"),
    ("Item Description", "Item"),
    ("Bottles Sold", "Bottles Sold"),
    ("Sale (Dollars)", "Sale ($)"),
]


def build_html_report(rows, start_date, keywords):
    today_str = dt.date.today().isoformat()

    def esc(v):
        return html.escape(str(v)) if v is not None else ""

    table_rows = []
    for r in rows:
        cells = [esc(r.get(col)) for col, _ in REPORT_COLUMNS]
        table_rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")

    header_cells = "".join(f"<th>{label}</th>" for _, label in REPORT_COLUMNS)
    body = "\n".join(table_rows) if table_rows else (
        f'<tr><td colspan="{len(REPORT_COLUMNS)}">No matching records found.</td></tr>'
    )

    keyword_list = ", ".join(esc(k) for k in keywords)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Iowa Liquor Sales Keyword Report — {today_str}</title>
<style>
  body {{ font-family: Arial, sans-serif; margin: 2rem; color: #222; }}
  h1 {{ font-size: 1.4rem; }}
  .meta {{ color: #555; margin-bottom: 1.5rem; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ccc; padding: 6px 10px; font-size: 0.9rem; text-align: left; }}
  th {{ background: #f2f2f2; }}
  tr:nth-child(even) {{ background: #fafafa; }}
</style>
</head>
<body>
  <h1>Iowa Liquor Sales Keyword Report</h1>
  <div class="meta">
    Generated {today_str} &middot; Records from {start_date} to today &middot;
    Keywords: {keyword_list} &middot; {len(rows)} matching row(s)
  </div>
  <table>
    <thead><tr>{header_cells}</tr></thead>
    <tbody>
      {body}
    </tbody>
  </table>
</body>
</html>
"""


def write_csv(rows, path):
    if not rows:
        fieldnames = [k for k, _ in REPORT_COLUMNS]
    else:
        fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def main():
    try:
        rows, start_date = fetch_rows()
    except Exception as exc:
        print(f"ERROR fetching data: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(rows)} matching row(s)")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    today_str = dt.date.today().isoformat()
    html_path = os.path.join(OUTPUT_DIR, f"report-{today_str}.html")
    csv_path = os.path.join(OUTPUT_DIR, f"report-{today_str}.csv")
    latest_path = os.path.join(OUTPUT_DIR, "latest.html")

    report_html = build_html_report(rows, start_date, KEYWORDS)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(report_html)
    with open(latest_path, "w", encoding="utf-8") as f:
        f.write(report_html)
    write_csv(rows, csv_path)

    print(f"Wrote {html_path}")
    print(f"Wrote {csv_path}")
    print(f"Updated {latest_path}")


if __name__ == "__main__":
    main()

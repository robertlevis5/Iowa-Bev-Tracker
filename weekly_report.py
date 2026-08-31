#!/usr/bin/env python3
"""
Weekly Iowa Liquor Sales keyword report.

Queries the Iowa Liquor Sales open dataset (data.iowa.gov, dataset id
m3tr-qhgy) via the Socrata SODA API for rows in the last 7 days where the
Store Name or Vendor Name matches any of the target keywords, and writes an
HTML report.

Data source: https://data.iowa.gov/resource/m3tr-qhgy.json
(Public dataset, no API key required for reasonable use. Socrata does allow
registering a free app token to raise rate limits — see SODA_APP_TOKEN below.)
"""

import csv
import datetime as dt
import html
import os
import sys
import urllib.parse
import urllib.request
import urllib.error
import json

# ---- Configuration -------------------------------------------------------

DATASET_URL = "https://data.iowa.gov/resource/m3tr-qhgy.json"

KEYWORDS = [
    "Westside Spirits",
    "Benz Distributing",
]

# How many days back to look. 7 covers "since last week's report."
DAYS_BACK = 7

# Optional: put a free Socrata app token here (or set env var SODA_APP_TOKEN)
# to avoid throttling. Not required for this data volume.
APP_TOKEN = os.environ.get("SODA_APP_TOKEN", "")

OUTPUT_DIR = "reports"

# ---- Build the query -------------------------------------------------------

def build_where_clause(keywords, days_back):
    start_date = (dt.date.today() - dt.timedelta(days=days_back)).isoformat()
    date_clause = f"date >= '{start_date}T00:00:00'"

    keyword_clauses = []
    for kw in keywords:
        # Escape single quotes for SoQL string literals
        safe_kw = kw.replace("'", "''")
        keyword_clauses.append(f"upper(store_name) like upper('%{safe_kw}%')")
        keyword_clauses.append(f"upper(vendor_name) like upper('%{safe_kw}%')")

    keyword_where = " OR ".join(keyword_clauses)
    return f"{date_clause} AND ({keyword_where})", start_date


def fetch_rows(where_clause):
    params = {
        "$where": where_clause,
        "$order": "date DESC",
        "$limit": "5000",
    }
    url = DATASET_URL + "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(url)
    # Socrata (and many public APIs) return 403 for requests with no/blank
    # User-Agent, since that's a common bot fingerprint. Set a normal one.
    req.add_header(
        "User-Agent",
        "Mozilla/5.0 (compatible; weekly-report-script/1.0; "
        "+https://github.com/)",
    )
    req.add_header("Accept", "application/json")
    if APP_TOKEN:
        req.add_header("X-App-Token", APP_TOKEN)

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Surface the response body -- Socrata usually includes a helpful
        # JSON error message (e.g. bad column name) in the body.
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} from Socrata API. Response body: {body}") from e

    return data


# ---- Report generation ------------------------------------------------------

REPORT_COLUMNS = [
    ("date", "Date"),
    ("store_name", "Store Name"),
    ("city", "City"),
    ("vendor_name", "Vendor Name"),
    ("item_description", "Item"),
    ("bottles_sold", "Bottles Sold"),
    ("sale_dollars", "Sale ($)"),
]


def build_html_report(rows, start_date, keywords):
    today_str = dt.date.today().isoformat()

    def esc(v):
        return html.escape(str(v)) if v is not None else ""

    table_rows = []
    for r in rows:
        date_val = r.get("date", "")[:10]
        cells = [
            esc(date_val),
            esc(r.get("store_name")),
            esc(r.get("city")),
            esc(r.get("vendor_name")),
            esc(r.get("item_description")),
            esc(r.get("bottles_sold")),
            esc(r.get("sale_dollars")),
        ]
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
    where_clause, start_date = build_where_clause(KEYWORDS, DAYS_BACK)
    print(f"Query window start: {start_date}")
    print(f"SoQL $where: {where_clause}")

    try:
        rows = fetch_rows(where_clause)
    except Exception as exc:
        print(f"ERROR fetching data: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Fetched {len(rows)} matching row(s)")

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

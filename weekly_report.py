#!/usr/bin/env python3
"""
Weekly Iowa Liquor Sales keyword report.

Downloads the current year's full Iowa Liquor Sales file (a ZIP of CSV
parts) from the Iowa Data Hub (idh-be.iowa.gov) and filters locally for rows where the Store Name or
Vendor Name matches any target keyword, then writes an HTML + CSV report.

Notes:
- Iowa's open-data platform migrated in 2026 to a new "Iowa Data Hub" that
  only offers full-file downloads, one dataset per calendar year, so this
  script downloads the whole file and filters it in Python.
- The state publishes this data with roughly a two-week lag, so the report
  looks back LOOKBACK_DAYS from today (not just 7), and if that window is
  empty it shows the most recent matching records instead.
"""

import csv
import datetime as dt
import html
import io
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

# ---- Configuration -------------------------------------------------------

# Known dataset IDs per year on the Iowa Data Hub:
#   2024 -> 1261, 2025 -> 1262 (later years are guessed from the pattern)
KNOWN_YEAR_DATASET_IDS = {
    2024: 1261,
    2025: 1262,
}

BASE_URL = "https://idh-be.iowa.gov/api/v1/datasets/{id}/rows.csv"

KEYWORDS = [
    "Westside Spirits",
    "Benz Distributing",
]

# Look back this many days from today. 21 covers the ~2 week reporting lag
# plus about a week of data. Raise it if you want a longer history.
LOOKBACK_DAYS = 21

# If nothing falls in the window, show this many of the newest matches.
FALLBACK_ROWS = 15

OUTPUT_DIR = "reports"

# Our standard field name -> acceptable CSV header names (compared after
# lowercasing and stripping spaces/punctuation, so "Store Name", "store_name"
# and "STORE NAME" all match "storename").
FIELD_ALIASES = {
    "date": ["date", "orderedon"],
    "store_name": ["storename", "name"],
    "city": ["city", "storecity"],
    "vendor_name": ["vendorname", "vendor"],
    "item_description": ["itemdescription", "imdesc", "itemname", "item"],
    "bottles_sold": ["bottlessold", "salesbottles"],
    "sale_dollars": ["saledollars", "salesdollars", "sale"],
}
REQUIRED_FIELDS = ["date", "store_name", "vendor_name"]

REPORT_COLUMNS = [
    ("date", "Date"),
    ("store_name", "Store Name"),
    ("city", "City"),
    ("vendor_name", "Vendor Name"),
    ("item_description", "Item"),
    ("bottles_sold", "Bottles Sold"),
    ("sale_dollars", "Sale ($)"),
]


# ---- Download --------------------------------------------------------------

def guess_dataset_id_for_year(year):
    if year in KNOWN_YEAR_DATASET_IDS:
        return KNOWN_YEAR_DATASET_IDS[year]
    last_known_year = max(KNOWN_YEAR_DATASET_IDS)
    return KNOWN_YEAR_DATASET_IDS[last_known_year] + (year - last_known_year)


def download_to_file(dataset_id):
    """Stream the download to a temp file (it is a large ZIP)."""
    url = BASE_URL.format(id=dataset_id)
    req = urllib.request.Request(url)
    req.add_header(
        "User-Agent",
        "Mozilla/5.0 (compatible; weekly-report-script/1.0; +https://github.com/)",
    )
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".download")
    size = 0
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                tmp.write(chunk)
                size += len(chunk)
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise
    tmp.close()
    return tmp.name, size


def fetch_current_year_file():
    year = dt.date.today().year
    guessed = guess_dataset_id_for_year(year)
    candidates = [guessed] + [guessed + d for d in (1, -1, 2, -2)]

    errors = []
    for ds_id in candidates:
        url = BASE_URL.format(id=ds_id)
        try:
            print(f"Trying dataset id {ds_id} for year {year}: {url}")
            path, size = download_to_file(ds_id)
            print(f"Success: dataset id {ds_id} downloaded {size:,} bytes")
            return path, ds_id
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:300]
            errors.append(f"  id {ds_id}: HTTP {e.code} - {body}")
        except Exception as e:
            errors.append(f"  id {ds_id}: {e}")

    raise RuntimeError(
        "Could not find the current year's Iowa Liquor Sales dataset. Tried:\n"
        + "\n".join(errors)
        + "\n\nUpdate KNOWN_YEAR_DATASET_IDS by hand -- check "
        "https://data.iowa.gov/catalog for the current year's dataset ID."
    )


def iter_csv_readers(path):
    """Yield (name, DictReader) for each CSV in the download. The file is a
    ZIP containing one or more CSV parts; a plain CSV is also handled."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = sorted(n for n in zf.namelist() if n.lower().endswith(".csv"))
            if not names:
                raise RuntimeError(f"ZIP has no CSV files. Contents: {zf.namelist()}")
            print(f"ZIP contains {len(names)} CSV file(s): {names}")
            for n in names:
                with zf.open(n) as raw:
                    text = io.TextIOWrapper(raw, encoding="utf-8-sig",
                                            errors="replace", newline="")
                    yield n, csv.DictReader(text)
    else:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            yield os.path.basename(path), csv.DictReader(f)


# ---- Parsing and matching --------------------------------------------------

def norm_header(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def detect_columns(headers):
    """Map our standard field names to the CSV's actual header names."""
    by_norm = {}
    for h in headers:
        by_norm.setdefault(norm_header(h), h)
    colmap = {}
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if alias in by_norm:
                colmap[field] = by_norm[alias]
                break
    return colmap


def norm_text(s):
    return " ".join((s or "").upper().split())


def matches_keywords(store, vendor, keywords):
    store_n, vendor_n = norm_text(store), norm_text(vendor)
    for kw in keywords:
        kw_n = norm_text(kw)
        if kw_n in store_n or kw_n in vendor_n:
            return True
    return False


def parse_date(value):
    if not value:
        return None
    v = value.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%Y %I:%M:%S %p"):
        try:
            return dt.datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    try:
        return dt.date.fromisoformat(v[:10])
    except ValueError:
        return None


def fetch_rows():
    csv.field_size_limit(10_000_000)
    path, dataset_id = fetch_current_year_file()

    total = 0
    bad_dates = 0
    sample_bad_date = None
    newest_date = None
    all_matches = []
    printed_sample = False

    try:
        for part_name, reader in iter_csv_readers(path):
            headers = reader.fieldnames or []
            print(f"[{part_name}] columns: {headers}")
            colmap = detect_columns(headers)
            print(f"[{part_name}] column mapping: {colmap}")
            missing = [f for f in REQUIRED_FIELDS if f not in colmap]
            if missing:
                raise RuntimeError(
                    f"Could not find required column(s) {missing} in {part_name}. "
                    f"Actual headers were: {headers}. Update FIELD_ALIASES in "
                    "weekly_report.py to match."
                )

            for raw in reader:
                total += 1
                raw_date = raw.get(colmap["date"], "")
                if not printed_sample:
                    print(f"Sample date value: {raw_date!r}")
                    printed_sample = True
                d = parse_date(raw_date)
                if d is None:
                    bad_dates += 1
                    if sample_bad_date is None:
                        sample_bad_date = raw_date
                    continue
                if newest_date is None or d > newest_date:
                    newest_date = d

                store = raw.get(colmap["store_name"], "")
                vendor = raw.get(colmap["vendor_name"], "")
                if matches_keywords(store, vendor, KEYWORDS):
                    row = {field: raw.get(col, "") for field, col in colmap.items()}
                    row["date"] = d.isoformat()
                    row["_d"] = d
                    all_matches.append(row)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    if total == 0:
        raise RuntimeError("The download contained no data rows.")
    if bad_dates == total:
        raise RuntimeError(
            f"Could not parse any dates. Example value: {sample_bad_date!r}. "
            "Update parse_date() in weekly_report.py."
        )

    window_start = dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)
    in_window = [r for r in all_matches if r["_d"] >= window_start]
    in_window.sort(key=lambda r: r["_d"], reverse=True)
    all_matches.sort(key=lambda r: r["_d"], reverse=True)

    print(f"Dataset {dataset_id}: {total:,} rows scanned "
          f"({bad_dates:,} with unreadable dates)")
    print(f"Newest date in dataset: {newest_date}")
    print(f"Total keyword matches in file: {len(all_matches)}")
    print(f"Matches since {window_start.isoformat()}: {len(in_window)}")

    return {
        "rows": in_window,
        "fallback": all_matches[:FALLBACK_ROWS] if not in_window else [],
        "window_start": window_start.isoformat(),
        "newest_date": newest_date.isoformat() if newest_date else "unknown",
        "total_rows": total,
        "total_matches": len(all_matches),
        "dataset_id": dataset_id,
    }


# ---- Report generation ------------------------------------------------------

def esc(v):
    return html.escape(str(v)) if v is not None else ""


NUMERIC_KEYS = {"bottles_sold", "sale_dollars"}


def to_number(v):
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(v)) or 0)
    except ValueError:
        return 0.0


def summarize(rows, key):
    """Totals per store or vendor, grouped on a normalized name so small
    spelling/case/spacing differences merge. Sorted by dollars, highest first."""
    agg = {}
    for r in rows:
        raw_name = (r.get(key) or "").strip() or "(blank)"
        a = agg.setdefault(norm_text(raw_name),
                           {"name": raw_name, "rows": 0, "bottles": 0.0, "dollars": 0.0})
        a["rows"] += 1
        a["bottles"] += to_number(r.get("bottles_sold"))
        a["dollars"] += to_number(r.get("sale_dollars"))
    return sorted(agg.values(), key=lambda a: a["dollars"], reverse=True)


def summary_html(title, items, label):
    body = "".join(
        f"<tr><td>{esc(a['name'])}</td><td class=\"num\">{a['rows']:,}</td>"
        f"<td class=\"num\">{a['bottles']:,.0f}</td>"
        f"<td class=\"num\">${a['dollars']:,.2f}</td></tr>"
        for a in items
    )
    total_rows = sum(a["rows"] for a in items)
    total_bottles = sum(a["bottles"] for a in items)
    total_dollars = sum(a["dollars"] for a in items)
    return (
        f"<div class=\"summary\"><h2>{esc(title)}</h2>"
        f"<div class=\"scroll\"><table><thead><tr><th>{esc(label)}</th><th class=\"num\">Rows</th>"
        f"<th class=\"num\">Bottles</th><th class=\"num\">Sales ($)</th></tr></thead>"
        f"<tbody>{body}</tbody>"
        f"<tfoot><tr><td>Total</td><td class=\"num\">{total_rows:,}</td>"
        f"<td class=\"num\">{total_bottles:,.0f}</td>"
        f"<td class=\"num\">${total_dollars:,.2f}</td></tr></tfoot>"
        f"</table></div></div>"
    )


def table_html(rows, table_id=None):
    """Data table. With table_id, headers are click-to-sort (see PAGE_JS)."""
    header_cells = []
    for key, label in REPORT_COLUMNS:
        kind = "num" if key in NUMERIC_KEYS else "text"
        cls = ' class="num"' if kind == "num" else ""
        header_cells.append(f'<th data-type="{kind}"{cls}>{label}</th>')
    header = "".join(header_cells)

    body_rows = []
    for r in rows:
        cells = []
        for key, _ in REPORT_COLUMNS:
            val = r.get(key)
            if key in NUMERIC_KEYS:
                cells.append(f'<td class="num" data-v="{to_number(val)}">{esc(val)}</td>')
            else:
                cells.append(f"<td>{esc(val)}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    body = "\n".join(body_rows) if body_rows else (
        f'<tr><td colspan="{len(REPORT_COLUMNS)}">No matching records found.</td></tr>'
    )
    id_attr = f' id="{table_id}"' if table_id else ""
    return (f'<div class="scroll"><table{id_attr}><thead><tr>{header}</tr></thead>'
            f"<tbody>\n{body}\n</tbody></table></div>")


PAGE_CSS = """
  body { font-family: Arial, sans-serif; margin: 2rem; color: #222; }
  h1 { font-size: 1.4rem; }
  h2 { font-size: 1.1rem; margin: 1.5rem 0 0.5rem; }
  .meta { color: #555; margin-bottom: 1.5rem; line-height: 1.6; }
  .note { color: #555; }
  .summaries { display: flex; flex-wrap: wrap; gap: 2rem; }
  .summary { flex: 1 1 340px; min-width: 0; }
  .scroll { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border: 1px solid #ccc; padding: 6px 10px; font-size: 0.9rem; text-align: left; }
  th { background: #f2f2f2; }
  tfoot td { font-weight: bold; background: #f2f2f2; }
  tr:nth-child(even) { background: #fafafa; }
  .num { text-align: right; }
  #data th { cursor: pointer; user-select: none; }
  #data th:hover { background: #e6e6e6; }
  #data th[data-sort="asc"]::after { content: " \\25B2"; font-size: 0.7em; }
  #data th[data-sort="desc"]::after { content: " \\25BC"; font-size: 0.7em; }
  .tools { display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; margin: 0.5rem 0 0.75rem; }
  #search { padding: 6px 10px; font-size: 0.95rem; width: 100%; max-width: 320px; border: 1px solid #aaa; border-radius: 4px; }
  #count { color: #555; font-size: 0.9rem; }
"""

PAGE_JS = """
(function () {
  var table = document.getElementById('data');
  if (!table) return;
  var tbody = table.tBodies[0];
  var rows = Array.prototype.slice.call(tbody.rows);
  var search = document.getElementById('search');
  var count = document.getElementById('count');
  var heads = table.tHead.rows[0].cells;
  var dir = {};

  function update() {
    var q = search.value.toLowerCase();
    var shown = 0;
    rows.forEach(function (r) {
      var ok = r.textContent.toLowerCase().indexOf(q) !== -1;
      r.style.display = ok ? '' : 'none';
      if (ok) shown++;
    });
    count.textContent = 'Showing ' + shown + ' of ' + rows.length + ' rows';
  }
  search.addEventListener('input', update);

  Array.prototype.forEach.call(heads, function (th, i) {
    th.addEventListener('click', function () {
      var num = th.getAttribute('data-type') === 'num';
      dir[i] = dir[i] === 'asc' ? 'desc' : 'asc';
      var mult = dir[i] === 'asc' ? 1 : -1;
      rows.sort(function (a, b) {
        var x = a.cells[i], y = b.cells[i];
        var xv = num ? parseFloat(x.getAttribute('data-v')) : x.textContent.toLowerCase();
        var yv = num ? parseFloat(y.getAttribute('data-v')) : y.textContent.toLowerCase();
        return xv < yv ? -mult : (xv > yv ? mult : 0);
      });
      rows.forEach(function (r) { tbody.appendChild(r); });
      Array.prototype.forEach.call(heads, function (h) { h.removeAttribute('data-sort'); });
      th.setAttribute('data-sort', dir[i]);
    });
  });
  update();
})();
"""


def build_html_report(result, keywords):
    today_str = dt.date.today().isoformat()
    keyword_list = ", ".join(esc(k) for k in keywords)
    rows = result["rows"]

    if rows:
        summaries = (
            '<div class="summaries">'
            + summary_html("Totals by store", summarize(rows, "store_name"), "Store")
            + summary_html("Totals by vendor", summarize(rows, "vendor_name"), "Vendor")
            + "</div>"
        )
        main_section = (
            "<h2>All matching records</h2>"
            '<div class="tools"><input id="search" type="search" '
            'placeholder="Search store, vendor, item, city..." aria-label="Search records">'
            '<span id="count"></span></div>'
            '<p class="note">Click a column heading to sort.</p>'
            + table_html(rows, table_id="data")
        )
        fallback_section = ""
    else:
        summaries = ""
        main_section = table_html(rows)
        fallback_section = (
            "<h2>Most recent matching records in the dataset</h2>"
            '<p class="note">Nothing matched inside the report window, so '
            "these are the newest matches the state has published so far.</p>"
            + table_html(result["fallback"])
        )

    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>Iowa Liquor Sales Keyword Report — {today_str}</title>\n"
        f"<style>{PAGE_CSS}</style>\n</head>\n<body>\n"
        "  <h1>Iowa Liquor Sales Keyword Report</h1>\n"
        "  <div class=\"meta\">\n"
        f"    Generated {today_str}<br>\n"
        f"    Keywords: {keyword_list}<br>\n"
        f"    Window: {esc(result['window_start'])} to today &middot; {len(rows):,} matching row(s)<br>\n"
        f"    Newest date in state data: {esc(result['newest_date'])} &middot; "
        f"{result['total_rows']:,} rows scanned &middot; "
        f"{result['total_matches']:,} total keyword matches this year\n"
        "  </div>\n"
        f"  {summaries}\n  {main_section}\n  {fallback_section}\n"
        f"<script>{PAGE_JS}</script>\n</body>\n</html>\n"
    )


def write_csv(rows, path):
    fieldnames = [k for k, _ in REPORT_COLUMNS]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def main():
    try:
        result = fetch_rows()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    today_str = dt.date.today().isoformat()
    html_path = os.path.join(OUTPUT_DIR, f"report-{today_str}.html")
    csv_path = os.path.join(OUTPUT_DIR, f"report-{today_str}.csv")
    latest_path = os.path.join(OUTPUT_DIR, "latest.html")

    report_html = build_html_report(result, KEYWORDS)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(report_html)
    with open(latest_path, "w", encoding="utf-8") as f:
        f.write(report_html)
    write_csv(result["rows"] or result["fallback"], csv_path)

    print(f"Wrote {html_path}")
    print(f"Wrote {csv_path}")
    print(f"Updated {latest_path}")


if __name__ == "__main__":
    main()

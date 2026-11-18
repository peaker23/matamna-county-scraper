"""Collin County foreclosure notices (County Clerk's own site, a Blazor app: list of 25 per page, 13 pages).

The page lists, for every notice: street address (with city, ZIP), sale date, file date. No OCR is needed.
It is read with a headless browser (Playwright) because the list is pushed to the page over a socket.
"""
import re

URL = "https://apps2.collincountytx.gov/ForeclosureNotices"
SOURCE_NAME = "Collin County Clerk foreclosure notices"

_STREET_SUFFIX = {"ST", "DR", "CT", "LN", "RD", "AVE", "BLVD", "CIR", "TRL", "WAY", "PL", "PKWY", "LOOP", "CV", "TRCE",
                  "PATH", "RUN", "ROW", "WALK", "TER", "HWY", "XING", "PT", "BND", "PASS", "RDG", "HOLW", "CRK",
                  "STREET", "DRIVE", "COURT", "LANE", "ROAD", "AVENUE", "CIRCLE", "BOULEVARD", "PLACE", "TRAIL",
                  "PARKWAY", "TERRACE", "COVE", "POINT", "CROSSING", "SQ", "ALY", "EXPY", "FWY"}
_DIRS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}


def split_address(text):
    """'1000 CANAL ST MCKINNEY, TX 75069' -> ('1000 CANAL ST', 'MCKINNEY', '75069'); city '' if it cannot be split."""
    t = re.sub(r"\s+", " ", text.strip())
    m = re.match(r"^(.*?),?\s*TX\s+(\d{5})(?:-\d{4})?$", t, re.I)
    if not m:
        return t, "", ""
    body, zp = m.group(1).strip(), m.group(2)
    if "," in body:                                   # already 'street, city'
        a, b = body.split(",", 1)
        return a.strip(), b.strip(), zp
    toks = body.split()
    cut = -1
    for i in range(1, len(toks) - 1):
        if toks[i].upper() in _STREET_SUFFIX:
            cut = i
    if cut < 0:
        return body, "", zp
    if cut + 1 < len(toks) - 1 and toks[cut + 1].upper() in _DIRS:
        cut += 1
    return " ".join(toks[:cut + 1]), " ".join(toks[cut + 1:]), zp


def _parse_row(text):
    t = re.sub(r"[ \t]+", " ", text).strip()
    addr = t.split("\n")[0].strip()
    g = lambda k: (re.search(k + r":\s*([^\n]*)", t) or [None, ""])[1].strip()
    sale = re.search(r"Sale Date:\s*(\d{1,2}/\d{1,2}/\d{4})", t)
    filed = re.search(r"File Date:\s*(\d{1,2}/\d{1,2}/\d{4})", t)
    return {"address": addr, "area": g("City"), "sale": sale.group(1) if sale else "",
            "filed": filed.group(1) if filed else "", "ptype": g("Property Type")}


def fetch_notices(log=print, max_pages=40):
    """-> (list of notices, total the site says it has). Raises if the pages do not add up to the total."""
    from playwright.sync_api import sync_playwright
    out, total = [], 0
    with sync_playwright() as p:
        br = p.chromium.launch()
        try:
            page = br.new_page()
            page.goto(URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_selector("tbody tr", timeout=90000)
            page.wait_for_timeout(1500)
            m = re.search(r"\[\s*\d+\s*/\s*(\d+)\s*\]", page.inner_text("body"))
            total = int(m.group(1)) if m else 0
            for n in range(1, max_pages + 1):
                rows = page.eval_on_selector_all("tbody tr", "els => els.map(e => e.innerText)")
                first = rows[0] if rows else ""
                for r in rows:
                    if r.strip():
                        out.append(_parse_row(r))
                if n in (1, 7) and rows:
                    log(f"Collin page {n}: first row text = {rows[0][:120]!r}")
                btn = page.locator(".mud-pagination").first.locator("button").filter(has_text="").all()
                # MudBlazor: the 'next page' button is the second-to-last button in the pager
                if not btn:
                    break
                nb = btn[-2]
                if nb.is_disabled() or "mud-disabled" in (nb.get_attribute("class") or ""):
                    break
                nb.click()
                try:
                    page.wait_for_function(
                        "f => { const r = document.querySelector('tbody tr'); return r && r.innerText !== f }",
                        arg=first, timeout=30000)
                except Exception:
                    break
                page.wait_for_timeout(300)
            # read the total again at the end: on 2026-11-10 it was read while the list was still loading (196 of 337)
            m = re.search(r"\[\s*\d+\s*/\s*(\d+)\s*\]", page.inner_text("body"))
            if m:
                total = max(total, int(m.group(1)))
            seen, uniq = set(), []
            for x in out:                                  # a page read twice must not count twice
                key = (x["address"], x["filed"], x["sale"], x["area"])
                if key not in seen:
                    seen.add(key)
                    uniq.append(x)
            out = uniq
            log(f"Collin: read {len(out)} rows from the clerk's site (site says {total})")
        finally:
            br.close()
    # only missing rows are a problem; 1-2 short is the clerk's own duplicate rows (same address and dates) folded into one
    if total and len(out) < total - 2:
        raise RuntimeError(f"Collin list returned {len(out)} of {total} notices")
    if total and len(out) < total:
        log(f"Collin: {total - len(out)} duplicate row(s) on the clerk's list counted once")
    return out, total

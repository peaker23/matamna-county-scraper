"""Probate leads for Collin / Dallas / Denton / Tarrant.

Sources (official):
  A. County Clerk official public records (publicsearch.us, all four counties): Affidavit of Heirship, probate records,
     wills recorded in the real-property records. Index gives the decedent name, recorded date, instrument number and,
     usually, a legal description (lot / block / subdivision, city).
  B. Probate court filings, Collin (apps2.collincountytx.gov Judicial Online Search) and Denton (Odyssey public access):
     estate cases with case number, decedent name, file date and case type. No address.
  (Dallas and Tarrant court portals require a human-verification step, so they are not read.)

Address rules (owner's choice):
  * legal description on the index -> exactly one CAD parcel with that lot / block / subdivision -> added
  * otherwise the decedent's name -> exactly one residential CAD parcel whose owner is that same person (same last,
    first and middle name or initial) -> added as "Probate — candidate address" with a note
  * anything else -> Scrape Queue with the reason. Never merged with another row.

PROBATE_MODE=count (default for manual runs) parses and prints; write adds rows.
"""
import os
import re
import sys
import time
import traceback
from collections import Counter
from datetime import datetime, timedelta

import requests

from . import cad

MODE = (os.environ.get("PROBATE_MODE") or "count").lower()
DAYS = int(os.environ.get("PROBATE_DAYS") or 14)
ONLY = [c.strip() for c in (os.environ.get("PROBATE_COUNTIES") or "").split(",") if c.strip()]
COUNTIES = ["Collin", "Denton", "Dallas", "Tarrant"]
SOURCE_NAME = "Probate (County Clerk records / probate court)"
ID_FIELD = "Source record ID"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}

# publicsearch document types (codes differ by county)
DOC_TYPES = {"Tarrant": ["AOH", "PROB", "WILL"], "Dallas": ["AH", "AHC", "PB", "PROB", "WILL", "WY", "WIL"],
             "Denton": ["AH", "AHC", "PB", "WIL"], "Collin": ["AH", "PROB", "WILL"]}
# CAD layer fields for owner-name and legal-description searches
OWNERF = {"Tarrant": "OWNER_NAME", "Dallas": "OWNERNME1", "Denton": "owner_name", "Collin": "ownerName"}
LEGALF = {"Tarrant": "LEGAL_1", "Denton": "legal_desc", "Collin": "legalDescription"}
STATEF = {"Tarrant": "STATE", "Denton": "state_cd"}            # state category code, where the layer has it
CITYZ = {"Tarrant": ("CITY", "ZIPCODE"), "Denton": ("situs_city", "situs_zip"), "Collin": ("situsCity", "situsZip"),
         "Dallas": ("", "")}
COURT_URL = {"Collin": "https://apps2.collincountytx.gov/JudicialOnlineSearch2/case",
             "Denton": "https://justice1.dentoncounty.gov/PublicAccess/default.aspx"}


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- names
_STOP = {"ESTATE", "EST", "OF", "THE", "DECD", "DECEASED", "DEC", "HEIRS", "HEIR", "AKA", "A/K/A", "IN", "MATTER",
         "AND", "&", "ET", "AL", "ETAL", "UNKNOWN", "ALL"}
_SUFFIX = {"JR", "SR", "II", "III", "IV"}


def name_from_index(grantors):
    """publicsearch party list -> decedent (last, first, middle). Index order 'NYSTROM RACHEL RENE ESTATE' /
    'JUAREZ PETE DECD AKA'; natural order 'ESTATE OF ARACELY G URDA DECEASED'. None if not clear."""
    cands = []
    for g in grantors or []:
        u = re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 &/]", " ", str(g).upper())).strip()
        m = re.match(r"^(?:THE\s+)?ESTATE\s+OF\s+(.+)$", u)
        if m:                                              # natural order after 'ESTATE OF'
            body = re.split(r"\b(?:DECEASED|DECD|DEC|AKA|A/K/A|ET AL|ETAL)\b", m.group(1))[0]
            w = [x for x in body.split() if x not in _STOP and x not in _SUFFIX]
            if len(w) >= 2:
                if len(w) > 2 and w[-2] in ("ST", "DE", "LA", "DEL", "VAN", "VON", "DA", "DI", "LE", "MC", "MAC"):
                    w = w[:-2] + [w[-2] + " " + w[-1]]
                cands.append((w[-1], w[0], w[1] if len(w) > 2 else ""))
            continue
        if re.search(r"\b(ESTATE|EST|DECD|DECEASED)\b", u):
            words = [w for w in re.split(r"\b(?:ESTATE|EST|DECD|DECEASED)\b", u)[0].split() if w]
            words = [w for w in words if w not in _STOP and w not in _SUFFIX]
            if len(words) >= 2:
                cands.append((words[0], words[1], words[2] if len(words) > 2 else ""))
    uniq = {c for c in cands}
    return next(iter(uniq)) if len(uniq) == 1 else None


def decedent(d):
    """Decedent from grantor first, then grantee / other parties (only names marked ESTATE / DECD)."""
    for k in ("grantor", "grantee"):
        n = name_from_index(d.get(k))
        if n:
            return n
    extra = []
    for k in ("parties", "additionalParties"):
        for x in d.get(k) or []:
            extra.append(x.get("name") if isinstance(x, dict) else x)
    return name_from_index([x for x in extra if x])


COLLIN_CITIES = ("ROYSE CITY", "ALLEN", "ANNA", "BLUE RIDGE", "CARROLLTON", "CELINA", "DALLAS", "FAIRVIEW",
                 "FARMERSVILLE", "FRISCO", "GARLAND", "JOSEPHINE", "LAVON", "LEWISVILLE", "LOWRY CROSSING", "LUCAS",
                 "MCKINNEY", "MELISSA", "MURPHY", "NEVADA", "PARKER", "PLANO", "PRINCETON", "PROSPER", "RICHARDSON",
                 "SACHSE", "ST PAUL", "WESTON", "WYLIE")


def split_city(legal, city):
    """'LOT 49 BLOCK Q MIDWAY CROSSING DALLAS' -> ('LOT 49 BLOCK Q MIDWAY CROSSING', 'DALLAS')."""
    u = legal.upper().rstrip(" ,;")
    for c in COLLIN_CITIES:
        if u.endswith(" " + c) and len(u) > len(c) + 12:
            return u[: -len(c)].rstrip(" ,;"), city or c
    return legal, city


def name_from_style(style):
    """'In the Estate Of Patricia B. Assenheimer' / 'The Estate of James E Potts a/k/a ..., Deceased' ->
    (ASSENHEIMER, PATRICIA, B). None for guardianships or unclear styles."""
    s = re.sub(r"\s+", " ", str(style or "")).strip()
    if re.search(r"guardianship|incapacitated|minor|trust\b|marriage", s, re.I):
        return None
    m = re.search(r"estate\s+of\s+(.+)", s, re.I)
    if not m:
        return None
    n = re.split(r",|\b(?:a/k/a|aka|deceased|decd)\b", m.group(1), flags=re.I)[0]
    w = [x for x in re.sub(r"[^A-Za-z ]", " ", n).upper().split() if x not in _SUFFIX]
    if len(w) < 2:
        return None
    if len(w) > 2 and w[-2] in ("ST", "DE", "LA", "DEL", "VAN", "VON", "DA", "DI", "LE", "MC", "MAC"):
        w = w[:-2] + [w[-2] + " " + w[-1]]           # 'Paul Timothy St Clair' -> last name 'ST CLAIR'
    first, last = w[0], w[-1]
    mid = w[1] if len(w) > 2 else ""
    return (last, first, mid)


def name_text(n):
    return " ".join(x for x in (n[1].title(), n[2].title(), n[0].title()) if x)


# ---------------------------------------------------------------- CAD
def _q(county, where, fields):
    url = cad.LAYERS[county][0]
    r = requests.post(url, data={"where": where, "outFields": ",".join(f for f in fields if f),
                                 "returnGeometry": "false", "f": "json"}, headers=UA, timeout=60).json()
    if "error" in r:
        raise RuntimeError(str(r["error"])[:150])
    return [f.get("attributes", {}) for f in r.get("features", [])]


def _parcel(county, a):
    sf, idf = cad.LAYERS[county][1], cad.LAYERS[county][2]
    cf, zf = CITYZ[county]
    situs = str(a.get(sf) or "").split(",")[0].strip()
    return {"id": str(a.get(idf)), "situs": situs, "city": str(a.get(cf) or "").strip() if cf else "",
            "zip": str(a.get(zf) or "").strip()[:5] if zf else "", "state": str(a.get(STATEF.get(county, "")) or "")}


def _residential(county, p):
    st = p.get("state", "").upper()
    if st:
        return st[:1] in ("A", "B", "E", "M")
    return p["situs"][:1].isdigit()            # no category on the layer (Collin / Dallas): needs a street address


def by_owner(county, n):
    """Exactly one residential parcel owned by this same person -> parcel dict, else (None, why)."""
    if county not in OWNERF or not n:
        return None, "no owner search"
    of = OWNERF[county]
    last, first, mid = n
    like = f"{last} {first}".replace("'", "''")
    fields = [of, cad.LAYERS[county][1], cad.LAYERS[county][2], *CITYZ[county], STATEF.get(county, "")]
    try:
        rows = _q(county, f"UPPER({of}) LIKE '{like}%'", fields)
    except Exception as e:
        return None, f"CAD owner search error {e}"
    want = [f"{last} {first} {mid}".strip(), f"{last} {first} {mid[:1]}".strip()] if mid else [f"{last} {first}"]
    hits = {}
    for a in rows:
        o = re.sub(r"[^A-Z0-9 &]", " ", str(a.get(of) or "").upper())
        o = re.sub(r"\s+", " ", re.sub(r"\b(EST|ESTATE OF|ESTATE|DECD|DECEASED)\b", " ", o)).strip()
        head = re.split(r"\s+(?:&|AND)\s+", o)[0].strip()
        head = " ".join(w for w in head.split() if w not in _SUFFIX)
        if head in want:
            p = _parcel(county, a)
            if _residential(county, p):
                hits[p["id"]] = p
    if len(hits) == 1:
        return next(iter(hits.values())), ""
    return None, f"{len(hits)} residential CAD parcels owned by {name_text(n)}"


def by_legal(county, legal, city):
    if not legal:
        return None, "no legal description"
    if county in ("Tarrant", "Denton"):
        v = cad.verify_legal(county, legal, city)
        if v.get("ok"):
            return {"id": v["id"], "situs": v["situs"], "city": v.get("city") or city, "zip": v.get("zip", "")}, ""
        return None, str(v.get("why"))
    if county == "Collin":
        p = cad.parse_legal(legal, city)
        if not p:
            return None, "legal description not readable"
        lf = LEGALF["Collin"]
        key = max(p["words"], key=len).replace("'", "''")
        try:
            rows = _q(county, f"UPPER({lf}) LIKE '%{key}%' AND UPPER({lf}) LIKE '%LOT {p['lot']}%'",
                      [lf, cad.LAYERS[county][1], cad.LAYERS[county][2], *CITYZ[county]])
        except Exception as e:
            return None, f"CAD error {e}"
        hits = {}
        for a in rows:
            lg = " " + re.sub(r"[^A-Z0-9 ]", " ", str(a.get(lf) or "").upper()) + " "
            lg = re.sub(r"\b(?:BLK|BK)\b", "BLOCK", lg)
            if not re.search(r"\bLOT\s+" + re.escape(p["lot"]) + r"\b", lg):
                continue
            if p["block"] and not re.search(r"\bBLOCK\s+" + re.escape(p["block"]) + r"\b", lg):
                continue
            if all(f" {w} " in lg for w in p["words"]):
                q = _parcel(county, a)
                if q["situs"][:1].isdigit():
                    hits[q["id"]] = q
        if len(hits) == 1:
            return next(iter(hits.values())), ""
        return None, f"{len(hits)} CAD parcels match the legal description"
    return None, "no CAD legal-description search for this county"


# ---------------------------------------------------------------- source A: County Clerk records
def index_legal(d):
    """'FORT WORTH, Subdivision: SOUTH RIDGE, Lot: 12, Block: 14' / Dallas 'Subdivision- Town: GARLAND Name: X Lot: 12
    Block: 2' -> ('LOT 12 BLOCK 14 SOUTH RIDGE', city)."""
    parts = [str(x) for x in (d.get("propertyAddress") or [])] + [str(x) for x in (d.get("legalDescription") or [])
                                                                    if isinstance(d.get("legalDescription"), list)]
    txt = " ; ".join(parts)
    city = ""
    m = re.search(r"Town:\s*([A-Z .'-]+?)\s+Name:", txt, re.I) or re.match(r"\s*([A-Z .'-]+?),\s*Subdivision", txt, re.I)
    if m:
        city = m.group(1).strip()
    sub = re.search(r"(?:Subdivision:|Name:)\s*([^,;]+?)(?:,|\s+Lot:|;|$)", txt, re.I)
    lot = re.search(r"Lot:\s*([0-9A-Z-]+)", txt, re.I)
    blk = re.search(r"Block:\s*([0-9A-Z-]+)", txt, re.I)
    if not (sub and lot):
        return "", city, txt
    legal = f"LOT {lot.group(1)}" + (f" BLOCK {blk.group(1)}" if blk else "") + f" {sub.group(1).strip()}"
    return legal, city, txt


def clerk_docs(county, d0, d1):
    from .publicsearch import PublicSearch
    ps = PublicSearch(county.lower())
    ps.connect()
    out, seen = [], set()
    try:
        day = d0
        while day <= d1:
            q = {"limit": "250", "offset": "0", "department": "RP", "docTypes": ",".join(DOC_TYPES[county]),
                 "recordedDateRange": f"{day:%Y%m%d},{day:%Y%m%d}", "searchType": "advancedSearch"}
            msg = ps._ask("@kofile/FETCH_DOCUMENTS/v4", {"query": q, "workspaceID": "search"},
                          r"FETCH_DOCUMENTS_FULFILLED")
            data = msg["payload"]["data"]
            order = data.get("byOrder") or list(data.get("byHash", {}).keys())
            for k in order:
                doc = data["byHash"].get(str(k)) or data["byHash"].get(k)
                if doc and str(doc.get("id")) not in seen:
                    seen.add(str(doc.get("id")))
                    doc["_url"] = ps.doc_url(str(doc.get("id")))
                    out.append(doc)
            day += timedelta(days=1)
            time.sleep(0.2)
    finally:
        ps.close()
    return out


def clerk_records(county, d0, d1):
    docs = clerk_docs(county, d0, d1)
    log(f"{county}: {len(docs)} County Clerk probate documents {d0}..{d1}; types "
        f"{Counter(str(d.get('docType') or d.get('docTypeCode')) for d in docs).most_common(6)}")
    if docs and MODE == "count":
        import json as _j
        for d in docs[:2]:
            show = {k: d.get(k) for k in ("grantor", "grantee", "parties", "additionalParties", "legals", "lot", "block",
                                          "propAddress", "propertyAddress", "legalDescription", "township")}
            log(f"   DEBUG {county} {d.get('instrumentNumber')}: {_j.dumps(show, default=str)[:900]}")
            log(f"   DEBUG ocrText len {len(str(d.get('ocrText') or ''))}: {str(d.get('ocrText') or '')[:400]!r}")
    recs = []
    for d in docs:
        inst = str(d.get("instrumentNumber") or d.get("docNumber") or d.get("id"))
        dtype = str(d.get("docType") or d.get("docTypeCode") or "").upper()
        n = decedent(d)
        legal, city, raw = index_legal(d)
        if county == "Collin" and legal:
            legal, city = split_city(legal, city)
        filed = ""
        try:
            m, dd, y = [int(x) for x in str(d.get("recordedDate")).split("/")]
            filed = f"{y:04d}-{m:02d}-{dd:02d}"
        except Exception:
            pass
        label = ("Probate — affidavit of heirship" if "HEIR" in dtype else
                 "Probate — will recorded" if "WILL" in dtype else "Probate — probate record recorded")
        recs.append({"rid": f"probate:{county.lower()}:{inst}", "inst": inst, "type": label, "dtype": dtype,
                     "name": n, "names": "; ".join(str(x) for x in d.get("grantor") or []),
                     "legal": legal, "city": city, "raw": raw, "filed": filed, "url": d["_url"],
                     "src": f"{county} County Clerk official records ({dtype.title()}, instrument {inst})"})
    return recs


# ---------------------------------------------------------------- source B: probate court (Collin, Denton)
COURT_TYPE = [(r"independent admin", "Probate — independent administration application"),
              (r"letters testamentary|testamentary", "Probate — letters application"),
              (r"letters of admin|dependent admin|administration", "Probate — estate administration"),
              (r"muniment", "Probate — muniment application"), (r"heirship", "Probate — heirship application"),
              (r"small estate", "Probate — small estate affidavit")]


def court_label(t):
    for rx, lab in COURT_TYPE:
        if re.search(rx, t or "", re.I):
            return lab
    return "Probate — estate case"


def denton_cases(d0, d1):
    from playwright.sync_api import sync_playwright
    out = []
    with sync_playwright() as p:
        br = p.chromium.launch()
        try:
            pg = br.new_page()
            pg.goto(COURT_URL["Denton"], wait_until="domcontentloaded", timeout=90000)
            pg.select_option("select", label="Probate Court")
            pg.click("text=JP & County Court: Civil, Family & Probate Case Records")
            pg.wait_for_selector("#DateFiled", timeout=60000)
            pg.check("#DateFiled")
            pg.fill("#DateFiledOnAfter", f"{d0:%m/%d/%Y}")
            pg.fill("#DateFiledOnBefore", f"{d1:%m/%d/%Y}")
            pg.click("input[type=submit][value*='Search' i], #SearchSubmit")
            pg.wait_for_load_state("domcontentloaded", timeout=90000)
            pg.wait_for_timeout(1500)
            body = pg.inner_text("body")
            if re.search(r"human|verify you are|security check", body, re.I):
                raise RuntimeError("Denton portal asked for human verification; not read")
            rows = pg.eval_on_selector_all("tr", "els => els.map(e => [...e.querySelectorAll('td')].map(t => t.innerText)"
                                                 ".concat([e.querySelector('a') ? e.querySelector('a').href : '']))")
            for r in rows:
                cells = [re.sub(r"\s+", " ", c).strip() for c in r]
                if len(cells) >= 4 and re.match(r"^PR-\d{4}-\d+", cells[0]):
                    m = re.search(r"(\d{2}/\d{2}/\d{4})", cells[2])
                    out.append({"case": cells[0].split()[0], "style": cells[1], "filed": m.group(1) if m else "",
                                "ctype": cells[3], "url": cells[-1] or COURT_URL["Denton"]})
        finally:
            br.close()
    return out


def collin_cases(d0, d1, max_pages=30):
    from playwright.sync_api import sync_playwright
    out = []
    with sync_playwright() as p:
        br = p.chromium.launch()
        try:
            pg = br.new_page()
            pg.goto(COURT_URL["Collin"], wait_until="domcontentloaded", timeout=90000)
            pg.wait_for_selector("text=Show Advanced Filters", timeout=90000)
            pg.click("text=Show Advanced Filters")
            pg.wait_for_timeout(1500)
            for n in ("Probate", "Criminal", "Civil", "Family"):          # leave only Probate ticked
                box = pg.locator(".mud-grid-item", has_text=n).locator("input[type=checkbox]").first
                want = n == "Probate"
                if box.is_checked() != want:
                    box.click()
                    pg.wait_for_timeout(2500)
            for page_no in range(max_pages):
                rows = pg.eval_on_selector_all("tbody tr", "els => els.map(e => e.innerText)")
                first = rows[0] if rows else ""
                older = False
                for r in rows:
                    parts = [x.strip() for x in r.split("\n") if x.strip()]
                    if len(parts) < 3:
                        continue
                    m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", " ".join(parts))
                    filed = datetime.strptime(m.group(1), "%m/%d/%Y").date() if m else None
                    if filed and filed < d0:
                        older = True
                        continue
                    if filed and filed > d1:
                        continue
                    if re.match(r"^PB\d-\d+-\d{4}", parts[0]):
                        out.append({"case": parts[0], "style": parts[1], "filed": m.group(1) if m else "",
                                    "ctype": "", "url": COURT_URL["Collin"]})
                if older or not rows:
                    break
                nxt = pg.locator(".mud-table-pagination-actions button, .mud-pagination button").all()
                btn = [b for b in nxt if (b.get_attribute("aria-label") or "").lower().startswith("next")]
                b = btn[0] if btn else (nxt[-2] if len(nxt) >= 2 else None)
                if not b or b.is_disabled():
                    break
                b.click()
                try:
                    pg.wait_for_function("f => { const r = document.querySelector('tbody tr'); return r && r.innerText !== f }",
                                         arg=first, timeout=30000)
                except Exception:
                    break
        finally:
            br.close()
    return out


def court_records(county, d0, d1):
    cases = denton_cases(d0, d1) if county == "Denton" else collin_cases(d0, d1)
    log(f"{county}: {len(cases)} probate court cases filed {d0}..{d1}")
    recs = []
    for c in cases:
        n = name_from_style(c["style"])
        if not n:
            continue                                    # guardianship / not a decedent's estate
        try:
            m, dd, y = [int(x) for x in c["filed"].split("/")]
            filed = f"{y:04d}-{m:02d}-{dd:02d}"
        except Exception:
            filed = ""
        recs.append({"rid": f"probatecase:{county.lower()}:{c['case']}", "inst": c["case"], "type": court_label(c["ctype"]),
                     "dtype": c["ctype"], "name": n, "names": c["style"], "legal": "", "city": "", "raw": c["style"],
                     "filed": filed, "url": c["url"], "case": c["case"],
                     "src": f"{county} County probate court case {c['case']}" + (f" ({c['ctype']})" if c["ctype"] else "")})
    return recs


# ---------------------------------------------------------------- resolve + write
def resolve(county, rec):
    """-> (row fields or None, reason held)."""
    p, why1 = by_legal(county, rec["legal"], rec["city"]) if rec["legal"] else (None, "no legal description")
    how = "legal description on the index matched one CAD parcel"
    rtype = rec["type"]
    if not p:
        p, why2 = by_owner(county, rec["name"]) if rec["name"] else (None, "decedent name not clear")
        how = f"decedent name ({name_text(rec['name'])}) matched the owner of one residential CAD parcel" if p else ""
        if p:
            rtype = "Probate — candidate address"
        else:
            return None, f"{why1}; {why2}"
    city = p.get("city") or rec.get("city") or ""
    if not city:
        return None, f"CAD parcel {p['id']} ({p['situs']}) found but its city is unknown"
    addr = cad.full_address(p["situs"], city.title() if city.isupper() else city, p.get("zip", ""))
    return {"addr": addr, "cad": p["id"], "type": rtype, "how": how}, ""


def run(county, d0, d1):
    recs = clerk_records(county, d0, d1)
    if county in ("Collin", "Denton"):
        try:
            recs += court_records(county, d0, d1)
        except Exception as e:
            traceback.print_exc()
            log(f"{county}: probate court list not read: {type(e).__name__}: {str(e)[:200]}")
    return recs


def write(sh, county, recs, run_id):
    from .sheet import now_str, today
    ws, head, vals = sh.snapshot(county)
    for h in ("Record type / status", "Actual street address", ID_FIELD, "Original notes"):
        if h not in head:
            raise RuntimeError(f"{county} tab has no '{h}' column")
    known = sh.known_ids(head, vals, [ID_FIELD, "Case number", "Foreclosure document ID"])
    c_a, c_t = head.index("Actual street address"), head.index("Record type / status")
    have_addr = {}
    for i, r in enumerate(vals[1:], start=2):
        if c_t < len(r) and "PROBATE" in r[c_t].upper() and c_a < len(r) and r[c_a].strip():
            k = cad._street_key(r[c_a])
            if k[0]:
                have_addr.setdefault(k, i)
    add, held, detail, skip = [], [], [], 0
    at = now_str()
    from .sheet import iso_day
    since = sh.county_since(county)                # turned back on in the web app: nothing filed before that day
    for rec in recs:
        if since and iso_day(rec.get("filed")) and iso_day(rec.get("filed")) < since:
            continue
        if rec["rid"] in known or rec["inst"] in known:
            skip += 1
            continue
        res, why = resolve(county, rec)
        if res:
            k = cad._street_key(res["addr"])
            if k[0] and k in have_addr:
                where = f"probate row {have_addr[k]}" if have_addr[k] else "a probate row added earlier in this run"
                res, why = None, (f"same street address as {where} (another record); not merged - "
                                  f"check whether it is the same estate")
        if not res:
            held.append({"First seen": today(), "Last checked": at, "County": county, "Record type": rec["type"],
                         "Official ID": rec["rid"], "Source record ID": rec["inst"], "Source URL": rec["url"],
                         "Filed date": rec["filed"], "Name / grantor": rec["names"],
                         "Index address / legal": rec["raw"][:300], "Reason held": why[:250], "Resolved": ""})
            detail.append([at, run_id, "probate", county, rec["rid"], rec["inst"], "held for review", "", rec["filed"], "",
                           "", rec["names"], why[:250], rec["url"]])
            continue
        k = cad._street_key(res["addr"])
        if k[0]:
            have_addr[k] = 0
        note = (f"Auto-added {today()} from {rec['src']}; recorded/filed {rec['filed']}. Decedent: "
                f"{name_text(rec['name']) if rec['name'] else rec['names']}. Address: {res['how']} ({res['cad']}).")
        if res["type"] == "Probate — candidate address":
            note += " Matched by name only: confirm the decedent owned this property."
        row = {"County": county, "Record type / status": res["type"], "Actual street address": res["addr"],
               "Name / grantor": rec["names"], "Name source / qualification": "Decedent / estate named in the record",
               "Filed date": rec["filed"], ID_FIELD: rec["rid"], "Case number": rec.get("case", ""),
               "CAD / property ID": res["cad"], "Source added on": today(),
               "Property address / legal description": rec["legal"] or rec["raw"][:200],
               "Public records source address": rec["raw"][:200], "Public records source URL": rec["url"],
               "Probate action (source)": rec["dtype"], "Probate action date": rec["filed"], "Original notes": note}
        add.append({k2: v for k2, v in row.items() if k2 in head})
        detail.append([at, run_id, "probate", county, rec["rid"], rec["inst"], "added", res["addr"], rec["filed"], "", "",
                       rec["names"], res["how"], rec["url"]])
    written, bad = sh.append(county, add, ID_FIELD) if add else (0, [])
    sh.queue(held)
    sh.resolve_queue({r[ID_FIELD] for r in add if r[ID_FIELD] not in bad}, f"added to {county} tab {today()}")
    sh.detail(detail)
    return {"found": len(recs), "new": written, "review": len(held), "skip": skip, "bad": bad}


def main():
    d1 = datetime.now().date()
    d0 = d1 - timedelta(days=DAYS)
    log(f"Probate run, mode={MODE}, {d0}..{d1}")
    sh = None
    if MODE == "write":
        from .sheet import Sheet
        sh = Sheet()
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    failed, summary = False, {}
    for county in ONLY or COUNTIES:
        t0 = time.time()
        try:
            if sh is not None:
                sh.county_rules(refresh=True)          # read again before each county: a pause during the run counts
            if sh is not None and not sh.county_on(county):
                from .sheet import now_str
                summary[county] = "paused in the web app"
                sh.log([[now_str(), county, SOURCE_NAME, "paused", "", "", "", "", "", "", "", "",
                         "paused in the web app (Fetch log > Scrape counties); not scraped"]])
                log(f"{county}: paused in the web app, skipped")
                continue
            recs = run(county, d0, d1)
            if MODE == "count":
                res = Counter()
                for rec in recs[:60]:
                    r, why = resolve(county, rec)
                    res["added" if r else "held"] += 1
                    if len(res) and sum(res.values()) <= 6:
                        log(f"   {rec['rid']} {rec['type']} | {rec['names'][:60]} | legal '{rec['legal']}' city "
                            f"'{rec['city']}' -> {r['addr'] + ' [' + r['type'] + ']' if r else 'HELD: ' + why}")
                summary[county] = {"records": len(recs), "first 60 resolved": dict(res)}
            else:
                s = write(sh, county, recs, run_id)
                summary[county] = s
                from .sheet import now_str
                sh.log([[now_str(), county, SOURCE_NAME, "complete" if not s["bad"] else "partial",
                         f"{d0}..{d1}", s["found"], s["new"], s["review"], s["skip"], s["found"], "", "",
                         f"probate run {run_id} · {int(time.time() - t0)}s"]])
        except Exception as e:
            traceback.print_exc()
            failed = True
            summary[county] = f"FAILED: {type(e).__name__}: {str(e)[:200]}"
        log(f"{county}: {summary[county]} ({int(time.time() - t0)}s)")
    log("SUMMARY " + str(summary))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

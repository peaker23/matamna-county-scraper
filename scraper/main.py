"""Daily county scrape — phase 1: foreclosure notices (Collin, Tarrant, Dallas, Denton County Clerk).

Rules (from the owner's checklist):
  * one record = county + official document ID; never merged with another notice
  * a row is added only with a street address stated in the official notice, or one unique CAD parcel match;
    otherwise it goes to "Scrape Queue" with the reason, and is re-checked next run
  * Filed date = clerk recorded date; Sale date = exact date in the notice, or first Tuesday of the
    official sale month (noted as derived); nothing is guessed
  * partial / failed checks are logged as such, never as "no new records"
  * every notice added / held / errored gets one row in "Scrape Detail" (with the reason); notices already in the
    sheet are only counted in "Scrape Log"
"""
import os
import re
import signal
import sys
import time
import traceback
from datetime import datetime, timedelta

from . import cad, collin, notice
from .publicsearch import PublicSearch
from .sheet import CHI, Sheet, now_str, today

SOURCE = "Foreclosure notices (County Clerk)"
COUNTIES = {"Collin": "collin", "Tarrant": "tarrant", "Dallas": "dallas", "Denton": "denton"}   # Collin first: fast, no OCR
RUN_SLOTS = [(1, 0), (7, 0), (10, 0), (13, 0), (16, 0), (19, 0)]   # Chicago time: 1am (full catch-up run) + 7am, 10am, 1pm, 4pm, 7pm
FIRST_RUN_DAYS = 45          # first ever run looks back this far
OVERLAP_DAYS = 7             # later runs re-read the last week (late indexing)
BUDGET_MIN = float(os.environ.get("BUDGET_MIN") or 40)   # stop OCR before the job timeout; rest goes next run

SKIP_DETAIL = {"already in sheet", "held (re-checked once a day)", "before resume date"}   # results not written to Scrape Detail

T0 = time.time()
RUN_ID = os.environ.get("GITHUB_RUN_ID", "local")
RUN_URL = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY', '')}"
           f"/actions/runs/{RUN_ID}") if os.environ.get("GITHUB_RUN_ID") else ""
TRIGGER = {"schedule": "scheduled", "workflow_dispatch": "manual (Fetch now / GitHub)"}.get(
    os.environ.get("GITHUB_EVENT_NAME", ""), os.environ.get("GITHUB_EVENT_NAME", "local"))


def log(*a):
    print(*a, flush=True)


def left_s():
    return BUDGET_MIN * 60 - (time.time() - T0)


class NoticeTimeout(Exception):
    pass


def _alarm(signum, frame):
    raise NoticeTimeout("notice took longer than 180 s (site or OCR stalled); skipped this run")


signal.signal(signal.SIGALRM, _alarm)


def iso(mdy):
    """'10/1/2026' -> '2026-10-01'."""
    try:
        m, d, y = [int(x) for x in str(mdy).split("/")]
        return f"{y:04d}-{m:02d}-{d:02d}"
    except Exception:
        return ""


def run_county(sh, county, slug, days_back, deadline, detail):
    stats = {"found": 0, "new": 0, "review": 0, "skip": 0, "filed": 0, "sale_exact": 0, "sale_month": 0, "err": 0,
             "left": 0}
    last = sh.last_ok(county, SOURCE)
    end = datetime.now(CHI).date()
    start = (datetime.strptime(last, "%Y-%m-%d").date() - timedelta(days=OVERLAP_DAYS)) if last \
        else end - timedelta(days=FIRST_RUN_DAYS)
    if days_back:
        start = min(start, end - timedelta(days=days_back))
    oq = sh.queue_oldest(county)                   # held notices are re-checked until resolved (max 90 days back)
    if oq:
        start = min(start, max(datetime.strptime(oq, "%Y-%m-%d").date(), end - timedelta(days=90)))
    since = sh.county_since(county)                # turned back on in the web app: nothing filed before that day
    if since:
        start = max(start, datetime.strptime(since, "%Y-%m-%d").date())
    window = f"{start:%Y-%m-%d}..{end:%Y-%m-%d}"
    at = now_str()

    def det(doc_id, inst, result, addr="", filed="", sale="", basis="", name="", why="", url=""):
        detail.append([at, RUN_ID, TRIGGER, county, doc_id, inst, result, addr, filed, sale, basis, name, why, url])

    removed = sh.dedupe_auto(county, "Foreclosure document ID")
    moved = sh.quarantine_auto_same_address(county, cad._street_key, now_str(), today())
    if moved:
        log(f"{county}: moved {moved} auto-added rows whose address was already in the sheet to Scrape Queue")
    if removed:
        log(f"{county}: removed {removed} duplicate auto-added rows")
    ws, head, vals = sh.snapshot(county)
    known = sh.known_ids(head, vals, ["Foreclosure document ID", "Source record ID"])
    addr_keys = sh.address_keys(head, vals, cad._street_key)

    ps = PublicSearch(slug)
    ps.connect()
    try:
        docs, total = ps.foreclosures(f"{start:%Y%m%d}", f"{end:%Y%m%d}")
        stats["found"] = len(docs)
        log(f"{county}: {len(docs)} notices in {window} (index says {total})")
        if total and len(docs) < total:
            raise RuntimeError(f"index returned {len(docs)} of {total}")
        cities = {str(d.get("id") or d.get("docId")): str(((d.get("propAddress") or [{}])[0] or {}).get("city") or "").strip()
                  for d in docs}
        fixed = sh.fix_auto_addresses(county, lambda a: cad.verify_address(county, a), cities)
        if fixed:
            log(f"{county}: added the city to {fixed} auto-added addresses")
            ws, head, vals = sh.snapshot(county)
            addr_keys = sh.address_keys(head, vals, cad._street_key)
        # fill blank fields of rows the scraper added earlier (never overwrites a filled cell)
        facts = {}
        for d in docs:
            mt, y, mo = notice.sale_month_from_index(d)
            ex = notice.sale_date_from_index(d)
            f = {"Filed date": iso(d.get("recordedDate")), "Sale month (source)": mt or "",
                 "Source record ID": str(d.get("instrumentNumber") or d.get("docNumber") or ""),
                 "Foreclosure source URL": ps.doc_url(str(d.get("id") or d.get("docId"))),
                 "Name / grantor": "; ".join(x.title() for x in (d.get("grantor") or []) if x),
                 "Property address / legal description": "; ".join(d.get("propertyAddress") or [])}
            if ex:
                f["Sale date"] = ex.isoformat()
            elif y and mo:
                f["Sale date"] = notice.first_tuesday(y, mo).isoformat()
                f["_note"] = (f"Sale date derived from sale month '{mt}' (first Tuesday of the month); "
                              f"not an exact date confirmed from the notice.")
            facts[str(d.get("id") or d.get("docId"))] = f
        filled = sh.fill_auto_blanks(county, facts)
        if filled:
            log(f"{county}: filled {filled} blank cells in earlier auto-added rows")
            ws, head, vals = sh.snapshot(county)
        recent = sh.queue_checked_since(county, 20)   # held notices are re-checked at most once a day (OCR is slow)
        docs = sorted(docs, key=lambda d: iso(d.get("recordedDate")) or "", reverse=True)   # newest filings first
        waited = 0
        add, held = [], []
        for n, d in enumerate(docs, 1):
            doc_id, inst = str(d.get("id") or d.get("docId")), str(d.get("instrumentNumber") or d.get("docNumber") or "")
            url = ps.doc_url(doc_id)
            names = [x for x in (d.get("grantor") or []) if x]
            filed = iso(d.get("recordedDate"))
            if since and filed and filed < since:
                det(doc_id, inst, "before resume date", filed=filed, name="; ".join(names), url=url)
                continue
            if doc_id in known or (inst and inst in known):
                stats["skip"] += 1
                det(doc_id, inst, "already in sheet", filed=filed, name="; ".join(names), url=url)
                continue
            if doc_id in recent:
                waited += 1
                det(doc_id, inst, "held (re-checked once a day)", filed=filed, name="; ".join(names),
                    why="still in Scrape Queue; checked again within the last 20 hours", url=url)
                continue
            if time.time() > deadline:
                stats["left"] += 1
                det(doc_id, inst, "not checked (time budget)", filed=filed, name="; ".join(names),
                    why="run time limit reached; checked on the next run", url=url)
                continue
            if n % 25 == 0:
                log(f"  {county}: {n}/{len(docs)} ({int(time.time() - T0)}s)")
            signal.alarm(180)                        # one stalled notice must never hang the whole run
            try:
                legal = "; ".join(d.get("propertyAddress") or [])
                month_txt, sy, sm = notice.sale_month_from_index(d)

                text, ocr_err = "", ""
                try:
                    text = notice.ocr_pages(ps.page_images(doc_id))
                except Exception as e:
                    ocr_err = str(e)[:120]
                stated = notice.find_address(text) if text else None
                exact = notice.find_sale_date(text) if text else None
                principal = notice.find_principal(text) if text else None

                # address: stated in the notice (verified by the notice itself), confirmed against CAD when possible
                addr, why, cad_id, cad_note = None, "", "", ""
                if stated:
                    v = cad.verify_address(county, stated)
                    if v.get("ok") is False and str(v.get("why", "")).startswith("0"):
                        why = f"address in notice not found in CAD ({stated})"
                    else:
                        addr, cad_id = stated, v.get("id", "") if v.get("ok") else ""
                        if v.get("ok") and v.get("city"):          # same parcel: write it in the sheet's style
                            zm = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", stated)
                            addr = cad.full_address(v.get("situs") or stated.split(",")[0], v["city"],
                                                    v.get("zip") or (zm.group(1) if zm else ""))
                        cad_note = "notice address, CAD parcel " + cad_id if cad_id else \
                            f"notice address (CAD: {v.get('why', 'not checked')})"
                else:
                    idx = (d.get("propAddress") or [{}])[0] or {}
                    a1 = str(idx.get("address1") or "")
                    if not a1[:1].isdigit():               # Dallas: the street address is in propertyAddress
                        a1 = next((str(x) for x in (d.get("propertyAddress") or []) if str(x)[:1].isdigit()), a1)
                    if a1[:1].isdigit() and not a1.upper().startswith(("LOT", "BLK", "BLOCK")):
                        cand = a1 if re.search(r"\b(TX|TEXAS)\b", a1.upper()) else \
                            f"{a1}, {idx.get('city', '')}, TX".replace(" ,", ",")
                        v = cad.verify_address(county, cand)
                        if v.get("ok"):
                            parts = [x.strip() for x in a1.split(",")]
                            city = v.get("city") or str(idx.get("city") or "").strip() or \
                                (parts[1] if len(parts) > 2 and not parts[1][:1].isdigit() else "")
                            zm = re.search(r"\b(\d{5})\b", " ".join(str(x) for x in (idx.get("zip"), a1, legal) if x))
                            zp = v.get("zip") or (zm.group(1) if zm else "")
                            addr, cad_id = cad.full_address(v.get("situs") or a1.split(",")[0], city, zp), v.get("id", "")
                            cad_note = f"index address matched one CAD parcel {cad_id}"
                        else:
                            why = f"index address not confirmed by CAD: {v.get('why')}"
                    else:
                        # legal description only: accept it when it matches exactly one CAD parcel
                        icity = str(idx.get("city") or "").strip()
                        v, src = {"ok": False, "why": "no legal description"}, ""
                        for src, ltxt in (("index", a1 or legal), ("notice", notice.find_legal(text) if text else "")):
                            if ltxt:
                                v = cad.verify_legal(county, ltxt, icity)
                                if v.get("ok"):
                                    break
                        if v.get("ok"):
                            addr, cad_id = cad.full_address(v["situs"], v.get("city") or icity, v.get("zip", "")), v["id"]
                            cad_note = f"legal description ({src}: {v.get('legal')}) matched one CAD parcel {cad_id}"
                        else:
                            why = (f"legal description only; not matched to one CAD parcel ({v.get('why')})" if text
                                   else f"notice image not readable ({ocr_err or 'no pages'}); {v.get('why')}")

                sale_date, sale_note, basis = "", "", ""
                idx_exact = notice.sale_date_from_index(d)
                if exact:
                    sale_date, basis = exact.isoformat(), "exact (notice)"
                    stats["sale_exact"] += 1
                elif idx_exact:
                    sale_date, basis = idx_exact.isoformat(), "exact (clerk index)"
                    stats["sale_exact"] += 1
                elif sy and sm:
                    sale_date = notice.first_tuesday(sy, sm).isoformat()
                    basis = f"first Tuesday of {month_txt}"
                    sale_note = (f"Sale date derived from sale month '{month_txt}' (first Tuesday of the month); "
                                 f"not an exact date confirmed from the notice.")
                    stats["sale_month"] += 1
                if filed:
                    stats["filed"] += 1

                akey = cad._street_key(addr) if addr else (None, None)
                if addr and akey[0] and akey in addr_keys:
                    why = (f"same street address already in the sheet (row {addr_keys[akey]}) under another record ID; "
                           f"check whether it is the same foreclosure")
                    stated, addr = stated or addr, None
                if not addr:
                    held.append({"First seen": today(), "Last checked": now_str(), "County": county,
                                 "Record type": "Foreclosure notice", "Official ID": doc_id, "Source record ID": inst,
                                 "Source URL": url, "Filed date": filed, "Sale month (source)": month_txt or "",
                                 "Name / grantor": "; ".join(names), "Index address / legal": legal,
                                 "Address found in notice": stated or "", "Reason held": why, "Resolved": ""})
                    det(doc_id, inst, "held for review", stated or "", filed, sale_date, basis, "; ".join(names),
                        why, url)
                    continue

                notes = [f"Auto-added {today()} from {county} County Clerk foreclosure index + notice OCR."]
                if sale_note:
                    notes.append(sale_note)
                if cad_note.startswith("legal description"):
                    notes.append(f"No street address in the notice; address from the {cad_note}.")
                elif cad_id:
                    notes.append(f"Address matched one CAD parcel ({cad_id}).")
                rec = {
                    "County": county,
                    "Record type / status": "Foreclosure notice",
                    "Actual street address": addr,
                    "Name / grantor": "; ".join(x.title() for x in names),
                    "Filed date": filed,
                    "Sale date": sale_date,
                    "Sale month (source)": month_txt or "",
                    "Foreclosure document ID": doc_id,
                    "Source record ID": inst,
                    "Source added on": today(),
                    "Property address / legal description": legal,
                    "Foreclosure source actual address": stated or "",
                    "Foreclosure source URL": url,
                    "Original notes": " ".join(notes),
                }
                if principal and "Original loan principal (notice)" in head:
                    rec["Original loan principal (notice)"] = f"{principal:.2f}"
                if cad_id and "CAD / property ID" in head:
                    rec["CAD / property ID"] = cad_id
                rec = {k: v for k, v in rec.items() if k in head}
                if akey[0]:
                    addr_keys[akey] = "new this run"
                rec["_det"] = [doc_id, inst, addr, filed, sale_date, basis, "; ".join(names), cad_note, url]
                add.append(rec)
            except Exception as e:
                if isinstance(e, NoticeTimeout):
                    try:
                        ps.close(); ps.connect()        # the socket may be the stalled part
                    except Exception:
                        pass
                stats["err"] += 1
                det(doc_id, inst, "error", filed=filed, name="; ".join(names), why=f"{type(e).__name__}: {str(e)[:200]}",
                    url=url)
            finally:
                signal.alarm(0)

        dets = [r.pop("_det") for r in add]
        written, bad = sh.append(county, add, "Foreclosure document ID")
        for dd in dets:
            res = "read-back failed" if dd[0] in bad else "added"
            det(dd[0], dd[1], res, dd[2], dd[3], dd[4], dd[5], dd[6], dd[7], dd[8])
        stats["new"] = written
        sh.resolve_queue({dd[0] for dd in dets if dd[0] not in bad}, f"added to {county} tab {today()}")
        stats["review"] = len(held)
        sh.queue(held)
        problems = ([f"removed {removed} duplicate auto-added rows"] if removed else []) + \
            ([f"moved {moved} same-address auto rows to Scrape Queue"] if moved else []) + \
            ([f"filled {filled} blank cells in earlier auto rows"] if filled else []) + \
            ([f"added the city to {fixed} auto-added addresses"] if fixed else [])
        if bad:
            problems.append(f"read-back failed for {bad}")
        if stats["err"]:
            problems.append(f"{stats['err']} notices errored")
        if stats["left"]:
            problems.append(f"{stats['left']} notices left for next run (time budget)")
        if waited:
            problems.append(f"{waited} held notices wait for their daily re-check")
        status = "complete" if not (bad or stats["err"] or stats["left"]) else "partial"
        return status, window, stats, "; ".join(problems)
    finally:
        ps.close()


def norm_date(v):
    """'11/05/2026' or '2026-11-05' -> '2026-11-05'; '' if not a date."""
    v = str(v or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
        return v
    return iso(v)


def run_collin(sh, county, deadline, detail):
    """Collin: the clerk's own notice list (address, sale date, file date); no OCR. See collin.py."""
    stats = {"found": 0, "new": 0, "review": 0, "skip": 0, "filed": 0, "sale_exact": 0, "sale_month": 0, "err": 0,
             "left": 0}
    at = now_str()
    since = sh.county_since(county)                # turned back on in the web app: nothing filed before that day

    def det(doc_id, result, addr="", filed="", sale="", basis="", why="", url=collin.URL):
        detail.append([at, RUN_ID, TRIGGER, county, doc_id, "", result, addr, filed, sale, basis, "", why, url])

    notices, total = collin.fetch_notices(log)
    stats["found"] = len(notices)
    ws, head, vals = sh.snapshot(county)
    id_field = next((h for h in ("Foreclosure document ID", "Source record ID") if h in head), None)
    if not id_field or "Actual street address" not in head:
        raise RuntimeError(f"{county} tab has no 'Foreclosure document ID' / 'Actual street address' column")
    known = sh.known_ids(head, vals, [id_field])
    # addresses already in the sheet (foreclosure rows) with their filed / sale dates
    c_a, c_t = head.index("Actual street address"), sh.col(head, "Record type / status")
    c_f, c_s = sh.col(head, "Filed date"), sh.col(head, "Sale date")
    seen = {}
    for i, r in enumerate(vals[1:], start=2):
        g = lambda c: (r[c] if 0 <= c < len(r) else "").strip()
        if c_t >= 0 and "FORECLOS" not in g(c_t).upper():
            continue
        k = cad._street_key(g(c_a)) if g(c_a) else (None, None)
        if k[0]:
            seen.setdefault(k, []).append((norm_date(g(c_f)), norm_date(g(c_s)), i))
    add, held = [], []
    title_n = {}
    for n, d in enumerate(notices, 1):
        street, city, zp = collin.split_address(d["address"])
        key = cad._street_key(street)
        filed, sale = iso(d["filed"]), iso(d["sale"])
        if key[0]:
            doc_id = f"collin:{filed}:{key[0]}-{(key[1] or '').replace(' ', '-')}"
        else:      # no street address on the list (e.g. "FILED 10/29/2026 - DECEMBER SALE - FRISCO"): several can look the same
            base = f"collin:{filed}:{re.sub(r'[^A-Z0-9]+', '-', d['address'].upper())}:{sale}"
            title_n[base] = title_n.get(base, 0) + 1
            doc_id = base if title_n[base] == 1 else f"{base}:{title_n[base]}"
        if since and filed and filed < since:
            det(doc_id, "before resume date", d["address"], filed, sale, why=f"filed before {county} was turned back on ({since})")
            continue
        if filed:
            stats["filed"] += 1
        if sale:
            stats["sale_exact"] += 1
        if doc_id in known:
            stats["skip"] += 1
            det(doc_id, "already in sheet", d["address"], filed, sale)
            continue
        if time.time() > deadline:
            stats["left"] += 1
            det(doc_id, "not checked (time budget)", d["address"], filed, sale, why="checked on the next run")
            continue
        try:
            if not key[0]:
                raise ValueError(f"the clerk's list gives no street address for this notice (listed only as "
                                 f"'{d['address'][:80]}'); a person has to find the property")
            old = seen.get(key, [])
            if old:
                same = [o for o in old if (not o[0] and not o[1]) or (filed and o[0] == filed) or
                        (sale and o[1] == sale and not o[0])]
                if same:
                    stats["skip"] += 1
                    det(doc_id, "already in sheet", d["address"], filed, sale, "exact (clerk list)",
                        f"same address and dates as row {same[0][2]}")
                    continue
                raise LookupError(f"same street address already in the sheet (row {old[0][2]}) with other dates "
                                  f"(filed {old[0][0] or '-'}, sale {old[0][1] or '-'}); check whether it is a new filing")
            v = cad.verify_address(county, f"{street}, {city}, TX {zp}".replace(" ,", ","))
            if v.get("ok") is False and str(v.get("why", "")).startswith("0"):
                raise LookupError(f"address on the clerk's list not found in CAD ({d['address']})")
            cad_id = v.get("id", "") if v.get("ok") else ""
            addr = cad.full_address(v.get("situs") or street, v.get("city") or city, v.get("zip") or zp) \
                if v.get("ok") else cad.full_address(street, city, zp)
            notes = [f"Auto-added {today()} from the Collin County Clerk foreclosure notice list "
                     f"(sale date and file date are as listed by the clerk)."]
            notes.append(f"Address matched one CAD parcel ({cad_id})." if cad_id else
                         f"Address as listed by the clerk (CAD: {v.get('why', 'not checked')}).")
            rec = {"County": county, "Record type / status": "Foreclosure notice", "Actual street address": addr,
                   "Filed date": filed, "Sale date": sale, "Foreclosure document ID": doc_id,
                   "Source added on": today(), "Property address / legal description": d["address"],
                   "Foreclosure source actual address": d["address"], "Foreclosure source URL": collin.URL,
                   "Original notes": " ".join(notes)}
            if cad_id and "CAD / property ID" in head:
                rec["CAD / property ID"] = cad_id
            rec = {k: val for k, val in rec.items() if k in head}
            seen.setdefault(key, []).append((filed, sale, 0))
            rec["_det"] = [doc_id, "", addr, filed, sale, "exact (clerk list)", "", notes[-1], collin.URL]
            add.append(rec)
        except Exception as e:
            why = str(e)[:250]
            if isinstance(e, (LookupError, ValueError)):
                held.append({"First seen": today(), "Last checked": now_str(), "County": county,
                             "Record type": "Foreclosure notice", "Official ID": doc_id, "Source record ID": "",
                             "Source URL": collin.URL, "Filed date": filed, "Sale month (source)": "",
                             "Name / grantor": "", "Index address / legal": d["address"],
                             "Address found in notice": d["address"], "Reason held": why, "Resolved": ""})
                det(doc_id, "held for review", d["address"], filed, sale, "exact (clerk list)", why)
            else:
                stats["err"] += 1
                det(doc_id, "error", d["address"], filed, sale, why=f"{type(e).__name__}: {why}")
    from collections import Counter
    log(f"{county}: held reasons: " + str(dict(Counter(h["Reason held"][:60] for h in held).most_common(5))))
    for h in [x for x in held if x["Reason held"].startswith("the clerk")][:6]:
        log(f"  not-a-street sample: {h['Index address / legal']!r} filed {h['Filed date']}")
    for h in held[:4]:
        log(f"  held sample: {h['Index address / legal']} filed {h['Filed date']} -> {h['Reason held'][:200]}")
    dets = [r.pop("_det") for r in add]
    written, bad = sh.append(county, add, id_field)
    for dd in dets:
        det(dd[0], "read-back failed" if dd[0] in bad else "added", dd[2], dd[3], dd[4], dd[5], dd[7])
    stats["new"] = written
    sh.resolve_queue({dd[0] for dd in dets if dd[0] not in bad}, f"added to {county} tab {today()}")
    stats["review"] = len(held)
    sh.queue(held)
    problems = ([f"read-back failed for {bad}"] if bad else []) + \
        ([f"{stats['err']} notices errored"] if stats["err"] else []) + \
        ([f"{stats['left']} notices left for next run (time budget)"] if stats["left"] else [])
    status = "complete" if not problems else "partial"
    return status, "all notices on the clerk's list", stats, "; ".join(problems)


def main():
    force = os.environ.get("FORCE", "false").lower() == "true"
    now = datetime.now(CHI)
    mins = now.hour * 60 + now.minute
    in_slot = any(0 <= mins - (h * 60 + m) <= 75 for h, m in RUN_SLOTS)   # allow GitHub's late starts
    if not force and not in_slot:
        log(f"Chicago time {now:%H:%M}: not a scheduled time (1am/7am/10am/1pm/4pm/7pm), nothing to do.")
        return 0
    days_back = int(os.environ.get("DAYS_BACK") or 0)
    sh = Sheet()
    if not force:                    # GitHub often starts scheduled runs late: allow an hour, but run once per slot
        last = sh.last_run_at(SOURCE)   # only foreclosure runs count (tax / probate runs write the same log)
        # 50 min: slots are 3 h apart; the 1am catch-up run can end shortly before the 7am slot and must not block it
        if last and (datetime.now(CHI).replace(tzinfo=None) - last).total_seconds() < 50 * 60:
            log(f"A run already finished at {last:%H:%M}; nothing to do.")
            return 0
    failed = False
    names = list(COUNTIES.items())
    tries = {}
    for i, (county, slug) in enumerate(names):
        c0, detail = time.time(), []
        deadline = time.time() + max(60, left_s() / (len(names) - i))     # share the time left fairly
        try:
            sh.county_rules(refresh=True)              # read again before each county: a pause during the run counts
            if not sh.county_on(county):
                status, window, s, msg = "paused", "", {}, "paused in the web app (Fetch log > Scrape counties); not scraped"
            elif county == "Collin":
                status, window, s, msg = run_collin(sh, county, deadline, detail)
            else:
                status, window, s, msg = run_county(sh, county, slug, days_back, deadline, detail)
        except Exception as e:
            msg403 = str(e)
            if "no authToken" in msg403 and "HTTP 403" in msg403:
                # The county site (Cloudflare) refused the GitHub machine. Not a code error: do not fail the whole workflow.
                tries[county] = tries.get(county, 0) + 1
                if tries[county] < 4 and left_s() > 25 * 60:
                    log(f"{county}: refused by the county site (HTTP 403); trying again later in this run (try {tries[county]} of 3)")
                    time.sleep(240)
                    names.append((county, slug))
                    continue
                status, window, s, msg = "incomplete", "", {}, "county site refused the GitHub machine (HTTP 403, Cloudflare check) after repeated tries; the next slot tries again"
            else:
                traceback.print_exc()
                status, window, s, msg = "incomplete", "", {}, f"{type(e).__name__}: {str(e)[:300]}"
                failed = True
        info = f"{TRIGGER} · run {RUN_ID} · {int(time.time() - c0)}s" + (f" · {RUN_URL}" if RUN_URL else "")
        row = [now_str(), county, SOURCE, status, window, s.get("found", ""), s.get("new", ""),
               s.get("review", ""), s.get("skip", ""), s.get("filed", ""), s.get("sale_exact", ""),
               s.get("sale_month", ""), (msg + " · " if msg else "") + info]
        try:                                   # write the logs per county, so a later crash never loses them
            # owner's choice (2026-11-11): notices skipped because they are already in the sheet (or wait for their daily
            # re-check) are counted in Scrape Log but get no Scrape Detail row; that tab grew ~7,000 rows a day
            sh.detail([r for r in detail if r[6] not in SKIP_DETAIL])
            sh.log([row])
        except Exception:
            traceback.print_exc()
            failed = True
        log(county, status, window, s, msg)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

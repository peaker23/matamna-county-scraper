"""Tax Lawsuit / Tax Delinquent leads from each county Tax Office's own weekly delinquent tax roll file.

Source per county (official, free, updated weekly by the Tax Assessor-Collector):
  Tarrant  tax roll zip (MASTER + receivable), tarrantcountytx.gov  -> receivable has Lawsuit Number / Filed Date
  Dallas   TRW file, dallascounty.org                              -> SUIT flag + CAUSENO
  Denton   delinquent tax roll (Master + Receivable), apps.dentoncounty.gov/website/taxfiles
  Collin   delinquent tax roll zip (Master + Receivable), collincountytx.gov
Collin and Denton use the same layout (Suit Number / Suit Date / Judgment Date in the receivable file).

Record types (sheet "Record type / status"):
  Tax Lawsuit    delinquent account with a suit / cause number on the tax roll
  Tax Delinquent delinquent account without a suit
IDs: taxsuit:<county>:<account>:<suit no>   taxdelq:<county>:<account>

TAX_MODE=count (default): download, parse and count only; nothing is written to the sheet.
"""
import io
import os
import re
import sys
import time
import traceback
import zipfile
from collections import Counter
from datetime import date, datetime
from urllib.parse import urljoin

import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
WORK = os.environ.get("TAX_WORK", "/tmp/taxroll")
MODE = (os.environ.get("TAX_MODE") or "count").lower()
ONLY = [c.strip() for c in (os.environ.get("TAX_COUNTIES") or "").split(",") if c.strip()]

PAGES = {
    "Collin": "https://www.collincountytx.gov/tax-assessor/property-taxes",
    "Denton": "https://apps.dentoncounty.gov/website/taxfiles/",
    "Dallas": "https://www.dallascounty.org/departments/tax/tax-roll.php",
    "Tarrant": "https://www.tarrantcountytx.gov/en/tax/property-tax/tarrant-county-tax-roll.html",
}
RESIDENTIAL = ("A", "B", "E", "M")       # SPTB / state category: single-family, multifamily, rural w/ home, mobile home
SOURCE_NAME = "Delinquent tax roll (County Tax Office)"
DELQ_MIN_YEARS, DELQ_MIN_DUE = 2, 2000.0   # Tax Delinquent filter (owner's choice); Tax Lawsuit: all residential


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- download
def links(page_url):
    r = requests.get(page_url, headers=UA, timeout=60)
    r.raise_for_status()
    hrefs = re.findall(r"""href\s*=\s*["']([^"']+)["']""", r.text, re.I)
    return [urljoin(r.url, h.replace("&amp;", "&").strip()) for h in hrefs]


def pick(county):
    """The download URL(s) for the county's delinquent roll, found on the Tax Office's own page."""
    hs = links(PAGES[county])
    if county == "Collin":
        c = [h for h in hs if "delinquent-tax-roll" in h.lower() and ".zip" in h.lower()]
    elif county == "Tarrant":
        c = [h for h in hs if ".zip" in h.lower() and "roll" in h.lower()]
    elif county == "Dallas":
        c = [h for h in hs if "trw" in h.lower() and not h.lower().endswith(".pdf") and "sample" not in h.lower()]
    else:                                                    # Denton: a plain file listing
        c = [h for h in hs if h.startswith(PAGES["Denton"]) and ".zip" in h.lower()]
        log(f"Denton: zip files listed: {[h.rsplit('/', 1)[-1] for h in c][:40]}")
        # two zips: the delinquent roll (AM/AR members, small) and the full roll (MM/MR, large); try each and keep
        # the one whose members start with 'A' (see run())
    if not c:
        log(f"{county}: candidate links on the page: {[h for h in hs if re.search(r'zip|trw|roll|tax', h, re.I)][:30]}")
        raise RuntimeError(f"{county}: download link not found on {PAGES[county]}")
    log(f"{county}: download link(s): {c[:6]}")
    return c


def download(county, url):
    os.makedirs(WORK, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", url.rsplit("/", 1)[-1])[-80:] or "file"
    path = os.path.join(WORK, f"{county}_{abs(hash(url)) % 10 ** 8}_{name}")     # unique per URL
    t0 = time.time()
    with requests.get(url, headers=UA, timeout=300, stream=True) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    log(f"{county}: downloaded {os.path.getsize(path) / 1e6:.1f} MB in {int(time.time() - t0)}s "
        f"({r.headers.get('Content-Type', '')}, last-modified {r.headers.get('Last-Modified', '-')})")
    return path


def members(path):
    """[(name, opener)] for a zip (each member) or a plain file."""
    if zipfile.is_zipfile(path):
        z = zipfile.ZipFile(path)
        out = []
        for i in z.infolist():
            if i.is_dir():
                continue
            log(f"   member {i.filename}  {i.file_size / 1e6:.1f} MB")
            if zipfile.is_zipfile(z.open(i)) if i.filename.lower().endswith(".zip") else False:
                inner = os.path.join(WORK, os.path.basename(i.filename))
                with z.open(i) as src, open(inner, "wb") as dst:
                    dst.write(src.read())
                out += members(inner)
            else:
                out.append((i.filename, (lambda i=i: io.TextIOWrapper(z.open(i), encoding="latin-1", newline=""))))
        return out
    return [(os.path.basename(path), lambda: open(path, encoding="latin-1", newline=""))]


def first_len(opener):
    with opener() as f:
        for line in f:
            s = line.rstrip("\r\n")
            if s.strip():
                return len(s)
    return 0


# ---------------------------------------------------------------- field helpers
def money(s):
    s = (s or "").strip().replace(",", "").replace("$", "")
    if not s:
        return 0.0
    neg = s.endswith("-") or s.startswith("-")
    s = s.strip("-+ ")
    try:
        v = float(s) if "." in s else int(s) / 100.0
    except ValueError:
        return 0.0
    return -v if neg else v


def dt(s):
    s = (s or "").strip()
    if not s or s.startswith(("1/1/9999", "01/01/9999", "9999", "00000000")):
        return None
    for f in ("%m/%d/%Y", "%Y%m%d", "%Y-%m-%d", "%m%d%Y"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


def cut(line, a, b):
    """1-based inclusive columns a..b."""
    return line[a - 1:b]


def situs(num, name):
    """'0001917', 'PAXTON DR' -> '1917 PAXTON DR'; a zero / blank house number is dropped."""
    n = num.strip().lstrip("0")
    return f"{n} {name.strip()}".strip()


# ---------------------------------------------------------------- layouts
# Collin / Denton: Master (860 V1 / 950 V2) + Receivable (224 V1 / 226 V2)
def pa_master(line):
    return {"acct": cut(line, 1, 30).strip(), "sptb": cut(line, 31, 33).strip(), "roll": cut(line, 34, 36).strip(),
            "legal": cut(line, 37, 160).strip(), "cad": cut(line, 174, 203).strip(),
            "street": situs(cut(line, 229, 238), cut(line, 204, 228)),
            "owner": cut(line, 335, 384).strip(), "owner2": cut(line, 385, 434).strip(),
            "maddr": " ".join(x for x in (cut(line, 435, 484).strip(), cut(line, 485, 534).strip()) if x),
            "mcity": cut(line, 535, 584).strip(), "mstate": cut(line, 585, 604).strip(),
            "mzip": cut(line, 605, 622).strip(),
            "defer": bool(dt(cut(line, 623, 632))) and not dt(cut(line, 633, 642)),
            "total_due": money(cut(line, 836, 847))}


def pa_rec(line):
    v2 = len(line.rstrip("\r\n")) >= 226
    o1, o2 = (1, 2) if v2 else (0, 0)          # V2: tax unit 4 (not 3), sequence 3 (not 2)
    s = lambda a, b, o: cut(line, a + o, b + o)
    return {"acct": cut(line, 1, 30).strip(), "year": cut(line, 31, 34).strip(),
            "type": s(38, 40, o1).strip(), "due": money(s(76, 87, o2)), "delq": dt(s(88, 97, o2)),
            "judg": dt(s(108, 117, o2)), "suit": s(118, 135, o2).strip(), "suit_date": dt(s(136, 145, o2)),
            "bk": s(146, 163, o2).strip()}


# Tarrant: Master.dat (781) + Rec.DAT (267) -- layout revision 11-2026 (owner names widened to 50)
def ta_master(line):
    return {"acct": cut(line, 1, 30).strip(), "sptb": cut(line, 31, 33).strip(), "roll": cut(line, 34, 35).strip(),
            "legal": cut(line, 36, 155).strip(), "cad": cut(line, 169, 198).strip(),
            "street": situs(cut(line, 224, 230), cut(line, 199, 223)),
            "owner": cut(line, 327, 376).strip(), "owner2": cut(line, 377, 426).strip(),
            "maddr": " ".join(x for x in (cut(line, 427, 456).strip(), cut(line, 457, 486).strip()) if x),
            "mcity": cut(line, 487, 516).strip(), "mstate": cut(line, 517, 536).strip(),
            "mzip": cut(line, 537, 554).strip(),
            "defer": bool(dt(cut(line, 555, 564))) and not dt(cut(line, 565, 574)),
            "total_due": money(cut(line, 701, 712)) + money(cut(line, 713, 724))}


def ta_rec(line):
    return {"acct": cut(line, 1, 30).strip(), "year": cut(line, 31, 34).strip(), "type": cut(line, 38, 40).strip(),
            "due": money(cut(line, 79, 90)), "delq": dt(cut(line, 91, 100)), "judg": dt(cut(line, 111, 120)),
            "suit": cut(line, 121, 138).strip(), "suit_date": dt(cut(line, 139, 148)), "bk": cut(line, 149, 166).strip()}


# Dallas TRW (534): one record per account / year / jurisdiction
def da_rec(line):
    return {"acct": cut(line, 1, 34).strip(), "year": cut(line, 35, 38).strip(), "jur": cut(line, 39, 42).strip(),
            "cad": cut(line, 43, 76).strip(), "paid": dt(cut(line, 93, 100)), "duedate": dt(cut(line, 101, 108)),
            "bal": money(cut(line, 111, 121)), "suitflag": cut(line, 122, 122).strip(),
            "suit": cut(line, 123, 162).strip(), "bk": cut(line, 163, 163).strip() or cut(line, 164, 203).strip(),
            "defer": cut(line, 219, 219).strip() == "D", "sptb": cut(line, 222, 225).strip(),
            "owner": cut(line, 226, 265).strip(),
            "maddr": " ".join(x for x in (cut(line, 266, 305).strip(), cut(line, 306, 345).strip(),
                                          cut(line, 346, 385).strip()) if x),
            "mcity": cut(line, 386, 425).strip(), "mstate": cut(line, 426, 427).strip(),
            "mzip": cut(line, 428, 439).strip(), "roll": cut(line, 440, 440).strip(),
            "parcel_no": cut(line, 441, 448).strip(), "parcel_name": cut(line, 449, 488).strip(),
            "agree": cut(line, 489, 489).strip() == "Y", "total_due": money(cut(line, 490, 500))}


# ---------------------------------------------------------------- per-county parse -> accounts
def is_real(roll):
    return roll.upper() in ("R", "1", "01", "001", "RP", "REA", "REAL")


# Tarrant / Collin / Denton tax software stores the state category as a number; table from Tarrant's
# "SPTB Codes" sheet: 1 A1, 18 A3, 20 A2, 23 A4, 39 A, 4 B1, 5 B2, 11 B3, 15 B4, 12 E1, 33 E2, 29 M1
RES_NUM = {1, 18, 20, 23, 39, 4, 5, 11, 15, 12, 33, 29}


def is_res(sptb):
    s = sptb.strip()
    if s.isdigit():
        return int(s) in RES_NUM
    return s[:1].upper() in RESIDENTIAL


def probe_cad(county, accts):
    """Check the numeric category table against the appraisal district: for the commonest codes, look up two
    accounts in the CAD parcel layer and print what the CAD says (state code, situs city / ZIP)."""
    try:
        from . import cad
    except Exception as e:
        log(f"   CAD probe skipped: {e}")
        return
    if county not in cad.LAYERS:
        return
    url, sfield, idf = cad.LAYERS[county]
    by = {}
    for a in accts.values():
        if a["years"] and is_real(a["roll"]) and a.get("cad"):
            by.setdefault(a["sptb"], []).append(a)
    for code, rows in sorted(by.items(), key=lambda kv: -len(kv[1]))[:8]:
        for a in rows[:2]:
            v = a["cad"].strip()
            cands = []
            for x in (re.sub(r"[A-Z]+$", "", a["acct"].strip()), v, a["acct"].strip()):
                if x and x not in cands:
                    cands.append(x)
            try:
                f, r, q = {}, {}, ""
                for x in cands:
                    for q in ([f"{idf} = {int(x)}"] if x.isdigit() else []) + [f"{idf} = '{x}'"]:
                        r = requests.get(url, params={"where": q, "outFields": "*", "returnGeometry": "false",
                                                      "f": "json"}, headers=UA, timeout=30).json()
                        if r.get("features"):
                            f = r["features"][0].get("attributes", {})
                            break
                    if f:
                        break
                keep = {k: f[k] for k in f if re.search(r"state|class|sptb|cd|city|zip|situs|use", k, re.I)}
                log(f"   CAD probe {county} code {code}: tax acct {a['acct']} cad '{v}' situs '{a['street']}' [{q}] -> "
                    f"{str(keep)[:400] if f else 'not found: ' + str(r)[:200]}")
            except Exception as e:
                log(f"   CAD probe {county} code {code}: {type(e).__name__}: {str(e)[:120]}")


def build_two_file(county, files, master_fn, rec_fn, master_len, rec_len, today):
    """Collin / Denton / Tarrant: aggregate receivables per account, then attach master info."""
    mfile = [f for f in files if f[2] in master_len]
    rfile = [f for f in files if f[2] in rec_len]
    if not mfile or not rfile:
        raise RuntimeError(f"{county}: master / receivable file not recognised by record length "
                           f"(files: {[(f[0], f[2]) for f in files]})")
    acc = {}
    nrec, types = 0, Counter()
    for name, op, ln in rfile:
        with op() as fh:
            for line in fh:
                if not line.strip():
                    continue
                nrec += 1
                r = rec_fn(line)
                # keep only what matters: an amount due that is past its delinquency date, or under a suit
                # (the current-year bill, not yet delinquent, is most of the full Tarrant roll)
                if r["due"] <= 0 or not ((r["delq"] and r["delq"] <= today) or r["suit"]):
                    continue
                types[r["type"]] += 1
                a = acc.setdefault(r["acct"], {"years": set(), "due": 0.0, "suits": {}, "judg": None, "bk": False,
                                               "first_delq": None})
                if r["delq"] and r["delq"] <= today:
                    a["years"].add(r["year"])
                    a["due"] += r["due"]
                    a["first_delq"] = min(a["first_delq"] or r["delq"], r["delq"])
                if r["suit"]:
                    sd = a["suits"].get(r["suit"])
                    a["suits"][r["suit"]] = min(sd or r["suit_date"], r["suit_date"]) if r["suit_date"] else sd
                if r["judg"]:
                    a["judg"] = max(a["judg"] or r["judg"], r["judg"])
                if r["bk"]:
                    a["bk"] = True
    log(f"{county}: {nrec} receivable records, {len(acc)} accounts with an amount due; "
        f"receivable types (top): {types.most_common(8)}")
    nmas, rolls, sptbs = 0, Counter(), Counter()
    out = {}
    for name, op, ln in mfile:
        with op() as fh:
            for line in fh:
                if not line.strip():
                    continue
                nmas += 1
                m = master_fn(line)
                a = acc.get(m["acct"])
                if not a:
                    continue
                rolls[m["roll"]] += 1
                sptbs[m["sptb"][:1]] += 1
                m.update(a)
                out[m["acct"]] = m
    log(f"{county}: {nmas} master records; roll codes of owing accounts: {rolls.most_common(8)}; "
        f"SPTB first letters: {sptbs.most_common(12)}")
    return out


def build_dallas(files, today):
    tf = [f for f in files if 520 <= f[2] <= 560]
    if not tf:
        raise RuntimeError(f"Dallas: TRW file not recognised (files: {[(f[0], f[2]) for f in files]})")
    out, nrec, rolls, sptbs = {}, 0, Counter(), Counter()
    for name, op, ln in tf:
        with op() as fh:
            for line in fh:
                if not line.strip():
                    continue
                nrec += 1
                r = da_rec(line)
                if r["bal"] <= 0 or r["paid"] or not ((r["duedate"] and r["duedate"] < today) or r["suitflag"]
                                                       or r["suit"]):
                    continue
                a = out.get(r["acct"])
                if not a:
                    a = {k: r[k] for k in ("acct", "cad", "sptb", "roll", "owner", "maddr", "mcity", "mstate", "mzip",
                                           "parcel_no", "parcel_name")}
                    a.update({"street": situs(r["parcel_no"], r["parcel_name"]), "legal": "",
                              "years": set(), "due": 0.0, "suits": {}, "judg": None, "bk": False, "defer": False,
                              "first_delq": None, "total_due": 0.0, "agree": False})
                    out[r["acct"]] = a
                    rolls[r["roll"]] += 1
                    sptbs[r["sptb"][:1]] += 1
                if r["duedate"] and r["duedate"] < today:
                    a["years"].add(r["year"])
                    a["due"] += r["bal"]
                    a["first_delq"] = min(a["first_delq"] or r["duedate"], r["duedate"])
                if r["suitflag"] or r["suit"]:
                    a["suits"].setdefault(r["suit"] or f"(flag {r['suitflag']})", None)
                a["bk"] = a["bk"] or bool(r["bk"] and r["bk"] != "N")
                a["defer"] = a["defer"] or r["defer"]
                a["agree"] = a["agree"] or r["agree"]
                a["total_due"] = max(a["total_due"], r["total_due"])
    log(f"Dallas: {nrec} TRW records, {len(out)} accounts with a balance; roll codes: {rolls.most_common(8)}; "
        f"category first letters: {sptbs.most_common(12)}")
    return out


def classify(accts):
    """-> (lawsuit list, delinquent list, counters)."""
    c, codes = Counter(), Counter()
    suits, delq = [], []
    for a in accts.values():
        if not a["years"]:
            c["owing but not yet delinquent"] += 1
            continue
        c["delinquent (any roll)"] += 1
        if not is_real(a["roll"]):
            c["skip: not real property"] += 1
            continue
        codes[a["sptb"]] += 1
        if a["suits"]:
            c["(real property, any category) with suit"] += 1
        if not is_res(a["sptb"]):
            c["skip: not residential"] += 1
            continue
        c["delinquent residential"] += 1
        if a["suits"]:
            c["Tax Lawsuit"] += 1
            if a["judg"]:
                c["  of which judgment entered"] += 1
            suits.append(a)
            continue
        if a["defer"]:
            c["skip delinquent: deferral (65+/disabled)"] += 1
            continue
        if a["bk"]:
            c["skip delinquent: bankruptcy"] += 1
            continue
        if len(a["years"]) >= DELQ_MIN_YEARS or a["due"] >= DELQ_MIN_DUE:
            c["Tax Delinquent (2+ yrs or $2,000+)"] += 1
            delq.append(a)
        else:
            c["skip delinquent: below threshold"] += 1
    log(f"   category codes of delinquent real-property accounts (top 25): {codes.most_common(25)}")
    return suits, delq, c


def sample(county, rows, label):
    for a in rows[:2]:
        log(f"   {label} sample: acct {a['acct']} cad {a.get('cad', '')} sptb {a['sptb']} roll {a['roll']} | "
            f"situs '{a['street']}' | mail city {a['mcity']} {a['mzip']} | years {sorted(a['years'])} "
            f"due {a['due']:.2f} | suits {dict((k, str(v)) for k, v in list(a['suits'].items())[:2])} "
            f"judg {a['judg']} | defer {a['defer']} bk {a['bk']}")


def run(county, today):
    t0 = time.time()
    urls = pick(county)
    files = []
    for u in urls[:3]:
        p = download(county, u)
        ms = members(p)
        if county in ("Collin", "Denton"):
            # AM / AR = delinquent roll master / receivable; MM / MR = full roll (not needed)
            ms = [m for m in ms if os.path.basename(m[0]).upper().startswith(("AM", "AR"))]
            if not ms:
                log(f"{county}: {u.rsplit('/', 1)[-1]} is not the delinquent roll (no AM/AR files); skipped")
                continue
        for name, op in ms:
            ln = first_len(op)
            log(f"   file {name}: first record length {ln}")
            files.append((name, op, ln))
        if county in ("Collin", "Denton"):
            break
    if county == "Dallas":
        accts = build_dallas(files, today)
    elif county == "Tarrant":
        accts = build_two_file(county, files, ta_master, ta_rec, range(770, 800), range(250, 300), today)
    else:
        accts = build_two_file(county, files, pa_master, pa_rec, (860, 950), (224, 226), today)
    suits, delq, c = classify(accts)
    log(f"{county}: counts {dict(c)}")
    if county != "Dallas" and MODE == "count":
        probe_cad(county, accts)
    sample(county, suits, "Tax Lawsuit")
    sample(county, delq, "Tax Delinquent")
    meta = {"asof": roll_date(county, [f[0] for f in files] + urls), "url": PAGES[county],
            "dallas_city": dallas_city_map(accts) if county == "Dallas" else {},
            "still": {norm_acct(k) for k in accts if norm_acct(k)}}      # every account still owing past-due / under suit
    log(f"{county}: roll as of {meta['asof'] or '?'}; done in {int(time.time() - t0)}s")
    return suits, delq, c, meta


# ---------------------------------------------------------------- write
TYPE_SUIT, TYPE_DELQ = "Delinquent tax lawsuit", "Delinquent property tax"     # the sheet's existing labels
ID_FIELD = "Source record ID"
CAD_CITY = {   # county -> (id field, id kind, situs field, city field, zip field) in cad.LAYERS[county]
    "Collin": ("propID", "num", "situsConcatShort", "situsCity", "situsZip"),
    "Denton": ("prop_id", "num", "situs", "situs_city", "situs_zip"),
    "Tarrant": ("ACCOUNT", "auto", "SITUS_ADDR", "CITY", "ZIPCODE"),
}


def roll_date(county, names):
    """Date the Tax Office produced the file, from its own file name: ...20261102... or AM103126 (MMDDYY)."""
    for n in names:
        b = os.path.basename(str(n))
        m = re.search(r"(20\d\d)(\d\d)(\d\d)", b)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        m = re.match(r"A[MR](\d\d)(\d\d)(\d\d)\.DAT$", b, re.I)
        if m:
            return f"20{m.group(3)}-{m.group(1)}-{m.group(2)}"
    return ""


def dallas_city_map(accts):
    """Dallas' TRW situs is '4930 BROOKVIEW DR, DA' (a city code). Learn code -> city from owner-occupied
    accounts (mailing street = situs street); keep a code only when one city is >= 95% of those accounts."""
    seen = {}
    for a in accts.values():
        st, _, code = a["street"].partition(",")
        code = code.strip()
        if code and a["maddr"] and re.sub(r"\W", "", a["maddr"].upper()).startswith(re.sub(r"\W", "", st.upper())):
            seen.setdefault(code, Counter())[a["mcity"].strip().upper()] += 1
    out = {}
    for code, cnt in seen.items():
        city, n = cnt.most_common(1)[0]
        if city and n >= 5 and n / sum(cnt.values()) >= 0.95:
            out[code] = city
    log(f"Dallas: situs city codes learned: {len(out)} ({dict(list(out.items())[:12])})")
    return out


def norm_acct(v):
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper()).lstrip("0")


def cad_ids(county, a):
    if county == "Denton":
        return re.sub(r"[A-Z]+$", "", a["acct"].strip())
    if county == "Collin":
        return a.get("cad", "").strip()
    return a["acct"].strip()


def cad_lookup(county, rows):
    """{tax account: (situs, city, zip)} from the CAD parcel layer, queried 150 ids at a time."""
    from . import cad
    if county not in CAD_CITY or county not in cad.LAYERS or not rows:
        return {}
    url = cad.LAYERS[county][0]
    idf, kind, sf, cf, zf = CAD_CITY[county]
    ids = {a["acct"]: cad_ids(county, a) for a in rows}

    def q(where):
        r = requests.post(url, data={"where": where, "outFields": ",".join((idf, sf, cf, zf)),
                                     "returnGeometry": "false", "f": "json"}, headers=UA, timeout=60).json()
        if "error" in r:
            raise RuntimeError(str(r["error"])[:150])
        return [f.get("attributes", {}) for f in r.get("features", [])]

    form = None                      # how this layer wants the id written: number, text, or the last 8 digits
    for a in rows[:5]:
        v = ids[a["acct"]]
        tries = ([("num", f"{idf} = {int(v)}")] if v.isdigit() else []) + [("txt", f"{idf} = '{v}'")]
        if v.isdigit() and len(v) > 8:
            tries.append(("txt8", f"{idf} = '{v[-8:]}'"))
        for name, w in tries:
            try:
                if q(w):
                    form = name
                    break
            except Exception:
                pass
        if form:
            break
    if not form:
        log(f"{county}: CAD layer did not answer for sample ids {[ids[a['acct']] for a in rows[:5]]}; no city/ZIP")
        return {}
    conv = {"num": lambda v: (str(int(v)) if v.isdigit() else None), "txt": lambda v: f"'{v}'",
            "txt8": lambda v: f"'{v[-8:]}'"}[form]
    key = {"num": lambda v: str(int(v)) if v.isdigit() else v, "txt": lambda v: v, "txt8": lambda v: v[-8:]}[form]
    back = {}
    for acct, v in ids.items():
        if v and conv(v):
            back.setdefault(key(v), []).append(acct)
    out, keys, bad = {}, list(back), 0
    for i in range(0, len(keys), 150):
        part = keys[i:i + 150]
        vals = ",".join(conv(k) if form != "num" else k for k in part)
        try:
            for f in q(f"{idf} IN ({vals})"):
                k = str(f.get(idf) if form == "num" else f.get(idf) or "").strip()
                k = str(int(float(k))) if form == "num" and k else k
                for acct in back.get(k, []):
                    out[acct] = (str(f.get(sf) or "").strip(), str(f.get(cf) or "").strip(),
                                 str(f.get(zf) or "").strip()[:5])
        except Exception as e:
            bad += 1
            if bad <= 3:
                log(f"{county}: CAD batch failed: {e}")
    log(f"{county}: CAD city/ZIP found for {len(out)} of {len(rows)} accounts (id form {form})")
    return out


def years_text(ys):
    ys = sorted(int(y) for y in ys if str(y).isdigit())
    out, i = [], 0
    while i < len(ys):
        j = i
        while j + 1 < len(ys) and ys[j + 1] == ys[j] + 1:
            j += 1
        out.append(str(ys[i]) if i == j else f"{ys[i]}-{ys[j]}")
        i = j + 1
    return ", ".join(out)


def existing_tax(head, vals):
    """Accounts already in the tab: normalized account / CAD id -> {'suit', 'delq'} kinds of tax rows."""
    g = lambda r, h: (r[head.index(h)] if h in head and head.index(h) < len(r) else "").strip()
    out = {}
    for r in vals[1:]:
        t = g(r, "Record type / status").upper()
        if "TAX" not in t:
            continue
        kind = "suit" if ("SUIT" in t or "LAWSUIT" in t or g(r, "Tax roll suit reference")) else "delq"
        for v in (g(r, "Tax account number"), g(r, "CAD / property ID")):
            if norm_acct(v):
                out.setdefault(norm_acct(v), set()).add(kind)
    return out


def write_county(sh, county, suits, delq, meta, detail, run_info):
    from . import cad
    from .sheet import now_str, today
    ws, head, vals = sh.snapshot(county)
    for h in ("Record type / status", "Actual street address", "Tax account number", ID_FIELD, "Original notes"):
        if h not in head:
            raise RuntimeError(f"{county} tab has no '{h}' column")
    have = existing_tax(head, vals)
    known = sh.known_ids(head, vals, [ID_FIELD])
    todo, skipped = [], 0
    for kind, rows in (("suit", suits), ("delq", delq)):
        for a in rows:
            ks = have.get(norm_acct(a["acct"]), set()) | have.get(norm_acct(cad_ids(county, a)), set())
            rid = (f"taxsuit:{county.lower()}:{a['acct']}:{sorted(a['suits'])[0]}" if kind == "suit"
                   else f"taxdelq:{county.lower()}:{a['acct']}")
            if rid in known or (kind == "suit" and "suit" in ks) or (kind == "delq" and ks):
                skipped += 1
                continue
            todo.append((kind, rid, a))
    log(f"{county}: {len(todo)} to add, {skipped} already in the tab (same account)")
    geo = cad_lookup(county, [a for _, _, a in todo]) if todo else {}
    at, asof = now_str(), meta.get("asof") or ""
    recs, dets = [], []
    for kind, rid, a in todo:
        street, city, zp, src = a["street"], "", "", "tax roll"
        if county == "Dallas":
            st, _, code = street.partition(",")
            street, city = st.strip(), meta["dallas_city"].get(code.strip(), "")
            if city and re.sub(r"\W", "", a["maddr"].upper()).startswith(re.sub(r"\W", "", street.upper())):
                zp = a["mzip"][:5]                 # owner-occupied: the mailing ZIP is the property's ZIP
        elif a["acct"] in geo:
            s2, city, zp = geo[a["acct"]]
            src = "tax roll + CAD"
        if not street or not street[:1].isdigit():
            street = street or a.get("legal", "")
        addr = cad.full_address(street, city, zp) if street else ""
        suits_txt = "; ".join(sorted(k for k in a["suits"] if not k.startswith("(flag")))
        sd = min((v for v in a["suits"].values() if v), default=None)
        note = (f"Auto-added {today()} from the {county} County Tax Office delinquent tax roll (file as of {asof}). "
                f"{'Suit on the roll: ' + suits_txt + '. ' if suits_txt else ''}"
                f"Delinquent amount {a['due']:,.2f} for tax years {years_text(a['years'])}"
                f"{'; judgment ' + a['judg'].isoformat() if a.get('judg') else ''}. "
                f"Address from the {src}{'' if city else ' (city not on the roll; check)'}.")
        if kind == "suit" and not sd:
            note += " The roll gives no suit filed date, so Filed date is left blank."
        rec = {"County": county, "Record type / status": TYPE_SUIT if kind == "suit" else TYPE_DELQ,
               "Actual street address": addr, "Name / grantor": " & ".join(x for x in (a.get("owner", ""),
                                                                                       a.get("owner2", "")) if x),
               "Name source / qualification": "Owner of record on the county tax roll",
               "Filed date": sd.isoformat() if (kind == "suit" and sd) else "",
               "Tax roll as-of date": asof, "Amount due (roll)": f"{a['due']:.2f}",
               "Amount description": "Delinquent amount on the tax roll (all taxing units, past-due years only)",
               "Tax account number": a["acct"], "CAD / property ID": cad_ids(county, a) if county != "Dallas"
               else a.get("cad", ""), "Case number": suits_txt, "Tax roll suit reference": suits_txt,
               "Tax suit filed date": sd.isoformat() if sd else "",
               "Judgment date": a["judg"].isoformat() if a.get("judg") else "",
               "Tax years covered": years_text(a["years"]), ID_FIELD: rid, "Source added on": today(),
               "Property address / legal description": a.get("legal") or a["street"],
               "Public records source address": a["street"], "Public records source URL": meta["url"],
               "Original notes": note}
        if not sh.county_keeps(county, rec):        # filed before the county was turned back on in the web app
            continue
        recs.append({k: v for k, v in rec.items() if k in head})
        dets.append([at, run_info[0], run_info[1], county, rid, a["acct"], "added", addr, rec["Filed date"], "",
                     "", rec["Name / grantor"], "", meta["url"]])
    written, bad = 0, []
    for i in range(0, len(recs), 1500):                 # batches; each batch is read back by Sheet.append
        w, b = sh.append(county, recs[i:i + 1500], ID_FIELD)
        written += w
        bad += b
        log(f"{county}: wrote {written} of {len(recs)}")
    badset = set(bad)
    for d in dets:
        if d[4] in badset:
            d[6] = "read-back failed"
    detail.extend(dets)
    return {"found": len(suits) + len(delq), "new": written, "skip": skipped, "bad": bad,
            "suit": sum(1 for k, _, _ in todo if k == "suit"), "delq": sum(1 for k, _, _ in todo if k == "delq")}


# ---------------------------------------------------------------- archive: accounts that left the roll (paid)
ARCH_TAB, MISS_TAB = "Tax Archive", "Tax Missing"       # both are in the web app's SKIP_SHEETS (not lead tabs)
ARCH_META = ["Archived on", "From tab", "Reason"]
MISS_HEAD = ["Source record ID", "County", "Tax account number", "First missing (run date)", "Last checked"]
APP_COLS = ("Lead Status", "Lead Notes", "Status Updated", "Starred", "Email Log")   # written by the web app
ARCH_MAX_SHARE = 0.20      # more than this share of a county's tax rows missing at once = probably a bad file
ARCH_MIN_DAYS = 5          # an account must be missing on two runs at least this many days apart


def archive_candidates(county, head, vals, still):
    """Rows this script added (taxsuit:/taxdelq: id, notes 'Auto-added') whose account is no longer on the
    delinquent roll, and that nobody worked on in the web app (status, notes, star, email all empty).
    -> (candidates [(row number, record id, account)], tax rows this script added, missing but worked on)."""
    col = {h: i for i, h in enumerate(head)}
    g = lambda r, h: (r[col[h]] if h in col and col[h] < len(r) else "").strip()
    pre = (f"taxsuit:{county.lower()}:", f"taxdelq:{county.lower()}:")
    out, total, worked = [], 0, 0
    for i, r in enumerate(vals[1:], start=2):
        rid = g(r, ID_FIELD)
        if not rid.startswith(pre) or not g(r, "Original notes").startswith("Auto-added"):
            continue
        total += 1
        acct = g(r, "Tax account number")
        if not norm_acct(acct) or norm_acct(acct) in still:
            continue
        if any(g(r, h) for h in APP_COLS):
            worked += 1
            continue
        out.append((i, rid, acct))
    return out, total, worked


def split_missing(county, cand, miss_vals, day):
    """Two-step rule. miss_vals = 'Tax Missing' rows. -> (ready to archive [(row, rid, acct, first)],
    the new 'Tax Missing' rows for this county)."""
    seen = {r[0]: r for r in miss_vals[1:] if len(r) > 3 and r[1] == county}
    ready, keep = [], []
    for i, rid, acct in cand:
        first = seen[rid][3] if rid in seen else day
        try:
            gap = (date.fromisoformat(day) - date.fromisoformat(first[:10])).days
        except ValueError:
            gap = 0
        if rid in seen and gap >= ARCH_MIN_DAYS:
            ready.append((i, rid, acct, first))
        else:
            keep.append([rid, county, acct, first, day])
    return ready, keep


def delete_rows(ws, rows):
    """Delete sheet rows (1-based) in one batch request, bottom first so the numbers stay valid."""
    runs = []
    for r in sorted(set(rows), reverse=True):
        if runs and runs[-1][0] == r + 1:
            runs[-1][0] = r
        else:
            runs.append([r, r])
    reqs = [{"deleteDimension": {"range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": a - 1, "endIndex": b}}}
            for a, b in runs]
    for k in range(0, len(reqs), 500):
        ws.spreadsheet.batch_update({"requests": reqs[k:k + 500]})


def archive_county(sh, county, still, day):
    """An account missing from the roll is first listed in 'Tax Missing'. If it is still missing on a later run
    (>= ARCH_MIN_DAYS later), its whole row is copied to 'Tax Archive', read back, then deleted from the county tab.
    If it comes back on the roll it simply drops off 'Tax Missing'. Nothing is written in count mode."""
    ws, head, vals = sh.snapshot(county)
    cand, total, worked = archive_candidates(county, head, vals, still)
    res = {"missing": len(cand), "worked": worked, "archived": 0, "waiting": 0, "note": ""}
    if not total or not still:
        res["note"] = "no tax rows or empty roll; nothing archived"
        return res
    if len(cand) > ARCH_MAX_SHARE * total:
        res["note"] = f"{len(cand)} of {total} tax rows missing at once: looks like a bad roll file; nothing archived"
        log(f"{county}: archive skipped: {res['note']}")
        return res
    try:
        mws = sh.tab(MISS_TAB, MISS_HEAD) if MODE == "write" else sh.book.worksheet(MISS_TAB)
        mvals = mws.get_all_values()
    except Exception:
        mws, mvals = None, [MISS_HEAD]
    ready, keep = split_missing(county, cand, mvals, day)
    res["waiting"] = len(keep)
    log(f"{county}: archive check: {total} tax rows, {len(cand)} no longer on the roll ({len(ready)} missing on an "
        f"earlier run too -> archive, {len(keep)} wait for the next run), {worked} missing but worked on (kept)")
    if MODE != "write":
        res["note"] = f"count mode: would archive {len(ready)}"
        return res
    moved = []
    if ready:
        aws = sh.tab(ARCH_TAB, ARCH_META + head)
        ahead = aws.row_values(1)
        add = [h for h in head if h not in ahead]
        if add:
            if aws.col_count < len(ahead) + len(add):
                aws.add_cols(len(ahead) + len(add) - aws.col_count)
            ahead = ahead + add
            aws.update("A1", [ahead], value_input_option="RAW")
        before = Counter()
        old = aws.get_all_values()
        ic = ahead.index(ID_FIELD)
        for r in old[1:]:
            if len(r) > ic:
                before[r[ic]] += 1
        rows = []
        for i, rid, acct, first in ready:
            src = vals[i - 1]
            rec = {h: (src[k] if k < len(src) else "") for k, h in enumerate(head)}
            rec.update({"Archived on": day, "From tab": county,
                        "Reason": f"Account {acct} not on the {county} delinquent tax roll on two runs "
                                  f"(first missing {first}): paid or removed"})
            rows.append([rec.get(h, "") for h in ahead])
        for k in range(0, len(rows), 1500):
            aws.append_rows(rows[k:k + 1500], value_input_option="RAW", insert_data_option="INSERT_ROWS")
        after = Counter()                             # read back: each archived id is there one more time
        for r in aws.get_all_values()[1:]:
            if len(r) > ic:
                after[r[ic]] += 1
        ok = {rid for _, rid, _, _ in ready if after[rid] == before[rid] + 1}
        _, head2, vals2 = sh.snapshot(county)         # fresh numbers: delete by record id, not by old row number
        ic2 = head2.index(ID_FIELD)
        dels = [i for i, r in enumerate(vals2[1:], start=2) if len(r) > ic2 and r[ic2].strip() in ok]
        delete_rows(ws, dels)
        moved = sorted(ok)
        res["archived"] = len(dels)
        if len(ok) < len(ready):
            res["note"] = f"archive read-back failed for {len(ready) - len(ok)} (left in the tab)"
        log(f"{county}: archived {len(dels)} rows to '{ARCH_TAB}'")
    others = [r for r in mvals[1:] if len(r) > 1 and r[1] != county]
    failed = [[rid, county, acct, first, day] for _, rid, acct, first in ready if rid not in set(moved)]
    mws.clear()
    mws.update("A1", [MISS_HEAD] + others + keep + failed, value_input_option="RAW")
    return res


def main():
    today = date.today()
    log(f"Tax roll run, mode={MODE}, {today}")
    failed, summary = False, {}
    try:                                     # read-only: the columns each county tab has (for the write step)
        from .sheet import Sheet
        sh = Sheet()
        for county in ["Collin", "Denton", "Dallas", "Tarrant"]:
            ws, head, vals = sh.snapshot(county)
            tc = head.index("Record type / status") if "Record type / status" in head else -1
            types = Counter((r[tc] if 0 <= tc < len(r) else "") for r in vals[1:])
            log(f"{county} tab: {len(vals) - 1} rows; columns: {head}; record types: {types.most_common(8)}")
    except Exception as e:
        log(f"sheet header check skipped: {type(e).__name__}: {str(e)[:150]}")
    from .sheet import now_str
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    trig = {"schedule": "scheduled", "workflow_dispatch": "manual (GitHub)"}.get(os.environ.get("GITHUB_EVENT_NAME", ""),
                                                                               "local")
    run_url = (f"https://github.com/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{run_id}"
               if os.environ.get("GITHUB_RUN_ID") else "")
    for county in ONLY or ["Collin", "Denton", "Dallas", "Tarrant"]:
        c0 = time.time()
        try:
            if MODE == "write":
                sh.county_rules(refresh=True)          # read again before each county: a pause during the run counts
            if MODE == "write" and not sh.county_on(county):
                summary[county] = "paused in the web app"
                sh.log([[now_str(), county, SOURCE_NAME, "paused", "", "", "", "", "", "", "", "",
                         "paused in the web app (Fetch log > Scrape counties); not scraped"]])
                continue
            suits, delq, c, meta = run(county, today)
            summary[county] = {"Tax Lawsuit": len(suits), "Tax Delinquent": len(delq)}
            if MODE != "write":
                try:
                    summary[county]["archive"] = archive_county(sh, county, meta["still"], today.isoformat())
                except Exception as e:
                    log(f"{county}: archive check failed: {type(e).__name__}: {str(e)[:200]}")
            if MODE == "write":
                detail = []
                s = write_county(sh, county, suits, delq, meta, detail, (run_id, trig))
                summary[county].update(s)
                try:                                  # after the new rows: paid accounts leave the tab
                    ar = archive_county(sh, county, meta["still"], today.isoformat())
                except Exception as e:
                    traceback.print_exc()
                    ar = {"archived": 0, "waiting": 0, "note": f"archive failed: {type(e).__name__}: {str(e)[:150]}"}
                summary[county]["archive"] = ar
                status = "complete" if not s["bad"] else "partial"
                msg = (f"added {s['suit']} {TYPE_SUIT} + {s['delq']} {TYPE_DELQ}; roll as of {meta['asof']}"
                       + (f"; read-back failed for {len(s['bad'])}" if s["bad"] else "")
                       + f"; archived {ar['archived']} paid accounts, {ar['waiting']} off the roll (archived if still"
                         f" off next run)" + (f" ({ar['note']})" if ar.get("note") else ""))
                info = f"{trig} · run {run_id} · {int(time.time() - c0)}s" + (f" · {run_url}" if run_url else "")
                sh.detail(detail)
                sh.log([[now_str(), county, SOURCE_NAME, status, f"tax roll {meta['asof']}", s["found"], s["new"],
                         0, s["skip"], "", "", "", msg + " · " + info]])
        except Exception as e:
            traceback.print_exc()
            summary[county] = f"FAILED: {type(e).__name__}: {str(e)[:200]}"
            failed = True
            if MODE == "write":
                try:
                    sh.log([[now_str(), county, SOURCE_NAME, "incomplete", "", "", "", "", "", "", "", "",
                             f"{type(e).__name__}: {str(e)[:300]}"]])
                except Exception:
                    traceback.print_exc()
    log("SUMMARY " + str(summary))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

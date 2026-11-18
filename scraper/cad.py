"""Official appraisal-district parcel services: confirm an address (or a legal description) is one real parcel."""
import re

import requests

# situs city / ZIP fields where the layer has them (Dallas' layer only has the mailing city)
CITYF = {"Tarrant": ("CITY", "ZIPCODE"), "Denton": ("situs_city", "situs_zip")}
LAYERS = {
    "Tarrant": ("https://mapit.tarrantcounty.com/arcgis/rest/services/Dynamic/TADParcels/FeatureServer/0/query",
                "SITUS_ADDR", "ACCOUNT"),
    "Dallas": ("https://maps.dcad.org/prdwa/rest/services/Property/ParcelQuery/MapServer/4/query",
               "SITEADDRESS", "PARCELID"),
    "Denton": ("https://gis.dentoncounty.gov/arcgis/rest/services/CAD/MapServer/0/query", "situs", "prop_id"),
    "Collin": ("https://services2.arcgis.com/uXyoacYrZTPTKD3R/arcgis/rest/services/CCAD_Parcel_Feature_Set/FeatureServer/4/query",
               "situsConcatShort", "propID"),
}
SUFFIX = {"STREET": "ST", "AVENUE": "AVE", "DRIVE": "DR", "LANE": "LN", "ROAD": "RD", "COURT": "CT", "CIRCLE": "CIR",
          "BOULEVARD": "BLVD", "PLACE": "PL", "TRAIL": "TRL", "PARKWAY": "PKWY", "TERRACE": "TER", "HIGHWAY": "HWY",
          "COVE": "CV", "POINT": "PT", "CROSSING": "XING"}


def _street_key(addr):
    """'8329 Snow Egret Way, Fort Worth, TX 76118' -> ('8329', 'SNOW EGRET')."""
    s = addr.upper().split(",")[0]
    s = re.sub(r"\s+(UNIT|APT|STE|SUITE|#).*$", "", s)
    m = re.match(r"(\d+[A-Z]?)\s+(.*)$", s.strip())
    if not m:
        return None, None
    words = [SUFFIX.get(w, w) for w in m.group(2).split()]
    if len(words) > 1 and words[-1] in set(SUFFIX.values()) | {"WAY", "LOOP", "PATH", "RUN", "ROW", "WALK"}:
        words = words[:-1]
    words = [w for w in words if w not in ("N", "S", "E", "W", "NORTH", "SOUTH", "EAST", "WEST")] or words
    return m.group(1), " ".join(words)


def verify_address(county, addr):
    """Returns {'ok': bool, 'id': parcel/account id, 'situs': official situs} — ok only for exactly one parcel."""
    if county not in LAYERS or not addr:
        return {"ok": None, "why": "no CAD layer for this county"}
    url, field, idf = LAYERS[county]
    num, street = _street_key(addr)
    if not num or not street:
        return {"ok": False, "why": "not a street address"}
    # query on house number + first street word only ("9237 DESERTROCK ROAD FORT WORTH" has no comma before the
    # city), then keep the parcels whose own street words all appear in the address
    first = street.split()[0]
    where = f"UPPER({field}) LIKE '{num} %{first.replace(chr(39), chr(39) * 2)}%'"
    try:
        cf, zf = CITYF.get(county, ("", ""))
        out = ",".join(x for x in (field, idf, cf, zf) if x)
        r = requests.get(url, params={"where": where, "outFields": out, "returnGeometry": "false",
                                      "f": "json"}, timeout=40)
        feats = r.json().get("features", [])
    except Exception as e:
        return {"ok": None, "why": f"CAD error {e}"}
    seen = {}
    for f in feats:
        a = f.get("attributes", {})
        seen[str(a.get(idf))] = (str(a.get(field) or "").split(",")[0].strip(), str(a.get(cf) or "").strip() if cf else "",
                                 str(a.get(zf) or "").strip()[:5] if zf else "")
    up = " " + re.sub(r"[^A-Z0-9 ]", " ", addr.upper()) + " "
    up = " ".join(SUFFIX.get(w, w) for w in up.split())
    def fits(situs):
        n2, st2 = _street_key(situs)
        return n2 == num and all(f" {w} " in f" {up} " for w in (st2 or "").split())
    if len(seen) > 1:
        seen = {k: v for k, v in seen.items() if fits(v[0])} or seen
    elif len(seen) == 1 and not fits(next(iter(seen.values()))[0]):
        return {"ok": False, "why": "0 CAD matches (street differs)"}
    if len(seen) == 1:
        pid, (situs, city, zp) = next(iter(seen.items()))
        return {"ok": True, "id": pid, "situs": situs, "city": city, "zip": zp if zp.isdigit() else ""}
    return {"ok": False, "why": f"{len(seen)} CAD matches"}


def full_address(street, city, zp=""):
    """'1108 WISHING TREE LN', 'KELLER', '' -> '1108 Wishing Tree Ln, Keller, TX'."""
    t = lambda x: " ".join(w.capitalize() if not w[:1].isdigit() else w for w in str(x).split())
    a = t(street)
    if city:
        a += ", " + t(city)
    a += ", TX"
    if zp:
        a += " " + zp
    return a


# ---------- legal description -> one parcel (Tarrant: LEGAL_1 like "MONCRIEF, W B ADDITION Lot 18  & 19") ----------
LEGALF = {"Tarrant": "LEGAL_1", "Denton": "legal_desc"}
_NOISE = {"ADDITION", "ADDN", "ADD", "ESTATES", "ESTATE", "SUBDIVISION", "SUBD", "SUB", "PHASE", "PH", "SECTION",
          "SEC", "THE", "OF", "TO", "AN", "A", "AND", "CITY", "COUNTY", "TARRANT", "TEXAS", "TX", "REVISED", "REV",
          "UNIT", "TRACT", "ACRES", "AC", "LOT", "LOTS", "LT", "BLOCK", "BLK", "BK", "NO", "PT", "PART"}


def parse_legal(text, city=""):
    """'LOT 18 W B MONCRIEF ADDITION FORT WORTH' -> {'lot': '18', 'block': '', 'words': ['MONCRIEF', ...]}.
    Also reads the usual notice wording 'LOT 3, BLOCK C, DANBURY PARKS, AN ADDITION TO THE CITY OF KELLER'."""
    t = re.sub(r"[^A-Z0-9 ]", " ", str(text).upper())
    t = re.sub(r"\s+", " ", t).strip()
    lot = re.search(r"\b(?:LOT|LT)S?\s+([0-9]+[A-Z]?|[A-Z])\b", t)
    blk = re.search(r"\b(?:BLOCK|BLK|BK)\s+([0-9]+[A-Z]?|[A-Z]{1,2})\b", t)
    if not lot:
        return None
    rest = t
    m = re.search(r"\bAN ADDITION\b|\bADDITION TO\b|\bACCORDING TO\b|\bIN THE CITY\b|\bCITY OF\b", rest)
    if m:                                         # notice wording: the name ends before "AN ADDITION TO THE CITY ..."
        rest = rest[:m.start()]
    rest = re.sub(r"\b(?:LOT|LT)S?\s+\S+(\s+(?:AND|&)\s+\S+)?", " ", rest)
    rest = re.sub(r"\b(?:BLOCK|BLK|BK)\s+\S+", " ", rest)
    if city:
        rest = re.sub(r"\b" + re.escape(city.upper()) + r"\s*$", " ", rest.strip())
    words = [w for w in rest.split() if w not in _NOISE and not w.isdigit() and len(w) > 1]
    if not words:
        return None
    return {"lot": lot.group(1), "block": blk.group(1) if blk else "", "words": words}


def verify_legal(county, text, city=""):
    """One CAD parcel whose legal description has the same lot, block and subdivision words -> like verify_address."""
    if county not in LEGALF or county not in LAYERS:
        return {"ok": None, "why": "no CAD legal-description search for this county"}
    p = parse_legal(text, city)
    if not p:
        return {"ok": False, "why": "legal description not readable (no lot / subdivision)"}
    url, field, idf = LAYERS[county]
    lf = LEGALF[county]
    cf, zf = CITYF.get(county, ("", ""))
    key = max(p["words"], key=len)                 # most distinctive word of the subdivision name
    where = f"UPPER({lf}) LIKE '%{key.replace(chr(39), chr(39) * 2)}%' AND UPPER({lf}) LIKE '%LOT {p['lot']}%'"
    try:
        r = requests.get(url, params={"where": where, "outFields": ",".join(x for x in (field, idf, lf, cf, zf) if x),
                                      "returnGeometry": "false", "f": "json"}, timeout=40)
        feats = r.json().get("features", [])
    except Exception as e:
        return {"ok": None, "why": f"CAD error {e}"}
    seen = {}
    for f in feats:
        a = f.get("attributes", {})
        lg = " " + re.sub(r"[^A-Z0-9 ]", " ", str(a.get(lf) or "").upper()) + " "
        lg = re.sub(r"\b(?:BLK|BK)\b", "BLOCK", lg)
        if not re.search(r"\bLOT\s+" + re.escape(p["lot"]) + r"\b", lg):
            continue
        if p["block"] and not re.search(r"\bBLOCK\s+" + re.escape(p["block"]) + r"\b", lg):
            continue
        if not all(f" {w} " in lg for w in p["words"]):
            continue
        ccity = str(a.get(cf) or "").strip().upper() if cf else ""
        if city and ccity and ccity != city.strip().upper():
            continue
        situs = str(a.get(field) or "").split(",")[0].strip()
        if situs[:1].isdigit():
            seen[str(a.get(idf))] = (situs, ccity, str(a.get(zf) or "").strip()[:5] if zf else "")
    if len(seen) == 1:
        pid, (situs, c, zp) = next(iter(seen.items()))
        return {"ok": True, "id": pid, "situs": situs, "city": c or city, "zip": zp if zp.isdigit() else "",
                "legal": f"Lot {p['lot']}" + (f" Block {p['block']}" if p["block"] else "") + " " + " ".join(p["words"])}
    return {"ok": False, "why": f"{len(seen)} CAD parcels match the legal description"}

"""OCR of a foreclosure notice + extraction of the facts the sheet needs.

Only values that are written out in the notice are returned; anything not found stays None.
"""
import io
import re
from datetime import date

MONTHS = {m: i for i, m in enumerate(
    ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
     "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"], 1)}
MON3 = {k[:3]: v for k, v in MONTHS.items()}


def ocr_pages(pages):
    """PNG bytes list -> one text string (Tesseract)."""
    import pytesseract
    from PIL import Image
    texts = []
    for b in pages:
        try:
            img = Image.open(io.BytesIO(b)).convert("L")
            if max(img.size) > 3400:                    # huge scans make Tesseract crawl
                img.thumbnail((3400, 3400))
            texts.append(pytesseract.image_to_string(img, timeout=60))
        except Exception as e:      # one bad page must not lose the others
            texts.append(f"[ocr error: {e}]")
    return "\n".join(texts)


def first_tuesday(year, month):
    d = date(year, month, 1)
    return d.replace(day=1 + (1 - d.weekday()) % 7)


_ADDR_LEAD = r"(?:commonly\s+known\s+as|property\s+address|street\s+address|physical\s+address|known\s+as|located\s+at|situs\s+address)\s*[:\-]?\s*"
_ADDR = (r"(\d{1,6}[A-Z]?\s+[A-Z0-9][A-Z0-9 .'#/\-]{2,60}?"          # number + street
         r"(?:,?\s*(?:UNIT|APT|STE|SUITE|#)\s*[A-Z0-9\-]+)?"         # unit
         r",?\s+[A-Z][A-Z .'\-]{2,30}?,?\s+(?:TX|TEXAS)\.?,?\s*(\d{5})(?:-\d{4})?)")


def find_address(text):
    """Street address stated in the notice ("commonly known as ..."). None when not stated."""
    t = re.sub(r"[ \t]+", " ", text.upper().replace("\n", " "))
    m = re.search(_ADDR_LEAD + _ADDR, t, re.I)
    if m:
        return _clean_addr(m.group(1))
    return None


def _clean_addr(a):
    a = re.sub(r"\s+", " ", a).strip(" ,.")
    a = re.sub(r"\bTEXAS\b", "TX", a)
    return a


def find_sale_date(text):
    """Exact sale date written in the notice -> date, else None."""
    t = re.sub(r"\s+", " ", text.upper())
    pats = [r"DATE\s+OF\s+SALE\s*[:\-]?\s*(?:TUESDAY,?\s*)?([A-Z]{3,9})\.?\s+(\d{1,2}),?\s+(20\d\d)",
            r"SALE\s+DATE\s*[:\-]?\s*(?:TUESDAY,?\s*)?([A-Z]{3,9})\.?\s+(\d{1,2}),?\s+(20\d\d)",
            r"TUESDAY,?\s+([A-Z]{3,9})\.?\s+(\d{1,2}),?\s+(20\d\d)",
            r"DATE\s+OF\s+SALE\s*[:\-]?\s*(\d{1,2})/(\d{1,2})/(20\d\d)"]
    for i, p in enumerate(pats):
        m = re.search(p, t)
        if not m:
            continue
        try:
            if i == 3:
                d = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            else:
                mon = MONTHS.get(m.group(1)) or MON3.get(m.group(1)[:3])
                if not mon:
                    continue
                d = date(int(m.group(3)), mon, int(m.group(2)))
            if d.weekday() == 1:            # Texas non-judicial sales are on Tuesdays
                return d
        except ValueError:
            continue
    return None


def find_principal(text):
    """Original principal amount stated in the notice -> float, else None."""
    t = re.sub(r"\s+", " ", text.upper())
    m = re.search(r"ORIGINAL\s+PRINCIPAL\s+(?:AMOUNT|SUM|BALANCE)?\s*(?:OF)?\s*\$\s?([\d,]+(?:\.\d\d)?)", t)
    if not m:
        m = re.search(r"IN\s+THE\s+(?:ORIGINAL\s+)?(?:PRINCIPAL\s+)?AMOUNT\s+OF\s+\$\s?([\d,]+(?:\.\d\d)?)", t)
    if m:
        try:
            v = float(m.group(1).replace(",", ""))
            return v if v >= 1000 else None
        except ValueError:
            return None
    return None


def sale_month_from_index(doc):
    """('Nov 2026', year, month) from the clerk index, or (None, None, None)."""
    fd = (doc.get("docDetails") or {}).get("foreclosure") or {}
    y, mname = fd.get("saleYear"), str(fd.get("saleMonth") or "").upper()
    mon = MONTHS.get(mname)
    if not (y and mon):
        sd = str(doc.get("saleDate") or "")
        m = re.match(r"([A-Za-z]{3})\w*\s+(20\d\d)", sd)
        if m:
            mon, y = MON3.get(m.group(1).upper()), int(m.group(2))
        m = re.match(r"(\d{1,2})/\d{1,2}/(20\d\d)", sd)
        if m and not mon:
            mon, y = int(m.group(1)), int(m.group(2))
    if y and mon:
        return f"{list(MON3.keys())[mon - 1].title()} {y}", int(y), int(mon)
    return None, None, None


def find_legal(text):
    """'LOT 3, BLOCK C, DANBURY PARKS, AN ADDITION TO THE CITY OF KELLER ...' from the notice / Exhibit A, else ''."""
    t = re.sub(r"\s+", " ", text.upper())
    m = re.search(r"\b(?:LOT|LT)S?\s+[0-9]+[A-Z]?\b.{0,160}?(?:ADDITION|SUBDIVISION|ESTATES|ADDN)\b", t)
    return m.group(0) if m else ""


def sale_date_from_index(doc):
    """Exact sale date the clerk index shows (Dallas: '12/01/2026') -> date, only when it is a Tuesday."""
    sd = str(doc.get("saleDate") or "")
    m = re.match(r"(\d{1,2})/(\d{1,2})/(20\d\d)", sd)
    if not m:
        return None
    try:
        d = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None
    return d if d.weekday() == 1 else None

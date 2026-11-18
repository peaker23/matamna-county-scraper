"""Google Sheet access: column-name based writes, duplicate checks, run log and review queue."""
import json
import os
import re
from datetime import timedelta, datetime
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials

CHI = ZoneInfo("America/Chicago")
LOG_TAB = "Scrape Log"
LOG_HEAD = ["Run at (Chicago)", "County", "Source", "Status", "Window", "Found", "New rows", "To review",
            "Skipped (already in sheet)", "Filed date verified", "Sale date exact", "Sale date from month", "Detail"]
DETAIL_TAB = "Scrape Detail"
DETAIL_HEAD = ["Run at (Chicago)", "Run ID", "Trigger", "County", "Official ID", "Source record ID", "Result",
               "Address", "Filed date", "Sale date", "Sale date basis", "Name / grantor", "Reason / note", "Source URL"]
QUEUE_TAB = "Scrape Queue"
QUEUE_HEAD = ["First seen", "Last checked", "County", "Record type", "Official ID", "Source record ID", "Source URL",
              "Filed date", "Sale month (source)", "Name / grantor", "Index address / legal", "Address found in notice",
              "Reason held", "Resolved"]
COUNTY_TAB = "Scrape Counties"   # set in the web app (Fetch log > Scrape counties): County | Scrape (On/Off) | Resume from | ...


def now_str():
    return datetime.now(CHI).strftime("%Y-%m-%d %H:%M")


def today():
    return datetime.now(CHI).strftime("%Y-%m-%d")


def iso_day(v):
    """'2026-11-13', '2026-11-13 08:00' or '11/13/2026' -> '2026-11-13'; anything else -> ''."""
    v = str(v or "").strip()
    m = re.match(r"(20\d\d)-(\d\d)-(\d\d)", v)
    if m:
        return m.group(0)
    m = re.match(r"(\d{1,2})/(\d{1,2})/(20\d\d)", v)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return ""


class Sheet:
    def __init__(self):
        info = json.loads(os.environ["GOOGLE_SA_JSON"])
        creds = Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        self.book = gspread.authorize(creds).open_by_key(os.environ["SHEET_ID"])

    def tab(self, name, head=None):
        try:
            return self.book.worksheet(name)
        except gspread.WorksheetNotFound:
            if head is None:
                raise
            ws = self.book.add_worksheet(name, rows=200, cols=len(head))
            ws.update("A1", [head], value_input_option="RAW")
            return ws

    # ---------- which counties to scrape (web app: Fetch log > Scrape counties) ----------
    def county_rules(self, refresh=False):
        """{county: {"on": bool, "since": "YYYY-MM-DD" or ""}} from the 'Scrape Counties' tab. Kept for the run;
        refresh=True reads the tab again (done before each county, so a county paused during a run is skipped).
        A county that is not listed (or no tab at all) is on, with no start date."""
        if refresh or getattr(self, "_county_rules", None) is None:
            rules = {}
            try:
                for r in self.book.worksheet(COUNTY_TAB).get_all_values()[1:]:
                    r = list(r) + ["", "", ""]
                    name = r[0].strip()
                    if name:
                        rules[name] = {"on": r[1].strip().lower() not in ("off", "paused", "false", "no"),
                                       "since": iso_day(r[2])}
            except gspread.WorksheetNotFound:
                pass
            self._county_rules = rules
        return self._county_rules

    def county_on(self, county):
        """False when the county is paused in the web app: the scrapers skip it completely."""
        return self.county_rules().get(county, {}).get("on", True)

    def county_since(self, county):
        """Day the county was turned back on in the web app: only records filed on/after it are added ('' = no limit)."""
        return self.county_rules().get(county, {}).get("since", "")

    def county_keeps(self, county, rec):
        """False when the county is paused, or the record was filed before the county was turned back on.
        A record without a filed date (tax-delinquent accounts) is kept."""
        if not self.county_on(county):
            return False
        since, filed = self.county_since(county), iso_day(rec.get("Filed date"))
        return not (since and filed and filed < since)

    # ---------- county tabs ----------
    def snapshot(self, county):
        """Fresh header + every value (never trust old column numbers)."""
        ws = self.tab(county)
        vals = ws.get_all_values()
        head = vals[0] if vals else []
        return ws, head, vals

    @staticmethod
    def col(head, name):
        try:
            return head.index(name)
        except ValueError:
            return -1

    def known_ids(self, head, vals, names):
        """Set of every value already in the given ID columns."""
        ids = set()
        for n in names:
            c = self.col(head, n)
            if c < 0:
                continue
            for r in vals[1:]:
                v = (r[c] if c < len(r) else "").strip()
                if v:
                    ids.add(v)
        return ids

    def address_keys(self, head, vals, keyf):
        """(house number, street) of every row's 'Actual street address' -> row number."""
        c, tc = self.col(head, "Actual street address"), self.col(head, "Record type / status")
        out = {}
        if c < 0:
            return out
        for i, r in enumerate(vals[1:], start=2):
            a = (r[c] if c < len(r) else "").strip()
            if tc >= 0 and "FORECLOS" not in (r[tc] if tc < len(r) else "").upper():
                continue                       # a tax / probate lead at the same address is a different record
            if a:
                k = keyf(a)
                if k[0] and k not in out:
                    out[k] = i
        return out

    def quarantine_auto_same_address(self, county, keyf, now, day):
        """A scraper-added row whose street address already belongs to an earlier row (any source) is not merged:
        it is removed from the county tab and listed in Scrape Queue for a person to decide. Returns rows moved."""
        ws, head, vals = self.snapshot(county)
        ac, nc = self.col(head, "Actual street address"), self.col(head, "Original notes")
        ic, sc = self.col(head, "Foreclosure document ID"), self.col(head, "Source record ID")
        uc, fc = self.col(head, "Foreclosure source URL"), self.col(head, "Filed date")
        if ac < 0 or nc < 0:
            return 0
        g = lambda r, c: (r[c] if 0 <= c < len(r) else "").strip()
        tc = self.col(head, "Record type / status")
        first, move = {}, []
        for i, r in enumerate(vals[1:], start=2):
            if tc >= 0 and "FORECLOS" not in g(r, tc).upper():
                continue
            a = g(r, ac)
            k = keyf(a) if a else (None, None)
            if not k[0]:
                continue
            if k in first and g(r, nc).startswith("Auto-added"):
                move.append((i, r, first[k]))
            elif k not in first:
                first[k] = i
        if not move:
            return 0
        items = [{"First seen": day, "Last checked": now, "County": county, "Record type": "Foreclosure notice",
                  "Official ID": g(r, ic), "Source record ID": g(r, sc), "Source URL": g(r, uc),
                  "Filed date": g(r, fc), "Address found in notice": g(r, ac),
                  "Reason held": f"same street address as foreclosure row {j} (another notice ID); not merged - check it"}
                 for i, r, j in move]
        self.queue(items, apply_rules=False)          # rows already in the sheet: never dropped
        for i, r, j in sorted(move, reverse=True):
            ws.delete_rows(i)
        return len(move)

    def fill_auto_blanks(self, county, facts):
        """facts: {official ID: {header: value, '_note': text}} from the clerk index. For rows the scraper added
        (notes start 'Auto-added'), write a value only into an EMPTY cell; a derived-date note is appended to the
        notes once. Returns the number of cells written."""
        from gspread.utils import rowcol_to_a1
        ws, head, vals = self.snapshot(county)
        ic, nc = self.col(head, "Foreclosure document ID"), self.col(head, "Original notes")
        if ic < 0 or nc < 0:
            return 0
        g = lambda r, c: (r[c] if 0 <= c < len(r) else "").strip()
        ups = []
        for i, r in enumerate(vals[1:], start=2):
            note = g(r, nc)
            f = facts.get(g(r, ic))
            if not f or not note.startswith("Auto-added"):
                continue
            wrote_sale = False
            for h, v in f.items():
                c = self.col(head, h)
                if h.startswith("_") or c < 0 or not v or g(r, c):
                    continue
                ups.append({"range": rowcol_to_a1(i, c + 1), "values": [[v]]})
                wrote_sale = wrote_sale or h == "Sale date"
            if wrote_sale and f.get("_note") and f["_note"] not in note:
                ups.append({"range": rowcol_to_a1(i, nc + 1), "values": [[(note + " " + f["_note"]).strip()]]})
        for k in range(0, len(ups), 400):
            ws.batch_update(ups[k:k + 400], value_input_option="RAW")
        return len(ups)

    def dedupe_auto(self, county, id_field):
        """Delete repeat rows of the same official ID that the scraper itself added (notes start 'Auto-added').
        The first copy is kept; rows a person added or edited are never touched. Returns rows removed."""
        ws, head, vals = self.snapshot(county)
        c, nc = self.col(head, id_field), self.col(head, "Original notes")
        if c < 0 or nc < 0:
            return 0
        first, drop = {}, []
        for i, r in enumerate(vals[1:], start=2):
            v = (r[c] if c < len(r) else "").strip()
            if not v:
                continue
            if v in first:
                norm = lambda row: [x.strip() for x in row] + [""] * (len(head) - len(row))
                same = norm(vals[first[v] - 1])[:len(head)] == norm(r)[:len(head)]
                if (r[nc] if nc < len(r) else "").startswith("Auto-added") and same:
                    drop.append(i)
            else:
                first[v] = i
        for i in sorted(drop, reverse=True):
            ws.delete_rows(i)
        return len(drop)

    def fix_auto_addresses(self, county, verify, city_by_id=None):
        """Scraper-added rows whose address has no city ('916 Sequoia Way, TX'): look the street up in CAD again
        and write 'street, City, TX'. Only rows the scraper added (notes start 'Auto-added') are touched."""
        import re
        from . import cad
        from gspread.utils import rowcol_to_a1
        ws, head, vals = self.snapshot(county)
        ups = []
        ac, nc = self.col(head, "Actual street address"), self.col(head, "Original notes")
        ic = self.col(head, "Foreclosure document ID")
        city_by_id = city_by_id or {}
        if ac < 0 or nc < 0:
            return 0
        n = 0
        for i, r in enumerate(vals[1:], start=2):
            a = (r[ac] if ac < len(r) else "").strip()
            note = r[nc] if nc < len(r) else ""
            if not note.startswith("Auto-added") or not re.match(r"^[^,]+,?\s*TX\.?(\s+\d{5})?$", a, re.I):
                continue
            street = re.sub(r",?\s*TX.*$", "", a, flags=re.I).strip(" ,")
            v = verify(street)
            city = v.get("city") or city_by_id.get((r[ic] if 0 <= ic < len(r) else "").strip(), "")
            if v.get("ok") and city:
                ups.append({"range": rowcol_to_a1(i, ac + 1),
                            "values": [[cad.full_address(v.get("situs") or street, city, v.get("zip", ""))]]})
                n += 1
        for k in range(0, len(ups), 400):
            ws.batch_update(ups[k:k + 400], value_input_option="RAW")
        return n

    def append(self, county, records, id_field):
        """Append records (dict: header -> value) by header name, then read back and confirm each id is there once."""
        if not records:
            return 0, []
        records = [r for r in records if self.county_keeps(county, r)]   # paused county / filed before it was resumed
        if not records:
            return 0, []
        ws, head, vals = self.snapshot(county)
        missing = sorted({k for r in records for k in r} - set(head))
        if missing:
            raise RuntimeError(f"{county}: columns not in sheet: {missing}")
        before = self.known_ids(head, vals, [id_field])
        rows, keep = [], []
        for r in records:
            if str(r.get(id_field, "")) in before:      # someone added it meanwhile
                continue
            rows.append([str(r.get(h, "")) if r.get(h) is not None else "" for h in head])
            keep.append(r)
        if not rows:
            return 0, []
        ws.append_rows(rows, value_input_option="RAW", insert_data_option="INSERT_ROWS")
        # read back
        _, head2, vals2 = self.snapshot(county)
        c = self.col(head2, id_field)
        counts = {}
        for row in vals2[1:]:
            v = row[c] if c < len(row) else ""
            counts[v] = counts.get(v, 0) + 1
        bad = [r[id_field] for r in keep if counts.get(str(r[id_field]), 0) != 1]
        return len(keep) - len(bad), bad

    # ---------- log + queue ----------
    def log(self, rows):
        ws = self.tab(LOG_TAB, LOG_HEAD)
        ws.append_rows(rows, value_input_option="RAW")

    def detail(self, rows):
        """One row per notice looked at in a run: added / held / already in sheet / error, with the reason."""
        if not rows:
            return
        ws = self.tab(DETAIL_TAB, DETAIL_HEAD)
        for i in range(0, len(rows), 500):
            ws.append_rows([[str(v) for v in r] for r in rows[i:i + 500]], value_input_option="RAW")

    def last_run_at(self, source=None):
        """Time (naive, Chicago) of the newest scraper row in Scrape Log (only rows of `source` when given), else None."""
        try:
            vals = self.tab(LOG_TAB, LOG_HEAD).get_all_values()
        except Exception:
            return None
        ts = [r[0] for r in vals[1:] if len(r) > 2 and r[2] != "Fetch now button" and (source is None or r[2] == source) and re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d", r[0])]
        return datetime.strptime(max(ts)[:16], "%Y-%m-%d %H:%M") if ts else None

    def last_ok(self, county, source):
        """Date (YYYY-MM-DD) of the last complete run for this county/source, else None."""
        try:
            vals = self.tab(LOG_TAB, LOG_HEAD).get_all_values()
        except Exception:
            return None
        best = None
        for r in vals[1:]:
            if len(r) > 3 and r[1] == county and r[2] == source and r[3] == "complete":
                best = max(best or "", r[0][:10])
        return best

    def queue(self, items, apply_rules=True):
        """Add / refresh held records (keyed by Official ID). Keeps the first-seen date and anything a person
        typed in 'Resolved'. One batch write, so a long list does not hit the Sheets write quota.
        apply_rules: drop records of a paused county / filed before the county was turned back on."""
        if apply_rules:
            items = [it for it in (items or []) if self.county_keeps(it.get("County", ""), it)]
        if not items:
            return
        ws = self.tab(QUEUE_TAB, QUEUE_HEAD)
        vals = ws.get_all_values()
        idx = {r[4]: i for i, r in enumerate(vals) if len(r) > 4 and i > 0}
        rc = QUEUE_HEAD.index("Resolved")
        new, ups = [], []
        for it in items:
            row = [it.get(h, "") for h in QUEUE_HEAD]
            if it["Official ID"] in idx:
                i = idx[it["Official ID"]]
                old = vals[i] + [""] * (len(QUEUE_HEAD) - len(vals[i]))
                row[0] = old[0] or row[0]                  # keep first-seen date
                row[rc] = old[rc] or row[rc]               # never overwrite a person's note
                ups.append({"range": f"A{i + 1}", "values": [row]})
            else:
                new.append(row)
        for k in range(0, len(ups), 400):
            ws.batch_update(ups[k:k + 400], value_input_option="RAW")
        if new:
            ws.append_rows(new, value_input_option="RAW")

    def resolve_queue(self, ids, text):
        """Mark queue rows whose Official ID was added to the county tab (only if 'Resolved' is still empty)."""
        if not ids:
            return
        try:
            ws = self.book.worksheet(QUEUE_TAB)
        except Exception:
            return
        vals = ws.get_all_values()
        rc = QUEUE_HEAD.index("Resolved")
        ups = [{"range": f"{chr(65 + rc)}{i + 1}", "values": [[text]]}
               for i, r in enumerate(vals) if i > 0 and len(r) > 4 and r[4] in ids and not (r[rc] if rc < len(r) else "")]
        for k in range(0, len(ups), 400):
            ws.batch_update(ups[k:k + 400], value_input_option="RAW")

    def queue_oldest(self, county):
        """Earliest Filed date of this county's open (unresolved) queue rows, so they are re-checked; else None."""
        try:
            vals = self.book.worksheet(QUEUE_TAB).get_all_values()
        except Exception:
            return None
        rc, fc = QUEUE_HEAD.index("Resolved"), QUEUE_HEAD.index("Filed date")
        ds = [r[fc] for r in vals[1:] if len(r) > fc and r[2] == county and not (r[rc] if rc < len(r) else "")
              and re.match(r"20\d\d-\d\d-\d\d$", r[fc])]
        return min(ds) if ds else None

    def queue_checked_since(self, county, hours):
        """Official IDs of this county's open queue rows that were checked in the last `hours` hours (skip them this run)."""
        try:
            vals = self.book.worksheet(QUEUE_TAB).get_all_values()
        except Exception:
            return set()
        rc, lc, oc = QUEUE_HEAD.index("Resolved"), QUEUE_HEAD.index("Last checked"), QUEUE_HEAD.index("Official ID")
        cut = (datetime.now(CHI) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
        return {r[oc] for r in vals[1:] if len(r) > max(lc, oc) and r[2] == county and r[oc]
                and not (r[rc] if rc < len(r) else "") and r[lc] >= cut}

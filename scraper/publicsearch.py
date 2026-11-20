"""Tarrant / Dallas / Denton County Clerk official records (publicsearch.us, Neumo).

The site loads search results over a WebSocket (wss://<county>.tx.publicsearch.us/ws):
  - GET the home page -> window.__data.user.authToken / ip (anonymous session)
  - send {"type": "@kofile/FETCH_DOCUMENTS/v4", ...}   -> one page of index records
  - send {"type": "fetch-a-document", ...}              -> page image URLs of one notice
"""
import json
import re
import time
import uuid
from datetime import datetime, timedelta

import requests
import websocket

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


class PublicSearch:
    def __init__(self, county):
        self.county = county                      # "tarrant" | "dallas" | "denton"
        self.host = f"{county}.tx.publicsearch.us"
        self.http = requests.Session()
        self.http.headers["User-Agent"] = UA
        self.ws = None
        self.token = None
        self.ip = None

    # ---------- session ----------
    def connect(self):
        html, m, status = "", None, "no response"
        for attempt in range(5):
            try:
                resp = self.http.get(f"https://{self.host}/", timeout=30)
                html, status = resp.text, f"HTTP {resp.status_code}"
            except requests.RequestException as e:
                html, status = "", f"{type(e).__name__}"
            m = re.search(r'"authToken"\s*:\s*"([^"]+)"', html)
            if m:
                break
            print(f"  {self.county}: no authToken (try {attempt + 1}/5, {status}, {len(html)} bytes: {html[:100]!r})", flush=True)
            time.sleep((20, 40, 60, 90, 120)[attempt])
            self.http.cookies.clear()
        if not m:
            raise RuntimeError(f"no authToken on home page after 5 tries ({status}; site changed or blocked?)")
        self.token = m.group(1)
        ip = re.search(r'"ip"\s*:\s*"([^"]+)"', html)
        self.ip = ip.group(1) if ip else ""
        cookie = "; ".join(f"{c.name}={c.value}" for c in self.http.cookies)
        self.ws = websocket.create_connection(
            f"wss://{self.host}/ws", timeout=60,
            header=[f"User-Agent: {UA}"] + ([f"Cookie: {cookie}"] if cookie else []),
            origin=f"https://{self.host}")

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self.ws = None

    def _ask(self, msg_type, payload, want):
        """Send one message and wait for the reply; the site drops idle sockets, so reconnect once and retry."""
        try:
            return self._ask1(msg_type, payload, want)
        except (websocket.WebSocketException, OSError) as e:
            self.close()
            time.sleep(1)
            self.connect()
            return self._ask1(msg_type, payload, want)

    def _ask1(self, msg_type, payload, want):
        """Send one message and wait for the reply whose type matches `want` (regex)."""
        cid = str(uuid.uuid4())
        self.ws.send(json.dumps({"type": msg_type, "payload": payload, "authToken": self.token,
                                 "ip": self.ip, "correlationId": cid, "sync": True}))
        deadline = time.time() + 60
        while time.time() < deadline:
            raw = self.ws.recv()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if re.search(want, msg.get("type", "")) and (msg.get("correlationId") in (cid, None)):
                return msg
            if "REJECTED" in msg.get("type", "") or msg.get("type") == "error":
                raise RuntimeError(f"{msg_type} rejected: {str(msg)[:300]}")
        raise TimeoutError(f"no reply to {msg_type}")

    # ---------- foreclosure index ----------
    def _range(self, date_from, date_to, page_size):
        out, offset, total = [], 0, None
        while True:
            q = {"limit": str(page_size), "offset": str(offset), "department": "FC",
                 "recordedDateRange": f"{date_from},{date_to}", "searchType": "advancedSearch"}
            msg = self._ask("@kofile/FETCH_DOCUMENTS/v4", {"query": q, "workspaceID": "search"},
                            r"FETCH_DOCUMENTS_FULFILLED")
            data = msg["payload"]["data"]
            total = msg["payload"].get("meta", {}).get("numRecords", 0)
            order = data.get("byOrder") or list(data.get("byHash", {}).keys())
            docs = [data["byHash"][str(k)] if str(k) in data["byHash"] else data["byHash"].get(k) for k in order]
            out.extend(d for d in docs if d)
            offset += page_size
            if not docs or offset >= total:
                break
            time.sleep(0.3)
        if total and len({str(d.get("id") or d.get("docId")) for d in out}) < total:
            raise RuntimeError(f"index {date_from}..{date_to}: got {len(out)} of {total} notices")
        return out, total

    def foreclosures(self, date_from, date_to, page_size=250):
        """All foreclosure notices recorded between two dates (YYYYMMDD). Returns (docs, total).

        The index has no stable sort across pages (paging a long range repeats some notices and skips others),
        so it is read one recording day at a time; each day fits in one page. Duplicates are dropped by ID.
        """
        d0 = datetime.strptime(date_from, "%Y%m%d").date()
        d1 = datetime.strptime(date_to, "%Y%m%d").date()
        seen, out, total = set(), [], 0
        while d0 <= d1:
            day = f"{d0:%Y%m%d}"
            docs, n = self._range(day, day, page_size)
            total += n or 0
            for d in docs:
                k = str(d.get("id") or d.get("docId"))
                if k not in seen:
                    seen.add(k)
                    out.append(d)
            d0 += timedelta(days=1)
            time.sleep(0.2)
        return out, len(out)

    # ---------- one notice ----------
    def page_images(self, doc_id):
        """PNG bytes of every page of one notice (free unofficial preview images)."""
        msg = self._ask("fetch-a-document", {"id": int(doc_id)}, r"^document$")
        urls = msg["payload"].get("urls") or []
        pages = []
        for u in urls:
            full = u if u.startswith("http") else f"https://{self.host}{u}"
            r = self.http.get(full, timeout=60)
            if r.ok and r.headers.get("content-type", "").startswith("image"):
                pages.append(r.content)
        return pages

    def doc_url(self, doc_id):
        return f"https://{self.host}/doc/{doc_id}"

#!/usr/bin/env python3
"""PS Medico weekly auto-update pipeline (runs on GitHub Actions, free tier).

Layers, per the 24 Nov 2026 spec:
  1. NPPA revised-MRP worksheets (nppa.gov.in): fetch the publications page,
     download the dated worksheet PDFs newer than NPPA_SINCE_DAYS, parse them
     (same column heuristic as the builder's parse_nppa_cancer.py), and merge
     the revised MRPs into the price channel with an honest source string.
  2. PMBJP / Jan Aushadhi (pmbi.co.in): try the public product API; when it is
     down (it intermittently 500s), fall back to the checked-in dated
     baseline data/pmbjp_prices.csv and say so. JA rows are published in the
     channel's "ja" section for the app's salt-page kendra line.
  3. Live re-verification: every name already in the published channel, plus
     the tier-1 everyday list and famous brands (data/verify_extra.txt), is
     re-checked through the worker's own /live endpoint (PharmEasy real MRP).
  4. CDSCO NSQ: best-effort page check; report-only in v1 (the builder parses
     new monthly lists in-session - the app's NSQ data is current).
  5. Publish: the merged channel goes to the worker via POST /publish (the
     admin key from the GitHub secret PSM_ADMIN_KEY). Same merge rules as the
     worker's priceRotate: name+pack key, freshest 6,000 packs, name-sorted,
     salt-truth layer preserved.

Honesty rules:
  - a government site being unreachable is a WARNING in the report, not a
    pipeline failure; the checked-in baselines keep the app truthful
  - the pipeline only exits non-zero when the core failed (nothing published)
  - every number in the run report is counted, never assumed

Output: run_report.md (also echoed to the GitHub step summary)."""
import concurrent.futures, csv, datetime, io, json, os, re, sys, time
import urllib.request, urllib.error, urllib.parse

PSM_BASE = os.environ.get("PSM_BASE", "https://ps-medico.psc-core.workers.dev")
ADMIN_KEY = os.environ.get("PSM_ADMIN_KEY", "")
HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
NPPA_PAGE = "https://nppa.gov.in/view_more?id=3"
PMBJP_API = "https://www.pmbi.co.in/api/v1/website/getAllProductForWeb"
CDSCO_URLS = ["https://cdsco.gov.in/opencms/opencms/en/Alerts/",
              "https://cdsco.mohfw.gov.in/"]
NPPA_SINCE_DAYS = 21          # how far back to look for new NPPA worksheets
VERIFY_CONCURRENCY = 4
VERIFY_LIMIT = int(os.environ.get("PSM_VERIFY_LIMIT", "0"))   # 0 = verify all (the weekly default)
REPORT = []                   # (level, line) pairs

def log(level, line):
    REPORT.append((level, line))
    print(f"[{level}] {line}")

def http_get(url, timeout=40, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read() if binary else r.read().decode("utf-8", "replace")

def post_publish(body, timeout=90):
    req = urllib.request.Request(PSM_BASE + "/publish", data=body.encode("utf-8"),
                                 method="POST",
                                 headers={"X-PSM-Key": ADMIN_KEY, "User-Agent": UA,
                                          "Content-Type": "text/plain"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

MONTHS = {m: i + 1 for i, m in enumerate(["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"])}
def asof_ts(a):
    m = re.search(r"(\d{1,2})\s+([A-Z][a-z]{2})\s+(\d{4})", str(a or ""))
    if not m: return 0
    try:
        return int(datetime.datetime(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1))).timestamp() * 1000)
    except Exception:
        return 0

def today_str():
    return f"{datetime.datetime.now(datetime.timezone.utc):%-d %b %Y}"

# ---------------------------------------------------------------- layer 2: PMBJP
def fetch_pmbjp():
    """Jan Aushadhi rows -> list of dicts {n, f, u, p, g} + provenance."""
    try:
        data = json.loads(http_get(PMBJP_API, timeout=30))
        items = data if isinstance(data, list) else data.get("data") or data.get("products") or []
        rows = []
        for it in items:
            name = str(it.get("Generic Name") or it.get("generic_name") or it.get("name") or "").strip()
            price = it.get("MRP") or it.get("mrp") or it.get("price")
            if not name or price is None: continue
            try: price = float(str(price).replace(",", ""))
            except ValueError: continue
            if not (0 < price < 100000): continue
            rows.append({"n": name.lower(), "f": str(it.get("form") or "").lower(),
                         "u": str(it.get("Unit Size") or it.get("unit") or "").strip(),
                         "p": round(price, 2), "g": str(it.get("Group Name") or "").strip()})
        if rows:
            log("ok", f"PMBJP: {len(rows)} rows from the live pmbi.co.in API")
            return rows, f"pmbi.co.in API, {today_str()}"
    except Exception as e:
        log("warn", f"PMBJP: live API unreachable ({str(e)[:60]}) - using the checked-in baseline")
    rows = []
    with open(os.path.join(HERE, "data", "pmbjp_prices.csv"), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try: price = float(r["MRP"])
            except (ValueError, KeyError): continue
            if not (0 < price < 100000): continue
            rows.append({"n": (r.get("Generic Name") or "").lower(), "f": "",
                         "u": (r.get("Unit Size") or "").strip(), "p": round(price, 2),
                         "g": (r.get("Group Name") or "").strip()})
    log("ok", f"PMBJP: {len(rows)} rows from the baseline CSV (dated below - the app labels it honestly)")
    return rows, f"Jan Aushadhi checked-in list, {BASELINE_DATE}"

# ---------------------------------------------------------------- layer 1: NPPA
def fetch_nppa():
    """New dated worksheet PDFs -> parsed revised-MRP rows (nm, pk, p, a)."""
    rows = []
    try:
        html = http_get(NPPA_PAGE, timeout=60)
    except Exception as e:
        log("warn", f"NPPA: publications page unreachable ({str(e)[:60]}) - no new worksheets this run")
        return rows
    # dated /storage/uploads PDFs, newest first
    docs = {}
    for href, title in re.findall(r'href="([^"]+\.pdf)"[^>]*>(.*?)</a>', html, re.S):
        m = re.search(r'(\d{1,2})-(\d{1,2})-(\d{4})', href)
        if not m: continue
        d = datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if (datetime.date.today() - d).days > NPPA_SINCE_DAYS: continue
        url = href if href.startswith("http") else "https://nppa.gov.in/" + href.lstrip("/")
        docs[url] = re.sub(r"<[^>]+>", " ", title).strip()[:60]
    if not docs:
        log("ok", f"NPPA: no new dated worksheets in the last {NPPA_SINCE_DAYS} days")
        return rows
    log("ok", f"NPPA: {len(docs)} new/ recent document(s): " + "; ".join(list(docs.values())[:3]))
    parsed_any = 0
    for url, title in docs.items():
        try:
            pdf_bytes = http_get(url, timeout=90, binary=True)
            got = parse_nppa_worksheet(pdf_bytes)
        except Exception as e:
            log("warn", f"NPPA: failed to download/parse {url.rsplit('/', 1)[-1]} ({str(e)[:50]})")
            continue
        if got:
            d = re.search(r'(\d{1,2})-(\d{1,2})-(\d{4})', url)
            ds = f"{int(d.group(1))} {['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][int(d.group(2))]} {d.group(3)}" if d else today_str()
            for g in got:
                nm = (g.get("brand") or g.get("drug") or "").strip()
                if not nm: continue
                try: p = float(g.get("revised_mrp") or g.get("old_mrp") or 0)
                except (TypeError, ValueError): p = 0
                if not (0 < p < 5000000): continue
                rows.append({"nm": nm.lower()[:90], "pk": str(g.get("pack") or "")[:80],
                             "p": round(p, 2), "a": f"NPPA revised MRP (nppa.gov.in) {ds}",
                             "c": (g.get("drug") or "")[:220]})
            parsed_any += len(got)
        else:
            log("warn", f"NPPA: {url.rsplit('/', 1)[-1]} did not match the worksheet format - left for manual review")
    log("ok", f"NPPA: {parsed_any} worksheet rows parsed into the channel")
    return rows

def parse_nppa_worksheet(pdf_bytes):
    """Column-position parse of an NPPA 'Revised MRP' worksheet (the format of
    the builder's parse_nppa_cancer.py). Returns [] when the PDF is another
    NPPA document type - that is an honest 'not this format', not an error."""
    try:
        import pdfplumber
    except ImportError:
        log("warn", "NPPA: pdfplumber not installed - worksheets skipped this run")
        return []
    rows, cur = [], None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            words = page.extract_words() or []
            hdr = [w for w in words if w["text"] in ("S.No.", "Company", "Drug", "Brand", "Pack", "Old", "Revised")]
            if len(hdr) < 4: continue
            xs = {}
            for w in words:
                if w["text"] == "S.No.": xs["sno"] = w["x1"]
                elif w["text"] == "Company": xs["co"] = w["x0"]
                elif w["text"] == "Drug": xs["drug"] = w["x0"]
                elif w["text"] == "Brand": xs["brand"] = w["x0"]
                elif w["text"] == "Pack": xs["pack"] = w["x0"]
                elif w["text"] == "Old": xs["old"] = w["x0"]
                elif w["text"] == "Revised": xs["rev"] = w["x0"]
            if len(xs) < 6: continue
            lines = {}
            for w in words:
                lines.setdefault(round(w["top"] / 3), []).append(w)
            def take(lw, x0, x1):
                return " ".join(w["text"] for w in lw if x0 <= (w["x0"] + w["x1"]) / 2 < x1)
            bounds = [("co", xs.get("co", 0), xs.get("drug", 0)),
                      ("drug", xs.get("drug", 0), xs.get("brand", 0)),
                      ("brand", xs.get("brand", 0), xs.get("pack", 0)),
                      ("pack", xs.get("pack", 0), xs.get("old", 0)),
                      ("old", xs.get("old", 0), xs.get("rev", 10 ** 9)),
                      ("rev", xs.get("rev", 0), 10 ** 9)]
            for key in sorted(lines):
                lw = sorted(lines[key], key=lambda w: w["x0"])
                txt = " ".join(w["text"] for w in lw)
                m = re.match(r"^(\d{1,4})\s", txt)
                if m:
                    if cur: rows.append(cur)
                    cur = {"sno": int(m.group(1))}
                if cur is None: continue
                for field, x0, x1 in bounds:
                    v = take(lw, x0, x1).strip()
                    if v: cur[field] = (cur.get(field, "") + " " + v).strip()
        if cur: rows.append(cur)
    out = []
    for r in rows:
        def num(k):
            m = re.search(r"([\d,]+(?:\.\d+)?)", str(r.get(k, "")))
            return float(m.group(1).replace(",", "")) if m else None
        rev, old = num("rev"), num("old")
        if r.get("drug") and (rev or old):
            out.append({"company": r.get("co", ""), "drug": r.get("drug", ""),
                        "brand": r.get("brand", ""), "pack": r.get("pack", ""),
                        "old_mrp": old, "revised_mrp": rev})
    return out

# ---------------------------------------------------------------- layer 4: CDSCO
def check_cdsco():
    for u in CDSCO_URLS:
        try:
            http_get(u, timeout=25)
            log("ok", f"CDSCO: {u} reachable - the builder should check for a new NSQ month in-session")
            return
        except Exception:
            pass
    log("warn", "CDSCO: site unreachable from this runner - NSQ refresh deferred to the builder")

# ---------------------------------------------------------------- layer 3: live
def live_verify(channel):
    queries = []
    seen = set()
    for e in channel.get("prices", []):
        q = " ".join(str(e.get("nm", "")).split()[:3])
        if q and q not in seen:
            seen.add(q); queries.append(str(e["nm"])[:60])
    try:
        with open(os.path.join(HERE, "data", "verify_extra.txt"), encoding="utf-8") as f:
            for l in f:
                l = l.strip()
                if l and l not in seen:
                    seen.add(l); queries.append(l)
    except FileNotFoundError:
        pass
    log("ok", f"live re-verify: {len(queries)} medicines queued (channel + tier-1 + famous)"
             + (f" [capped to {VERIFY_LIMIT} for this test run]" if VERIFY_LIMIT else ""))
    if VERIFY_LIMIT:
        queries = queries[:VERIFY_LIMIT]
    got, failed = [], 0
    lock_print = {"done": 0}
    import threading
    pr = threading.Lock()

    def one(q):
        url = PSM_BASE + "/live?q=" + urllib.parse.quote(q)
        try:
            j = json.loads(http_get(url, timeout=45))
            if j.get("ok") and isinstance(j.get("prices"), list):
                out = []
                for e in j["prices"]:
                    try: p = float(e.get("p"))
                    except (TypeError, ValueError): continue
                    if e.get("nm") and 0 < p < 500000:
                        out.append({"nm": str(e["nm"])[:90], "pk": str(e.get("pk") or "")[:80],
                                    "p": round(p, 2), "a": str(e.get("a") or "")[:40],
                                    "c": str(e.get("c") or "")[:220]})
                return out
        except Exception:
            pass
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=VERIFY_CONCURRENCY) as ex:
        for res in ex.map(one, queries):
            with pr:
                lock_print["done"] += 1
                if lock_print["done"] % 300 == 0:
                    print(f"  ... {lock_print['done']}/{len(queries)} re-verified")
            if res is None:
                failed += 1
            else:
                got.extend(res)
    log("ok", f"live re-verify: {len(got)} fresh price rows ({failed} query failures)")
    return got

# ---------------------------------------------------------------- merge + publish
def main():
    if not ADMIN_KEY:
        log("fail", "PSM_ADMIN_KEY is not set - add the repository secret (README step 3)")
        write_report(); sys.exit(1)
    channel = json.loads(http_get(PSM_BASE + "/data", timeout=60))
    log("ok", f"channel now: {channel.get('n')} prices, dver {channel.get('dver')}, date {channel.get('date')}")

    ja_rows, ja_src = fetch_pmbjp()
    nppa_rows = fetch_nppa()
    check_cdsco()
    live_rows = live_verify(channel)

    by = {}
    for e in channel.get("prices", []):
        by[e["nm"].lower() + "|" + (e.get("pk") or "")] = e
    for e in nppa_rows + live_rows:
        by[e["nm"].lower() + "|" + (e.get("pk") or "")] = e
    allp = list(by.values())
    if len(allp) > 6000:
        allp.sort(key=lambda x: -asof_ts(x.get("a")))
        allp = allp[:6000]
    allp.sort(key=lambda x: x["nm"])

    sby = {}
    for s in (channel.get("salts") or []):
        if s and s.get("nm"):
            sby[s["nm"].lower() + "|" + (s.get("pk") or "")] = s
    for e in live_rows:
        if e.get("c"):
            sby[e["nm"].lower() + "|" + (e.get("pk") or "")] = {"nm": e["nm"], "pk": e["pk"], "c": e["c"]}
    salts = list(sby.values())[:2500]

    channel["prices"] = allp
    channel["salts"] = salts
    channel["n"] = len(allp)
    channel["ja"] = ja_rows
    channel["ja_source"] = ja_src
    channel["date"] = today_str()
    channel["dver"] = "w" + str(int(time.time() * 1000))
    body = json.dumps(channel)
    if len(body) > 990000:
        log("fail", f"channel too large to publish ({len(body)} bytes) - refusing")
        write_report(); sys.exit(1)
    resp = post_publish(body)
    if resp.get("ok") is not True:
        log("fail", "publish failed: " + json.dumps(resp)[:200])
        write_report(); sys.exit(1)
    log("ok", f"published: {channel['n']} prices ({len(live_rows)} + {len(nppa_rows)} refreshed this run), "
             f"{len(ja_rows)} Jan Aushadhi rows ({ja_src}), {len(salts)} salt-truth rows")
    write_report()

def write_report():
    with open(os.path.join(HERE, "run_report.md"), "w", encoding="utf-8") as f:
        f.write("# PS Medico weekly update - run report\n\n")
        f.write(f"*{datetime.datetime.now(datetime.timezone.utc).isoformat()}*\n\n")
        for level, line in REPORT:
            icon = {"ok": "✅", "warn": "⚠️", "fail": "❌"}[level]
            f.write(f"- {icon} {line}\n")
        f.write("\nA ⚠️ is a government source being slow or changed format - the app keeps its "
                "last published data and stays honest. A ❌ means nothing was published this run.\n")
    print("\nreport written to run_report.md")

BASELINE_DATE = "19 Nov 2026"

if __name__ == "__main__":
    main()

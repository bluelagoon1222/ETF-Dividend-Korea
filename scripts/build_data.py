#!/usr/bin/env python3
"""
ETF-Dividend-Korea data builder.

Sources (all public pages, no API key):
  1. SEIBro (Korea Securities Depository) - every ETF distribution: record date,
     pay date, amount per share  [official]
  2. Naver Finance ETF list - current price, NAV, market cap, category
  3. FunETF divBasic - 1-year return, and per-share taxable amount where the
     issuer publishes it (e.g. KODEX)

Output: data/etf.json  (the previous file is kept if a required source fails)
"""
import datetime as dt
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "etf.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MIN_AUM_EOK = 100          # market cap filter, 100 eok KRW
HISTORY_DAYS = 400         # distribution history window
SEIBRO_PAGE = 30           # server returns at most 30 rows per call
KST = dt.timezone(dt.timedelta(hours=9))
TODAY = dt.datetime.now(KST).date()

NAVER_TAB = {1: "domestic", 2: "domestic", 3: "derivative", 4: "overseas",
             5: "commodity", 6: "bond", 7: "other"}


def log(*a):
    print(*a, flush=True)


# ------------------------------------------------------------------ SEIBro
def seibro_distributions():
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    page = ("https://seibro.or.kr/websquare/control.jsp?"
            "w2xPath=/IPORTAL/user/etf/BIP_CNTS06030V.xml&menuNo=179")
    s.get(page, timeout=30)
    api = "https://seibro.or.kr/websquare/engine/proworks/callServletService.jsp"
    hdr = {"Content-Type": "application/xml; charset=UTF-8", "Referer": page,
           "Origin": "https://seibro.or.kr"}
    fr = (TODAY - dt.timedelta(days=HISTORY_DAYS)).strftime("%Y%m%d")
    to = (TODAY + dt.timedelta(days=60)).strftime("%Y%m%d")

    def body(action, start, end):
        return ('<reqParam action="%s" task="ksd.safe.bip.cnts.etf.process.EtfExerInfoPTask">'
                '<MENU_NO value="179"/><CMM_BTN_ABBR_NM value=""/>'
                '<W2XPATH value="/IPORTAL/user/etf/BIP_CNTS06030V.xml"/>'
                '<etf_sort_level_cd value="0"/><etf_big_sort_cd value=""/>'
                '<START_PAGE value="%d"/><END_PAGE value="%d"/>'
                '<etf_sort_cd value=""/><isin value=""/><mngco_custno value=""/>'
                '<RGT_RSN_DTAIL_SORT_CD value=""/>'
                '<fromRGT_STD_DT value="%s"/><toRGT_STD_DT value="%s"/></reqParam>'
                % (action, start, end, fr, to)).encode("utf-8")

    def post(action, start, end):
        for attempt in range(4):
            try:
                r = s.post(api, data=body(action, start, end), headers=hdr, timeout=60)
                if r.status_code == 200:
                    return r.content.decode("utf-8", "replace")
            except requests.RequestException:
                pass
            time.sleep(2 + attempt * 2)
        raise RuntimeError(f"SEIBro request failed ({action} {start}-{end})")

    cnt_txt = post("exerInfoDtramtPayStatPlistCnt", 1, SEIBRO_PAGE)
    m = re.search(r'LIST_CNT value="(\d+)"', cnt_txt)
    total = int(m.group(1)) if m else 0
    log(f"SEIBro total rows: {total}")
    if total == 0:
        raise RuntimeError("SEIBro returned zero rows")

    rows, seen_pages = [], set()
    start = 1
    while start <= total + SEIBRO_PAGE:
        txt = post("exerInfoDtramtPayStatPlist", start, start + SEIBRO_PAGE - 1)
        batch = parse_rows(txt)
        if not batch:
            break
        sig = tuple((b.get("ISIN"), b.get("RGT_STD_DT")) for b in batch[:3])
        if sig in seen_pages:          # server ignored the offset -> stop, avoid duplicates
            log("SEIBro pagination repeated a page; stopping")
            break
        seen_pages.add(sig)
        rows.extend(batch)
        start += SEIBRO_PAGE
        time.sleep(0.4)
    log(f"SEIBro fetched rows: {len(rows)}")
    if len(rows) < total * 0.9:
        raise RuntimeError(f"SEIBro incomplete: {len(rows)} of {total}")

    out, dedup = [], set()
    for b in rows:
        isin = b.get("ISIN", "")
        key = (isin, b.get("RGT_STD_DT"), b.get("RGT_RSN_DTAIL_NM"))
        if key in dedup:
            continue
        dedup.add(key)
        try:
            amt = float(b.get("ESTM_STDPRC") or 0)
        except ValueError:
            amt = 0
        if amt <= 0 or len(isin) != 12:
            continue
        out.append({
            "isin": isin, "code": isin[3:9],
            "rec": b.get("RGT_STD_DT"), "pay": b.get("TH1_PAY_TERM_BEGIN_DT"),
            "amt": amt, "kind": b.get("RGT_RSN_DTAIL_NM", ""),
            "type": b.get("ETF_SORT_NM", ""), "issuer": b.get("REP_SECN_NM", ""),
            "name": b.get("KOR_SECN_NM", ""),
        })
    return out, total


def parse_rows(txt):
    rows = []
    for blk in re.findall(r"<result>(.*?)</result>", txt, re.S):
        rows.append({k: v for k, v in re.findall(r'<(\w+) value="([^"]*)"\s*/>', blk)})
    return rows


# ------------------------------------------------------------------ Naver
def naver_list():
    r = requests.get("https://finance.naver.com/api/sise/etfItemList.nhn",
                     headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    items = r.json()["result"]["etfItemList"]
    log(f"Naver ETF list: {len(items)}")
    return {it["itemcode"]: it for it in items}


# ------------------------------------------------------------------ FunETF
def funetf_one(isin):
    url = "https://www.funetf.co.kr/api/public/product/view/divBasic?itemId=" + isin
    for attempt in range(3):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Referer": "https://www.funetf.co.kr/"},
                             timeout=30)
            if r.status_code == 200:
                j = r.json()
                basic = j.get("basic") or {}
                lst = j.get("list") or []
                return isin, {
                    "ret1y": basic.get("suikRt12"),
                    "tax": {x.get("payGijunDt"): x.get("taxDivAmt") for x in lst
                            if x.get("payGijunDt") and x.get("taxDivAmt") is not None},
                }
        except (requests.RequestException, ValueError):
            pass
        time.sleep(1 + attempt)
    return isin, None


# ------------------------------------------------------------------ build
def build():
    dists, seibro_total = seibro_distributions()
    naver = naver_list()

    by_isin = {}
    for d in dists:
        by_isin.setdefault(d["isin"], []).append(d)

    window_start = (TODAY - dt.timedelta(days=365)).strftime("%Y%m%d")
    today_s = TODAY.strftime("%Y%m%d")
    candidates = []
    for isin, evs in by_isin.items():
        nv = naver.get(isin[3:9])
        if not nv:
            continue
        aum = nv.get("marketSum") or 0
        recent = [e for e in evs if window_start < e["rec"] <= today_s]
        if not recent or aum < MIN_AUM_EOK:
            continue
        candidates.append(isin)
    log(f"candidates (AUM >= {MIN_AUM_EOK} eok, paid in last 12m): {len(candidates)}")

    fun = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        for isin, res in ex.map(funetf_one, candidates):
            fun[isin] = res
    fun_ok = sum(1 for v in fun.values() if v)
    log(f"FunETF ok: {fun_ok}/{len(candidates)}")

    etfs = []
    for isin in candidates:
        nv = naver[isin[3:9]]
        evs = sorted(by_isin[isin], key=lambda e: e["rec"])
        f = fun.get(isin) or {}
        taxmap = f.get("tax") or {}
        price = nv.get("nowVal")
        hist = []
        for e in evs:
            t = taxmap.get(e["rec"])
            hist.append([e["rec"], e["pay"], e["amt"], t])
        recent = [h for h in hist if window_start < h[0] <= today_s]
        amt12 = sum(h[2] for h in recent)
        tax_known = [h for h in recent if h[3] is not None]
        tax_ratio = (sum(h[3] for h in tax_known) / sum(h[2] for h in tax_known)) \
            if tax_known and len(tax_known) == len(recent) and sum(h[2] for h in tax_known) > 0 else None
        name = nv.get("itemname") or evs[-1]["name"]
        etfs.append({
            "isin": isin, "code": isin[3:9], "name": name,
            "issuer": evs[-1]["issuer"], "seibro_type": evs[-1]["type"],
            "cat": NAVER_TAB.get(nv.get("etfTabCode"), "other"),
            "covered_call": ("커버드콜" in name) or ("프리미엄" in name),
            "price": price, "nav": nv.get("nav"), "aum_eok": nv.get("marketSum"),
            "ret3m": nv.get("threeMonthEarnRate"), "ret1y": f.get("ret1y"),
            "dist12": amt12, "cnt12": len(recent),
            "yield12": (amt12 / price) if price else None,
            "tax_ratio": tax_ratio,
            "short_history": evs[0]["rec"] > (TODAY - dt.timedelta(days=300)).strftime("%Y%m%d")
                             and len(recent) >= 2,
            "hist": hist,
        })
    etfs.sort(key=lambda x: -(x["yield12"] or 0))

    out = {
        "status": "ok",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "asof": today_s,
        "sources": {"seibro_rows": seibro_total, "naver": len(naver), "funetf_ok": fun_ok,
                    "candidates": len(candidates)},
        "min_aum_eok": MIN_AUM_EOK,
        "etfs": etfs,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"wrote {OUT} ({OUT.stat().st_size:,} bytes, {len(etfs)} ETFs)")


if __name__ == "__main__":
    try:
        build()
    except Exception as e:  # keep the previous data file on any hard failure
        log(f"BUILD FAILED, previous data kept: {e}")
        sys.exit(1)

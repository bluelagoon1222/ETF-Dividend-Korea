#!/usr/bin/env python3
"""
Access probe for ETF-Dividend-Korea.
Checks whether GitHub Actions (US runners) can reach each data source.
Writes probe/result.json. No API keys needed.
"""
import datetime as dt
import json
import re
import time
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent.parent / "probe" / "result.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
results = []


def record(name, ok, **kw):
    kw.update({"name": name, "ok": ok})
    results.append(kw)
    print(("OK   " if ok else "FAIL ") + name, {k: v for k, v in kw.items() if k not in ("sample",)})


def seibro():
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    page = "https://seibro.or.kr/websquare/control.jsp?w2xPath=/IPORTAL/user/etf/BIP_CNTS06030V.xml&menuNo=179"
    t = time.time()
    try:
        r = s.get(page, timeout=30)
        record("seibro_page", r.status_code == 200, status=r.status_code, sec=round(time.time() - t, 2))
    except Exception as e:
        record("seibro_page", False, error=str(e)[:300])
        return
    to_d = dt.date.today()
    fr_d = to_d - dt.timedelta(days=365)

    def body(action, end_page):
        return ('<reqParam action="%s" task="ksd.safe.bip.cnts.etf.process.EtfExerInfoPTask">'
                '<MENU_NO value="179"/><CMM_BTN_ABBR_NM value=""/>'
                '<W2XPATH value="/IPORTAL/user/etf/BIP_CNTS06030V.xml"/>'
                '<etf_sort_level_cd value="0"/><etf_big_sort_cd value=""/>'
                '<START_PAGE value="1"/><END_PAGE value="%d"/>'
                '<etf_sort_cd value=""/><isin value=""/><mngco_custno value=""/>'
                '<RGT_RSN_DTAIL_SORT_CD value=""/>'
                '<fromRGT_STD_DT value="%s"/><toRGT_STD_DT value="%s"/></reqParam>'
                % (action, end_page, fr_d.strftime("%Y%m%d"), to_d.strftime("%Y%m%d")))

    api = "https://seibro.or.kr/websquare/engine/proworks/callServletService.jsp"
    hdr = {"Content-Type": "application/xml; charset=UTF-8", "Referer": page,
           "Origin": "https://seibro.or.kr"}
    for action, end_page, label in (("exerInfoDtramtPayStatPlistCnt", 30, "seibro_count"),
                                    ("exerInfoDtramtPayStatPlist", 30, "seibro_page30"),
                                    ("exerInfoDtramtPayStatPlist", 5000, "seibro_bulk5000")):
        t = time.time()
        try:
            r = s.post(api, data=body(action, end_page).encode("utf-8"), headers=hdr, timeout=90)
            txt = r.content.decode("utf-8", "replace")
            rows = len(re.findall(r"<r>", txt))
            cnt = re.search(r'LIST_CNT value="(\d+)"', txt)
            first = re.search(r"<r>(.*?)</r>", txt, re.S)
            record(label, r.status_code == 200 and (rows > 0 or cnt is not None),
                   status=r.status_code, sec=round(time.time() - t, 2), rows=rows,
                   list_cnt=int(cnt.group(1)) if cnt else None, bytes=len(r.content),
                   sample=first.group(1)[:600] if first else txt[:400])
        except Exception as e:
            record(label, False, error=str(e)[:300])
        time.sleep(1)


def funetf():
    for isin in ("KR7069500007", "KR7102110004"):
        url = "https://www.funetf.co.kr/api/public/product/view/divBasic?itemId=" + isin
        t = time.time()
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Referer": "https://www.funetf.co.kr/"}, timeout=30)
            j = r.json()
            lst = j.get("list") or []
            record("funetf_divBasic_" + isin, r.status_code == 200 and bool(lst), status=r.status_code,
                   sec=round(time.time() - t, 2), rows=len(lst),
                   with_tax=sum(1 for x in lst if x.get("taxDivAmt") is not None),
                   sample=json.dumps(lst[:2], ensure_ascii=False)[:600])
        except Exception as e:
            record("funetf_divBasic_" + isin, False, error=str(e)[:300])
        time.sleep(1)


def naver():
    url = "https://finance.naver.com/api/sise/etfItemList.nhn"
    t = time.time()
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        j = r.json()
        items = j.get("result", {}).get("etfItemList", [])
        record("naver_etf_list", r.status_code == 200 and bool(items), status=r.status_code,
               sec=round(time.time() - t, 2), rows=len(items),
               sample=json.dumps(items[:1], ensure_ascii=False)[:600])
    except Exception as e:
        record("naver_etf_list", False, error=str(e)[:300])


if __name__ == "__main__":
    seibro()
    funetf()
    naver()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"run_at_utc": dt.datetime.utcnow().isoformat(timespec="seconds"),
                               "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", OUT)

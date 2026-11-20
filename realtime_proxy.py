#!/usr/bin/env python3
"""本機即時報價小服務（只用 Python 內建模組，不需 pip install）。

為什麼需要它：證交所 MIS、期交所 MIS 的即時報價不允許網頁（瀏覽器）直接跨網域讀取，
所以網頁改問這支只在你電腦上執行的小程式，再由它去向證交所／期交所拿報價。

用法：
    python realtime_proxy.py              # 預設只監聽 127.0.0.1:8765（只有你自己的電腦連得到）
    python realtime_proxy.py --port 8800

介面（網頁會自己呼叫）：
    GET /health
    GET /stock?ids=2330,6488          上市（tse）與上櫃（otc）自動判斷
    GET /futures?id=TX                台指期 TX、小型 MTX、電子 TE、金融 TF、微型 TMF、櫃買 GTF、非金電 XIF

資料來源：臺灣證券交易所基本市況報導網站（mis.twse.com.tw）、臺灣期貨交易所行情資訊網（mis.taifex.com.tw）。
報價僅供參考，可能有延遲，非投資建議。
"""
import argparse
import http.cookiejar
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TWSE_BASE = os.environ.get("RT_TWSE_BASE", "https://mis.twse.com.tw")
TAIFEX_BASE = os.environ.get("RT_TAIFEX_BASE", "https://mis.taifex.com.tw")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
CACHE_SECONDS = 3  # 同一組查詢 3 秒內直接回舊結果，避免把上游打爆

# FinMind 期貨代號 → 期交所 MIS 的商品代號（CID）
FUT_CID = {"TX": "TXF", "MTX": "MXF", "TMF": "TMF", "TE": "EXF", "TF": "FXF", "GTF": "GTF", "XIF": "XIF"}

_lock = threading.Lock()
_cache = {}
_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))
_twse_warm = [0.0]


class Upstream(Exception):
    pass


def _num(x):
    """'1,234.50' / '2330.0000' → float；'-'、空字串 → None"""
    if x is None:
        return None
    s = str(x).replace(",", "").strip()
    if s in ("", "-", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _first(x):
    """MIS 五檔欄位 '2050.0000_2055.0000_' → 2050.0"""
    if not x:
        return None
    return _num(str(x).split("_")[0])


def _cached(key, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (now, val)
    return val


def _open(req, timeout=10):
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise Upstream(f"上游 HTTP {e.code}")
    except Exception as e:  # 網路、逾時
        raise Upstream(f"連不上上游：{e}")


def _warm_twse():
    """證交所 MIS 要先有 session cookie，每 10 分鐘更新一次。"""
    if time.time() - _twse_warm[0] < 600:
        return
    req = urllib.request.Request(TWSE_BASE + "/stock/index?lang=zhHant", headers={"User-Agent": UA})
    try:
        _open(req)
    except Upstream:
        pass  # 沒拿到 cookie 也先試試看
    _twse_warm[0] = time.time()


def get_stocks(ids):
    ex = "|".join(f"{m}_{i}.tw" for i in ids for m in ("tse", "otc"))
    url = f"{TWSE_BASE}/stock/api/getStockInfo.jsp?ex_ch={urllib.parse.quote(ex, safe='|_.')}&json=1&delay=0&_={int(time.time()*1000)}"
    _warm_twse()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": TWSE_BASE + "/stock/index?lang=zhHant"})
    j = json.loads(_open(req).decode("utf-8", "replace"))
    out = []
    for m in j.get("msgArray") or []:
        last, prev = _num(m.get("z")), _num(m.get("y"))
        bid, ask = _first(m.get("b")), _first(m.get("a"))
        src = "last"
        price = last
        if price is None:  # 剛好這一刻沒有新成交：用買賣價中間值，再不行用昨收
            if bid is not None and ask is not None:
                price, src = round((bid + ask) / 2, 2), "mid"
            elif prev is not None:
                price, src = prev, "prev"
        chg = round(price - prev, 2) if price is not None and prev else None
        out.append({
            "id": m.get("c"), "name": m.get("n"), "market": m.get("ex"),
            "price": price, "priceSrc": src, "prev": prev, "change": chg,
            "changePct": round(chg / prev * 100, 2) if chg is not None and prev else None,
            "open": _num(m.get("o")), "high": _num(m.get("h")), "low": _num(m.get("l")),
            "volume": _num(m.get("v")),  # 累計成交量（張；指數則無）
            "bid": bid, "ask": ask, "upper": _num(m.get("u")), "lower": _num(m.get("w")),
            "date": m.get("d"), "time": m.get("t"),
        })
    return {"ok": True, "source": "TWSE MIS", "data": out, "serverTime": time.strftime("%H:%M:%S")}


def _fut_row(x):
    price, prev = _num(x.get("CLastPrice")), _num(x.get("CRefPrice"))
    return {
        "symbol": x.get("SymbolID"), "name": x.get("DispCName"),
        "price": price, "prev": prev,
        "change": _num(x.get("CDiff")), "changePct": _num(x.get("CDiffRate")),
        "open": _num(x.get("COpenPrice")), "high": _num(x.get("CHighPrice")), "low": _num(x.get("CLowPrice")),
        "volume": _num(x.get("CTotalVolume")), "openInterest": _num(x.get("OpenInterest")),
        "bid": _num(x.get("CBestBidPrice")), "ask": _num(x.get("CBestAskPrice")),
        "date": x.get("CDate"), "time": x.get("CTime"),
    }


def get_futures(fid):
    cid = FUT_CID.get(fid.upper())
    if not cid:
        raise Upstream(f"不認得的期貨代碼 {fid}；支援 " + "、".join(FUT_CID))
    body = json.dumps({
        "MarketType": "0", "SymbolType": "F", "KindID": "1", "CID": cid,
        "ExpireMonth": "", "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A",
    }).encode()
    req = urllib.request.Request(TAIFEX_BASE + "/futures/api/getQuoteList", data=body, method="POST", headers={
        "User-Agent": UA, "Content-Type": "application/json",
        "Origin": TAIFEX_BASE, "Referer": TAIFEX_BASE + "/futures/",
    })
    j = json.loads(_open(req).decode("utf-8", "replace"))
    rows = (j.get("RtData") or {}).get("QuoteList") or []
    spot = next((_fut_row(x) for x in rows if str(x.get("SymbolID", "")).endswith("-S")), None)
    # 單一月份合約（排除價差單，其代號含 "/"），期交所依到期月由近到遠排序
    cons = [_fut_row(x) for x in rows if str(x.get("SymbolID", "")).endswith("-F") and "/" not in str(x.get("SymbolID", ""))]
    if not cons:
        raise Upstream("期交所沒有回傳合約（可能是非交易時段或代碼錯誤）")
    near = next((c for c in cons if c["price"] is not None), cons[0])
    return {"ok": True, "source": "TAIFEX MIS", "id": fid.upper(), "spot": spot, "near": near,
            "contracts": cons[:6], "serverTime": time.strftime("%H:%M:%S")}


class Handler(BaseHTTPRequestHandler):
    server_version = "RealtimeProxy/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _send(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")  # 讓 https 網頁可連到本機

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/health":
                return self._send(200, {"ok": True, "service": "realtime_proxy", "time": time.strftime("%H:%M:%S")})
            if u.path == "/stock":
                ids = [i for i in re.split(r"[,\s]+", (q.get("ids") or [""])[0]) if i]
                if not ids or len(ids) > 20 or not all(re.fullmatch(r"[0-9A-Za-z]{3,6}", i) for i in ids):
                    return self._send(400, {"ok": False, "error": "ids 需為 1–20 個股票代碼"})
                return self._send(200, _cached(("s", tuple(ids)), lambda: get_stocks(ids)))
            if u.path == "/futures":
                fid = (q.get("id") or [""])[0]
                if not re.fullmatch(r"[A-Za-z]{2,4}", fid):
                    return self._send(400, {"ok": False, "error": "id 需為期貨代碼，例如 TX"})
                return self._send(200, _cached(("f", fid.upper()), lambda: get_futures(fid)))
            self._send(404, {"ok": False, "error": "not found"})
        except Upstream as e:
            self._send(502, {"ok": False, "error": str(e)})
        except Exception as e:
            self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})


def main(argv=None):
    ap = argparse.ArgumentParser(description="本機即時報價小服務")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="預設只開放本機；不建議改成 0.0.0.0")
    a = ap.parse_args(argv)
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"即時報價服務已啟動：http://{a.host}:{a.port}  （關閉這個視窗或按 Ctrl+C 即停止）")
    print("網頁會自動連線；盤中約每 5 秒更新一次。資料來源：證交所 MIS、期交所 MIS，僅供參考。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

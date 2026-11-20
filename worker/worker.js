// Cloudflare Worker：即時報價轉接服務（證交所 MIS／期交所 MIS → 加上 CORS 後回給網頁）
// 與 realtime_proxy.py 功能相同，端點也相同：/health、/stock?ids=2330,6488、/futures?id=TX
// 只允許下列來源的網頁呼叫（避免別人拿你的額度）；要加自己的網域，改 ALLOW 即可。
const ALLOW = [/^https:\/\/leocpa\.github\.io$/, /^null$/, /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/];
const TWSE = "https://mis.twse.com.tw";
const TAIFEX = "https://mis.taifex.com.tw";
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36";
const FUT_CID = { TX: "TXF", MTX: "MXF", TMF: "TMF", TE: "EXF", TF: "FXF", GTF: "GTF", XIF: "XIF" };
const CACHE_SECONDS = 3;

class Upstream extends Error {}
const num = (x) => {
  if (x == null) return null;
  const s = String(x).replace(/,/g, "").trim();
  if (s === "" || s === "-" || s === "--") return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
};
const first = (x) => (x ? num(String(x).split("_")[0]) : null);
const r2 = (x) => Math.round(x * 100) / 100;
const hhmmss = () => new Date().toLocaleTimeString("sv-SE", { timeZone: "Asia/Taipei" });

async function getJson(url, init) {
  let r;
  try { r = await fetch(url, init); } catch (e) { throw new Upstream("連不上上游：" + e.message); }
  if (!r.ok) throw new Upstream("上游 HTTP " + r.status);
  try { return await r.json(); } catch (e) { throw new Upstream("上游回傳不是 JSON（可能被擋或維護中）"); }
}

async function twseCookie() {
  try {
    const r = await fetch(TWSE + "/stock/index?lang=zhHant", { headers: { "User-Agent": UA } });
    const sc = r.headers.get("set-cookie") || "";
    return sc.split(/,(?=\s*[\w.-]+=)/).map((c) => c.split(";")[0].trim()).filter(Boolean).join("; ");
  } catch (e) { return ""; }
}

async function getStocks(ids) {
  const ex = ids.flatMap((i) => ["tse", "otc"].map((m) => `${m}_${i}.tw`)).join("|");
  const cookie = await twseCookie();
  const j = await getJson(`${TWSE}/stock/api/getStockInfo.jsp?ex_ch=${encodeURIComponent(ex).replace(/%7C/g, "|")}&json=1&delay=0&_=${Date.now()}`,
    { headers: { "User-Agent": UA, Referer: TWSE + "/stock/index?lang=zhHant", Cookie: cookie } });
  const data = (j.msgArray || []).map((m) => {
    const last = num(m.z), prev = num(m.y), bid = first(m.b), ask = first(m.a);
    let price = last, priceSrc = "last";
    if (price == null) {
      if (bid != null && ask != null) { price = r2((bid + ask) / 2); priceSrc = "mid"; }
      else if (prev != null) { price = prev; priceSrc = "prev"; }
    }
    const change = price != null && prev ? r2(price - prev) : null;
    return {
      id: m.c, name: m.n, market: m.ex, price, priceSrc, prev, change,
      changePct: change != null && prev ? r2((change / prev) * 100) : null,
      open: num(m.o), high: num(m.h), low: num(m.l), volume: num(m.v),
      bid, ask, upper: num(m.u), lower: num(m.w), date: m.d, time: m.t,
    };
  });
  return { ok: true, source: "TWSE MIS", data, serverTime: hhmmss() };
}

const futRow = (x) => ({
  symbol: x.SymbolID, name: x.DispCName, price: num(x.CLastPrice), prev: num(x.CRefPrice),
  change: num(x.CDiff), changePct: num(x.CDiffRate), open: num(x.COpenPrice), high: num(x.CHighPrice), low: num(x.CLowPrice),
  volume: num(x.CTotalVolume), openInterest: num(x.OpenInterest), bid: num(x.CBestBidPrice), ask: num(x.CBestAskPrice),
  date: x.CDate, time: x.CTime,
});

async function getFutures(fid) {
  const cid = FUT_CID[fid.toUpperCase()];
  if (!cid) throw new Upstream("不認得的期貨代碼 " + fid + "；支援 " + Object.keys(FUT_CID).join("、"));
  const j = await getJson(TAIFEX + "/futures/api/getQuoteList", {
    method: "POST",
    headers: { "User-Agent": UA, "Content-Type": "application/json", Origin: TAIFEX, Referer: TAIFEX + "/futures/" },
    body: JSON.stringify({ MarketType: "0", SymbolType: "F", KindID: "1", CID: cid, ExpireMonth: "", RowSize: "全部", PageNo: "", SortColumn: "", AscDesc: "A" }),
  });
  const rows = (j.RtData && j.RtData.QuoteList) || [];
  const spotRaw = rows.find((x) => String(x.SymbolID || "").endsWith("-S"));
  const cons = rows.filter((x) => { const s = String(x.SymbolID || ""); return s.endsWith("-F") && !s.includes("/"); }).map(futRow);
  if (!cons.length) throw new Upstream("期交所沒有回傳合約（可能是非交易時段或代碼錯誤）");
  const near = cons.find((c) => c.price != null) || cons[0];
  return { ok: true, source: "TAIFEX MIS", id: fid.toUpperCase(), spot: spotRaw ? futRow(spotRaw) : null, near, contracts: cons.slice(0, 6), serverTime: hhmmss() };
}

export default {
  async fetch(req) {
    const origin = req.headers.get("Origin");
    const allowed = !origin || ALLOW.some((re) => re.test(origin));
    const cors = {
      "Access-Control-Allow-Origin": origin || "*", "Vary": "Origin",
      "Access-Control-Allow-Methods": "GET, OPTIONS", "Access-Control-Allow-Headers": "*",
    };
    const send = (code, obj) => new Response(JSON.stringify(obj), {
      status: code, headers: { ...cors, "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
    });
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    if (!allowed) return send(403, { ok: false, error: "此來源不被允許：" + origin });
    const u = new URL(req.url);
    try {
      if (u.pathname === "/health") return send(200, { ok: true, service: "realtime_worker", time: hhmmss() });
      const cache = typeof caches !== "undefined" ? caches.default : null;
      const ckey = new Request(u.origin + u.pathname + u.search);
      if (cache) { const hit = await cache.match(ckey); if (hit) return new Response(hit.body, { status: hit.status, headers: { ...cors, "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" } }); }
      let out;
      if (u.pathname === "/stock") {
        const ids = (u.searchParams.get("ids") || "").split(/[,\s]+/).filter(Boolean);
        if (!ids.length || ids.length > 20 || !ids.every((i) => /^[0-9A-Za-z]{3,6}$/.test(i))) return send(400, { ok: false, error: "ids 需為 1–20 個股票代碼" });
        out = await getStocks(ids);
      } else if (u.pathname === "/futures") {
        const fid = u.searchParams.get("id") || "";
        if (!/^[A-Za-z]{2,4}$/.test(fid)) return send(400, { ok: false, error: "id 需為期貨代碼，例如 TX" });
        out = await getFutures(fid);
      } else return send(404, { ok: false, error: "not found" });
      const resp = send(200, out);
      if (cache) { const c = resp.clone(); await cache.put(ckey, new Response(c.body, { status: 200, headers: { "Cache-Control": `max-age=${CACHE_SECONDS}`, "Content-Type": "application/json; charset=utf-8" } })); }
      return resp;
    } catch (e) {
      return send(e instanceof Upstream ? 502 : 500, { ok: false, error: e instanceof Upstream ? e.message : `${e.name}: ${e.message}` });
    }
  },
};

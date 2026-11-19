const INDEX_HTML = __INDEX_HTML__;
const MANIFEST = __MANIFEST__;
const ICON = __ICON__;
const ICON_FILES = __ICON_FILES__;
const SERVICE_WORKER = __SERVICE_WORKER__;

const GROUPS = {
  sp500: ["AAPL","MSFT","NVDA","AMZN","GOOG","META","BRK-B","AVGO","TSLA","JPM","LLY","WMT","V","XOM","UNH","MA","COST","NFLX","ORCL","HD"],
  healthcare: ["LLY","UNH","JNJ","ABBV","MRK","TMO","ABT","ISRG","AMGN","GILD","VRTX","SYK"],
  financial: ["JPM","BAC","WFC","GS","MS","C","BLK","AXP","SCHW","COF"],
  telecom: ["TMUS","VZ","T","CHTR","LUMN","FYBR","USM","TDS"],
  consumer: ["AMZN","WMT","COST","HD","MCD","LOW","NKE","SBUX","TGT","PG","KO","PEP"],
  energy: ["XOM","CVX","COP","EOG","SLB","MPC","PSX","OXY","VLO","KMI"]
};
const GROUP_LABELS = {sp500:"S&P 500 representative",healthcare:"Healthcare",financial:"Financials",telecom:"Telecom operators",consumer:"Consumer products",energy:"Energy"};
const COOKIE = "denny888_holdings";
const cache = new Map();

function json(value, status = 200, headers = {}) {
  return new Response(JSON.stringify(value), {status, headers:{"content-type":"application/json; charset=utf-8","cache-control":"no-store",...headers}});
}
function validSymbol(value) {
  const symbol = String(value || "").trim().toUpperCase();
  if (!symbol || symbol.length > 20 || !/^[A-Z0-9.\-^=]+$/.test(symbol)) throw new Error("請輸入有效的美股代號，例如 AAPL 或 BRK-B。");
  return symbol;
}
function decodeHoldings(request) {
  const raw = request.headers.get("cookie") || "";
  const found = raw.split(/;\s*/).find(v => v.startsWith(`${COOKIE}=`));
  if (!found) return [];
  try {
    const encoded = found.slice(COOKIE.length + 1).replace(/-/g,"+").replace(/_/g,"/");
    const text = decodeURIComponent(escape(atob(encoded)));
    const rows = JSON.parse(text);
    return Array.isArray(rows) ? rows : [];
  } catch { return []; }
}
function holdingsCookie(rows) {
  const text = unescape(encodeURIComponent(JSON.stringify(rows)));
  const encoded = btoa(text).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/g,"");
  return `${COOKIE}=${encoded}; Path=/; Max-Age=31536000; SameSite=Lax; Secure`;
}
function ema(values, span) {
  const alpha = 2 / (span + 1); let previous = values[0];
  return values.map((value, index) => index ? (previous = value * alpha + previous * (1-alpha)) : previous);
}
function enrich(points) {
  const closes = points.map(p => p.close), e12 = ema(closes,12), e26 = ema(closes,26);
  const macd = closes.map((_,i)=>e12[i]-e26[i]), signal = ema(macd,9);
  return points.map((p,i)=>({...p,macd:macd[i],signal:signal[i],histogram:macd[i]-signal[i]}));
}
async function history(symbol) {
  symbol = validSymbol(symbol);
  const cached = cache.get(symbol);
  if (cached && Date.now() - cached.at < 240000) return cached.data;
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}?range=1y&interval=1d&events=history&includeAdjustedClose=true`;
  const response = await fetch(url, {headers:{"user-agent":"Mozilla/5.0 Denny888/1.0"}});
  if (!response.ok) throw new Error(`行情服務暫時無法取得 ${symbol}`);
  const payload = await response.json();
  const result = payload?.chart?.result?.[0];
  const quote = result?.indicators?.quote?.[0], timestamps = result?.timestamp || [];
  if (!quote || timestamps.length < 35) throw new Error("可用的日線資料不足。");
  const points = timestamps.map((ts,i)=>({
    date:new Date(ts*1000).toISOString().slice(0,10), open:Number(quote.open?.[i]), high:Number(quote.high?.[i]),
    low:Number(quote.low?.[i]), close:Number(quote.close?.[i]), volume:Number(quote.volume?.[i]||0)
  })).filter(p=>Number.isFinite(p.close) && Number.isFinite(p.open) && Number.isFinite(p.high) && Number.isFinite(p.low));
  const data = enrich(points);
  cache.set(symbol,{at:Date.now(),data}); return data;
}
function events(points) {
  const rows=[];
  for(let i=1;i<points.length;i++){
    const a=points[i-1],b=points[i];
    if(a.macd<=a.signal&&b.macd>b.signal)rows.push({kind:"golden",label:"黃金交叉",date:b.date});
    if(a.macd>=a.signal&&b.macd<b.signal)rows.push({kind:"death",label:"死亡交叉",date:b.date});
    if(a.macd<=0&&b.macd>0)rows.push({kind:"positive",label:"MACD 負轉正",date:b.date});
    if(a.macd>=0&&b.macd<0)rows.push({kind:"negative",label:"MACD 正轉負",date:b.date});
  }
  return rows;
}
async function detail(symbol) {
  const points=(await history(symbol)).slice(-120); return {symbol:validSymbol(symbol),date:points.at(-1)?.date||"",points,events:events(points).reverse()};
}
async function quoteHolding(holding) {
  const points=await history(holding.symbol), latest=points.at(-1), previous=points.at(-2);
  const price=latest.close, shares=Number(holding.shares||0), cost=Number(holding.cost||0);
  return {symbol:holding.symbol,shares,cost,price,value:shares?price*shares:null,pnl:shares&&cost?(price-cost)*shares:null,
    pnl_pct:cost?(price/cost-1)*100:null,change_pct:previous?(price/previous.close-1)*100:0,
    macd:latest.macd,signal:latest.signal,histogram:latest.histogram,date:latest.date};
}
async function api(request, url) {
  const path=url.pathname, method=request.method;
  if(path==="/api/holdings"&&method==="GET") return json(decodeHoldings(request));
  if(path==="/api/holdings"&&method==="POST"){
    try { const body=await request.json(), symbol=validSymbol(body.symbol), shares=Number(body.shares), cost=Number(body.cost);
      if(!(shares>0)||cost<0)throw new Error("股數須大於 0，持有成本不可小於 0。");
      const rows=decodeHoldings(request).filter(x=>x.symbol!==symbol); rows.push({symbol,shares,cost,source:"app"}); rows.sort((a,b)=>a.symbol.localeCompare(b.symbol));
      return json({symbol,shares,cost},200,{"set-cookie":holdingsCookie(rows)});
    } catch(error){return json({error:error.message},400);}
  }
  if(path.startsWith("/api/holdings/")&&method==="DELETE"){
    try {const symbol=validSymbol(decodeURIComponent(path.slice(14))),rows=decodeHoldings(request).filter(x=>x.symbol!==symbol);return json({ok:true},200,{"set-cookie":holdingsCookie(rows)});}catch(error){return json({error:error.message},400);}
  }
  if(path==="/api/quotes"){
    const holdings=decodeHoldings(request), settled=await Promise.allSettled(holdings.map(quoteHolding));
    const quotes=[],errors=[]; settled.forEach((item,i)=>item.status==="fulfilled"?quotes.push(item.value):errors.push({symbol:holdings[i].symbol,error:item.reason?.message||"行情錯誤"}));
    return json({quotes,errors,sheet:{count:0,error:null},holding_count:holdings.length});
  }
  if(path.startsWith("/api/history/")){
    try {const symbol=validSymbol(decodeURIComponent(path.slice(13))),points=(await history(symbol)).slice(-120);return json({symbol,points});}catch(error){return json({error:error.message},400);}
  }
  if(path.startsWith("/api/stock-detail/")){
    try{return json(await detail(decodeURIComponent(path.slice(18))));}catch(error){return json({error:error.message},400);}
  }
  if(path==="/api/alerts"){
    const holdings=decodeHoldings(request),rows=[];
    for(const holding of holdings){try{const points=await history(holding.symbol);for(const event of events(points.slice(-120)).slice(-30)){const p=points.find(x=>x.date===event.date);rows.push({symbol:holding.symbol,kind:event.kind,label:event.label,date:event.date,macd:p?.macd||0,signal:p?.signal||0});}}catch{}}
    return json(rows.sort((a,b)=>b.date.localeCompare(a.date)).slice(0,100));
  }
  if(path==="/api/scan"&&method==="POST"){
    const holdings=decodeHoldings(request),alerts=[],errors=[];
    for(const holding of holdings){try{const points=await history(holding.symbol);const latest=events(points.slice(-3));for(const e of latest){const p=points.find(x=>x.date===e.date);alerts.push({symbol:holding.symbol,kind:e.kind,label:e.label,date:e.date,macd:p.macd,signal:p.signal});}}catch(error){errors.push({symbol:holding.symbol,error:error.message});}}
    return json({alerts,errors});
  }
  if(path==="/api/sync-sheet"&&method==="POST") return json({count:0,new:0,error:"Codex Site 版本使用此瀏覽器的本機持股清單。"});
  if(path==="/api/market-scan"){
    const group=url.searchParams.get("group")||"healthcare", symbols=GROUPS[group]||GROUPS.healthcare;
    const settled=await Promise.allSettled(symbols.map(async symbol=>{const points=await history(symbol),p=points.at(-1),prev=points.at(-2),v=points.slice(-21,-1).reduce((s,x)=>s+x.volume,0)/20,recent=points.slice(-5),baseline=points.slice(-35,-5),baselineVolume=baseline.length===30?baseline.reduce((s,x)=>s+x.volume,0)/30:0,maxVolumeDay=recent.reduce((best,x)=>x.volume>best.volume?x:best,recent[0]||p),spike=baselineVolume>0&&maxVolumeDay.volume>1.5*baselineVolume;
      const ev=events(points.slice(-120)),last=k=>[...ev].reverse().find(x=>x.kind===k)?.date||"";
      return {symbol,close:p.close,change_pct:prev?(p.close/prev.close-1)*100:0,golden:last("golden"),death:last("death"),positive:last("positive"),negative:last("negative"),volume:p.volume,vol_pct:baselineVolume?(maxVolumeDay.volume/baselineVolume-1)*100:0,spike,spike_dates:spike?[maxVolumeDay.date]:[],avg_dollar_volume:v*p.close,macd:p.macd,signal:p.signal,date:p.date};}));
    const rows=settled.filter(x=>x.status==="fulfilled").map(x=>x.value); if(!rows.length)return json({error:"目前沒有可用行情，請稍後重試。"},503);
    return json({group,group_label:GROUP_LABELS[group]||group,as_of:rows.map(x=>x.date).sort().at(-1),rows,count:rows.length,volume_threshold:1.5,volume_window:5,volume_baseline_window:30});
  }
  return json({error:"Not found"},404);
}

export default {
  async fetch(request) {
    const url=new URL(request.url);
    try {
      if(url.pathname.startsWith("/api/")) return await api(request,url);
      if(url.pathname==="/manifest.webmanifest")return new Response(MANIFEST,{headers:{"content-type":"application/manifest+json"}});
      if(url.pathname==="/icon.svg")return new Response(ICON,{headers:{"content-type":"image/svg+xml"}});
      if (ICON_FILES[url.pathname]) {
        const bytes = Uint8Array.from(atob(ICON_FILES[url.pathname]), char => char.charCodeAt(0));
        const type = url.pathname === "/favicon.ico" ? "image/x-icon" : "image/png";
        return new Response(bytes, {headers:{"content-type":type}});
      }
      if(url.pathname==="/sw.js")return new Response(SERVICE_WORKER,{headers:{"content-type":"application/javascript","service-worker-allowed":"/"}});
      return new Response(INDEX_HTML,{headers:{"content-type":"text/html; charset=utf-8","cache-control":"no-cache"}});
    } catch(error){return json({error:error?.message||"Unexpected error"},500);}
  }
};

#!/usr/bin/env python3
"""CS2 daily scanner scaffold.

Uses the confirmed SteamDT candle order: [timestamp, open, close, high, low].
The API authentication mode is configurable via STEAMDT_AUTH_MODE.
"""
import csv, json, os, sys, time, urllib.request, urllib.error, urllib.parse
from datetime import date, datetime

API_HOST = os.getenv("STEAMDT_API_HOST", "https://open.steamdt.com")
API_PATH = os.getenv("STEAMDT_API_PATH", "/open/cs2/item/v1/kline")
API_KEY = os.environ.get("STEAMDT_API_KEY", "")
AUTH_MODE = os.getenv("STEAMDT_AUTH_MODE", "bearer")
ITEMS = os.getenv("ITEMS_FILE", "data/steamdt_selected_items.csv")
RAW = os.getenv("RAW_FILE", "data/steamdt_daily_1y_raw.csv")
PLATFORM = os.getenv("STEAMDT_PLATFORM", "YOUPIN")

if not API_KEY:
    raise SystemExit("STEAMDT_API_KEY is not set")

def headers():
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if AUTH_MODE.lower() == "x-api-key": h["X-API-KEY"] = API_KEY
    else: h["Authorization"] = "Bearer " + API_KEY
    return h

def request_item(name):
    body = json.dumps({"marketHashName": name, "type": 1, "platform": PLATFORM, "specialStyle": ""}).encode()
    req = urllib.request.Request(API_HOST + API_PATH, data=body, headers=headers(), method="POST")
    with urllib.request.urlopen(req, timeout=45) as r:
        obj = json.loads(r.read().decode("utf-8"))
    if not obj.get("success", True):
        raise RuntimeError(f"API failed for {name}: {obj}")
    return obj.get("data") or []

def load_rows():
    rows = []
    if not os.path.exists(RAW): return rows
    with open(RAW, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            try: rows.append({"marketHashName":r["marketHashName"],"group":r["group"],"date":r["date"],"timestamp":r.get("timestamp",""),"open":float(r["v1"]),"close":float(r["v2"]),"high":float(r["v3"]),"low":float(r["v4"])})
            except (KeyError, ValueError): pass
    return rows

def merge_api(rows, name, group, values):
    by={(r["marketHashName"],r["date"]):r for r in rows}
    for a in values:
        if not isinstance(a,(list,tuple)) or len(a)<5: continue
        ts=int(a[0]); dt=datetime.utcfromtimestamp(ts).date().isoformat()
        try: vals=list(map(float,a[1:5]))
        except (TypeError,ValueError): continue
        by[(name,dt)]={"marketHashName":name,"group":group,"date":dt,"timestamp":str(ts),"open":vals[0],"close":vals[1],"high":vals[2],"low":vals[3]}
    return list(by.values())

def cross(a,b,i): return i>0 and a[i]>b[i] and a[i-1]<=b[i-1]
def ema(xs, span):
    k=2/(span+1); out=[xs[0]]
    for x in xs[1:]: out.append(x*k+out[-1]*(1-k))
    return out
def sma(xs,p,i): return sum(xs[i-p+1:i+1])/p if i>=p-1 else None
def signal_types(g):
    c=[x["close"] for x in g]; o=[x["open"] for x in g]; h=[x["high"] for x in g]; l=[x["low"] for x in g]; i=len(g)-1
    if i<35: return []
    out=[]
    # MACD double golden cross (standard and fast); KDJ cross is approximated from RSV smoothed values.
    def macd(fast,slow,sig):
        dif=[a-b for a,b in zip(ema(c,fast),ema(c,slow))]; dea=ema(dif,sig)
        k=[]; d=[]
        for j in range(len(c)):
            lo=min(l[max(0,j-8):j+1]); hi=max(h[max(0,j-8):j+1]); r=50 if hi==lo else 100*(c[j]-lo)/(hi-lo)
            k.append(r if j==0 else k[-1]*2/3+r/3); d.append(k[j] if j==0 else d[-1]*2/3+k[j]/3)
        return cross(dif,dea,i) and any(cross(k,d,j) for j in range(max(1,i-2),i+1))
    if macd(12,26,9) or macd(5,12,4): out.append("双金叉")
    # Two MA families: all pairwise crosses within the last 3 bars, final bullish ordering.
    def triple(ps):
        ma=[[sma(c,p,j) for j in range(len(c))] for p in ps]
        if any(v is None for v in [ma[0][i],ma[1][i],ma[2][i]]) or not (ma[0][i]>ma[1][i]>ma[2][i]): return False
        return any(ma[0][j] is not None and ma[1][j] is not None and ma[2][j] is not None and ma[0][j]>ma[1][j] and ma[1][j]>ma[2][j] and (j==0 or not (ma[0][j-1]>ma[1][j-1]>ma[2][j-1])) for j in range(max(0,i-2),i+1))
    if triple((5,12,21)) or triple((7,14,21)): out.append("均线合一")
    body=abs(c[i-1]-o[i-1]); upper=h[i-1]-max(o[i-1],c[i-1])
    if upper>=body and upper>=.01*c[i-1] and c[i]>h[i-1]: out.append("上影吞没")
    lower=min(o[i],c[i])-l[i]; body=abs(c[i]-o[i])
    # A confirmed two/three-day needle requires today's bar to be the follow-through bar.
    for lag,label in [(2,"2日承接"),(3,"3日承接")]:
        j=i-lag
        if j>=0 and min(o[j],c[j])-l[j]>=2*abs(c[j]-o[j]) and min(o[j],c[j])-l[j]>=.02*c[j] and (c[j]-l[j])/max(h[j]-l[j],1e-9)>=.6 and min(l[j+1:i+1])>=l[j] and c[i]>=c[j]:
            out.append("单针承接"); break
    return list(dict.fromkeys(out))

def push(text):
    token=API_KEY # never use API key as Telegram token
    tg=os.environ.get("TELEGRAM_BOT_TOKEN"); chat=os.environ.get("TELEGRAM_CHAT_ID")
    if not tg or not chat: print(text); return
    body=urllib.parse.urlencode({"chat_id":chat,"text":text}).encode()
    req=urllib.request.Request(f"https://api.telegram.org/bot{tg}/sendMessage",data=body,method="POST")
    urllib.request.urlopen(req,timeout=30).read()

def main():
    rows=load_rows(); items=[]
    with open(ITEMS,encoding="utf-8-sig",newline="") as f: items=list(csv.DictReader(f))
    for n,it in enumerate(items,1):
        try: rows=merge_api(rows,it["marketHashName"],it["group"],request_item(it["marketHashName"]))
        except Exception as e: print(f"WARN {it['marketHashName']}: {e}",file=sys.stderr)
        if n%50==0: print(f"fetched {n}/{len(items)}")
    os.makedirs(os.path.dirname(RAW) or ".",exist_ok=True)
    with open(RAW,"w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=["marketHashName","group","date","timestamp","v1","v2","v3","v4"]); w.writeheader()
        for r in sorted(rows, key=lambda x: (x["marketHashName"], x["date"])):
            w.writerow({
                "marketHashName": r["marketHashName"],
                "group": r["group"],
                "date": r["date"],
                "timestamp": r["timestamp"],
                "v1": r["open"],
                "v2": r["close"],
                "v3": r["high"],
                "v4": r["low"],
            })

    latest = max(r["date"] for r in rows)
    found = []
        g=sorted([r for r in rows if r["marketHashName"]==name],key=lambda x:x["date"])
        if g[-1]["date"]!=latest or len(g)<300: continue
        types=signal_types(g)
        if len(types)>=2 and 30<=g[-1]["close"]<=13000: found.append((name,g[0]["group"],g[-1]["close"],types))
    if found:
        msg=f"CS2 扫盘 {latest}\n去除刀皮｜双/三买点共振\n"+"\n".join(f"{n}｜{grp}｜{p:.2f}\n  {' + '.join(t)}（{len(t)}买点）" for n,grp,p,t in found)
    else: msg=f"CS2 扫盘 {latest}\n未发现满足条件的双/三买点共振。"
    print(msg); push(msg)
if __name__ == "__main__": main()

#!/usr/bin/env python3
"""CS2 daily scanner scaffold.

Uses the confirmed SteamDT candle order: [timestamp, open, close, high, low].
The API authentication mode is configurable via STEAMDT_AUTH_MODE.
"""
import csv, json, os, sys, time, urllib.request, urllib.error, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    rows = load_rows()

    # 排除历史数据中的全部刀皮
    rows = [
        row for row in rows
        if row.get("group") != "knife_FN"
    ]

    with open(ITEMS, encoding="utf-8-sig", newline="") as f:
        items = list(csv.DictReader(f))

    # API 请求也排除刀皮
    items = [
        item for item in items
        if item.get("group") != "knife_FN"
    ]

    def fetch_one(item):
        return item, request_item(item["marketHashName"])

    total = len(items)
    print(f"items to fetch: {total}")

    # 并发请求，避免 2000 多个饰品顺序请求耗时过长
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(fetch_one, item)
            for item in items
        ]

        for n, future in enumerate(as_completed(futures), 1):
            try:
                item, data = future.result()
                rows = merge_api(
                    rows,
                    item["marketHashName"],
                    item["group"],
                    data,
                )
            except Exception as e:
                print(f"WARN fetch failed: {e}", file=sys.stderr)

            if n % 50 == 0 or n == total:
                print(f"fetched {n}/{total}")

    if not rows:
        raise RuntimeError("No K-line data received")

    os.makedirs(os.path.dirname(RAW) or ".", exist_ok=True)

    with open(RAW, "w", encoding="utf-8-sig", newline="") as f:
        fieldnames = [
            "marketHashName",
            "group",
            "date",
            "timestamp",
            "v1",
            "v2",
            "v3",
            "v4",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for row in sorted(
            rows,
            key=lambda x: (x["marketHashName"], x["date"]),
        ):
            writer.writerow({
                "marketHashName": row["marketHashName"],
                "group": row["group"],
                "date": row["date"],
                "timestamp": row["timestamp"],
                "v1": row["open"],
                "v2": row["close"],
                "v3": row["high"],
                "v4": row["low"],
            })

    latest = max(row["date"] for row in rows)
    found = []

    names = {
        row["marketHashName"]
        for row in rows
        if row.get("group") != "knife_FN"
    }

    for name in names:
        group_rows = sorted(
            [
                row for row in rows
                if row["marketHashName"] == name
                and row.get("group") != "knife_FN"
            ],
            key=lambda x: x["date"],
        )

        if len(group_rows) < 300:
            continue

        if group_rows[-1]["date"] != latest:
            continue

        types = signal_types(group_rows)

        # 单买点、双买点、三买点全部推送；价格范围为 30–13000
        if (
            len(types) >= 1
            and 30 <= group_rows[-1]["close"] <= 13000
        ):
            found.append((
                name,
                group_rows[0]["group"],
                group_rows[-1]["close"],
                types,
            ))

    if found:
        found.sort(key=lambda x: (-len(x[3]), x[0]))
        lines = [
            f"CS2 扫盘 {latest}",
            "去除刀皮｜单/双/三买点",
            "",
        ]

        for name, group, price, types in found:
            level = f"{len(types)}买点"
            lines.append(f"{name}｜{group}｜{price:.2f}")
            lines.append(
                f"  {level}：{' + '.join(types)}"
            )

        message = "\n".join(lines)
    else:
        message = (
            f"CS2 扫盘 {latest}\n"
            "未发现满足条件的单/双/三买点。"
        )

    print(message)
    push(message)


if __name__ == "__main__":
    main()

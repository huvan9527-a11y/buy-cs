#!/usr/bin/env python3
"""CS2 daily scanner scaffold.

Uses the confirmed SteamDT candle order: [timestamp, open, close, high, low].
The API authentication mode is configurable via STEAMDT_AUTH_MODE.
"""
import csv, json, os, sys, time, urllib.request, urllib.error, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import date, datetime, timezone, timedelta

SHANGHAI_TZ = timezone(timedelta(hours=8))

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
    body = json.dumps({"marketHashName": name, "type": 2, "platform": PLATFORM, "specialStyle": ""}).encode()
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
        ts = int(a[0])
        dt = datetime.fromtimestamp(ts, SHANGHAI_TZ).date().isoformat()
        try: vals=list(map(float,a[1:5]))
        except (TypeError,ValueError): continue
        by[(name,dt)]={"marketHashName":name,"group":group,"date":dt,"timestamp":str(ts),"open":vals[0],"close":vals[1],"high":vals[2],"low":vals[3]}
    return list(by.values())

def merge_into_index(by, name, group, values):
    for a in values:
        if not isinstance(a, (list, tuple)) or len(a) < 5:
            continue
        try:
            ts = int(a[0])
            vals = list(map(float, a[1:5]))
        except (TypeError, ValueError):
            continue
        dt = datetime.fromtimestamp(ts, SHANGHAI_TZ).date().isoformat()
        by[(name, dt)] = {
            "marketHashName": name,
            "group": group,
            "date": dt,
            "timestamp": str(ts),
            "open": vals[0],
            "close": vals[1],
            "high": vals[2],
            "low": vals[3],
        }


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
    if macd(12, 26, 9): out.append("双金叉 12/26/9")
    if macd(5, 12, 4): out.append("双金叉 5/12/4")
    # Two MA families: all pairwise crosses within the last 3 bars, final bullish ordering.
    def triple(ps):
        ma=[[sma(c,p,j) for j in range(len(c))] for p in ps]
        if any(v is None for v in [ma[0][i],ma[1][i],ma[2][i]]) or not (ma[0][i]>ma[1][i]>ma[2][i]): return False
        return any(ma[0][j] is not None and ma[1][j] is not None and ma[2][j] is not None and ma[0][j]>ma[1][j] and ma[1][j]>ma[2][j] and (j==0 or not (ma[0][j-1]>ma[1][j-1]>ma[2][j-1])) for j in range(max(0,i-2),i+1))
    if triple((5, 12, 21)): out.append("均线合一 MA5/12/21")
    if triple((7, 14, 21)): out.append("均线合一 MA7/14/21")
    body=abs(c[i-1]-o[i-1]); upper=h[i-1]-max(o[i-1],c[i-1])
    if upper>=body and upper>=.01*c[i-1] and c[i]>h[i-1]: out.append("上影吞没")
    # 单针：下影线至少为实体的 20%，并且保留价格占比与上部收盘条件。
    for lag in (2, 3):
        j = i - lag
        if j < 0:
            continue
        needle_body = abs(c[j] - o[j])
        needle_lower = min(o[j], c[j]) - l[j]
        entity_bottom = min(o[j], c[j])
        held = min(l[j + 1:i + 1]) >= entity_bottom
        if (
            needle_lower >= 0.2 * needle_body
            and needle_lower >= 0.02 * c[j]
            and (c[j] - l[j]) / max(h[j] - l[j], 1e-9) >= 0.6
            and held
            and c[i] >= c[j]
        ):
            out.append("单针承接")
            break
    return list(dict.fromkeys(out))

def push(text):
    tg = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not tg or not chat:
        print(text)
        return

    # Telegram 单条消息有长度限制，按行拆分，避免买点多时发送失败。
    chunks = []
    current = ""
    for line in text.splitlines(True):
        if len(current) + len(line) > 3800:
            if current:
                chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)

    for chunk in chunks:
        body = urllib.parse.urlencode({
            "chat_id": chat,
            "text": chunk,
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tg}/sendMessage",
            data=body,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=30).read()

def family_count(types):
    """参数组合分开展示，但同一家族只计一个独立买点。"""
    families = set()
    for name in types:
        if name.startswith("双金叉"):
            families.add("双金叉")
        elif name.startswith("均线合一"):
            families.add("均线合一")
        elif name == "上影吞没":
            families.add("上影吞没")
        elif name == "单针承接":
            families.add("单针承接")
    return len(families)


def main():
    rows = load_rows()

    # 排除历史数据中的全部刀皮
    rows = [
        row for row in rows
        if row.get("group") != "knife_FN"
    ]

    # 建立索引，避免每请求一件饰品都重新遍历全部历史数据。
    row_index = {
        (row["marketHashName"], row["date"]): row
        for row in rows
    }

    with open(ITEMS, encoding="utf-8-sig", newline="") as f:
        items = list(csv.DictReader(f))

    # API 请求也排除刀皮
    items = [
        item for item in items
        if item.get("group") != "knife_FN"
    ]

    total = len(items)
    print(f"items to fetch: {total}")

    # K线接口限制为每分钟 120 次。
    # 单线程每次间隔 0.60 秒，控制在每分钟约 100 次，避免触发 4005。
    last_request = 0.0
    scan_started = time.monotonic()

    for n, item in enumerate(items, 1):
        wait = 0.60 - (time.monotonic() - last_request)
        if wait > 0:
            time.sleep(wait)

        last_request = time.monotonic()

        try:
            data = request_item(item["marketHashName"])
            merge_into_index(
                row_index,
                item["marketHashName"],
                item["group"],
                data,
            )
        except Exception as e:
            message = str(e)
            print(
                f"WARN fetch failed for {item['marketHashName']}: {message}",
                file=sys.stderr,
            )

            # 如果接口刚好返回 4005，等待一个完整限流窗口后重试一次。
            if "4005" in message:
                time.sleep(65)
                try:
                    data = request_item(item["marketHashName"])
                    merge_into_index(
                        row_index,
                        item["marketHashName"],
                        item["group"],
                        data,
                    )
                except Exception as retry_error:
                    print(
                        f"WARN retry failed for {item['marketHashName']}: {retry_error}",
                        file=sys.stderr,
                    )

        if n % 50 == 0 or n == total:
            elapsed = time.monotonic() - scan_started
            rate = n / elapsed if elapsed > 0 else 0
            remaining = (total - n) / rate if rate > 0 else 0
            pct = 100 * n / total if total else 100
            print(
                f"progress: {n}/{total} ({pct:.1f}%) | "
                f"elapsed: {elapsed / 60:.1f} min | "
                f"ETA: {remaining / 60:.1f} min",
                flush=True,
            )

    rows = list(row_index.values())

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

    # 选择大多数饰品共同拥有的最新日期，避免某个饰品多一天数据
    # 导致其他饰品全部被跳过。
    latest_by_item = {}
    for row in rows:
        if row.get("group") == "knife_FN":
            continue
        name = row["marketHashName"]
        if name not in latest_by_item or row["date"] > latest_by_item[name]:
            latest_by_item[name] = row["date"]

    if not latest_by_item:
        raise RuntimeError("No valid item dates found")

    date_counts = Counter(latest_by_item.values())
    latest = date_counts.most_common(1)[0][0]
    print(f"scan date: {latest}")
    print(f"items by date: {date_counts.most_common(5)}")

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
        found.sort(key=lambda x: (-family_count(x[3]), x[0]))
        lines = [
            f"CS2 扫盘 {latest}",
            "去除刀皮｜单/双/三买点",
            "",
        ]

        for name, group, price, types in found:
            level = f"{family_count(types)}类买点"
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

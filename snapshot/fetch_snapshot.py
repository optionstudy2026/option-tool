#!/usr/bin/env python3
"""GitHub Actions 侧：抓同花顺收盘价 → 反解 IV → 回写 payoff.html 内嵌快照 → 由 workflow 提交
key 从环境变量 HITHINK_KEY 读（GitHub Secret）；本地测试可用 --key-file 从账户密码.db 取。
用法：HITHINK_KEY=xxx python3 fetch_snapshot.py --html payoff.html
"""
import argparse, json, os, re, sqlite3, statistics, sys, time
from datetime import date, datetime, timedelta
import requests

ap = argparse.ArgumentParser()
ap.add_argument("--html", default="payoff.html")
ap.add_argument("--key-file", default="/home/ubuntu/quant/账户密码.db", help="本地测试用；CI 用 HITHINK_KEY 环境变量")
ap.add_argument("--concurrency", type=int, default=4)
ap.add_argument("--gap", type=float, default=0.25)
ap.add_argument("--dry-run", action="store_true")
a = ap.parse_args()

KEY = os.environ.get("HITHINK_KEY") or (
    sqlite3.connect(a.key_file).execute("SELECT password FROM accounts WHERE company LIKE '同花顺%'").fetchone()[0]
    if os.path.exists(a.key_file) else None)
if not KEY:
    sys.exit("没有 key：CI 里设 HITHINK_KEY secret")
H = {"X-api-key": KEY}
BASE = "https://fuyao.aicubes.cn"
R = 0.02

import math
def N(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def b76(F, K, T, r, s, is_call):
    d1=(math.log(F/K)+0.5*s*s*T)/(s*math.sqrt(T)); d2=d1-s*math.sqrt(T); df=math.exp(-r*T)
    return df*(F*N(d1)-K*N(d2)) if is_call else df*(K*N(-d2)-F*N(-d1))
IV_CAP = 3.0   # 超过 300% 视为深实/深虚失真，置空（避免污染微笑拟合）
def ivsolve(price, F, K, T, is_call, r=R):
    if not price or not F or not K or T <= 0: return None
    if price <= b76(F,K,T,r,1e-6,is_call)+1e-9: return None
    lo, hi = 0.005, 5.0
    if b76(F,K,T,r,hi,is_call) < price: return None
    for _ in range(120):
        m = (lo+hi)/2
        if b76(F,K,T,r,m,is_call) < price: lo = m
        else: hi = m
    v = (lo+hi)/2
    return None if v > IV_CAP else v
def tdays(d0, d1):
    n, d = 0, d0 + timedelta(days=1)
    while d <= d1:
        if d.weekday() < 5: n += 1
        d += timedelta(days=1)
    return n
def code_of(prod, exp, cp, K, ex):
    ym = exp[2:]
    if ex == "DCE":   return f"{prod}{ym}-{cp}-{K:g}.DCE"
    if ex == "SHFE":  return f"{prod}{ym}{cp}{K:g}.SHF"
    if ex == "INE":   return f"{prod}{ym}{cp}{K:g}.INE"
    if ex == "CZCE":  return f"{prod}{exp[3]}{exp[4:]}{cp}{K:g}.CZC"
    if ex == "GFEX":  return f"{prod}{ym}-{cp}-{K:g}.GFE"
    return None
def api(path, **p):
    for _ in range(4):
        try:
            j = requests.get(BASE+path, headers=H, params=p, timeout=30).json()
            if j.get("code") in (4001,): time.sleep(3); continue
            return j
        except Exception: time.sleep(2)
    return {"code": -1}

html = open(a.html, encoding="utf-8").read()
m = re.search(r"const SNAPSHOT_SKELETON = (\{.*?\});\n", html, re.S)
if not m: sys.exit("payoff.html 里没找到 SNAPSHOT_SKELETON")
sk = json.loads(m.group(1))
old_date = sk.get("trade_date")
print("骨架交易日:", old_date, "| 品种:", len(sk["products"]))

today = date.today()
jobs = []
for p, info in sk["products"].items():
    if not info.get("verified"): continue
    if not info.get("last_trade_date"): continue
    y, mo, d = (int(x) for x in info["last_trade_date"].split("-"))
    T = max(tdays(today, date(y, mo, d)), 0.5)/252.0
    for st in info["strikes"]:
        K = st[0]
        for cp in ("C", "P"):
            cd = info.get("codes", {}).get(f"{cp}{K:g}") if info.get("kind") == "etf" else None
            cd = cd or code_of(p, info["expiry"], cp, K, info.get("exchange"))
            if cd: jobs.append((p, K, cp, cd, T))
print("待抓合约数:", len(jobs))

from concurrent.futures import ThreadPoolExecutor
import threading
lock = threading.Lock()
res = {}
def one(job):
    p, K, cp, cd, T = job
    j = api("/api/options/prices/daily", thscode=cd)
    px = dt = None
    if j.get("code") == 0:
        items = (j.get("data") or {}).get("item") or []
        use = [x for x in items if x.get("timestamp") and datetime.fromtimestamp(x["timestamp"]/1000).date() <= today]
        if use:
            last = use[-1]
            px = last.get("close_price"); dt = datetime.fromtimestamp(last["timestamp"]/1000).date().isoformat()
    with lock:
        res[(p, K, cp)] = (px, dt)
    time.sleep(a.gap)
with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
    list(ex.map(one, jobs))
dates = [v[1] for v in res.values() if v[1]]
new_date = max(dates) if dates else None
ok = sum(1 for v in res.values() if v[0] is not None)
print(f"抓取完成：成功 {ok}/{len(jobs)}，最新行情日 {new_date}")
if new_date and new_date.replace("-", "") == str(old_date):
    print("没有新交易日（行情日与骨架相同）→ 不动页面，退出")
    sys.exit(0)

# 标的价：商品取期货收盘
for p, info in sk["products"].items():
    if not info.get("verified") or info.get("kind") == "etf": continue
    fut = f"{p}{info['expiry'][2:]}." + {"DCE": "DCE", "SHFE": "SHF", "INE": "INE", "CZCE": "CZC", "GFEX": "GFE"}.get(info.get("exchange"), "DCE")
    j = api("/api/futures/prices/daily", thscode=fut)
    items = (j.get("data") or {}).get("item") or []
    use = [x for x in items if x.get("timestamp") and datetime.fromtimestamp(x["timestamp"]/1000).date() <= today]
    if use:
        info["underlying_close"] = use[-1].get("close_price") or use[-1].get("settle_price") or info.get("underlying_close")
        info["forward"] = info["underlying_close"]
    time.sleep(0.2)

# 回写 strikes（[K, 收盘价, IV%]）
for p, info in sk["products"].items():
    if not info.get("verified"): continue
    T = None
    if info.get("last_trade_date"):
        y, mo, d = (int(x) for x in info["last_trade_date"].split("-"))
        T = max(tdays(today, date(y, mo, d)), 0.5)/252.0
    F = info.get("forward") or info.get("underlying_close")
    for st in info["strikes"]:
        K = st[0]
        c = res.get((p, K, "C")); p_ = res.get((p, K, "P"))
        if c and c[0] is not None:
            st[1] = c[0]
            ivc = ivsolve(c[0], F, K, T, True)
            st[2] = None if ivc is None else round(ivc*100, 2)
        if p_ and p_[0] is not None:
            ivp = ivsolve(p_[0], F, K, T, False)
            info.setdefault("puts_px", {})[f"{K:g}"] = [p_[0], None if ivp is None else round(ivp*100, 2)]
sk["trade_date"] = new_date.replace("-", "")
sk["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " (GitHub Actions)"
new_json = json.dumps(sk, ensure_ascii=False, separators=(",", ":"))
out = html[:m.start(1)] + new_json + html[m.end(1):]

# 页面上标注数据来源
out = out.replace("内置快照 ", "快照 ")
if a.dry_run:
    open("/tmp/payoff_updated.html", "w", encoding="utf-8").write(out)
    print("dry-run：写出 /tmp/payoff_updated.html（未改仓库文件）")
else:
    open(a.html, "w", encoding="utf-8").write(out)
    print("已回写", a.html, "| 新交易日", sk["trade_date"], "| 大小", round(len(out)/1024), "KB")

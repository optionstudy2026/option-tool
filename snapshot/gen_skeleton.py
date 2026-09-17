#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 payoff.html 的 SNAPSHOT_SKELETON —— 全品种 × 全月份 × ATM±N 档行权价

数据源：rqdatac（本机跑；骨架变化不频繁，品种/月份调整时手动重跑一次）
       CI 侧只负责每天用同花顺抓价格填进骨架（fetch_snapshot.py），不跑本脚本。

用法：
  RQDATA_USER=xxx RQDATA_PASS=xxx python3 gen_skeleton.py \
      --old-html payoff.html --out skeleton.json --atm-bands 20 --date 2026-09-16

设计要点：
- products 的 key 从「品种」改为「品种|月份」（如 AG|202610），每个 entry 字段结构与旧骨架一致，
  前端只需按 key 切分品种/月份，其余逻辑不动。
- 品种元数据（中文名、ETF 券商代码 codes）优先继承旧骨架，rqdatac 补乘数/到期日/行权方式/交易所。
- 行权价按标的收盘价取上下各 N 档（默认 20），控制合约数与快照体积。
"""
import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta

ap = argparse.ArgumentParser()
ap.add_argument('--old-html', default='payoff.html', help='旧版页面（读 SNAPSHOT_SKELETON 继承元数据）')
ap.add_argument('--out', default='skeleton.json')
ap.add_argument('--atm-bands', type=int, default=20, help='ATM 上下各取几档行权价')
ap.add_argument('--date', default=None, help='基准日 YYYY-MM-DD，默认今天')
ap.add_argument('--bands-etf', type=int, default=15, help='ETF 期权的档数（ETF 行权价密集）')
a = ap.parse_args()

USER = os.environ.get('RQDATA_USER')
PASS = os.environ.get('RQDATA_PASS')
if not (USER and PASS):
    sys.exit('需要环境变量 RQDATA_USER / RQDATA_PASS（不要把凭据写进文件）')

import rqdatac
rqdatac.init(USER, PASS)

D = a.date or date.today().isoformat()
TODAY = date.fromisoformat(D)


def ym_of(ub_id):
    """标的合约代码 → YYYYMM 月份标记（AG2610→202610；郑商所 FG610→202610）"""
    m = re.match(r'^([A-Za-z]{1,2})(\d{3,4})$', ub_id)
    if not m:
        return None
    num = m.group(2)
    if len(num) == 4:
        return '20' + num
    return '20' + num[0] + num[1:].zfill(2)


def tdays(d0, d1):
    n = 0
    d = d0
    while d < d1:
        if d.weekday() < 5:
            n += 1
        d = date.fromordinal(d.toordinal() + 1)
    return n


# ---------- 旧骨架（继承中文名 / ETF codes）----------
old = {}
try:
    h = open(a.old_html, encoding='utf-8').read()
    m = re.search(r'const SNAPSHOT_SKELETON = (\{.*?\});\n', h, re.S)
    if m:
        old = json.loads(m.group(1)).get('products', {})
    print(f'旧骨架继承：{len(old)} 个品种')
except Exception as e:
    print('读旧骨架失败（继续，元数据走 rqdatac）:', e)

# ---------- rqdatac：当日全部有效期权合约 ----------
df = rqdatac.all_instruments(type='Option', date=D)
print(f'rqdatac 当日有效期权合约：{len(df)}，标的 {df["underlying_order_book_id"].nunique()} 个')

ub_ids = sorted(df['underlying_order_book_id'].dropna().unique().tolist())
px = {}
B = 200
# 取价基准日：当天日线要收盘后才有，日盘/夜盘运行时回退到最近有数据的交易日
PX_DATE = None
for back in range(0, 12):
    d = (TODAY - timedelta(days=back)).isoformat()
    try:
        if len(rqdatac.get_price(ub_ids[:1], start_date=d, end_date=d, frequency='1d')) == 0:
            continue
    except Exception:
        continue
    PX_DATE = d
    break
PX_DATE = PX_DATE or D
print('标的取价日:', PX_DATE)
for i in range(0, len(ub_ids), B):
    chunk = ub_ids[i:i + B]
    try:
        p = rqdatac.get_price(chunk, start_date=PX_DATE, end_date=PX_DATE, frequency='1d')
        for ob in chunk:
            try:
                row = p.loc[ob]
                v = row['close'] if 'close' in row else None
                if v is None:
                    v = row['settlement']
                px[ob] = float(v)
            except Exception:
                pass
    except Exception as e:
        print('  取价失败', chunk[:3], e)
print(f'拿到标的收盘价：{len(px)}/{len(ub_ids)}')

# ---------- 生成新骨架 ----------
products = {}
for (ub_id, _matk), grp in df.groupby(['underlying_order_book_id', 'maturity_date']):
    if not ub_id:
        continue
    prod = str(grp['underlying_symbol'].iloc[0])
    exch = str(grp['exchange'].iloc[0])
    mul = float(grp['contract_multiplier'].iloc[0])
    mat = grp['maturity_date'].iloc[0]
    mat_d = mat.date() if hasattr(mat, 'date') else datetime.strptime(str(mat)[:10], '%Y-%m-%d').date()
    if mat_d < TODAY:
        continue                                  # 已到期，跳过
    ex_type = str(grp['exercise_type'].iloc[0]).upper()
    is_etf = str(exch).upper() in ('SSE', 'SZSE', 'XSHG', 'XSHE')
    kind = 'etf' if is_etf else 'commodity'
    if is_etf:
        prod = str(ub_id).split('.')[0]
        ym = mat_d.strftime('%Y%m')
    else:
        prod = str(grp['underlying_symbol'].iloc[0])
        ym = ym_of(ub_id)
    if not ym:
        continue
    S = px.get(ub_id)
    ks = sorted(float(x) for x in grp['strike_price'].dropna().unique())
    if not ks:
        continue
    bands = a.bands_etf if is_etf else a.atm_bands
    if S:
        idx = min(range(len(ks)), key=lambda i: abs(ks[i] - S))
        lo, hi = max(0, idx - bands), min(len(ks), idx + bands + 1)
        sel = ks[lo:hi]
    else:
        sel = ks
    ok_name = None
    for key, v in old.items():
        base = key.split('|')[0]
        if base == prod or str(v.get('underlying_code', '')).startswith(prod):
            ok_name = v.get('name')
            break
    entry = {
        'name': ok_name or prod,
        'mul': mul,
        'kind': kind,
        'exchange': exch,
        'expiry': ym,
        'underlying_code': str(ub_id),
        'underlying_close': round(S, 4) if S else None,
        'forward': round(S, 4) if S else None,
        'style': 'american' if ex_type == 'A' else 'european',
        'last_trade_date': mat_d.isoformat(),
        'dte_trading': tdays(TODAY, mat_d),
        'atm_probe': (min(sel, key=lambda k: abs(k - S)) if (S and sel) else (sel[len(sel) // 2] if sel else None)),
        'verified': True,
        'strikes': [[k, None, None] for k in sel],
    }
    if is_etf:
        # ETF 期权：同花顺只认券商合约代码（rqdatac 的 order_book_id 即该代码），
        # 按 'C4'/'P4.7' 的 key 存映射，前端查表用。
        sfx = '.SH' if str(exch).upper() in ('SSE', 'XSHG') else '.SZ'
        codes = {}
        for _, r in grp.iterrows():
            try:
                kk = float(r['strike_price'])
            except Exception:
                continue
            cp = 'C' if str(r['option_type']).upper().startswith('C') else 'P'
            codes[f'{cp}{kk:g}'] = f"{r['order_book_id']}{sfx}"
        if codes:
            entry['codes'] = codes
    products[f'{prod}|{ym}'] = entry

sk = {
    'trade_date': D.replace('-', ''),
    'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' (rqdatac skeleton)',
    'products': products,
}
tot = sum(len(v['strikes']) for v in products.values())
with open(a.out, 'w', encoding='utf-8') as f:
    json.dump(sk, f, ensure_ascii=False, separators=(',', ':'))
print(f'写出 {a.out}：标的月份组合 {len(products)} 个，行权价合计 {tot}，合约数约 {tot * 2}')
print(f'文件大小：{os.path.getsize(a.out) / 1024:.1f} KB')

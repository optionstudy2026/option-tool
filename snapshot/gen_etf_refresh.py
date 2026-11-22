#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只重刷 payoff 骨架里的 ETF 部分（商品沿用现有骨架，不缩窄）

用户定的口径（2026-11-22）：
- 档位不设固定数字（原先 --bands-etf 15 硬截 ATM 上下 15 档是错的）
- 取该月份「当日有报价/有盘口」的全部档位；确实有 37 档就 37 档，只有 4 档就 4 档
- 商品不动：沿用旧骨架里的商品条目，只剔除已过期月份

数据源：
- 行情数据.db  option_call_quotes / option_put_quotes —— 档位、最新价、IV、盘口
- 行情数据.db  underlying_quotes                   —— ETF 标的收盘价
- 同花顺 API   /api/options/contracts/list         —— ETF 券商合约码（行情库没有全档映射）

用法：
  python3 gen_etf_refresh.py --out skeleton_etf.json [--date 20261120]
                             [--old-html payoff.html] [--inject]
  --inject  额外把新骨架写回 payoff.html 的 SNAPSHOT_SKELETON 块
"""
import argparse
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta

MARKET_DB = '/home/ubuntu/quant/分析系统/行情数据.db'
MAP_DB = '/home/ubuntu/quant/分析系统/合约映射.db'
CRED_DB = '/home/ubuntu/quant/账户密码.db'
BASE_URL = 'https://fuyao.aicubes.cn'
IS_ETF = re.compile(r'^\d{6}$')
TICKER_RE = re.compile(r'^(\d{6})([CP])(\d{4})M(\d+)$')


def tdays(d0, d1):
    n, d = 0, d0
    while d < d1:
        if d.weekday() < 5:
            n += 1
        d = date.fromordinal(d.toordinal() + 1)
    return n


def get_key():
    con = sqlite3.connect(CRED_DB)
    try:
        return con.execute("select password from accounts where company like '同花顺%'").fetchone()[0]
    finally:
        con.close()


def fetch_etf_contracts(key, etfs):
    """拉同花顺全市场期权合约，只留 ETF → {(etf, expiry): {(cp, strike): thscode}} 和到期日"""
    import requests
    H = {'X-api-key': key}
    out, last_trade = {}, {}
    off, lim = 0, 1000
    while True:
        j = None
        for _ in range(3):
            try:
                r = requests.get(BASE_URL + '/api/options/contracts/list', headers=H,
                                 params={'limit': lim, 'offset': off}, timeout=40)
                j = r.json()
                break
            except Exception as e:
                print('  重试 offset', off, e, file=sys.stderr)
        if not j or j.get('code') != 0:
            print('  拉取失败 offset', off, j if j else '', file=sys.stderr)
            break
        items = (j.get('data') or {}).get('item') or []
        if not items:
            break
        for it in items:
            vc = str(it.get('variety_code') or '')
            if not vc.endswith('O'):
                continue
            etf = vc[:-1]
            if etf not in etfs:
                continue
            m = TICKER_RE.match(str(it.get('ticker') or ''))
            if not m:
                continue
            cp, yymm, kpt = m.group(2), m.group(3), int(m.group(4))
            expiry = '20' + yymm
            strike = kpt / 1000.0
            out.setdefault((etf, expiry), {})[(cp, strike)] = it['thscode']
            ltd = it.get('last_trade_date')
            if ltd:
                last_trade[(etf, expiry)] = ltd
        off += lim
        if off > 200000:
            break
    return out, last_trade


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='基准交易日 YYYYMMDD，默认行情库最新日')
    ap.add_argument('--out', default='skeleton_etf.json')
    ap.add_argument('--old-html', default='payoff.html', help='旧骨架：商品条目从这里继承')
    ap.add_argument('--inject', action='store_true', help='把新骨架写回 --old-html 的 SNAPSHOT_SKELETON')
    a = ap.parse_args()

    mkt = sqlite3.connect(f'file:{MARKET_DB}?mode=ro', uri=True)
    D = a.date or mkt.execute('select max(trade_date) from option_call_quotes').fetchone()[0]
    BASE = date(int(D[:4]), int(D[4:6]), int(D[6:8]))
    print(f'基准交易日 {D}')

    # ---------- 旧骨架 ----------
    html = open(a.old_html, encoding='utf-8').read()
    m = re.search(r'const SNAPSHOT_SKELETON = (\{.*?\});\n', html, re.S)
    if not m:
        sys.exit('旧 HTML 里没找到 SNAPSHOT_SKELETON')
    old = json.loads(m.group(1))
    old_products = old.get('products') or {}
    print(f'旧骨架：{len(old_products)} 个组合，trade_date={old.get("trade_date")}')

    # ---------- 交易所链（ETF 部分）----------
    rows = {}
    for tbl in ('option_call_quotes', 'option_put_quotes'):
        for prod, exp, k, px, iv, bid, ask in mkt.execute(
                f'select product,expiry,strike,latest_price,iv_theo,bid_price,ask_price '
                f'from {tbl} where trade_date=?', (D,)):
            if not IS_ETF.match(str(prod)):
                continue
            e = rows.setdefault((prod, exp), {}).setdefault(k, dict(px=None, iv=None, live=False))
            if px and px > 0:
                e['px'] = px if e['px'] is None else max(e['px'], px)  # call 侧价格覆盖 put 侧（后面分别取）
                e['live'] = True
            if iv and 0 < iv < 400:
                e['iv'] = e['iv'] or iv
            if (bid and bid > 0) or (ask and ask > 0):
                e['live'] = True
    # call/put 分开再取一次（上面统一字典只用于判活）
    calls, puts = {}, {}
    for tbl, tgt in (('option_call_quotes', calls), ('option_put_quotes', puts)):
        for prod, exp, k, px, iv in mkt.execute(
                f'select product,expiry,strike,latest_price,iv_theo from {tbl} where trade_date=?', (D,)):
            if IS_ETF.match(str(prod)):
                tgt.setdefault((prod, exp), {})[k] = (px, iv)

    spot = dict(mkt.execute('select code, close from underlying_quotes where trade_date=?', (D,)).fetchall())
    mkt.close()
    etfs = sorted({p for (p, _) in rows})
    print(f'行情库 ETF：{etfs}')

    # ---------- 券商合约码 ----------
    key = get_key()
    print('拉同花顺 ETF 合约列表 ...')
    codes_map, last_trade = fetch_etf_contracts(key, set(etfs))
    cnt = sum(len(v) for v in codes_map.values())
    print(f'  拿到 {(len(codes_map))} 个 ETF 月份组合，合约码 {cnt} 条')

    # ---------- 组装 ETF 条目 ----------
    products, report = {}, []
    for (prod, exp) in sorted(rows):
        allks = sorted(set(calls.get((prod, exp), {})) | set(puts.get((prod, exp), {})))
        live = sorted({k for k, v in rows[(prod, exp)].items() if v['live']})
        sel = live or allks                       # 该月当天有报价/盘口的档位；全无则退回全部档
        dropped = [k for k in allks if k not in sel]
        ltd = last_trade.get((prod, exp))
        if not ltd:
            dtes = [iv for iv in () ]
            report.append(f'{prod}|{exp} 缺到期日（同花顺无该月合约）→ 跳过')
            continue
        ltd_d = date(*(int(x) for x in ltd.split('-')))
        if ltd_d < BASE:
            report.append(f'{prod}|{exp} 已到期 {ltd} → 跳过')
            continue
        S = spot.get(prod)
        if not S:
            report.append(f'{prod}|{exp} 缺标的收盘价 → 跳过')
            continue

        cm = codes_map.get((prod, exp), {})
        # 只保留同花顺里真实挂牌的档位——库里存在但交易所没挂牌的 strike（如 510500 的 6.876）
        # 就是没有可交易合约，不该出现在报价表里
        listed = [k for k in sel if cm.get(('C', k)) or cm.get(('P', k))]
        unlisted = [k for k in sel if k not in listed]
        if listed:
            sel = listed
        codes = {}
        for k in sel:
            for cp in ('C', 'P'):
                cd = cm.get((cp, k))
                if cd:
                    codes[f'{cp}{k:g}'] = cd
        strikes = [[k, (round(calls[(prod, exp)][k][0], 6) if calls.get((prod, exp), {}).get(k, (None,))[0] else None), None]
                   for k in sel]
        puts_px = {}
        for k in sel:
            p = puts.get((prod, exp), {}).get(k)
            if p and p[0]:
                puts_px[f'{k:g}'] = [round(p[0], 6), round(p[1], 2) if p[1] else None]

        entry = {
            'name': (old_products.get(f'{prod}|{exp}') or {}).get('name') or prod,
            'mul': 10000.0,
            'kind': 'etf',
            'exchange': 'XSHG' if prod.startswith('5') else 'XSHE',
            'expiry': exp,
            'underlying_code': f'{prod}.{"XSHG" if prod.startswith("5") else "XSHE"}',
            'underlying_close': round(S, 4),
            'forward': round(S, 4),
            'style': 'european',
            'last_trade_date': ltd,
            'dte_trading': tdays(BASE, ltd_d),
            'atm_probe': min(sel, key=lambda k: abs(k - S)),
            'verified': True,
            'strikes': strikes,
        }
        if codes:
            entry['codes'] = codes
        if puts_px:
            entry['puts_px'] = puts_px
        products[f'{prod}|{exp}'] = entry
        report.append(f'{prod}|{exp} 档位 {len(sel)}/{len(allks)}'
                      + (f'（剔除无报价 {len(dropped)} 档：{[k for k in dropped][:12]}）' if dropped else '')
                      + (f'（剔除未挂牌 {len(unlisted)} 档：{unlisted[:8]}）' if unlisted else '')
                      + f'，券商码 {len(codes)}/{len(sel) * 2}，标的 {S}，到期 {ltd}')

    # ---------- 商品：沿用旧骨架，仅剔除已过期；顺手补齐缺失的 exchange（否则 CI 推不出合约码、抓不到价） ----------
    mapcon = sqlite3.connect(f'file:{MAP_DB}?mode=ro', uri=True)
    exch_of = dict(mapcon.execute(
        'select underlying_code, max(exchange) from option_contracts group by underlying_code').fetchall())
    mapcon.close()
    kept = dropped_old = fixed_exch = 0
    for k, v in old_products.items():
        prod = k.split('|')[0]
        if IS_ETF.match(str(prod)):
            continue                                    # ETF 全部用新数据
        ltd = v.get('last_trade_date')
        if ltd:
            ltd_d = date(*(int(x) for x in ltd.split('-')))
            if ltd_d < BASE:
                dropped_old += 1
                continue
        if not v.get('exchange'):
            ex = exch_of.get(v.get('underlying_code') or '')
            if ex:
                v['exchange'] = ex
                fixed_exch += 1
        products[k] = v
        kept += 1
    print(f'商品沿用旧骨架 {kept} 个（补 exchange {fixed_exch} 个），剔除已过期 {dropped_old} 个')

    sk = {
        'trade_date': D,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' (ETF 重刷：档位按交易日实际有报价/盘口，商品沿用旧骨架)',
        'products': products,
    }
    with open(a.out, 'w', encoding='utf-8') as f:
        json.dump(sk, f, ensure_ascii=False, separators=(',', ':'))
    tot = sum(len(v['strikes']) for v in products.values())
    etf_keys = [k for k, v in products.items() if v.get('kind') == 'etf']
    print(f'\n写出 {a.out}：组合 {len(products)}（ETF {len(etf_keys)} / 商品 {len(products) - len(etf_keys)}），行权价合计 {tot}')
    print('\n--- ETF 明细 ---')
    for line in report:
        print('  ' + line)

    if a.inject:
        blob = json.dumps(sk, ensure_ascii=False, separators=(',', ':'))
        new_html = html[:m.start(1)] + blob + html[m.end(1):]
        with open(a.old_html, 'w', encoding='utf-8') as f:
            f.write(new_html)
        print(f'\n已注入 {a.old_html}（{len(html)} → {len(new_html)} 字节）')


if __name__ == '__main__':
    main()

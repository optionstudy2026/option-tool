#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从本地行情库重建 payoff.html 的 SNAPSHOT_SKELETON（不依赖 rqdatac）

背景（2026-10-22 用户指示重建）：原 gen_skeleton.py 用 rqdatac 生成骨架，
米筐 2026-10-17 授权到期后骨架停在 2026-09-16，导致 payoff 里 ETF 只有
202609/202610/202612/202703 四个月份、没有 202611，且全品种价格停在 9 月。

用户规则：
- 只放**还有的**合约：期权到期日早于基准日的丢弃，不再留过期月份
- **没有行情数据的标的不进骨架**（159901/159922/588080 已删，以后有数据再加）
- 商品与 ETF 一起重刷

数据源（全部本地已入库，不联网）：
- 行情数据.db  option_call_quotes / option_put_quotes —— 行权价档位、IV、最新价、days_to_expiry
- 行情数据.db  underlying_quotes                   —— 标的收盘价
- 合约映射.db  option_contracts                    —— 乘数、期权到期日、交易所、ETF 券商合约代码

用法：
  python3 gen_skeleton_from_db.py [--date 20261022] [--out skeleton.json]
                                  [--bands-etf 15] [--bands 20] [--old-html payoff.html]
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta

MARKET_DB = '/home/ubuntu/quant/分析系统/行情数据.db'
MAP_DB = '/home/ubuntu/quant/分析系统/合约映射.db'

IS_ETF = re.compile(r'^\d{6}$')
EX2YF = {'SSE': 'XSHG', 'SZSE': 'XSHE'}


def tdays(d0, d1):
    """交易日近似：只跳周末（节假日不扣，与旧骨架口径一致）"""
    n = 0
    d = d0
    while d < d1:
        if d.weekday() < 5:
            n += 1
        d = date.fromordinal(d.toordinal() + 1)
    return n


def load_mapping():
    """{underlying_code: [(strike, direction, contract_code, multiplier, expiry_date, exchange)]}"""
    con = sqlite3.connect(f'file:{MAP_DB}?mode=ro', uri=True)
    rows = con.execute(
        'select underlying_code, strike, direction, contract_code, multiplier, expiry_date, exchange, contract_month '
        'from option_contracts').fetchall()
    con.close()
    out = {}
    for ub, k, d, cc, mul, exp, exch, mon in rows:
        out.setdefault(ub, []).append(dict(strike=k, direction=d, code=cc, mul=mul,
                                          expiry=exp, exch=exch, month=mon))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='基准交易日 YYYYMMDD，默认行情库最新日')
    ap.add_argument('--out', default='skeleton.json')
    ap.add_argument('--old-html', default='payoff.html', help='旧骨架，仅用于继承中文名')
    ap.add_argument('--bands-etf', type=int, default=15, help='ETF ATM 上下各取几档')
    ap.add_argument('--bands', type=int, default=20, help='商品 ATM 上下各取几档')
    a = ap.parse_args()

    mkt = sqlite3.connect(f'file:{MARKET_DB}?mode=ro', uri=True)
    D = a.date or mkt.execute('select max(trade_date) from option_call_quotes').fetchone()[0]
    BASE = date(int(D[:4]), int(D[4:6]), int(D[6:8]))
    print(f'基准交易日 {D}')

    # ---------- 旧骨架只继承中文名 ----------
    names = {}
    try:
        h = open(a.old_html, encoding='utf-8').read()
        m = re.search(r'const SNAPSHOT_SKELETON = (\{.*?\});\n', h, re.S)
        if m:
            for k, v in (json.loads(m.group(1)).get('products') or {}).items():
                names.setdefault(k.split('|')[0], v.get('name'))
            print(f'旧骨架继承中文名 {len(names)} 个')
    except Exception as e:
        print('读旧骨架失败（中文名退回代码）:', e)

    # ---------- 期权链 ----------
    def chain(tbl, field):
        out = {}
        for prod, exp, k, px, iv, dte in mkt.execute(
                f'select product, expiry, strike, latest_price, iv_theo, days_to_expiry '
                f'from {tbl} where trade_date=?', (D,)):
            out.setdefault((prod, exp), {})[k] = dict(px=px, iv=iv, dte=dte)
        return out
    calls = chain('option_call_quotes', 'c')
    puts = chain('option_put_quotes', 'p')
    print(f'期权链：call {len(calls)} 组 / put {len(puts)} 组')

    # ---------- 标的收盘价 ----------
    spot = dict(mkt.execute('select code, close from underlying_quotes where trade_date=?', (D,)).fetchall())
    mkt.close()
    print(f'标的收盘价 {len(spot)} 个')

    mapping = load_mapping()

    products, skipped_expired, skipped_nospot = {}, [], []
    for (prod, exp) in sorted(set(calls) | set(puts)):
        is_etf = bool(IS_ETF.match(prod))
        if is_etf:
            ub = prod                                    # ETF 期权的 underlying_code 就是标的代码
            cands = [prod]
            rows = mapping.get(prod) or []
        else:
            # 商品标的代码：其他所 4 位年月（AG2611），郑商所 3 位（MA612）——两套都试
            cands = [f'{prod}{exp[2:]}', f'{prod}{exp[3:]}']
            ub = next((c for c in cands if c in mapping), cands[0])
            if ub not in mapping:
                ub = next((c for c in cands if c in spot), cands[0])
            rows = mapping.get(ub) or []

        # 到期日：优先合约映射里该标的期权的到期日，退回 days_to_expiry
        # 只认与当前月份 exp 匹配的合约的到期日（否则各月会串成同一个到期日）
        exp_dates = sorted({r['expiry'] for r in rows
                            if r['expiry'] and str(r.get('month') or '') == str(exp) and r['expiry'] >= D})
        if exp_dates:
            ltd = exp_dates[0]
        else:
            dtes = [v['dte'] for v in list(calls.get((prod, exp), {}).values())
                    + list(puts.get((prod, exp), {}).values()) if v.get('dte')]
            if not dtes:
                skipped_expired.append(f'{prod}|{exp}(无到期日)')
                continue
            ltd = (BASE + timedelta(days=int(max(dtes)))).strftime('%Y%m%d')
        ltd_d = date(int(ltd[:4]), int(ltd[4:6]), int(ltd[6:8]))
        if ltd_d < BASE:                                     # 过期合约不进骨架
            skipped_expired.append(f'{prod}|{exp}(到期{ltd})')
            continue

        # 标的收盘价
        S = next((spot[c] for c in cands if c in spot), None)
        if not S:
            skipped_nospot.append(f'{prod}|{exp}')
            continue

        ks = sorted(set(calls.get((prod, exp), {})) | set(puts.get((prod, exp), {})))
        if not ks:
            continue
        bands = a.bands_etf if is_etf else a.bands
        idx = min(range(len(ks)), key=lambda i: abs(ks[i] - S))
        sel = ks[max(0, idx - bands):min(len(ks), idx + bands + 1)]

        mul = next((r['mul'] for r in rows if r['mul']), None)
        exch = next((r['exch'] for r in rows if r['exch']), None)
        if is_etf:
            exch_yf = EX2YF.get(exch, 'XSHG' if prod.startswith('5') else 'XSHE')
            ub_code = f'{prod}.{exch_yf}'
            kind, style, name = 'etf', 'european', prod
        else:
            exch_yf = exch or ''
            ub_code = ub
            kind, style, name = 'commodity', 'american', prod

        strikes = []
        for k in sel:
            c = calls.get((prod, exp), {}).get(k)
            strikes.append([k, (round(c['px'], 6) if c and c.get('px') else None), None])

        entry = {
            'name': names.get(prod) or name,
            'mul': float(mul) if mul else (10000.0 if is_etf else None),
            'kind': kind,
            'exchange': exch_yf,
            'expiry': exp,
            'underlying_code': ub_code,
            'underlying_close': round(S, 4),
            'forward': round(S, 4),
            'style': style,
            'last_trade_date': f'{ltd[:4]}-{ltd[4:6]}-{ltd[6:8]}',
            'dte_trading': tdays(BASE, ltd_d),
            'atm_probe': min(sel, key=lambda k: abs(k - S)),
            'verified': True,
            'strikes': strikes,
        }

        if is_etf:
            codes = {}
            for r in rows:
                if not r['strike'] or not r['code']:
                    continue
                if str(r['expiry']) != str(ltd):             # 只取同一到期日的合约，避免跨月串码
                    continue
                kk = r['strike'] / 1000.0                    # 合约映射 ETF 行权价按「点」存
                if kk not in sel:
                    continue
                cp = 'C' if str(r['direction']).upper().startswith('C') else 'P'
                sfx = '.SH' if exch == 'SSE' else '.SZ'
                codes[f'{cp}{kk:g}'] = f'{r["code"]}{sfx}'
            if codes:
                entry['codes'] = codes

        # put 侧价格/IV（前端画 put 腿用）
        pp = {}
        for k in sel:
            p = puts.get((prod, exp), {}).get(k)
            if p and p.get('px'):
                pp[f'{k:g}'] = [round(p['px'], 6), round(p['iv'], 2) if p.get('iv') else None]
        if pp:
            entry['puts_px'] = pp

        products[f'{prod}|{exp}'] = entry

    sk = {
        'trade_date': D,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' (本地行情库重建，无 rqdatac)',
        'products': products,
    }
    with open(a.out, 'w', encoding='utf-8') as f:
        json.dump(sk, f, ensure_ascii=False, separators=(',', ':'))

    tot = sum(len(v['strikes']) for v in products.values())
    etf_keys = [k for k, v in products.items() if v['kind'] == 'etf']
    print(f'\n写出 {a.out}：组合 {len(products)} 个（ETF {len(etf_keys)} / 商品 {len(products) - len(etf_keys)}），'
          f'行权价合计 {tot}，约 {os.path.getsize(a.out) / 1024:.1f} KB')
    print('ETF 组合：', ', '.join(sorted(etf_keys)))
    if skipped_expired:
        print(f'丢弃过期/无到期日 {len(skipped_expired)} 个：{skipped_expired[:8]}{" ..." if len(skipped_expired) > 8 else ""}')
    if skipped_nospot:
        print(f'缺标的收盘价跳过 {len(skipped_nospot)} 个：{skipped_nospot[:8]}')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
真实数据获取器（币安 USDT-M 永续）/ 兼容本地列表

目标：拉取“2025年5月”区间（可改）的市值 Top50（或本地文件给定列表）在币安 USDT 永续的 K线，输出成策略可用的 OHLCV CSV。

要点：
- Top50 源优先使用 Coingecko 当前 Top（免费接口不支持历史快照），作为近似；你也可以用本地文件替代严格名单。
- 自动筛掉币安期货不存在的币种，仅保留 `*USDT` 永续（PERPETUAL）。
- 输出文件名：`<SYMBOL>-USDT-SWAP.csv`，表头：timestamp,open,high,low,close,volume

默认行为（无需参数）：
  - 使用文件来源 symbols.txt（每行一个币种符号，如 ETH）
  - 拉取 2025-05-01 00:00:00 至 2025-11-01 00:00:00 的 1h K 线
  - 输出到 data 目录，文件名 <SYMBOL>-USDT-SWAP.csv

也可选择传参覆盖：
  python gen_sample_data.py --输出目录 data --开始 2025-05-01 00:00:00 --结束 2025-11-01 00:00:00 --周期 1h --来源 file --文件 symbols.txt
"""

from __future__ import annotations

import argparse
import csv
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List
import time

try:
    import requests
except Exception as e:
    requests = None


def iso_to_utc_ms(s: str) -> int:
    dt = datetime.strptime(s, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    return datetime.utcfromtimestamp(ms/1000).strftime('%Y-%m-%d %H:%M:%S')


def get_coingecko_top50() -> List[str]:
    if requests is None:
        print('缺少 requests，无法请求 Coingecko。')
        return []
    url = 'https://api.coingecko.com/api/v3/coins/markets'
    params = dict(vs_currency='usd', order='market_cap_desc', per_page=50, page=1, sparkline=False)
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        symbols = [x['symbol'].upper() for x in data]
        return symbols
    except Exception as e:
        print('获取 Coingecko Top50 失败：', e)
        return []


def get_binance_futures_symbols() -> List[str]:
    if requests is None:
        print('缺少 requests，无法请求 Binance。')
        return []
    url = 'https://fapi.binance.com/fapi/v1/exchangeInfo'
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        syms = []
        for s in data.get('symbols', []):
            if s.get('status') == 'TRADING' and s.get('contractType') == 'PERPETUAL' and s.get('quoteAsset') == 'USDT':
                syms.append(s['symbol'])  # e.g., ETHUSDT 或 1000BONKUSDT
        return syms
    except Exception as e:
        print('获取 Binance 期货合约列表失败：', e)
        return []


def fetch_binance_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> List[dict]:
    """拉取 USDT-M 期货 K线（fapi/v1/klines），返回标准行。"""
    if requests is None:
        print('缺少 requests，无法请求 Binance。')
        return []
    url = 'https://fapi.binance.com/fapi/v1/klines'
    out = []
    cur = start_ms
    limit = 1500
    while cur < end_ms:
        params = dict(symbol=symbol, interval=interval, startTime=cur, endTime=end_ms, limit=limit)
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            arr = r.json()
            if not arr:
                break
            for k in arr:
                # [ openTime, open, high, low, close, volume, closeTime, ... ]
                open_ms = int(k[0])
                close = float(k[4])
                out.append(dict(
                    timestamp=ms_to_iso(open_ms),
                    open=f"{float(k[1]):.6f}",
                    high=f"{float(k[2]):.6f}",
                    low=f"{float(k[3]):.6f}",
                    close=f"{close:.6f}",
                    volume=f"{float(k[5]):.2f}",
                ))
            # 下一页从最后一根的 closeTime+1 开始
            last_close = int(arr[-1][6])
            next_cur = last_close + 1
            if next_cur <= cur:
                break
            cur = next_cur
            time.sleep(0.2)  # 简单限速
        except Exception as e:
            print(f"请求 {symbol} 失败：", e)
            break
    return out


def build_base_to_symbol_map() -> dict:
    """将币安期货列表映射为 {baseAsset: symbol}，处理如 1000BONKUSDT 这类特殊命名。
    仅保留 USDT 永续、交易中。
    """
    if requests is None:
        return {}
    url = 'https://fapi.binance.com/fapi/v1/exchangeInfo'
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        out = {}
        for s in data.get('symbols', []):
            if s.get('status') == 'TRADING' and s.get('contractType') == 'PERPETUAL' and s.get('quoteAsset') == 'USDT':
                base = (s.get('baseAsset') or '').upper()
                sym = s.get('symbol')
                if base and sym:
                    out[base] = sym
        return out
    except Exception:
        return {}


def write_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['timestamp','open','high','low','close','volume'])
        w.writeheader()
        w.writerows(rows)


def main():
    p = argparse.ArgumentParser(description='拉取币安 USDT-M 永续 K线，输出为策略可用 CSV')
    p.add_argument('--输出目录', '--out-dir', dest='out_dir', default='data')
    p.add_argument('--来源', '--source', dest='source', default='file', choices=['coingecko','file'])
    p.add_argument('--文件', '--file', dest='file', default='symbols.txt', help='当来源=file 时，从该文件读取币种符号（每行一个，如 ETH）')
    p.add_argument('--开始', '--start', dest='start', default='2025-05-01 00:00:00')
    p.add_argument('--结束', '--end', dest='end', default='2025-11-01 00:00:00')
    p.add_argument('--周期', '--interval', dest='interval', default='1h', choices=['1m','5m','15m','1h'])
    args = p.parse_args()

    out_dir = Path(args.out_dir)
    start_ms = iso_to_utc_ms(args.start)
    end_ms = iso_to_utc_ms(args.end)

    # 1) 获取 Top50 列表（或本地文件）
    if args.source == 'coingecko':
        base = get_coingecko_top50()
    else:
        path = Path(args.file)
        if path.exists():
            base = [line.strip().upper() for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
        else:
            print(f'找不到列表文件：{path}，请先创建 symbols.txt（每行一个币种符号，如 ETH）')
            base = []
    if not base:
        print('未获得任何候选币种（网络限制或文件为空），停止。')
        return

    # 2) 查询币安期货可交易 USDT 永续（基于 baseAsset 的映射，处理 1000BONKUSDT 等）
    base_map = build_base_to_symbol_map()
    if not base_map:
        print('警告：未获取到合约映射，可能网络受限；将尝试按命名规则 (<SYM>USDT / 1000<SYM>USDT) 拉取。')

    # 3) 逐个币种拉取 K 线
    downloaded = 0
    for sym in base:
        # 构造候选合约名：映射命中优先，其次常规与 1000 前缀
        candidates = []
        if base_map.get(sym):
            candidates.append(base_map[sym])
        candidates.append(f"{sym}USDT")
        candidates.append(f"1000{sym}USDT")
        out_name = f"{sym}-USDT-SWAP.csv"
        out_path = out_dir / out_name
        if out_path.exists():
            try:
                if out_path.stat().st_size > 64:
                    print(f"已存在，跳过：{out_path}")
                    downloaded += 1
                    continue
            except Exception:
                print(f"已存在但无法检查大小，重新下载：{out_path}")
        ok = False
        for cand in dict.fromkeys(candidates):
            if not cand:
                continue
            print(f"尝试拉取 {sym} => {cand} ...")
            rows = fetch_binance_klines(cand, args.interval, start_ms, end_ms)
            if rows:
                write_csv(out_path, rows)
                downloaded += 1
                print(f"已保存：{out_path} 行数={len(rows)}")
                ok = True
                break
        if not ok:
            print(f"失败：{sym} 未能获取到任何K线（合约不存在或网络受限）")
        time.sleep(0.1)
    print(f"完成。成功下载 {downloaded} 个标的。")


if __name__ == '__main__':
    main()

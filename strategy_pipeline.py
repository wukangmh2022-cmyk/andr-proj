#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
python strategy_pipeline.py --data-dir data --out-dir output --config output/strategy_config.1h.json

策略流水线：横截面轮动 + 趋势段捕捉 + 顺势对齐过滤

说明（无第三方依赖的最小回测管线）：
- 读取多标的 OHLCV CSV（分钟或任意固定周期）
- 计算特征（价格波幅窗口收益、SMA/EMA、唐奇安、ATR）
- 构建横截面打分并挑选 Top-K 候选
- 强制顺势对齐（交易方向与价格波幅符号一致）
- 以 ATR 设初始止损与移动止盈，叠加时间止损
- 约束组合暴露，实际杠杆 ≤ 配置上限（默认 ≈ 1）
- 导出成交与汇总 CSV（便于复刻你现有分析）

期望的单标 CSV 列（需要表头）：
    timestamp,open,high,low,close,volume
其中 `timestamp` 支持 epoch 秒/毫秒、ISO 字符串或常见格式。

使用示例：
    python strategy_pipeline.py \
        --data-dir data/ \
        --symbols WIF-USDT-SWAP,BONK-USDT-SWAP,ETH-USDT-SWAP \
        --out-dir output/ \
        --config output/strategy_config.example.json

若省略 --config，将使用内置默认参数。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Iterable


# ------------------------
# 工具函数
# ------------------------

def parse_ts(v: str) -> int:
    v = (v or "").strip()
    if not v:
        raise ValueError("empty timestamp")
    # try int epoch
    try:
        iv = int(v)
        # heuristics: epoch seconds vs ms
        if iv > 10_000_000_000:  # assume ms
            return iv
        return iv * 1000
    except Exception:
        pass
    # try float epoch
    try:
        fv = float(v)
        if fv > 10_000_000_000:  # ms
            return int(fv)
        return int(fv * 1000)
    except Exception:
        pass
    # try ISO8601
    try:
        dt = datetime.fromisoformat(v.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        pass
    # try common formats
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
    ):
        try:
            dt = datetime.strptime(v, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            continue
    raise ValueError(f"unrecognized timestamp: {v}")


def zscore(values: List[Optional[float]]) -> List[Optional[float]]:
    arr = [x for x in values if x is not None]
    if not arr:
        return [None for _ in values]
    m = sum(arr) / len(arr)
    var = sum((x - m) ** 2 for x in arr) / len(arr)
    sd = math.sqrt(var)
    if sd == 0:
        return [0 if x is not None else None for x in values]
    return [((x - m) / sd) if x is not None else None for x in values]


# ------------------------
# 数据加载
# ------------------------

@dataclass
class Bar:
    ts: int  # epoch ms
    o: float
    h: float
    l: float
    c: float
    v: float


def load_csv_ohlcv(path: Path) -> List[Bar]:
    out: List[Bar] = []
    with path.open('r', encoding='utf-8') as f:
        rdr = csv.DictReader(f)
        # 兼容常见时间列名
        ts_key = None
        for cand in ("timestamp", "time", "ts", "date"):
            if cand in rdr.fieldnames:
                ts_key = cand
                break
        if ts_key is None:
            raise RuntimeError(f"CSV {path} missing timestamp column")
        def num(x):
            if x is None or x == "":
                return None
            try:
                return float(x)
            except Exception:
                return None
        for r in rdr:
            try:
                ts = parse_ts(r[ts_key])
                o = num(r.get('open'))
                h = num(r.get('high'))
                l = num(r.get('low'))
                c = num(r.get('close'))
                v = num(r.get('volume')) or 0.0
                if None in (o, h, l, c):
                    continue
                out.append(Bar(ts, o, h, l, c, v))
            except Exception:
                continue
    out.sort(key=lambda b: b.ts)
    return out


# ------------------------
# 技术指标
# ------------------------

def sma(vals: List[Optional[float]], n: int) -> List[Optional[float]]:
    q: deque = deque()
    s = 0.0
    out: List[Optional[float]] = []
    for x in vals:
        q.append(x)
        if x is not None:
            s += x
        if len(q) > n:
            y = q.popleft()
            if y is not None:
                s -= y
        valid = [t for t in q if t is not None]
        out.append((sum(valid) / len(valid)) if len(valid) == n else None)
    return out


def ema(vals: List[Optional[float]], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    k = 2 / (n + 1)
    prev = None
    for x in vals:
        if x is None:
            out.append(None)
            continue
        if prev is None:
            prev = x
        else:
            prev = x * k + prev * (1 - k)
        out.append(prev)
    return out


def donchian_high(prices: List[Optional[float]], n: int) -> List[Optional[float]]:
    q: deque = deque()
    out: List[Optional[float]] = []
    for x in prices:
        q.append(x)
        if len(q) > n:
            q.popleft()
        vals = [t for t in q if t is not None]
        out.append(max(vals) if len(vals) == n else None)
    return out


def donchian_low(prices: List[Optional[float]], n: int) -> List[Optional[float]]:
    q: deque = deque()
    out: List[Optional[float]] = []
    for x in prices:
        q.append(x)
        if len(q) > n:
            q.popleft()
        vals = [t for t in q if t is not None]
        out.append(min(vals) if len(vals) == n else None)
    return out


def true_range(h: List[Optional[float]], l: List[Optional[float]], c: List[Optional[float]]) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    prev_c: Optional[float] = None
    for hi, lo, cl in zip(h, l, c):
        if hi is None or lo is None or cl is None:
            out.append(None)
            prev_c = cl if cl is not None else prev_c
            continue
        trs = [hi - lo]
        if prev_c is not None:
            trs.append(abs(hi - prev_c))
            trs.append(abs(lo - prev_c))
        out.append(max(trs))
        prev_c = cl
    return out


def atr(h: List[Optional[float]], l: List[Optional[float]], c: List[Optional[float]], n: int) -> List[Optional[float]]:
    tr = true_range(h, l, c)
    return ema(tr, n)


# ------------------------
# 策略与回测
# ------------------------

@dataclass
class Config:
    L_ret: int = 60                 # 价格波幅收益窗口（bar 数）
    lookback_sma: int = 100         # SMA 回看期（动量1）
    ema_fast: int = 20              # EMA 快线
    ema_slow: int = 100             # EMA 慢线
    donchian_n: int = 20            # 唐奇安通道窗口
    atr_n: int = 14                 # ATR 窗口
    theta_ret: float = 0.003        # 价格波幅阈值（如 0.003=0.3%）
    rebalance_every: int = 15       # 重新打分/调仓间隔（bar 数）
    top_k: int = 5                  # 最大同时持仓数量（全市场）
    risk_per_trade: float = 0.005   # 单笔风险占权益的比例（0.5%）
    max_actual_leverage: float = 1.0  # 最大实际杠杆（总名义/权益）
    per_symbol_exposure_max: float = 0.2  # 单标的最大暴露占比
    min_actual_leverage: float = 0.0      # 单笔最小实际杠杆（0 表示不启用），例如 0.5 表示至少 0.5x
    fee_rate: float = 0.0006        # 费率（双边合计按成交名义）
    slippage_bps: float = 1.0       # 滑点（基点，1bp=0.01%）
    m1_init_sl_atr: float = 1.5     # 初始止损 ATR 倍数
    m2_trail_sl_atr: float = 2.0    # 移动止盈 ATR 倍数
    time_stop_bars: int = 12 * 60   # 时间止损（以 1m 数据计，默认 12h）
    initial_equity: float = 10000.0  # 初始资金（可由配置覆盖）
    allow_long: bool = True          # 允许做多
    allow_short: bool = True         # 允许做空
    # 市场过滤与动量阈值
    market_filter: bool = False      # 是否启用市场环境过滤
    market_symbol: str = 'BTC-USDT-SWAP'  # 市场基准符号
    market_L: int = 24               # 市场 ret_L 窗口
    market_theta: float = 0.002      # 市场阈值（绝对值小于该阈值则不交易）
    momentum_gate: bool = False      # 动量闸门：做多要求 mom1>0 & mom2>0（做空对称）
    z_score_thresh: float = 0.0      # z(mom1)+z(mom2) 的阈值
    # 金字塔加仓参数
    pyramid_max_adds: int = 3        # 每个持仓最多加仓次数
    pyramid_step_atr: float = 1.0    # 每次加仓的最小顺势位移（倍数ATR）
    pyramid_risk_multipliers: List[float] = field(default_factory=lambda: [1.0, 1.25, 1.5])
    # 保本/锁盈
    be_after_adds: int = 1
    be_rr: float = 1.0
    lock_after_adds: int = 2
    lock_atr_mult: float = 1.0
    # 候选池与冷却
    pool_size: int = 12               # 候选池大小（横截面择强）
    pool_mom_L1: int = 24 * 7         # 7天动量（1h=168）
    pool_mom_L2: int = 24 * 14        # 14天动量（1h=336）
    cooldown_bars: int = 24           # 亏损后冷却 bars 数
    # 收益率口径（仅用于报表展示）
    roi_mode: str = 'notional'        # 'notional' | 'margin' | 'equity'
    report_leverage: float = 10.0
    # 加仓后的保本/锁盈
    be_after_adds: int = 1           # 加仓达到多少次后启用保本（>=该次数）
    be_rr: float = 1.0               # 触发保本的R倍数（R=m1*ATR），如1.0表示浮盈≥1R
    lock_after_adds: int = 2         # 加仓达到多少次后启用台阶锁盈
    lock_atr_mult: float = 1.0       # 台阶锁盈：以上一次加仓价±lock_atr_mult*ATR 作为最低（最高）止损
    # 收益率口径（仅用于报表展示，不影响PnL/仓位）
    roi_mode: str = 'notional'       # 'notional' | 'margin' | 'equity'
    report_leverage: float = 10.0    # 当 roi_mode='margin' 时用于放大收益率的名义杠杆


@dataclass
class Position:
    symbol: str
    side: int  # +1 long, -1 short
    entry_ts: int
    entry_price: float
    qty: float
    init_stop: float
    trail_stop: float
    atr_mult: float
    max_fav_price: float
    reason: str = ""
    equity_entry: float = 0.0
    exposure_notional: float = 0.0
    exposure_frac: float = 0.0
    adds_done: int = 0
    last_add_price: float = 0.0
    acc_entry_notional: float = 0.0
    init_stop_dist: float = 0.0


@dataclass
class Trade:
    symbol: str
    side: str
    entry_ts: int
    entry_price: float
    exit_ts: int
    exit_price: float
    qty: float
    pnl: float
    pnl_pct: float
    fees: float
    reason: str
    equity_entry: float
    exposure_notional: float
    exposure_frac: float
    adds_done: int
    pnl_pct_raw: float = 0.0


def bps_to_price(price: float, bps: float) -> float:
    return price * (bps / 10000.0)


class Engine:
    def __init__(self, data: Dict[str, List[Bar]], cfg: Config, equity0: float = None):
        self.data = data
        self.cfg = cfg
        eq0 = cfg.initial_equity if equity0 is None else equity0
        self.equity = eq0
        self.cash = eq0
        self.trades: List[Trade] = []
        self.position: Dict[str, Position] = {}
        self.equity_curve: List[Tuple[int, float]] = []
        self._last_mtm: float = eq0

    def _compute_mtm(self, cur_bar: Dict[str, Optional[Bar]]) -> float:
        mtm = self.cash
        for s, pos in self.position.items():
            b = cur_bar.get(s)
            if not b:
                continue
            dir = 1 if pos.side > 0 else -1
            mtm += dir * pos.qty * (b.c - pos.entry_price)
        return mtm

    def run(self) -> None:
        # 构建全局时间轴与每个标的的游标
        syms = list(self.data.keys())
        all_ts = sorted(set(ts for s in syms for ts in (b.ts for b in self.data[s])))
        idx = {s: 0 for s in syms}
        # 市场基准（可选）
        m_ret_series: List[Tuple[int, Optional[float]]] = []
        m_ptr = 0
        mkt_ret_cur: Optional[float] = None
        if self.cfg.market_filter and self.cfg.market_symbol in self.data:
            m_bars = self.data[self.cfg.market_symbol]
            m_close = [b.c for b in m_bars]
            for i in range(len(m_bars)):
                j = i - self.cfg.market_L
                r = None
                if j >= 0 and m_close[j] != 0:
                    r = m_close[i] / m_close[j] - 1.0
                m_ret_series.append((m_bars[i].ts, r))
        # 预计算各标的指标（按各自 bar 对齐）
        features: Dict[str, Dict[str, List[Optional[float]]]] = {}
        for s in syms:
            bars = self.data[s]
            c = [b.c for b in bars]
            h = [b.h for b in bars]
            l = [b.l for b in bars]
            # 价格波幅收益 ret_L
            ret_L: List[Optional[float]] = [None] * len(c)
            for i in range(len(c)):
                j = i - self.cfg.L_ret
                if j >= 0 and c[j] != 0:
                    ret_L[i] = c[i] / c[j] - 1.0
            sma_v = sma(c, self.cfg.lookback_sma)
            ema_f = ema(c, self.cfg.ema_fast)
            ema_s = ema(c, self.cfg.ema_slow)
            don_hi = donchian_high(c, self.cfg.donchian_n)
            don_lo = donchian_low(c, self.cfg.donchian_n)
            atr_v = atr(h, l, c, self.cfg.atr_n)
            mom1: List[Optional[float]] = [None if sma_v[i] in (None, 0) else (c[i]/sma_v[i] - 1.0) for i in range(len(c))]
            mom2: List[Optional[float]] = [None if (ema_f[i] is None or ema_s[i] in (None, 0)) else (ema_f[i]/ema_s[i] - 1.0) for i in range(len(c))]
            # 候选池动量（长周期）
            L1 = max(1, int(self.cfg.pool_mom_L1)) if hasattr(self.cfg, 'pool_mom_L1') else 168
            L2 = max(1, int(self.cfg.pool_mom_L2)) if hasattr(self.cfg, 'pool_mom_L2') else 336
            momL1: List[Optional[float]] = [None]*len(c)
            momL2: List[Optional[float]] = [None]*len(c)
            for i in range(len(c)):
                j1 = i - L1
                j2 = i - L2
                if j1 >= 0 and c[j1] != 0:
                    momL1[i] = c[i]/c[j1] - 1.0
                if j2 >= 0 and c[j2] != 0:
                    momL2[i] = c[i]/c[j2] - 1.0
            # zscore 在 later 的横截面时点计算
            features[s] = {
                'ret_L': ret_L,
                'mom1': mom1,
                'mom2': mom2,
                'don_hi': don_hi,
                'don_lo': don_lo,
                'atr': atr_v,
                'momL1': momL1,
                'momL2': momL2,
            }

        last_rebalance_step = -10**9
        # 全局步进时点的最近 bar
        cur_bar: Dict[str, Optional[Bar]] = {s: None for s in syms}
        bars_since_entry: Dict[str, int] = defaultdict(int)
        cooldown: Dict[str, int] = defaultdict(int)  # 亏损后冷却计数

        for step, ts in enumerate(all_ts):
            # advance bars for each symbol up to current ts
            for s in syms:
                bars = self.data[s]
                while idx[s] < len(bars) and bars[idx[s]].ts <= ts:
                    cur_bar[s] = bars[idx[s]]
                    idx[s] += 1
                # update existing positions time-in-bar count
                if s in self.position and cur_bar[s] is not None:
                    bars_since_entry[s] += 1
            # 更新市场基准当前值
            if m_ret_series:
                while m_ptr < len(m_ret_series) and m_ret_series[m_ptr][0] <= ts:
                    mkt_ret_cur = m_ret_series[m_ptr][1]
                    m_ptr += 1

            # 更新移动止盈/止损并检查平仓
            to_close: List[Tuple[str, str]] = []  # (symbol, reason)
            for s, pos in list(self.position.items()):
                b = cur_bar.get(s)
                if b is None:
                    continue
                f = features[s]
                i = max(0, idx[s]-1)  # current bar index
                # update trailing based on max favorable price
                if pos.side > 0:
                    pos.max_fav_price = max(pos.max_fav_price, b.h)
                    atr_i = (f['atr'][i] or 0.0)
                    trail = pos.max_fav_price - self.cfg.m2_trail_sl_atr * atr_i
                    pos.trail_stop = max(pos.trail_stop, trail)
                    # 保本/锁盈（仅在达到指定加仓次数后生效）
                    if pos.adds_done >= self.cfg.be_after_adds and atr_i > 0:
                        r_move = b.c - pos.entry_price
                        if r_move >= self.cfg.be_rr * pos.atr_mult * atr_i:
                            pos.trail_stop = max(pos.trail_stop, pos.entry_price)
                    if pos.adds_done >= self.cfg.lock_after_adds and atr_i > 0 and pos.last_add_price:
                        lock_stop = pos.last_add_price - self.cfg.lock_atr_mult * atr_i
                        pos.trail_stop = max(pos.trail_stop, lock_stop)
                    # 触发止盈/止损
                    if b.l <= pos.trail_stop:
                        to_close.append((s, 'trail_stop'))
                    elif (bars_since_entry[s] >= self.cfg.time_stop_bars):
                        to_close.append((s, 'time_stop'))
                else:  # short
                    pos.max_fav_price = min(pos.max_fav_price, b.l)
                    atr_i = (f['atr'][i] or 0.0)
                    trail = pos.max_fav_price + self.cfg.m2_trail_sl_atr * atr_i
                    pos.trail_stop = min(pos.trail_stop, trail)
                    if pos.adds_done >= self.cfg.be_after_adds and atr_i > 0:
                        r_move = pos.entry_price - b.c
                        if r_move >= self.cfg.be_rr * pos.atr_mult * atr_i:
                            pos.trail_stop = min(pos.trail_stop, pos.entry_price)
                    if pos.adds_done >= self.cfg.lock_after_adds and atr_i > 0 and pos.last_add_price:
                        lock_stop = pos.last_add_price + self.cfg.lock_atr_mult * atr_i
                        pos.trail_stop = min(pos.trail_stop, lock_stop)
                    if b.h >= pos.trail_stop:
                        to_close.append((s, 'trail_stop'))
                    elif (bars_since_entry[s] >= self.cfg.time_stop_bars):
                        to_close.append((s, 'time_stop'))

            for s, reason in to_close:
                # 关闭并判断是否亏损以设置冷却
                self._exit_position(s, cur_bar[s], reason)
                if self.trades and self.trades[-1].symbol == s and self.trades[-1].pnl < 0:
                    cooldown[s] = max(cooldown.get(s, 0), getattr(self.cfg, 'cooldown_bars', 0))
                bars_since_entry.pop(s, None)

            # 调仓与入场
            if step - last_rebalance_step >= self.cfg.rebalance_every:
                last_rebalance_step = step

                # 计算该时点 mom1/mom2 的横截面 zscore
                mom1_vals: List[Optional[float]] = []
                mom2_vals: List[Optional[float]] = []
                val_syms: List[str] = []
                val_idx: Dict[str, int] = {}
                for s in syms:
                    i = max(0, idx[s]-1)
                    if cur_bar[s] is None:
                        continue
                    f = features[s]
                    m1 = f['mom1'][i]
                    m2 = f['mom2'][i]
                    if m1 is None or m2 is None:
                        continue
                    mom1_vals.append(m1)
                    mom2_vals.append(m2)
                    val_syms.append(s)
                    val_idx[s] = i
                z1 = zscore(mom1_vals)
                z2 = zscore(mom2_vals)

                # 冷却递减
                for k in list(cooldown.keys()):
                    if cooldown[k] <= 0:
                        cooldown.pop(k, None)
                    else:
                        cooldown[k] -= 1

                # 候选池：按 momL1+momL2 排序取前 pool_size
                pool_scores: List[Tuple[str, float]] = []
                for s in val_syms:
                    i = val_idx[s]
                    f = features[s]
                    ml1 = f.get('momL1', [None])[i]
                    ml2 = f.get('momL2', [None])[i]
                    sc = (ml1 if ml1 is not None else 0.0) + (ml2 if ml2 is not None else 0.0)
                    pool_scores.append((s, sc))
                pool_scores.sort(key=lambda t: t[1], reverse=True)
                pool_k = max(1, int(getattr(self.cfg, 'pool_size', 12)))
                pool_set = set(s for s,_ in pool_scores[:pool_k])

                # 构建候选列表并做顺势对齐过滤（动量闸门、Z分数阈值、市场过滤、候选池、冷却）
                candidates: List[Tuple[str, int, float]] = []  # (symbol, side, score)
                for (s, z1v, z2v) in zip(val_syms, z1, z2):
                    if s not in pool_set or cooldown.get(s, 0) > 0:
                        continue
                    i = val_idx[s]
                    f = features[s]
                    b = cur_bar[s]
                    if b is None:
                        continue
                    ret_L = f['ret_L'][i]
                    atr_v = f['atr'][i] or 0.0
                    don_hi = f['don_hi'][i]
                    don_lo = f['don_lo'][i]
                    if ret_L is None or atr_v is None:
                        continue
                    score = (z1v or 0.0) + (z2v or 0.0)
                    m1 = f['mom1'][i]
                    m2 = f['mom2'][i]
                    # 市场过滤（若启用且市场方向/强度不足则跳过）
                    if m_ret_series:
                        if mkt_ret_cur is None or abs(mkt_ret_cur) < self.cfg.market_theta:
                            continue
                    # 做多候选
                    long_ok = (
                        self.cfg.allow_long and ret_L > self.cfg.theta_ret and
                        don_hi is not None and b.c >= don_hi and
                        (not self.cfg.momentum_gate or (m1 is not None and m2 is not None and m1 > 0 and m2 > 0)) and
                        (self.cfg.z_score_thresh <= 0 or score >= self.cfg.z_score_thresh)
                    )
                    if m_ret_series and long_ok:
                        long_ok = long_ok and (mkt_ret_cur is not None and mkt_ret_cur > self.cfg.market_theta)
                    # 做空候选
                    short_ok = (
                        self.cfg.allow_short and ret_L < -self.cfg.theta_ret and
                        don_lo is not None and b.c <= don_lo and
                        (not self.cfg.momentum_gate or (m1 is not None and m2 is not None and m1 < 0 and m2 < 0)) and
                        (self.cfg.z_score_thresh <= 0 or -score >= self.cfg.z_score_thresh)
                    )
                    if m_ret_series and short_ok:
                        short_ok = short_ok and (mkt_ret_cur is not None and mkt_ret_cur < -self.cfg.market_theta)
                    if long_ok:
                        candidates.append((s, +1, score))
                    elif short_ok:
                        candidates.append((s, -1, -score))

                # 按分数排序，开仓至不超过 Top-K，且满足暴露约束
                candidates.sort(key=lambda t: t[2], reverse=True)
                # 对齐失效则平仓
                for s, pos in list(self.position.items()):
                    i = max(0, idx[s]-1)
                    f = features[s]
                    ret_L = f['ret_L'][i]
                    if ret_L is None:
                        continue
                    if (pos.side > 0 and ret_L < 0) or (pos.side < 0 and ret_L > 0):
                        self._exit_position(s, cur_bar[s], 'alignment_lost')
                        bars_since_entry.pop(s, None)

                # 现有持仓尝试“顺势加仓（金字塔）”
                for s, pos in list(self.position.items()):
                    if pos.adds_done >= self.cfg.pyramid_max_adds:
                        continue
                    b = cur_bar.get(s)
                    if b is None:
                        continue
                    f = features[s]
                    i = max(0, idx[s]-1)
                    atr_v = f['atr'][i] or 0.0
                    if atr_v <= 0:
                        continue
                    don_hi = f['don_hi'][i]
                    don_lo = f['don_lo'][i]
                    ret_L = f['ret_L'][i]
                    if ret_L is None:
                        continue
                    # 仅顺势加仓且需满足突破方向条件
                    want_long = (pos.side > 0 and ret_L > self.cfg.theta_ret and don_hi is not None and b.c >= don_hi)
                    want_short = (pos.side < 0 and ret_L < -self.cfg.theta_ret and don_lo is not None and b.c <= don_lo)
                    if not (want_long or want_short):
                        continue
                    # 价格相对上次加仓/入场已推进 pyramid_step_atr * ATR
                    step_ok = False
                    if pos.side > 0 and b.c >= (pos.last_add_price or pos.entry_price) + self.cfg.pyramid_step_atr * atr_v:
                        step_ok = True
                    if pos.side < 0 and b.c <= (pos.last_add_price or pos.entry_price) - self.cfg.pyramid_step_atr * atr_v:
                        step_ok = True
                    if not step_ok:
                        continue
                    # 资金与暴露约束
                    mtm_now = self._compute_mtm(cur_bar)
                    exposure_cur = sum(abs(p.qty * cur_bar[s2].c) for s2, p in self.position.items() if cur_bar.get(s2))
                    total_cap = self.cfg.max_actual_leverage * mtm_now
                    headroom = max(0.0, total_cap - exposure_cur)
                    per_symbol_cap = self.cfg.per_symbol_exposure_max * mtm_now
                    # 本次加仓的风险额度
                    mult_list = self.cfg.pyramid_risk_multipliers or [1.0]
                    mult = mult_list[min(pos.adds_done, len(mult_list)-1)]
                    risk_amount = mtm_now * self.cfg.risk_per_trade * mult
                    stop_dist = self.cfg.m1_init_sl_atr * atr_v
                    if risk_amount <= 0 or stop_dist <= 0:
                        continue
                    base_qty = risk_amount / stop_dist
                    add_notional = abs(base_qty * b.c)
                    # 受最小实际杠杆下限影响：若下限更大，则抬升到该下限的一部分（这里只针对新增）
                    min_notional = self.cfg.min_actual_leverage * mtm_now if self.cfg.min_actual_leverage > 0 else 0.0
                    desired_notional = max(add_notional, min_notional - exposure_cur)
                    allowed = min(headroom, per_symbol_cap - abs(pos.qty * b.c))
                    if allowed <= 0:
                        continue
                    final_notional = min(desired_notional, allowed)
                    if final_notional <= 0:
                        continue
                    add_qty = final_notional / max(b.c, 1e-9)
                    # 应用滑点
                    slip = bps_to_price(b.c, self.cfg.slippage_bps)
                    add_price = b.c + (slip if pos.side > 0 else -slip)
                    # 重新加权平均持仓
                    new_qty = pos.qty + add_qty
                    if new_qty <= 0:
                        continue
                    pos.entry_price = (pos.entry_price * pos.qty + add_price * add_qty) / new_qty
                    pos.qty = new_qty
                    pos.exposure_notional = abs(pos.qty * add_price)
                    pos.exposure_frac = pos.exposure_notional / max(mtm_now, 1e-9)
                    pos.adds_done += 1
                    pos.last_add_price = add_price
                    pos.acc_entry_notional += abs(add_qty * add_price)

                for s, side, score in candidates:
                    if len(self.position) >= self.cfg.top_k:
                        break
                    if s in self.position:
                        continue
                    b = cur_bar[s]
                    i = max(0, idx[s]-1)
                    f = features[s]
                    atr_v = f['atr'][i] or 0.0
                    if b is None or atr_v <= 0:
                        continue
                    # 头寸规模：按单笔风险与止损距离（m1*ATR）
                    stop_dist = self.cfg.m1_init_sl_atr * atr_v
                    # approximate contract as linear: qty * price exposure
                    # risk = stop_dist * qty => qty = risk / stop_dist
                    # 使用当前权益（含未实现盈亏）
                    mtm_now = self._compute_mtm(cur_bar)
                    risk_amount = mtm_now * self.cfg.risk_per_trade
                    if risk_amount <= 0:
                        continue
                    qty = risk_amount / max(stop_dist, 1e-9)
                    # 组合/单标暴露约束（实际杠杆与单标上限）
                    exposure_cur = sum(abs(p.qty * cur_bar[s2].c) for s2, p in self.position.items() if cur_bar.get(s2))
                    total_cap = self.cfg.max_actual_leverage * mtm_now
                    headroom = max(0.0, total_cap - exposure_cur)
                    # 应用“最小实际杠杆”下限（可选）
                    notional_risk = abs(qty * b.c)
                    min_notional = self.cfg.min_actual_leverage * mtm_now if self.cfg.min_actual_leverage > 0 else 0.0
                    desired_notional = max(notional_risk, min_notional)
                    per_symbol_cap = self.cfg.per_symbol_exposure_max * mtm_now
                    allowed_notional = min(per_symbol_cap, headroom)
                    if allowed_notional <= 0:
                        continue
                    final_notional = min(desired_notional, allowed_notional)
                    if final_notional <= 0:
                        continue
                    qty = final_notional / max(b.c, 1e-9)
                    # 建立仓位，入场考虑滑点
                    slip = bps_to_price(b.c, self.cfg.slippage_bps)
                    entry_price = b.c + (slip if side > 0 else -slip)
                    init_stop = entry_price - side * stop_dist
                    trail = init_stop
                    max_fav = b.h if side > 0 else b.l
                    exposure_notional = abs(qty * entry_price)
                    exposure_frac = exposure_notional / max(mtm_now, 1e-9)
                    self.position[s] = Position(
                        symbol=s, side=side, entry_ts=b.ts, entry_price=entry_price,
                        qty=qty, init_stop=init_stop, trail_stop=trail, atr_mult=self.cfg.m1_init_sl_atr,
                        max_fav_price=max_fav, reason='entry', equity_entry=mtm_now,
                        exposure_notional=exposure_notional, exposure_frac=exposure_frac,
                        adds_done=0, last_add_price=entry_price, acc_entry_notional=exposure_notional,
                        init_stop_dist=stop_dist
                    )
                    bars_since_entry[s] = 0

            # 记录权益曲线（按收盘价盯市）
            mtm = self._compute_mtm(cur_bar)
            self.equity_curve.append((ts, mtm))
            self._last_mtm = mtm

        # 收盘清算剩余持仓
        for s in list(self.position.keys()):
            self._exit_position(s, cur_bar.get(s), 'eod')

    def _exit_position(self, symbol: str, bar: Optional[Bar], reason: str) -> None:
        pos = self.position.get(symbol)
        if pos is None or bar is None:
            return
        # 平仓考虑滑点
        slip = bps_to_price(bar.c, self.cfg.slippage_bps)
        exit_price = bar.c - (slip if pos.side > 0 else -slip)
        dir = 1 if pos.side > 0 else -1
        gross = dir * pos.qty * (exit_price - pos.entry_price)
        notional_entry = pos.acc_entry_notional if hasattr(pos, 'acc_entry_notional') and pos.acc_entry_notional else abs(pos.qty * pos.entry_price)
        notional_exit = abs(pos.qty * exit_price)
        fees = self.cfg.fee_rate * (notional_entry + notional_exit)
        pnl = gross - fees
        self.cash += pnl
        # 收益率口径：名义/保证金/权益
        base_pct = pnl / max(notional_entry, 1e-9)
        pnl_pct = base_pct
        if getattr(self.cfg, 'roi_mode', 'notional') == 'margin':
            pnl_pct = base_pct * float(getattr(self.cfg, 'report_leverage', 10.0))
        elif getattr(self.cfg, 'roi_mode', 'notional') == 'equity':
            pnl_pct = pnl / max(self.position[symbol].equity_entry, 1e-9)
        self.trades.append(Trade(
            symbol=pos.symbol,
            side='long' if pos.side > 0 else 'short',
            entry_ts=pos.entry_ts,
            entry_price=pos.entry_price,
            exit_ts=bar.ts,
            exit_price=exit_price,
            qty=pos.qty,
            pnl=pnl,
            pnl_pct=pnl_pct,
            fees=fees,
            reason=reason,
            equity_entry=pos.equity_entry,
            exposure_notional=pos.exposure_notional,
            exposure_frac=pos.exposure_frac,
            adds_done=getattr(pos, 'adds_done', 0),
        ))
        del self.position[symbol]


# ------------------------
# 报表导出
# ------------------------

def split_stages(trades: List[Trade]) -> List[List[Trade]]:
    tr = sorted(trades, key=lambda t: t.entry_ts)
    n = len(tr)
    if n == 0:
        return [tr]
    k = n // 3
    if k == 0:
        return [tr]
    return [tr[:k], tr[k:2*k], tr[2*k:]]


def compute_summary(trades: List[Trade]) -> Dict[str, float]:
    if not trades:
        return {
            'N': 0,
            'win_rate': None,
            'pnl_sum': 0.0,
            'pnl_mean': None,
            'roi_mean': None,
            'roi_std': None,
            'payoff': None,
        }
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    roi = [t.pnl_pct for t in trades]
    m = sum(roi) / len(roi)
    var = sum((x - m) ** 2 for x in roi) / len(roi)
    std = math.sqrt(var)
    def mean(a):
        return sum(a) / len(a) if a else None
    payoff = None
    if wins and losses:
        payoff = mean([t.pnl_pct for t in wins]) / abs(mean([t.pnl_pct for t in losses]))
    return {
        'N': len(trades),
        'win_rate': len(wins) / len(trades),
        'pnl_sum': sum(t.pnl for t in trades),
        'pnl_mean': sum(t.pnl for t in trades) / len(trades),
        'roi_mean': m,
        'roi_std': std,
        'payoff': payoff,
    }


def export_trades(trades: List[Trade], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        # 中文表头（去掉时间戳，新增持仓天数、当前收益(占当前权益)）
        w.writerow(['交易对','方向','开仓时间','开仓价','平仓时间','平仓价','数量','收益','收益率','手续费','原因','仓位','累计收益','实际杠杆','当前回撤','持仓天数','当前收益(占权益)','加仓次数'])
        reason_map = {
            'trail_stop': '移动止盈/止损',
            'alignment_lost': '对齐失效',
            'time_stop': '时间止损',
            'eod': '收盘清算',
        }
        # 计算累计收益与回撤（按成交顺序）
        cum = 0.0
        peak = 0.0
        init_eq = trades[0].equity_entry if trades else 0.0
        # 若无可用入口权益，则以 10000 为基准
        base_eq = float(init_eq) if init_eq else 10000.0
        for t in trades:
            cum += t.pnl if hasattr(t, 'pnl') else 0.0
            eq = base_eq + cum
            peak = max(peak, eq)
            dd = (eq - peak) / peak if peak > 0 else 0.0
            hold_days = max(0.0, (t.exit_ts - t.entry_ts) / 86400000.0)
            curr_ret_on_equity = (t.pnl / eq) if eq > 0 else 0.0
            w.writerow([
                t.symbol,
                '多' if t.side == 'long' else '空',
                datetime.utcfromtimestamp(t.entry_ts/1000).strftime('%Y-%m-%d %H:%M:%S'),
                f"{t.entry_price:.8f}",
                datetime.utcfromtimestamp(t.exit_ts/1000).strftime('%Y-%m-%d %H:%M:%S'),
                f"{t.exit_price:.8f}",
                f"{t.qty:.6f}",
                f"{t.pnl:.2f}",
                f"{t.pnl_pct:.4f}",
                f"{t.fees:.2f}",
                reason_map.get(t.reason, t.reason),
                f"{t.exposure_frac:.4f}",
                f"{cum:.2f}",
                f"{t.exposure_frac:.4f}",
                f"{dd:.4f}",
                f"{hold_days:.4f}",
                f"{curr_ret_on_equity:.6f}",
                getattr(t, 'adds_done', 0),
            ])


def export_summary(trades: List[Trade], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    labels = ['前期','中期','后期']
    stages = split_stages(trades)
    with out_path.open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        # 中文表头（在末尾追加分布统计与分位数列）
        w.writerow([
            '分类','阶段','笔数','胜率','盈亏比','总收益','单笔均值','收益率均值','收益率波动',
            '收益率最小','收益率最大','收益率偏度',
            'p01','p05','p10','p25','p50','p75','p90','p95','p99'
        ])
        # per-stage
        for lab, st in zip(labels, stages):
            s = compute_summary(st)
            w.writerow([
                '阶段', lab, s['N'],
                f"{s['win_rate']:.4f}" if s['win_rate'] is not None else '',
                f"{s['payoff']:.4f}" if s['payoff'] is not None else '',
                f"{s['pnl_sum']:.2f}",
                f"{s['pnl_mean']:.2f}" if s['pnl_mean'] is not None else '',
                f"{s['roi_mean']:.4f}" if s['roi_mean'] is not None else '',
                f"{s['roi_std']:.4f}" if s['roi_std'] is not None else '',
                '', '', '', '', '', '', '', '', '', '', ''
            ])
        # overall
        s = compute_summary(trades)
        w.writerow([
            '总体','总体', s['N'],
            f"{s['win_rate']:.4f}" if s['win_rate'] is not None else '',
            f"{s['payoff']:.4f}" if s['payoff'] is not None else '',
            f"{s['pnl_sum']:.2f}",
            f"{s['pnl_mean']:.2f}" if s['pnl_mean'] is not None else '',
            f"{s['roi_mean']:.4f}" if s['roi_mean'] is not None else '',
            f"{s['roi_std']:.4f}" if s['roi_std'] is not None else '',
            '', '', '', '', '', '', '', '', '', '', ''
        ])

        # 分布统计（总体、多、空）
        def dist_and_quantiles(ts: List[Trade]):
            rois = [t.pnl_pct for t in ts if t is not None and t.pnl_pct is not None]
            if not rois:
                return None
            rois.sort()
            n = len(rois)
            mean = sum(rois)/n
            median = rois[n//2] if n%2==1 else (rois[n//2-1]+rois[n//2])/2
            var = sum((x-mean)**2 for x in rois)/n
            std = math.sqrt(var)
            mn = rois[0]
            mx = rois[-1]
            m3 = sum((x-mean)**3 for x in rois)/n
            skew = (m3/(std**3)) if std>0 else 0.0
            def pct(p):
                if n==1:
                    return rois[0]
                x = p*(n-1)
                i = int(math.floor(x))
                j = min(n-1, i+1)
                w = x - i
                return rois[i]*(1-w) + rois[j]*w
            qs = [pct(q) for q in (0.01,0.05,0.10,0.25,0.50,0.75,0.90,0.95,0.99)]
            return dict(N=n, mean=mean, median=median, std=std, mn=mn, mx=mx, skew=skew, qs=qs)

        # 额外指标（总体、多、空）：回撤、费用、胜/亏均值、利润因子、持仓时间与杠杆
        def extra_metrics(ts: List[Trade]):
            if not ts:
                return None
            # 基于成交点近似的权益与回撤
            base_eq = ts[0].equity_entry if ts[0].equity_entry else 10000.0
            cum = 0.0; peak = base_eq; max_dd = 0.0
            for t in ts:
                cum += t.pnl
                eq = base_eq + cum
                if eq > peak: peak = eq
                if peak > 0:
                    dd = (eq - peak) / peak
                    if dd < max_dd: max_dd = dd
            fees = sum(t.fees for t in ts)
            wins = [t for t in ts if t.pnl > 0]
            losses = [t for t in ts if t.pnl < 0]
            gp = sum(t.pnl for t in wins)
            gl = -sum(t.pnl for t in losses)  # 正数
            pf = (gp/gl) if gl>0 else None
            wr = (len(wins)/len(ts)) if ts else None
            avg_win_roi = (sum(t.pnl_pct for t in wins)/len(wins)) if wins else None
            avg_loss_roi = (sum(t.pnl_pct for t in losses)/len(losses)) if losses else None
            avg_win = (sum(t.pnl for t in wins)/len(wins)) if wins else None
            avg_loss = (sum(t.pnl for t in losses)/len(losses)) if losses else None
            # 近似持仓时间
            import statistics as st
            holds = [ (t.exit_ts - t.entry_ts)/86400000.0 for t in ts ]
            hold_mean = (sum(holds)/len(holds)) if holds else None
            hold_med = (st.median(holds) if holds else None)
            # 平均实际杠杆（单笔）与组合平均（以单笔加总暴露近似）
            lev_mean = (sum(getattr(t,'exposure_frac',0.0) for t in ts)/len(ts))
            return dict(max_dd=max_dd, fees=fees, pf=pf, wr=wr,
                        avg_win_roi=avg_win_roi, avg_loss_roi=avg_loss_roi,
                        avg_win=avg_win, avg_loss=avg_loss,
                        hold_mean=hold_mean, hold_med=hold_med,
                        lev_mean=lev_mean)

        buckets = {
            '分布-总体': trades,
            '分布-多': [t for t in trades if t.side=='long'],
            '分布-空': [t for t in trades if t.side=='short'],
        }
        for name, arr in buckets.items():
            d = dist_and_quantiles(arr)
            if not d:
                continue
            w.writerow([
                name,'—', d['N'], '', '', '', '',
                f"{d['mean']:.4f}", f"{d['std']:.4f}", f"{d['mn']:.4f}", f"{d['mx']:.4f}", f"{d['skew']:.4f}",
                *[f"{q:.4f}" for q in d['qs']]
            ])

        # 追加指标行（总体/多/空）
        for name, arr in buckets.items():
            e = extra_metrics(arr)
            if not e:
                continue
            w.writerow([
                name.replace('分布','指标'),'—', len(arr),
                f"{e['wr']:.4f}" if e['wr'] is not None else '',
                '',  # 盈亏比已在上方给出
                '', '',  # 收益合计/均值不再重复
                '', '',  # 收益率均值/波动不再重复
                '', '', '',  # 最小/最大/偏度
                # 分位数位空
                '', '', '', '', '', '', '', '',
            ])
            w.writerow([
                name.replace('分布','指标-明细'),'—', '', '', '', '', '', '', '', '', '', '',
                f"MaxDD={e['max_dd']:.4f}", f"Fees={e['fees']:.2f}", f"PF={(e['pf'] if e['pf'] is not None else 0):.4f}",
                f"AvgWinROI={(e['avg_win_roi'] if e['avg_win_roi'] is not None else 0):.4f}",
                f"AvgLossROI={(e['avg_loss_roi'] if e['avg_loss_roi'] is not None else 0):.4f}",
                f"AvgWin={(e['avg_win'] if e['avg_win'] is not None else 0):.2f}",
                f"AvgLoss={(e['avg_loss'] if e['avg_loss'] is not None else 0):.2f}",
                f"HoldMean={(e['hold_mean'] if e['hold_mean'] is not None else 0):.4f}",
                f"HoldMed={(e['hold_med'] if e['hold_med'] is not None else 0):.4f}",
                f"LevMean={e['lev_mean']:.4f}"
            ])


# ------------------------
# 命令行接口（支持中文参数名）
# ------------------------

def load_config(path: Optional[Path]) -> Config:
    if path is None or not path.exists():
        return Config()
    with path.open('r', encoding='utf-8') as f:
        raw = json.load(f)
    # 支持中文与英文键名
    key_map = {
        '价格波幅窗口': 'L_ret',
        'SMA回看期': 'lookback_sma',
        'EMA快线': 'ema_fast',
        'EMA慢线': 'ema_slow',
        '唐奇安窗口': 'donchian_n',
        'ATR窗口': 'atr_n',
        '价格波幅阈值': 'theta_ret',
        '调仓间隔': 'rebalance_every',
        '最多持仓数': 'top_k',
        '单笔风险占比': 'risk_per_trade',
        '最大实际杠杆': 'max_actual_leverage',
        '单标的最大暴露占比': 'per_symbol_exposure_max',
        '手续费率': 'fee_rate',
        '滑点基点': 'slippage_bps',
        '初始止损ATR倍数': 'm1_init_sl_atr',
        '移动止盈ATR倍数': 'm2_trail_sl_atr',
        '时间止损bar数': 'time_stop_bars',
        '初始资金': 'initial_equity',
        '允许做多': 'allow_long',
        '允许做空': 'allow_short',
        '市场过滤': 'market_filter',
        '市场基准': 'market_symbol',
        '市场窗口': 'market_L',
        '市场阈值': 'market_theta',
        '动量闸门': 'momentum_gate',
        'Z分数阈值': 'z_score_thresh',
        '金字塔加仓次数': 'pyramid_max_adds',
        '金字塔步长ATR': 'pyramid_step_atr',
        '金字塔风险乘数': 'pyramid_risk_multipliers',
        '收益率口径': 'roi_mode',
        '报告杠杆': 'report_leverage',
        '候选池大小': 'pool_size',
        '候选池7天窗口': 'pool_mom_L1',
        '候选池14天窗口': 'pool_mom_L2',
        '冷却bars': 'cooldown_bars',
        '保本加仓次数': 'be_after_adds',
        '保本R阈值': 'be_rr',
        '锁盈加仓次数': 'lock_after_adds',
        '锁盈ATR倍数': 'lock_atr_mult'
    }
    cfg = Config()
    for k, v in raw.items():
        key = key_map.get(k, k)
        if hasattr(cfg, key):
            setattr(cfg, key, v)
    return cfg


def main():
    p = argparse.ArgumentParser(description='横截面趋势流水线回测')
    # 同时支持英文与中文参数名
    p.add_argument('--data-dir', '--数据目录', dest='data_dir', required=True, help='含各标的 OHLCV CSV 的目录')
    p.add_argument('--symbols', '--标的', dest='symbols', required=False, help='以逗号分隔的符号列表；若省略，则自动扫描目录中所有 .csv 文件')
    p.add_argument('--out-dir', '--输出目录', dest='out_dir', default='output', help='成交与汇总 CSV 输出目录')
    p.add_argument('--config', '--配置文件', dest='config', default=None, help='JSON 配置文件路径（可选，支持中文键名）')
    args = p.parse_args()

    data_dir = Path(args.data_dir)
    out_dir = Path(args.out_dir)
    if args.symbols:
        sym_list = [s.strip() for s in args.symbols.split(',') if s.strip()]
    else:
        sym_list = [p.stem for p in data_dir.glob('*.csv')]
        if not sym_list:
            raise SystemExit(f"数据目录中未发现任何 CSV：{data_dir}")
    cfg = load_config(Path(args.config) if args.config else None)

    data: Dict[str, List[Bar]] = {}
    for s in sym_list:
        path = data_dir / f"{s}.csv"
        if not path.exists():
            raise SystemExit(f"Missing data file: {path}")
        data[s] = load_csv_ohlcv(path)
        if not data[s]:
            raise SystemExit(f"No valid rows in: {path}")

    engine = Engine(data, cfg)
    engine.run()

    trades_path = out_dir / 'trades.csv'
    summary_path = out_dir / 'strategy_summary.csv'
    export_trades(engine.trades, trades_path)
    export_summary(engine.trades, summary_path)

    print(f"已写入成交: {trades_path}")
    print(f"已写入汇总: {summary_path}")


if __name__ == '__main__':
    main()

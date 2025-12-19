import sys
import time
import threading
import os
import tempfile
# NOTE: inserted comment after line 3 for verification
import json
import subprocess
import platform
from collections import deque, defaultdict
import math
import random
from typing import List, Dict
import requests

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtCore import QUrl
from alert import AlertManager


class CryptoDataFetcher:
    def __init__(self):
        # No fixed list; the UI supplies desired CoinGecko IDs per slot
        self.crypto_data: List[Dict] = []
        # Cached Binance symbol sets (lowercase), with simple TTLs
        self._spot_symbols_cache: tuple[float, set[str]] | None = None
        self._futures_symbols_cache: tuple[float, set[str]] | None = None

    def _now(self) -> float:
        try:
            return time.time()
        except Exception:
            return 0.0

    def fetch_binance_spot_symbols_set(self, ttl_sec: int = 6 * 3600) -> set[str]:
        try:
            if self._spot_symbols_cache:
                ts, cached = self._spot_symbols_cache
                if self._now() - ts < ttl_sec:
                    return set(cached)
            url = "https://api.binance.com/api/v3/exchangeInfo"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json() or {}
            out: set[str] = set()
            for s in data.get("symbols", []):
                try:
                    if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT" and bool(s.get("isSpotTradingAllowed", True)):
                        sym = (s.get("symbol") or "").lower()
                        if sym:
                            out.add(sym)
                except Exception:
                    pass
            self._spot_symbols_cache = (self._now(), set(out))
            return out
        except Exception:
            # On error, return whatever we have cached, or empty
            return set(self._spot_symbols_cache[1]) if self._spot_symbols_cache else set()

    def fetch_binance_futures_symbols_set(self, ttl_sec: int = 6 * 3600) -> set[str]:
        try:
            if self._futures_symbols_cache:
                ts, cached = self._futures_symbols_cache
                if self._now() - ts < ttl_sec:
                    return set(cached)
            url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json() or {}
            out: set[str] = set()
            for s in data.get("symbols", []):
                try:
                    if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT":
                        sym = (s.get("symbol") or "").lower()
                        if sym:
                            out.add(sym)
                except Exception:
                    pass
            self._futures_symbols_cache = (self._now(), set(out))
            return out
        except Exception:
            return set(self._futures_symbols_cache[1]) if self._futures_symbols_cache else set()

    def fetch_crypto_prices(self) -> List[Dict]:
        try:
            ids = "bitcoin,eth,cardano,polkadot,chainlink"
            url = "https://api.coingecko.com/api/v3/coins/markets"
            params = {
                "vs_currency": "usd",
                "ids": ids,
                "order": "market_cap_desc",
                "per_page": 10,
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "24h",
            }
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return data
        except Exception:
            # Fallback demo values so UI always has content
            return [
                {"id": "bitcoin", "symbol": "btc", "current_price": 100000.0, "price_change_percentage_24h": 1.5},
                {"id": "eth", "symbol": "eth", "current_price": 4000.0, "price_change_percentage_24h": 1.2},
            ]

    def fetch_crypto_prices_for_ids(self, ids: List[str]) -> List[Dict]:
        try:
            if not ids:
                return []
            url = "https://api.coingecko.com/api/v3/coins/markets"
            params = {
                "vs_currency": "usd",
                "ids": ",".join(ids),
                "order": "market_cap_desc",
                "per_page": max(len(ids), 1),
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "24h",
            }
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            # Return fallback for requested IDs
            out: List[Dict] = []
            for cid in ids:
                out.append({
                    "id": cid,
                    "symbol": cid[:3],
                    "current_price": 1000.0,
                    "price_change_percentage_24h": 1.0,
                })
            return out


class FetchThread(QtCore.QThread):
    # Kept for optional HTTP fallback (unused by default)
    data_ready = QtCore.Signal(list)

    def __init__(self, fetcher: CryptoDataFetcher, ids: List[str]):
        super().__init__()
        self.fetcher = fetcher
        self.ids = ids

    def run(self):
        data = self.fetcher.fetch_crypto_prices_for_ids(self.ids)
        self.data_ready.emit(data)


class PriceWS(QtCore.QObject):
    price_update = QtCore.Signal(str, float, float)  # pair, price, pct

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ws_spot: QWebSocket | None = None
        self.ws_futures: QWebSocket | None = None
        self.pairs_spot: list[str] = []
        self.pairs_futures: list[str] = []
        self._reconnect_timer = QtCore.QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._reconnect)
        self.last_quote_volume: dict[str, float] = {}

    def connect_pairs(self, spot_pairs: list[str], futures_pairs: list[str]):
        self.pairs_spot = [p.lower() for p in spot_pairs if isinstance(p, str) and p]
        self.pairs_futures = [p.lower() for p in futures_pairs if isinstance(p, str) and p]
        self._open()

    def close(self):
        for attr in ("ws_spot", "ws_futures"):
            ws = getattr(self, attr, None)
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
                try:
                    ws.deleteLater()
                except Exception:
                    pass
                setattr(self, attr, None)

    def _open(self):
        self.close()
        if not self.pairs_spot and not self.pairs_futures:
            print("[WS] no pairs to subscribe")
            return
        if self.pairs_spot:
            url_spot = "wss://stream.binance.com:9443/stream?streams=" + "/".join(
                f"{p}@miniTicker" for p in self.pairs_spot
            )
            self.ws_spot = QWebSocket()
            self.ws_spot.textMessageReceived.connect(lambda m: self._on_msg(m))
            self.ws_spot.errorOccurred.connect(self._on_error)
            self.ws_spot.disconnected.connect(self._on_closed)
            print(f"[WS] opening SPOT {url_spot}")
            self.ws_spot.open(QUrl(url_spot))
        if self.pairs_futures:
            url_fut = "wss://fstream.binance.com/stream?streams=" + "/".join(
                f"{p}@miniTicker" for p in self.pairs_futures
            )
            self.ws_futures = QWebSocket()
            self.ws_futures.textMessageReceived.connect(lambda m: self._on_msg(m))
            self.ws_futures.errorOccurred.connect(self._on_error)
            self.ws_futures.disconnected.connect(self._on_closed)
            print(f"[WS] opening FUTURES {url_fut}")
            self.ws_futures.open(QUrl(url_fut))

    def _reconnect(self):
        self._open()

    def _on_closed(self):
        # try reconnect after short delay
        self._reconnect_timer.start(2000)

    def _on_error(self, err):
        # backoff reconnect
        try:
            print(f"[WS] error: {err}")
        except Exception:
            pass
        self._reconnect_timer.start(3000)

    def _on_msg(self, msg: str):
        try:
            obj = json.loads(msg)
            data = obj.get("data") or {}
            sym = (data.get("s") or "").lower()
            price_str = data.get("c") or data.get("p") or "0"
            pct_str = data.get("P") or "0"
            qvol_str = data.get("q") or "0"
            price = float(price_str)
            pct = float(pct_str)
            try:
                self.last_quote_volume[sym] = float(qvol_str)
            except Exception:
                pass
            if sym:
                self.price_update.emit(sym, price, pct)
        except Exception:
            try:
                print("[WS] message parse error")
            except Exception:
                pass

    def get_quote_volume(self, pair: str) -> float:
        return float(self.last_quote_volume.get(pair.lower(), 0.0))


class PriceWSMock(QtCore.QObject):
    price_update = QtCore.Signal(str, float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pairs: list[str] = []
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self.last_quote_volume: dict[str, float] = {}
        self._last_price: dict[str, float] = {}

    def connect_pairs(self, spot_pairs: list[str], futures_pairs: list[str]):
        # For mock, just merge both lists
        pairs = list(spot_pairs or []) + list(futures_pairs or [])
        self.pairs = [p.lower() for p in pairs if isinstance(p, str) and p]
        if not self.pairs:
            try:
                print("[WS-MOCK] no pairs to emit")
            except Exception:
                pass
            return
        for p in self.pairs:
            if p not in self._last_price:
                base = 100.0 if p.startswith("btc") else 10.0
                self._last_price[p] = base + random.random() * base
        try:
            print("[WS-MOCK] start " + ", ".join(self.pairs))
        except Exception:
            pass
        self._timer.start()

    def close(self):
        self._timer.stop()

    def _tick(self):
        for p in self.pairs:
            prev = self._last_price.get(p, 1.0)
            price = max(0.0001, prev * (1.0 + (random.random() - 0.5) * 0.01))
            self._last_price[p] = price
            pct = (random.random() - 0.5) * 2.0
            qvol = random.random() * 1000.0
            self.last_quote_volume[p] = qvol
            self.price_update.emit(p, float(price), float(pct))

    def get_quote_volume(self, pair: str) -> float:
        return float(self.last_quote_volume.get(pair.lower(), 0.0))


class PriceLabel(QtWidgets.QLabel):
    def __init__(self, owner: 'CryptoWidgetQt', index: int, parent=None):
        super().__init__(parent)
        self._owner = owner
        self._index = index
        self.setMouseTracking(True)
        self._press_pos: QtCore.QPoint | None = None
        self._pending_click = False

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        self._owner.edit_slot(self._index)
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.LeftButton:
            self._pending_click = True
            self._press_pos = event.globalPosition().toPoint()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.LeftButton and self._pending_click:
            self._pending_click = False
            was_drag = self._owner._consume_drag_flag()
            if not was_drag:
                try:
                    pos = self._press_pos or event.globalPosition().toPoint()
                    self._owner.on_label_click(self._index, pos)
                except Exception:
                    pass
        super().mouseReleaseEvent(event)

class ThumbnailPopup(QtWidgets.QWidget):
    def __init__(self, parent=None):
        # Use a plain top-level widget (not ToolTip) so other overlays (e.g., WeChat snip) can stack above
        super().__init__(parent)
        # 允许鼠标事件以支持拉伸
        if self.testAttribute(QtCore.Qt.WA_TransparentForMouseEvents):
            self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, False)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setWindowFlag(QtCore.Qt.FramelessWindowHint, True)
        try:
            self.setAttribute(QtCore.Qt.WA_AcceptTouchEvents, True)
        except Exception:
            pass
        try:
            self.grabGesture(QtCore.Qt.PinchGesture)
        except Exception:
            pass
        # K chart popups should be coverable by other apps (no stay-on-top)
        try:
            self.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint, False)
            self.setWindowFlags(self.windowFlags() & ~QtCore.Qt.WindowStaysOnTopHint)
        except Exception:
            pass
        self._pix: QtGui.QPixmap | None = None
        self._size = QtCore.QSize(240, 120)
        self.resize(self._size)
        self._ohlc: list[tuple[float, float, float, float]] | None = None
        self._ohlc2: list[tuple[float, float, float, float]] | None = None
        self._owner_ref = None
        self._mode: str = 'main'  # 'main' | 'dual'
        self._hover_x: int | None = None
        self._hover_y: int | None = None
        self._drawing_line = False
        self._drawing_start: QtCore.QPoint | None = None
        self._preview_line: tuple[int, int, int, int] | None = None
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self._tf_label: str | None = None
        # hit rects for tiny controls
        self._hit_tf_left: QtCore.QRect | None = None
        self._hit_tf_right: QtCore.QRect | None = None
        self._hit_sub_left: QtCore.QRect | None = None
        self._hit_sub_right: QtCore.QRect | None = None
        # 简易拉伸句柄
        self._resizing = False
        self._resize_edge = None  # 'right', 'bottom', 'corner'
        self._press_pos = None
        self._start_geom: QtCore.QRect | None = None
        self._handle_margin = 8
        # Delete shortcuts within the popup (covers Mac delete/backspace)
        self._sc_del = QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Delete), self)
        self._sc_del.setContext(QtCore.Qt.WindowShortcut)
        self._sc_del.activated.connect(self._handle_delete_shortcut)
        self._sc_bs = QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Backspace), self)
        self._sc_bs.setContext(QtCore.Qt.WindowShortcut)
        self._sc_bs.activated.connect(self._handle_delete_shortcut)

    def _handle_delete_shortcut(self):
        try:
            if self._owner_ref and self._owner_ref._delete_selected_thumb_line():
                self.update()
        except Exception:
            pass

    def set_pixmap(self, pix: QtGui.QPixmap | None):
        self._pix = pix
        self.update()

    def paintEvent(self, e: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        # Improve visual quality when painting pixmaps/text
        try:
            p.setRenderHint(QtGui.QPainter.Antialiasing, True)
            p.setRenderHint(QtGui.QPainter.TextAntialiasing, True)
            p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        except Exception:
            pass
        if self._pix is None:
            # 半透明黑底占位，避免纯黑屏
            p.fillRect(self.rect(), QtGui.QColor(0, 0, 0, 90))
            try:
                font_px = max(10, int(self.height() * 0.14))
                f = p.font(); f.setPointSize(font_px); p.setFont(f)
                p.setPen(QtGui.QPen(QtGui.QColor(220, 220, 220, 220)))
                msg = "Loading…"
                rect = self.rect()
                p.drawText(rect, QtCore.Qt.AlignCenter, msg)
            except Exception:
                pass
            return
        p.drawPixmap(0, 0, self._pix)
        if self._owner_ref:
            self._owner_ref._draw_thumb_lines_overlay(self, p, preview_line=self._preview_line, mode=getattr(self, '_mode', 'main'))
        if self._hover_x is not None and 0 <= self._hover_x < self.width():
            pen = QtGui.QPen(QtGui.QColor(173, 216, 230, 200))
            pen.setStyle(QtCore.Qt.DashLine)
            pen.setWidth(1)
            try:
                pen.setCosmetic(True)
            except Exception:
                pass
            p.setPen(pen)
            p.drawLine(self._hover_x, 0, self._hover_x, self.height())
        # Horizontal hover line
        if self._hover_y is not None and 0 <= self._hover_y < self.height():
            penh = QtGui.QPen(QtGui.QColor(173, 216, 230, 200))
            penh.setStyle(QtCore.Qt.DashLine)
            penh.setWidth(1)
            try:
                penh.setCosmetic(True)
            except Exception:
                pass
            p.setPen(penh)
            p.drawLine(0, self._hover_y, self.width(), self._hover_y)
        # Price label at y-axis for current hover position (overlays percentage labels)
        try:
            if self._hover_y is not None and self._ohlc:
                W, H = self.width(), self.height()
                pad = 6
                gutter = max(40, int(W * 0.14))
                left, right = pad, W - pad - gutter
                highs = [x[1] for x in (self._ohlc or [])]
                lows = [x[2] for x in (self._ohlc or [])]
                v_max = max(highs) if highs else 1.0
                v_min = min(lows) if lows else 0.0
                if v_max == v_min:
                    v_max += 1.0; v_min -= 1.0
                # layout similar to renderer
                dual_active = (getattr(self._owner_ref, 'thumb_dual_enabled', False) and bool(self._ohlc2) and getattr(self, '_mode', 'main') == 'main')
                ind = getattr(self._owner_ref, 'thumb_sub_indicator', 'none') if getattr(self, '_mode', 'main') == 'main' else getattr(self._owner_ref, 'thumb_dual_sub_indicator', 'none')
                sub_enabled = str(ind).lower() in ("rsi", "macd", "kdj")
                avail_h = H - 2 * pad
                if dual_active and sub_enabled:
                    top_h = int(avail_h * 0.38)
                    bot_h = int(avail_h * 0.38)
                    top_top, top_bot = pad, pad + top_h
                    main_top, main_bottom = top_bot + 2, top_bot + 2 + bot_h
                elif dual_active:
                    top_h = int(avail_h * 0.48)
                    top_top, top_bot = pad, pad + top_h
                    main_top, main_bottom = top_bot + 2, H - pad
                elif sub_enabled:
                    main_top, main_bottom = pad, pad + int(avail_h * 0.62)
                else:
                    main_top, main_bottom = pad, H - pad
                height = max(1, main_bottom - main_top)
                y = int(max(main_top, min(main_bottom - 1, self._hover_y)))
                # invert mapping
                price = v_max - (y - main_top) / height * (v_max - v_min)
                # format and draw on right gutter over percent labels
                # Smaller font (about 50% of previous sizing)
                font_px = max(7, int(min(H * 0.05, (W - right - pad) * 0.35)))
                f = p.font(); f.setPointSize(font_px); p.setFont(f)
                text = getattr(self._owner_ref, '_format_price', lambda v: f"{v:.4f}")(float(price))
                metrics = QtGui.QFontMetrics(p.font())
                tw, th = metrics.horizontalAdvance(text), metrics.height()
                tx = right + 4
                ty = int(y + th * 0.35)
                # background for readability
                bg_rect = QtCore.QRect(tx - 2, ty - th + 2, tw + 6, th)
                p.fillRect(bg_rect, QtGui.QColor(0, 0, 0, 180))
                p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 255)))
                p.drawText(tx, ty, text)
        except Exception:
            pass
        # Tiny header controls: timeframe and sub-indicator (top-left, small)
        try:
            y_base = 6
            pad_x = 6
            font_px = max(6, int(self.height() * 0.07 * 0.7))
            f = p.font(); f.setPointSize(font_px); p.setFont(f)
            pen_hdr = QtGui.QPen(QtGui.QColor(255,255,255,220)); p.setPen(pen_hdr)
            fm = QtGui.QFontMetrics(p.font())
            # Timeframe: "< 4h >" aligned to left; fallback to owner's current tf if label missing
            tf_fallback = None
            try:
                tf_fallback = (self._owner_ref.thumb_tf if getattr(self, '_mode', 'main') == 'main' else self._owner_ref.thumb_tf2)
            except Exception:
                tf_fallback = None
            tf_txt = str(self._tf_label or tf_fallback or "")
            left_txt = "<"; right_txt = ">"
            x = pad_x
            # draw left arrow
            p.drawText(x, y_base + fm.ascent(), left_txt)
            self._hit_tf_left = QtCore.QRect(x, y_base, fm.horizontalAdvance(left_txt), fm.height())
            x += fm.horizontalAdvance(left_txt) + 4
            # draw tf label
            p.drawText(x, y_base + fm.ascent(), tf_txt)
            x += fm.horizontalAdvance(tf_txt) + 4
            # draw right arrow
            p.drawText(x, y_base + fm.ascent(), right_txt)
            self._hit_tf_right = QtCore.QRect(x, y_base, fm.horizontalAdvance(right_txt), fm.height())

            # Sub indicator below timeframe, left-aligned: e.g., "< RSI >"
            sub_mode = getattr(self, '_mode', 'main')
            sub_name = (getattr(self._owner_ref, 'thumb_sub_indicator', 'none') if sub_mode == 'main' else getattr(self._owner_ref, 'thumb_dual_sub_indicator', 'none'))
            opts = ['rsi','macd','kdj']
            if sub_name not in opts:
                sub_name = opts[0]
            y2 = y_base + fm.height() + 2
            sub_label = sub_name.upper()
            x = pad_x
            p.drawText(x, y2 + fm.ascent(), left_txt)
            self._hit_sub_left = QtCore.QRect(x, y2, fm.horizontalAdvance(left_txt), fm.height())
            x += fm.horizontalAdvance(left_txt) + 4
            p.drawText(x, y2 + fm.ascent(), sub_label)
            x += fm.horizontalAdvance(sub_label) + 4
            p.drawText(x, y2 + fm.ascent(), right_txt)
            self._hit_sub_right = QtCore.QRect(x, y2, fm.horizontalAdvance(right_txt), fm.height())
        except Exception:
            pass

    def set_data_and_owner(self, ohlc: list[tuple[float, float, float, float]] | None, owner: 'CryptoWidgetQt', ohlc2: list[tuple[float, float, float, float]] | None = None, tf_label: str | None = None):
        self._ohlc = ohlc or []
        self._ohlc2 = ohlc2 or None
        self._owner_ref = owner
        self._tf_label = tf_label
        self._render_now()

    def set_mode(self, mode: str):
        self._mode = mode if mode in ('main', 'dual') else 'main'

    def _render_now(self):
        try:
            if self._owner_ref is None or self._ohlc is None:
                return
            if getattr(self, '_mode', 'main') == 'dual':
                pm = self._owner_ref._render_kline_pixmap_dual_window(self._ohlc, self.width(), self.height())
            else:
                pm = self._owner_ref._render_kline_pixmap(self._ohlc, self.width(), self.height(), ohlc2=self._ohlc2)
            self.set_pixmap(pm)
        except Exception:
            pass

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        # 尺寸变化时重绘以保证清晰
        self._render_now()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        pos = event.pos()
        # Tiny header clicks: timeframe and sub-indicator arrows
        try:
            if event.button() == QtCore.Qt.LeftButton and self._owner_ref is not None:
                if isinstance(self._hit_tf_left, QtCore.QRect) and self._hit_tf_left.contains(pos):
                    self._owner_ref._cycle_timeframe('dual' if getattr(self, '_mode', 'main')=='dual' else 'main', -1)
                    event.accept(); return
                if isinstance(self._hit_tf_right, QtCore.QRect) and self._hit_tf_right.contains(pos):
                    self._owner_ref._cycle_timeframe('dual' if getattr(self, '_mode', 'main')=='dual' else 'main', +1)
                    event.accept(); return
                if isinstance(self._hit_sub_left, QtCore.QRect) and self._hit_sub_left.contains(pos):
                    self._owner_ref._cycle_sub_indicator('dual' if getattr(self, '_mode', 'main')=='dual' else 'main', -1)
                    self._render_now(); event.accept(); return
                if isinstance(self._hit_sub_right, QtCore.QRect) and self._hit_sub_right.contains(pos):
                    self._owner_ref._cycle_sub_indicator('dual' if getattr(self, '_mode', 'main')=='dual' else 'main', +1)
                    self._render_now(); event.accept(); return
        except Exception:
            pass
        # no right-gutter triangle quick switch anymore; using top-left tiny controls instead
        r = self.rect()
        m = self._handle_margin
        edge = None
        if pos.x() >= r.right() - m and pos.y() >= r.bottom() - m:
            edge = 'corner'
            self.setCursor(QtCore.Qt.SizeFDiagCursor)
        elif pos.x() >= r.right() - m:
            edge = 'right'
            self.setCursor(QtCore.Qt.SizeHorCursor)
        elif pos.y() >= r.bottom() - m:
            edge = 'bottom'
            self.setCursor(QtCore.Qt.SizeVerCursor)
        if edge:
            self._resizing = True
            self._resize_edge = edge
            self._press_pos = event.globalPosition().toPoint()
            self._start_geom = self.frameGeometry()
            event.accept()
            return
        if event.button() == QtCore.Qt.LeftButton and not self._resizing:
            # Ensure this popup becomes the active window so key events (Delete) work on macOS
            try:
                self.activateWindow()
                self.raise_()
            except Exception:
                pass
            self.setFocus(QtCore.Qt.MouseFocusReason)
            mode = getattr(self, '_mode', 'main')
            started_drag = False
            if self._owner_ref and self._owner_ref._select_thumb_line_at(self, pos, mode=mode):
                # After selection, check if we grabbed an endpoint
                ep = self._owner_ref._selected_line_endpoint_at(self, pos, mode=mode)
                if ep is not None:
                    self._dragging_endpoint = ep
                    started_drag = True
                self._drawing_line = False
                self._drawing_start = None
                self._preview_line = None
            else:
                self._drawing_line = True
                self._drawing_start = pos
                self._preview_line = (pos.x(), pos.y(), pos.x(), pos.y())
            if started_drag:
                self.update()
            self.update()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._resizing and self._start_geom is not None and self._press_pos is not None:
            dp = event.globalPosition().toPoint() - self._press_pos
            new_w, new_h = self._start_geom.width(), self._start_geom.height()
            if self._resize_edge in ('right', 'corner'):
                new_w = max(160, int(self._start_geom.width() + dp.x()))
            if self._resize_edge in ('bottom', 'corner'):
                new_h = max(90, int(self._start_geom.height() + dp.y()))
            self.resize(new_w, new_h)
            event.accept()
            return
        # 更新光标形状提示可拉伸
        pos = event.pos(); r = self.rect(); m = self._handle_margin
        if pos.x() >= r.right() - m and pos.y() >= r.bottom() - m:
            self.setCursor(QtCore.Qt.SizeFDiagCursor)
        elif pos.x() >= r.right() - m:
            self.setCursor(QtCore.Qt.SizeHorCursor)
        elif pos.y() >= r.bottom() - m:
            self.setCursor(QtCore.Qt.SizeVerCursor)
        else:
            self.unsetCursor()
        if not self._resizing:
            if self._drawing_line and self._drawing_start is not None and event.buttons() & QtCore.Qt.LeftButton:
                self._preview_line = (
                    self._drawing_start.x(),
                    self._drawing_start.y(),
                    pos.x(),
                    pos.y()
                )
            elif getattr(self, '_dragging_endpoint', None) is not None and event.buttons() & QtCore.Qt.LeftButton:
                ep = int(self._dragging_endpoint)
                if self._owner_ref:
                    mode = getattr(self, '_mode', 'main')
                    self._owner_ref._update_selected_thumb_line_endpoint(self, ep, pos, mode=mode, commit=False)
                # No preview when dragging endpoint; line updates live
            if self.rect().contains(pos):
                self._hover_x = max(0, min(self.width() - 1, pos.x()))
                self._hover_y = max(0, min(self.height() - 1, pos.y()))
            else:
                self._hover_x = None
                self._hover_y = None
        else:
            self._hover_x = None
            self._hover_y = None
        self.update()
        super().mouseMoveEvent(event)

    def enterEvent(self, event: QtCore.QEvent) -> None:
        super().enterEvent(event)
        pos = self.mapFromGlobal(QtGui.QCursor.pos())
        if self.rect().contains(pos):
            self._hover_x = pos.x()
            self._hover_y = pos.y()
        else:
            self._hover_x = None
            self._hover_y = None
        self.update()

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        super().leaveEvent(event)
        self._hover_x = None
        self._hover_y = None
        self.update()

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        if self._owner_ref and event.key() in (QtCore.Qt.Key_Backspace, QtCore.Qt.Key_Delete):
            if self._owner_ref._delete_selected_thumb_line():
                self.update()
                event.accept()
                return
        super().keyPressEvent(event)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        # Zoom in only, up to 200%, anchored at cursor position
        dy = 0
        try:
            dy = int(event.angleDelta().y())
        except Exception:
            pass
        if dy == 0:
            try:
                dy = int(event.pixelDelta().y())
            except Exception:
                dy = 0
        # Debug log for wheel
        try:
            ad = event.angleDelta()
            pd = event.pixelDelta()
            print(f"[zoom] wheelEvent: angleDelta=({ad.x()},{ad.y()}) pixelDelta=({pd.x()},{pd.y()}) dy={dy} mode={getattr(self,'_mode','main')}", flush=True)
        except Exception:
            pass
        if dy <= 0 or self._owner_ref is None:
            try:
                if dy <= 0:
                    print("[zoom] wheelEvent ignored: dy<=0 (only zoom-in supported)", flush=True)
                elif self._owner_ref is None:
                    print("[zoom] wheelEvent ignored: no owner", flush=True)
            except Exception:
                pass
            event.ignore()
            return
        kind = 'dual' if getattr(self, '_mode', 'main') == 'dual' else 'main'
        cur_pct = int(getattr(self._owner_ref, 'thumb_dual_scale_percent' if kind == 'dual' else 'thumb_scale_percent', 100) or 100)
        step = 10 if abs(dy) >= 120 else 5
        new_pct = min(200, max(cur_pct, cur_pct + step))
        if new_pct == cur_pct:
            try:
                print(f"[zoom] wheelEvent no-op: cur_pct={cur_pct} new_pct={new_pct}", flush=True)
            except Exception:
                pass
            event.accept(); return
        try:
            anchor_g = event.globalPosition().toPoint()
        except Exception:
            anchor_g = QtGui.QCursor.pos()
        try:
            print(f"[zoom] wheelEvent apply: kind={kind} cur={cur_pct}->new={new_pct} anchor={anchor_g.x()},{anchor_g.y()}", flush=True)
        except Exception:
            pass
        self._apply_zoom_percent(new_pct, anchor_g, kind)
        event.accept()

    def event(self, e: QtCore.QEvent) -> bool:
        # Handle macOS trackpad pinch gesture
        try:
            if e.type() == QtCore.QEvent.Gesture:
                ge = e  # type: ignore
                pinch = ge.gesture(QtCore.Qt.PinchGesture)
                if pinch:
                    sf = float(pinch.scaleFactor())
                    try:
                        print(f"[zoom] QPinchGesture: scaleFactor={sf} state={pinch.state()}", flush=True)
                    except Exception:
                        pass
                    if sf > 1.01 and self._owner_ref is not None:
                        kind = 'dual' if getattr(self, '_mode', 'main') == 'dual' else 'main'
                        cur_pct = int(getattr(self._owner_ref, 'thumb_dual_scale_percent' if kind == 'dual' else 'thumb_scale_percent', 100) or 100)
                        inc = 10 if sf >= 1.08 else 5
                        new_pct = min(200, cur_pct + inc)
                        anchor_g = QtGui.QCursor.pos()
                        try:
                            print(f"[zoom] pinch apply: kind={kind} cur={cur_pct}->new={new_pct} anchor={anchor_g.x()},{anchor_g.y()}", flush=True)
                        except Exception:
                            pass
                        self._apply_zoom_percent(new_pct, anchor_g, kind)
                    e.accept()
                    return True
            elif e.type() == QtCore.QEvent.NativeGesture:
                # macOS native zoom (trackpad pinch)
                ng = e  # type: ignore
                try:
                    gtype = ng.gestureType()
                except Exception:
                    gtype = None
                if gtype == QtCore.Qt.ZoomNativeGesture and self._owner_ref is not None:
                    try:
                        val = float(ng.value())
                    except Exception:
                        val = 0.0
                    try:
                        print(f"[zoom] NativeGesture Zoom: value={val}", flush=True)
                    except Exception:
                        pass
                    if val > 0.001:
                        kind = 'dual' if getattr(self, '_mode', 'main') == 'dual' else 'main'
                        cur_pct = int(getattr(self._owner_ref, 'thumb_dual_scale_percent' if kind == 'dual' else 'thumb_scale_percent', 100) or 100)
                        inc = 10 if val >= 0.08 else 5
                        new_pct = min(200, cur_pct + inc)
                        try:
                            print(f"[zoom] native apply: kind={kind} cur={cur_pct}->new={new_pct}", flush=True)
                        except Exception:
                            pass
                        self._apply_zoom_percent(new_pct, QtGui.QCursor.pos(), kind)
                        e.accept()
                        return True
        except Exception:
            pass
        return super().event(e)

    def _apply_zoom_percent(self, new_pct: int, anchor_global: QtCore.QPoint, kind: str) -> None:
        base_w, base_h = 240, 120
        # Calculate local fraction of anchor
        w0, h0 = max(1, self.width()), max(1, self.height())
        pos_local = self.mapFromGlobal(anchor_global)
        u = max(0.0, min(1.0, float(pos_local.x()) / float(w0)))
        v = max(0.0, min(1.0, float(pos_local.y()) / float(h0)))
        new_w = int(base_w * (new_pct / 100.0))
        new_h = int(base_h * (new_pct / 100.0))
        # Update owner scale and persist
        if kind == 'dual':
            self._owner_ref.thumb_dual_scale_percent = int(new_pct)
        else:
            self._owner_ref.thumb_scale_percent = int(new_pct)
        try:
            self._owner_ref._save_config()
        except Exception:
            pass
        # Compute new top-left to keep anchor under cursor
        new_offset = QtCore.QPoint(int(u * new_w), int(v * new_h))
        new_tl = anchor_global - new_offset
        self.resize(new_w, new_h)
        self.move(new_tl)
        self.update()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._drawing_line and event.button() == QtCore.Qt.LeftButton and self._drawing_start is not None:
            if self._owner_ref:
                mode = getattr(self, '_mode', 'main')
                self._owner_ref._add_thumb_line_from_popup(self, self._drawing_start, event.pos(), mode=mode)
            self._drawing_line = False
            self._drawing_start = None
            self._preview_line = None
            self.update()
        elif getattr(self, '_dragging_endpoint', None) is not None and event.button() == QtCore.Qt.LeftButton:
            # Commit endpoint update
            if self._owner_ref:
                mode = getattr(self, '_mode', 'main')
                ep = int(self._dragging_endpoint)
                self._owner_ref._update_selected_thumb_line_endpoint(self, ep, event.pos(), mode=mode, commit=True)
            self._dragging_endpoint = None
            self.update()
        self._resizing = False
        self._resize_edge = None
        self._press_pos = None
        self._start_geom = None
        self.unsetCursor()
        super().mouseReleaseEvent(event)


class CryptoWidgetQt(QtWidgets.QWidget):
    # Async thumbnail data ready: (pair_l, timeframe, ohlc_list)
    thumb_data_ready = QtCore.Signal(str, str, list)
    def __init__(self, use_mock_ws: bool = False):
        super().__init__()
        self.setObjectName("CryptoWidgetQt")

        # Window flags: borderless + stay-on-top, but popups are at lower level than system overlays
        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.resize(240, 30)

        # Drag state
        self._drag_pos = None
        self._collapsed = False
        self._expanded_size = QtCore.QSize(240, 30)
        self._base_height = 30
        self._collapsed_width = 28

        # Load config (coins, geometry, collapsed)
        cfg = self._load_config()
        self.slots: List[str] = cfg.get("slots") or [
            "bitcoin",
            "eth",
            "cardano",
            "polkadot",
            "chainlink",
            "solana",
            "avalanche-2",
            "sui",
        ]
        self._cfg_geometry = cfg.get("geometry")  # [x, y, w, h]
        self._collapsed = bool(cfg.get("collapsed", False))
        # Price source preference: 'spot' or 'futures'
        self.prefer_price_source: str = str(cfg.get("prefer_price_source", "spot")).lower()
        if self.prefer_price_source not in ("spot", "futures"):
            self.prefer_price_source = "spot"
        # UI settings
        self.thumb_enabled: bool = bool(cfg.get("thumb_enabled", False))
        self.thumb_fetch_from_binance: bool = bool(cfg.get("thumb_fetch_from_binance", True))
        self.thumb_tf: str = cfg.get("thumb_tf", "1h")
        self.thumb_bars: int = int(cfg.get("thumb_bars", 50))
        # 主图均线叠加：none | ma | ema
        self.thumb_overlay_type: str = cfg.get("thumb_overlay_type", "ma")
        self.thumb_overlay_p1: int = int(cfg.get("thumb_overlay_p1", 5))
        self.thumb_overlay_p2: int = int(cfg.get("thumb_overlay_p2", 20))
        # 附图指标：none | rsi | macd | kdj（只可选一项）
        self.thumb_sub_indicator: str = cfg.get("thumb_sub_indicator", "none")
        # 样式与缩放
        self.thumb_chart_style: str = cfg.get("thumb_chart_style", "candle")
        self.thumb_scale_percent: int = int(cfg.get("thumb_scale_percent", 100))
        self.thumb_dual_scale_percent: int = int(cfg.get("thumb_dual_scale_percent", self.thumb_scale_percent))
        # 双图模式设置
        self.thumb_dual_enabled: bool = bool(cfg.get("thumb_dual_enabled", False))
        self.thumb_tf2: str = cfg.get("thumb_tf2", "4h")
        self.thumb_bars2: int = int(cfg.get("thumb_bars2", self.thumb_bars))
        # 颜色与叠加（主图与上图独立）
        self.thumb_overlay_color1: str = cfg.get("thumb_overlay_color1", "gold")
        self.thumb_overlay_color2: str = cfg.get("thumb_overlay_color2", "skyblue")
        self.thumb_dual_overlay_type: str = cfg.get("thumb_dual_overlay_type", "ma")
        self.thumb_dual_overlay_p1: int = int(cfg.get("thumb_dual_overlay_p1", 5))
        self.thumb_dual_overlay_p2: int = int(cfg.get("thumb_dual_overlay_p2", 20))
        self.thumb_dual_overlay_color1: str = cfg.get("thumb_dual_overlay_color1", "orange")
        self.thumb_dual_overlay_color2: str = cfg.get("thumb_dual_overlay_color2", "lime")
        self.thumb_dual_sub_indicator: str = cfg.get("thumb_dual_sub_indicator", "none")
        self._thumb_cache: dict[str, tuple[float, list[tuple[float, float, float, float]]]] = {}
        self._thumb_lines: dict[str, dict[str, list[tuple[float, float, float, float]]]] = {}
        raw_thumb_lines = cfg.get("thumb_lines")
        if isinstance(raw_thumb_lines, dict):
            for tpl_pair, tf_map in raw_thumb_lines.items():
                if not tpl_pair or not isinstance(tf_map, dict):
                    continue
                cleaned_tf: dict[str, list] = {}
                for tpl_tf, arr in tf_map.items():
                    if not tpl_tf or not isinstance(arr, list):
                        continue
                    cleaned_lines: list = []
                    for item in arr:
                        # Support legacy [x1,y1,x2,y2] and new dict {'x1f','p1','x2f','p2','fmt':'data'}
                        if isinstance(item, dict):
                            try:
                                x1f = float(item.get('x1f'))
                                p1 = float(item.get('p1'))
                                x2f = float(item.get('x2f'))
                                p2 = float(item.get('p2'))
                                fmt = str(item.get('fmt', 'data'))
                                cleaned_lines.append({'x1f': x1f, 'p1': p1, 'x2f': x2f, 'p2': p2, 'fmt': fmt})
                            except Exception:
                                continue
                        elif isinstance(item, (list, tuple)) and len(item) >= 4:
                            try:
                                x1, y1, x2, y2 = float(item[0]), float(item[1]), float(item[2]), float(item[3])
                            except Exception:
                                continue
                            cleaned_lines.append((x1, y1, x2, y2))
                    if cleaned_lines:
                        cleaned_tf[str(tpl_tf)] = cleaned_lines
                if cleaned_tf:
                    self._thumb_lines[str(tpl_pair)] = cleaned_tf
        self._thumb_line_selected: tuple[str, str, int] | None = None
        self._drag_started = False
        # Alerts config
        self.alerts_enabled: bool = bool(cfg.get("alerts_enabled", False))
        self.alert_threshold_percent: float = float(cfg.get("alert_threshold_percent", 5.0))
        self.alert_method: str = cfg.get("alert_method", "pct")  # pct | vol | volume | bull
        self.vol_window_samples: int = int(cfg.get("vol_window_samples", 120))
        self.vol_threshold_sigma: float = float(cfg.get("vol_threshold_sigma", 2.0))
        self.volume_window_samples: int = int(cfg.get("volume_window_samples", 60))
        self.volume_threshold_sigma: float = float(cfg.get("volume_threshold_sigma", 2.0))
        self.alert_periods: List[str] = cfg.get("alert_periods") or ["1m", "5m", "15m", "1h", "4h", "24h"]
        self.bull_min_change_percent: float = float(cfg.get("bull_min_change_percent", 0.5))
        self.bull_require_monotonic: bool = bool(cfg.get("bull_require_monotonic", True))
        self.alert_watchlist: List[str] = cfg.get("alert_watchlist") or []

        # UI scaling (zoom)
        self.ui_scale: float = float(cfg.get("ui_scale", 1.0))
        self._scale_min = 0.6
        self._scale_max = 2.0
        self._scale_step = 0.1
        # Base metrics
        self._base_font_px = 11
        self._base_margins = (6, 4, 6, 4)
        self._base_spacing = 6
        self._base_btn = 18
        self._base_label_minw = 48
        self._base_border_radius = 8
        self._base_border_width = 1
        self._collapsed_width_base = 28

        # Timed TTS announcer (Windows only; fail fast otherwise)
        self.tts_enabled: bool = bool(cfg.get("tts_enabled", False))
        self.tts_interval_min: int = int(cfg.get("tts_interval_min", 1))
        # 限定播报的币种（按 slots 存储）。
        # None 表示“全部”，空列表 [] 表示“一个也不播报”。
        self.tts_include_slots = cfg.get("tts_include_slots", None)
        self._tts_timer = QtCore.QTimer(self)
        self._tts_timer.setSingleShot(False)
        self._tts_timer.timeout.connect(self._speak_prices_if_ready)

        # Data
        self.fetcher = CryptoDataFetcher()
        self.worker: FetchThread | None = None
        self.ws = PriceWSMock(self) if bool(use_mock_ws) else PriceWS(self)
        self._price_signal_connected = False
        self.last_ws_price: dict[str, float] = {}
        # 新增：每个交易对的“昨日收盘价”
        self.prev_close: dict[str, float] = {}

        self._audit_threshold = 0.005  # 0.5%
        self.audit_timer = QtCore.QTimer(self)
        self.audit_timer.setInterval(180_000)  # 3 minutes
        self.audit_timer.timeout.connect(self._start_http_audit)
        self.audit_timer.start()
        self.audit_worker: FetchThread | None = None
        self.last_alert_time: dict[str, float] = {}
        self._alert_cooldown_sec = 60.0
        # Series buffers for volatility/volume detection
        self._series_maxlen = max(self.vol_window_samples, self.volume_window_samples, 120)
        self._price_series: dict[str, deque] = defaultdict(lambda: deque(maxlen=self._series_maxlen))
        self._vol_series: dict[str, deque] = defaultdict(lambda: deque(maxlen=self._series_maxlen))
        self._ts_series_maxlen = 10000
        self._price_ts: dict[str, deque] = defaultdict(lambda: deque(maxlen=self._ts_series_maxlen))
        self.alert = AlertManager(self)
        
        # RSI calculation data (multi-timeframe)
        self.rsi_timeframes: List[str] = cfg.get("rsi_timeframes") or ["15m", "1h", "4h"]
        self.rsi_period: int = int(cfg.get("rsi_period", 6))
        self._tf_seconds: dict[str, int] = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
        self._rsi_values_tf: dict[str, dict[str, float]] = defaultdict(dict)
        self._rsi_closes_tf: dict[str, dict[str, deque]] = defaultdict(lambda: defaultdict(lambda: deque(maxlen=self.rsi_period + 1)))
        self._rsi_last_bar_close_tf: dict[str, dict[str, float]] = defaultdict(dict)
        self._rsi_last_bar_ts_tf: dict[str, dict[str, float]] = defaultdict(dict)
        # Fallback tick-based buffers (unused for display when multi-TF is available)
        self._rsi_gains: dict[str, deque] = defaultdict(lambda: deque(maxlen=self.rsi_period))
        self._rsi_losses: dict[str, deque] = defaultdict(lambda: deque(maxlen=self.rsi_period))
        self._rsi_timer = QtCore.QTimer(self)
        self._rsi_timer.setInterval(60000)
        self._rsi_timer.timeout.connect(self._update_rsi_values)
        self._rsi_timer.start()

        # Alert indicator state: pair -> (level 1..3, expiry_ts)
        self._alert_indicator: dict[str, tuple[int, float]] = {}

        # UI
        self._build_ui()
        self._apply_style()
        self._install_drag_filters()
        # Popup/menu state
        self._menu_open = False
        self._thumb_pair_current: str | None = None
        self._thumb_index_visible: int | None = None
        self._thumb_ohlc_main: list[tuple[float, float, float, float]] | None = None
        self._thumb_ohlc_top: list[tuple[float, float, float, float]] | None = None
        self.thumb_data_ready.connect(self._on_thumb_data_ready)
        # Quick-switch triangle hit areas (updated during render)
        self._last_sub_tri_rect: QtCore.QRect | None = None
        self._last_dual_sub_tri_rect: QtCore.QRect | None = None

        # Zoom shortcuts (Ctrl + '+' / Ctrl + '-') active when widget or children focused
        self._shortcut_zoom_in = QtGui.QShortcut(QtGui.QKeySequence(QtGui.QKeySequence.ZoomIn), self)
        self._shortcut_zoom_in.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        self._shortcut_zoom_in.activated.connect(self.zoom_in)
        self._shortcut_zoom_out = QtGui.QShortcut(QtGui.QKeySequence(QtGui.QKeySequence.ZoomOut), self)
        self._shortcut_zoom_out.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        self._shortcut_zoom_out.activated.connect(self.zoom_out)
        # Also bind explicit Ctrl-based combos to satisfy "Ctrl + +/-" requirement
        for seq in ("Ctrl++", "Ctrl+=", "Ctrl+Plus"):
            sc = QtGui.QShortcut(QtGui.QKeySequence(seq), self)
            sc.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(self.zoom_in)
        for seq in ("Ctrl+-", "Ctrl+_", "Ctrl+Minus"):
            sc = QtGui.QShortcut(QtGui.QKeySequence(seq), self)
            sc.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(self.zoom_out)
        delete_seq = QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Delete), self)
        delete_seq.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        delete_seq.activated.connect(self._handle_thumb_line_delete_shortcut)
        bs_seq = QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Backspace), self)
        bs_seq.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        bs_seq.activated.connect(self._handle_thumb_line_delete_shortcut)

        # First paint: placeholders
        self._update_prices([])

        # 启动时预拉一遍 HTTP，顺便初始化 prev_close
        try:
            init_data = self.fetcher.fetch_crypto_prices_for_ids(self.slots)
            if init_data:
                self._update_prices(init_data)
        except Exception:
            pass

        # Apply geometry from config
        if isinstance(self._cfg_geometry, list) and len(self._cfg_geometry) == 4:
            x, y, w, h = self._cfg_geometry
            self.setGeometry(x, y, w, h)
            self._expanded_size = QtCore.QSize(w, h)
            self._base_height = int(h)
        # Start expanded for reliability; user can collapse manually.
        # Lock height and width to prevent manual stretching; only our code changes size.
        self._collapsed = False
        self._lock_height(self._expanded_size.height())
        self._lock_to_width(self._expanded_size.width())
        try:
            need_w = self._calc_min_width()
            if self.width() < need_w:
                self._lock_to_width(need_w)
                self.resize(need_w, self._expanded_size.height())
        except Exception:
            pass

        # Start WebSocket for live prices
        self._start_ws()

        # Start announcer timer if enabled
        if self.tts_enabled:
            self._restart_tts_timer()

    def _build_ui(self):
        root = QtWidgets.QFrame(self)
        root.setObjectName("root")
        root_layout = QtWidgets.QHBoxLayout(root)
        root_layout.setContentsMargins(6, 4, 6, 4)
        root_layout.setSpacing(6)
        self.root_frame = root
        self.root_layout = root_layout
        # Prevent child-driven resizing
        self.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        root.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)

        # Toggle button (compact)
        self.btn_toggle = QtWidgets.QToolButton(root)
        self.btn_toggle.setText("◀")
        self.btn_toggle.setFixedSize(18, 18)
        self.btn_toggle.clicked.connect(self.toggle_collapse)
        root_layout.addWidget(self.btn_toggle, 0, QtCore.Qt.AlignVCenter)

        # Prices container (per-coin labels showing only price)
        self.prices_widget = QtWidgets.QWidget(root)
        self.prices_layout = QtWidgets.QHBoxLayout(self.prices_widget)
        self.prices_layout.setContentsMargins(0, 0, 0, 0)
        self.prices_layout.setSpacing(6)
        self.prices_widget.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        root_layout.addWidget(self.prices_widget, 1)

        # Create labels per slot with an indicator row (small dots under price)
        self.labels: List[PriceLabel] = []
        self.dot_labels: List[QtWidgets.QLabel] = []
        self.pair_index: dict[str, int] = {}
        # Hover thumbnail popup
        self._thumb_popup = ThumbnailPopup(None)
        # Dual/top popup: separate black box above window
        self._dual_popup = ThumbnailPopup(None)
        self._dual_popup.set_mode('dual')
        for i, _id in enumerate(self.slots):
            cont = QtWidgets.QWidget(self.prices_widget)
            v = QtWidgets.QVBoxLayout(cont)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(0)

            lbl = PriceLabel(self, i, cont)
            lbl.setObjectName("price")
            lbl.setAlignment(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft)
            lbl.setText("…")
            lbl.setCursor(QtCore.Qt.PointingHandCursor)
            lbl.setMinimumWidth(48)
            v.addWidget(lbl, 0, QtCore.Qt.AlignVCenter)

            dots = QtWidgets.QLabel(cont)
            dots.setObjectName("dots")
            dots.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
            dots.setText("")
            dots.setFixedHeight(6)
            v.addWidget(dots, 0, QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)

            self.prices_layout.addWidget(cont)
            self.labels.append(lbl)
            self.dot_labels.append(dots)
        self._rebuild_pair_index()

        # Set layout
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(root)

    def _s(self, v: float | int) -> int:
        return int(round(float(v) * float(self.ui_scale)))

    def _apply_style(self):
        # Apply scaled layout metrics
        try:
            l, t, r, b = self._base_margins
            self.root_layout.setContentsMargins(self._s(l), self._s(t), self._s(r), self._s(b))
            self.root_layout.setSpacing(self._s(self._base_spacing))
            self.prices_layout.setSpacing(self._s(self._base_spacing))
        except Exception:
            pass

        # Apply control sizes
        try:
            btn_sz = self._s(self._base_btn)
            self.btn_toggle.setFixedSize(btn_sz, btn_sz)
        except Exception:
            pass
        for lbl in getattr(self, 'labels', []):
            try:
                lbl.setMinimumWidth(self._s(self._base_label_minw))
            except Exception:
                pass

        # Stylesheet with scaled font/border radius/border width
        font_px = max(6, self._s(self._base_font_px))
        radius = max(2, self._s(self._base_border_radius))
        border_w = max(1, self._s(self._base_border_width))
        btn_radius = max(3, int(radius * 0.6))
        self.setStyleSheet(
            f"""
            #CryptoWidgetQt {{ background: transparent; }}
            QFrame#root {{ background: rgba(255,255,255,200); border-radius: {radius}px; border: {border_w}px solid rgba(203,213,225,220); }}
            QLabel#price {{ color: #111827; font: {font_px}px 'Helvetica Neue', Arial, sans-serif; }}
            QLabel#dots {{ color: #6B7280; font: {max(5, font_px-5)}px 'Helvetica Neue', Arial, sans-serif; }}
            QToolButton {{ color: #111827; background: transparent; border: none; border-radius: {btn_radius}px; padding: 0px; }}
            QToolButton:hover {{ background: transparent; color: #111827; }}
            QToolTip {{ color: #111827; background-color: #FFFFFF; border: 1px solid #CBD5E1; }}
            """
        )

    # Zoom controls (Ctrl + '+' / Ctrl + '-')
    def zoom_in(self):
        if float(self.ui_scale) >= float(self._scale_max):
            return
        self.ui_scale = float(min(self._scale_max, round(float(self.ui_scale) + self._scale_step, 2)))
        self._apply_style()
        # Adjust geometry for scaled background and content
        try:
            new_h = max(20, int(round(self._base_height * self.ui_scale)))
            self._lock_height(new_h)
            if self._collapsed:
                target_w = self._collapsed_target_width()
                self._lock_to_width(target_w)
                self.resize(target_w, new_h)
            else:
                need_w = self._calc_min_width()
                cur_w = max(self.width(), need_w)
                self._lock_to_width(cur_w)
                self.resize(cur_w, new_h)
        except Exception:
            pass
        self._save_config()

    def zoom_out(self):
        if float(self.ui_scale) <= float(self._scale_min):
            return
        self.ui_scale = float(max(self._scale_min, round(float(self.ui_scale) - self._scale_step, 2)))
        self._apply_style()
        try:
            new_h = max(20, int(round(self._base_height * self.ui_scale)))
            self._lock_height(new_h)
            if self._collapsed:
                target_w = self._collapsed_target_width()
                self._lock_to_width(target_w)
                self.resize(target_w, new_h)
            else:
                need_w = self._calc_min_width()
                cur_w = max(self.width(), need_w)
                self._lock_to_width(cur_w)
                self.resize(cur_w, new_h)
        except Exception:
            pass
        self._save_config()

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        # Avoid interference with hover popup during menu
        self._menu_open = True
        try:
            self.hide_thumbnail()
        except Exception:
            pass
        menu = QtWidgets.QMenu(self)
        act_refresh = menu.addAction("Refresh Prices")
        # Price source preference submenu
        menu.addSeparator()
        sub_price = QtWidgets.QMenu("Price Source", menu)
        grp = QtGui.QActionGroup(sub_price)
        grp.setExclusive(True)
        act_src_spot = QtGui.QAction("Prefer Spot", sub_price)
        act_src_spot.setCheckable(True)
        act_src_fut = QtGui.QAction("Prefer Futures", sub_price)
        act_src_fut.setCheckable(True)
        grp.addAction(act_src_spot)
        grp.addAction(act_src_fut)
        current_pref = str(getattr(self, 'prefer_price_source', 'spot')).lower()
        if current_pref == 'futures':
            act_src_fut.setChecked(True)
        else:
            act_src_spot.setChecked(True)
        sub_price.addAction(act_src_spot)
        sub_price.addAction(act_src_fut)
        menu.addMenu(sub_price)
        act_alerts = menu.addAction("Alerts Settings…")
        act_announcer = menu.addAction("Announcer Settings…")
        act_ui = menu.addAction("UI Settings…")
        menu.addSeparator()
        menu.addAction("Quit")
        chosen = menu.exec(event.globalPos())
        if chosen is None:
            self._menu_open = False
            return
        if chosen.text() == "Refresh Prices":
            self.refresh()
        elif chosen is act_src_spot:
            self.prefer_price_source = 'spot'
            self._save_config()
            self._restart_ws()
        elif chosen is act_src_fut:
            self.prefer_price_source = 'futures'
            self._save_config()
            self._restart_ws()
        elif chosen.text() == "Alerts Settings…":
            self._open_alerts_settings()
        elif chosen.text() == "Announcer Settings…":
            self._open_announcer_settings()
        elif chosen.text() == "UI Settings…":
            self._open_ui_settings()
        elif chosen.text() == "Quit":
            QtWidgets.QApplication.quit()
        self._menu_open = False

    def mousePressEvent(self, e: QtGui.QMouseEvent) -> None:
        if e.button() == QtCore.Qt.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e: QtGui.QMouseEvent) -> None:
        if self._drag_pos is not None and e.buttons() & QtCore.Qt.LeftButton:
            prev = self.pos()
            new_pos = e.globalPosition().toPoint() - self._drag_pos
            self.move(new_pos)
            delta = new_pos - prev
            if not delta.isNull():
                self._drag_started = True
                self._move_popups_by_delta(delta)
            e.accept()

    def mouseReleaseEvent(self, e: QtGui.QMouseEvent) -> None:
        self._drag_pos = None
        self._drag_started = False

    def toggle_collapse(self):
        self._apply_collapsed(not self._collapsed)
        self._save_config()

    def _apply_collapsed(self, collapsed: bool):
        if not collapsed:
            # Lock size back to expanded values; prevent user stretching
            self._lock_to_width(self._expanded_size.width())
            self.resize(self._expanded_size)
            self.prices_widget.setVisible(True)
            self.btn_toggle.setText("◀")
            self._collapsed = False
        else:
            # Remember expanded width; keep height
            self._expanded_size = self.size()
            self.prices_widget.setVisible(False)
            # Collapse to narrow pill width, same height, ensure toggle is fully visible
            target_w = self._collapsed_target_width()
            self._lock_to_width(target_w)
            self.resize(target_w, self._expanded_size.height())
            self.updateGeometry()
            self.btn_toggle.setText("▶")
            self._collapsed = True

    def _collapsed_target_width(self) -> int:
        # Compute a safe collapsed width so the toggle button remains fully visible
        try:
            left, top, right, bottom = self.root_layout.getContentsMargins()
            btn_w = self.btn_toggle.sizeHint().width()
            base = left + btn_w + right
            scaled_min = self._s(self._collapsed_width_base)
            return max(scaled_min, base + 2)
        except Exception:
            return max(self._s(self._collapsed_width_base), 32)

    def _calc_min_width(self) -> int:
        try:
            left, top, right, bottom = self.root_layout.getContentsMargins()
            btn_w = self.btn_toggle.sizeHint().width()
            per_label = (self.labels[0].minimumWidth() if self.labels else self._s(self._base_label_minw)) + self.prices_layout.spacing()
            count = len(self.slots)
            base = left + btn_w + right + (count * per_label)
            return max(180, base)
        except Exception:
            return 240

    # --- Thumbnail preview (hover) ---
    def _ohlc_1h(self, pair_l: str, bars: int = 50, tf: str = "1h"):
        try:
            # Prefer Binance REST if enabled
            if bool(self.thumb_fetch_from_binance):
                data = self._fetch_klines_binance(pair_l, interval=tf, limit=int(bars))
                if data:
                    return data
            # Fallback: aggregate ticks by hour from local series
            dq = self._price_ts.get(pair_l)
            if not dq:
                return []
            # seconds per bucket
            tf_map = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "8h": 28800, "1d": 86400, "3d": 259200}
            sec = tf_map.get((tf or "1h").lower(), 3600)
            buckets: dict[int, list[float]] = {}
            for ts, pr in dq:
                h = int(ts // sec)
                buckets.setdefault(h, []).append(float(pr))
            hours = sorted(buckets.keys())[-int(bars):]
            ohlc = []
            for h in hours:
                arr = buckets[h]
                if not arr:
                    continue
                o_ = arr[0]
                h_ = max(arr)
                l_ = min(arr)
                c_ = arr[-1]
                ohlc.append((o_, h_, l_, c_))
            return ohlc
        except Exception:
            return []

    def _ohlc_from_local(self, pair_l: str, bars: int, tf: str) -> list[tuple[float, float, float, float]]:
        try:
            dq = self._price_ts.get(pair_l)
            if not dq:
                return []
            tf_map = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "8h": 28800, "1d": 86400, "3d": 259200}
            sec = tf_map.get((tf or "1h").lower(), 3600)
            buckets: dict[int, list[float]] = {}
            for ts, pr in dq:
                h = int(ts // sec)
                buckets.setdefault(h, []).append(float(pr))
            hours = sorted(buckets.keys())[-int(bars):]
            out = []
            for h in hours:
                arr = buckets[h]
                if not arr:
                    continue
                out.append((arr[0], max(arr), min(arr), arr[-1]))
            return out
        except Exception:
            return []

    def _fetch_klines_binance(self, pair_l: str, interval: str = "1h", limit: int = 50):
        try:
            now = time.time()
            key = f"{pair_l}:{interval}:{int(limit)}"
            # 30s cache to avoid hammering
            hit = self._thumb_cache.get(key)
            if hit and now - float(hit[0]) < 30.0:
                return hit[1]
            sym = (pair_l or "").upper()
            if not sym.endswith("USDT"):
                return []
            url = "https://api.binance.com/api/v3/klines"
            params = {"symbol": sym, "interval": interval, "limit": int(limit)}
            try:
                resp = requests.get(url, params=params, timeout=5)
                if resp.status_code != 200:
                    return []
                arr = resp.json()
            except Exception:
                return []
            out: list[tuple[float, float, float, float]] = []
            for k in arr:
                # k: [openTime, open, high, low, close, ...]
                try:
                    o = float(k[1]); h = float(k[2]); l = float(k[3]); c = float(k[4])
                    out.append((o, h, l, c))
                except Exception:
                    continue
            self._thumb_cache[key] = (now, out)
            return out
        except Exception:
            return []

    def _render_kline_pixmap(self, ohlc: list[tuple[float, float, float, float]], w: int = 240, h: int = 120, ohlc2: list[tuple[float, float, float, float]] | None = None) -> QtGui.QPixmap | None:
        if not ohlc:
            return None
        W, H = int(w), int(h)
        # HiDPI-aware offscreen rendering
        try:
            scr = QtWidgets.QApplication.primaryScreen()
            dpr = float(getattr(scr, 'devicePixelRatio', lambda: 1.0)()) if scr else 1.0
        except Exception:
            dpr = 1.0
        dpr = max(1.0, dpr)
        pm = QtGui.QPixmap(int(W * dpr), int(H * dpr))
        try:
            pm.setDevicePixelRatio(dpr)
        except Exception:
            pass
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        # Quality hints to reduce jaggies/blur
        try:
            p.setRenderHint(QtGui.QPainter.Antialiasing, True)
            p.setRenderHint(QtGui.QPainter.TextAntialiasing, True)
            p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
            if hasattr(QtGui.QPainter, 'HighQualityAntialiasing'):
                p.setRenderHint(QtGui.QPainter.HighQualityAntialiasing, True)
        except Exception:
            pass
        # Paint in logical coordinates (do NOT scale; devicePixelRatio handles it)
        try:
            # semi-transparent black background (~33% transparent)
            p.fillRect(0, 0, W, H, QtGui.QColor(0, 0, 0, 255))

            # bounds
            highs = [x[1] for x in ohlc]
            lows = [x[2] for x in ohlc]
            v_max = max(highs) if highs else 1.0
            v_min = min(lows) if lows else 0.0
            if v_max == v_min:
                v_max += 1.0
                v_min -= 1.0
            pad = 6
            gutter = max(40, int(W * 0.14))
            left, right = pad, W - pad - gutter
            dual = bool(self.thumb_dual_enabled)
            dual_active = bool(dual and ohlc2)
            sub_enabled = (self.thumb_sub_indicator in ("rsi", "macd", "kdj"))
            avail_h = H - 2 * pad
            if dual_active and sub_enabled:
                top_h = int(avail_h * 0.38)
                bot_h = int(avail_h * 0.38)
                sub_h = avail_h - top_h - bot_h - 2
                top_top, top_bot = pad, pad + top_h
                main_top, main_bottom = top_bot + 2, top_bot + 2 + bot_h
                sub_top, sub_bottom = main_bottom + 2, main_bottom + 2 + sub_h
            elif dual_active:
                top_h = int(avail_h * 0.48)
                bot_h = avail_h - top_h - 2
                top_top, top_bot = pad, pad + top_h
                main_top, main_bottom = top_bot + 2, top_bot + 2 + bot_h
                sub_top, sub_bottom = None, None
            elif sub_enabled:
                main_top, main_bottom = pad, pad + int(avail_h * 0.62)
                sub_top, sub_bottom = main_bottom + 2, H - pad
                top_top, top_bot = None, None
            else:
                main_top, main_bottom = pad, H - pad
                sub_top, sub_bottom = None, None
                top_top, top_bot = None, None
            width = right - left
            height = main_bottom - main_top
            n = len(ohlc)
            cw = width / max(1, n)
            bw = max(2.0, min(8.0, cw * 0.6))
            def y_of(v: float) -> int:
                return int(main_top + (v_max - v) / (v_max - v_min) * height)
            # Optional top chart in dual mode
            if dual_active and top_top is not None and ohlc2:
                highs2 = [x[1] for x in ohlc2]
                lows2 = [x[2] for x in ohlc2]
                v_max2 = max(highs2) if highs2 else 1.0
                v_min2 = min(lows2) if lows2 else 0.0
                if v_max2 == v_min2:
                    v_max2 += 1.0; v_min2 -= 1.0
                cw2 = width / max(1, len(ohlc2))
                def y2(v: float) -> int:
                    return int(top_top + (v_max2 - v) / (v_max2 - v_min2) * (top_bot - top_top))
                if (self.thumb_chart_style or 'candle').lower() == 'line':
                    pen = QtGui.QPen(QtGui.QColor(135, 206, 235))
                    pen.setWidth(max(1, int(H * 0.012)))
                    pen.setCapStyle(QtCore.Qt.RoundCap); pen.setJoinStyle(QtCore.Qt.RoundJoin)
                    p.setPen(pen)
                    prev = None
                    for i, (_, _, _, c_) in enumerate(ohlc2):
                        x = int(left + i * cw2 + cw2 / 2)
                        y = y2(c_)
                        pt = QtCore.QPoint(x, y)
                        if prev is not None:
                            p.drawLine(prev, pt)
                        prev = pt
                else:
                    bw2 = max(2.0, min(8.0, cw2 * 0.6))
                    for i, (o2, h2, l2, c2) in enumerate(ohlc2):
                        x = int(left + i * cw2 + cw2 / 2)
                        y_o = y2(o2); y_c = y2(c2); y_h = y2(h2); y_l = y2(l2)
                        up = c2 >= o2
                        col = QtGui.QColor(0, 200, 0) if up else QtGui.QColor(220, 0, 0)
                        pen = QtGui.QPen(col); pen.setWidth(max(1, int(H * 0.006)))
                        pen.setCapStyle(QtCore.Qt.RoundCap); pen.setJoinStyle(QtCore.Qt.RoundJoin)
                        p.setPen(pen)
                        p.drawLine(x, y_h, x, y_l)
                        body_top = min(y_o, y_c); body_bot = max(y_o, y_c)
                        if body_bot - body_top < 1: body_bot = body_top + 1
                        # Clamp body width to avoid overlap when cw2 is small, and align to pixels
                        rwf = max(1.0, min(bw2, max(1.0, cw2 - 1.0)))
                        rw = max(1, int(round(rwf)))
                        rx = int(round(x - rw / 2))
                        rect = QtCore.QRect(rx, int(body_top), rw, int(body_bot - body_top))
                        p.fillRect(rect, col); p.drawRect(rect)

                # Top overlay lines (independent)
                if self.thumb_dual_overlay_type in ("ma", "ema"):
                    closes2 = [c for (_, _, _, c) in ohlc2]
                    def _ma2(seq, n):
                        out, s, q = [], 0.0, []
                        for v in seq:
                            q.append(v); s += v
                            if len(q) > n: s -= q.pop(0)
                            out.append(s / len(q))
                        return out
                    def _ema2(seq, n):
                        out, k, prev = [], 2.0/(n+1.0), None
                        for v in seq:
                            prev = v if prev is None else (v*k + prev*(1-k))
                            out.append(prev)
                        return out
                    f2 = _ma2 if self.thumb_dual_overlay_type == 'ma' else _ema2
                    p1_2 = max(1, int(self.thumb_dual_overlay_p1)); p2_2 = max(1, int(self.thumb_dual_overlay_p2))
                    l1_2 = f2(closes2, p1_2); l2_2 = f2(closes2, p2_2)
                    def _col(name: str) -> QtGui.QColor:
                        mapping = {
                            'gold': QtGui.QColor(255,215,0), 'skyblue': QtGui.QColor(135,206,235),
                            'orange': QtGui.QColor(255,165,0), 'lime': QtGui.QColor(0,255,0),
                            'white': QtGui.QColor(255,255,255), 'yellow': QtGui.QColor(255,255,0),
                            'red': QtGui.QColor(255,0,0), 'green': QtGui.QColor(0,200,0),
                            'cyan': QtGui.QColor(0,255,255), 'magenta': QtGui.QColor(255,0,255),
                        }
                        return mapping.get((name or '').lower(), QtGui.QColor(255,165,0))
                    def draw2(vals, color: QtGui.QColor):
                        pen = QtGui.QPen(color); pen.setWidth(max(1, int(H * 0.006)))
                        pen.setCapStyle(QtCore.Qt.RoundCap); pen.setJoinStyle(QtCore.Qt.RoundJoin)
                        p.setPen(pen); prev_pt = None
                        for i, v in enumerate(vals):
                            x = int(left + i * cw2 + cw2 / 2)
                            y = y2(v); pt = QtCore.QPoint(x, y)
                            if prev_pt is not None: p.drawLine(prev_pt, pt)
                            prev_pt = pt
                    draw2(l1_2, _col(getattr(self, 'thumb_dual_overlay_color1', 'orange')))
                    draw2(l2_2, _col(getattr(self, 'thumb_dual_overlay_color2', 'lime')))

                # remove right-gutter triangle for dual; disable hit rect
                self._last_dual_sub_tri_rect = None

            if (self.thumb_chart_style or 'candle').lower() == 'line':
                # draw close price line
                pen = QtGui.QPen(QtGui.QColor(135, 206, 235))  # skyblue
                pen.setWidth(max(1, int(H * 0.012)))
                pen.setCapStyle(QtCore.Qt.RoundCap); pen.setJoinStyle(QtCore.Qt.RoundJoin)
                p.setPen(pen)
                prev = None
                for i, (_, _, _, c_) in enumerate(ohlc):
                    x = int(left + i * cw + cw / 2)
                    y = y_of(c_)
                    pt = QtCore.QPoint(x, y)
                    if prev is not None:
                        p.drawLine(prev, pt)
                    prev = pt
            else:
                # draw candles
                for i, (o_, h_, l_, c_) in enumerate(ohlc):
                    x = int(left + i * cw + cw / 2)
                    y_o = y_of(o_)
                    y_c = y_of(c_)
                    y_h = y_of(h_)
                    y_l = y_of(l_)
                    up = c_ >= o_
                    col = QtGui.QColor(0, 200, 0) if up else QtGui.QColor(220, 0, 0)
                    pen = QtGui.QPen(col)
                    pen.setWidth(max(1, int(H * 0.006)))
                    pen.setCapStyle(QtCore.Qt.RoundCap); pen.setJoinStyle(QtCore.Qt.RoundJoin)
                    p.setPen(pen)
                    # wick
                    p.drawLine(x, y_h, x, y_l)
                    # body
                    body_top = min(y_o, y_c)
                    body_bot = max(y_o, y_c)
                    if body_bot - body_top < 1:
                        body_bot = body_top + 1
                    # Clamp body width to avoid overlap when cw is small, and align to pixels
                    rwf = max(1.0, min(bw, max(1.0, cw - 1.0)))
                    rw = max(1, int(round(rwf)))
                    rx = int(round(x - rw / 2))
                    rect = QtCore.QRect(rx, int(body_top), rw, int(body_bot - body_top))
                    p.fillRect(rect, col)
                    p.drawRect(rect)

            # percentage guide lines relative to last close (labels in right gutter)
            try:
                last_close = ohlc[-1][3]
                # Extend to ±35%
                levels = list(range(-35, 40, 5))  # -35,-30,...,0,...,35
                pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 180))
                pen.setStyle(QtCore.Qt.DashLine)
                pen.setWidth(max(1, int(H * 0.008)))
                try:
                    pen.setCosmetic(True)
                except Exception:
                    pass
                p.setPen(pen)
                # Dynamic font size: avoid overlap at high density and large TF
                gutter_w = (W - right - pad)
                font_px_base = max(8, int(min(H * 0.09, gutter_w * 0.45)))
                # Compute min vertical spacing between visible guide lines
                ys = []
                for lv in levels:
                    price_lv = last_close * (1.0 + lv / 100.0)
                    y = y_of(price_lv)
                    if main_top <= y <= main_bottom:
                        ys.append(y)
                ys.sort()
                min_delta = None
                if len(ys) >= 2:
                    min_delta = min(abs(ys[i+1] - ys[i]) for i in range(len(ys)-1))
                if min_delta is not None and min_delta > 0:
                    font_px = max(7, min(font_px_base, int(min_delta * 0.8)))
                else:
                    font_px = font_px_base
                f = p.font(); f.setPointSize(font_px); p.setFont(f)
                for lv in levels:
                    price_lv = last_close * (1.0 + lv / 100.0)
                    y = y_of(price_lv)
                    if y < main_top or y > main_bottom:
                        continue
                    p.drawLine(left, y, right - 2, y)
                    label = ("+" if lv > 0 else "") + f"{lv}%"
                    tx = right + 4
                    ty = y + int(font_px * 0.35)
                    p.setPen(QtGui.QPen(QtGui.QColor(255,255,255,220)))
                    p.drawText(tx, ty, label)
                    p.setPen(pen)  # restore dashed pen
            except Exception:
                pass

            # remove right-gutter triangle for main; disable hit rect
            self._last_sub_tri_rect = None

            # overlay: MA/EMA on main
            closes = [c for (_, _, _, c) in ohlc]
            if self.thumb_overlay_type in ("ma", "ema"):
                def ma(seq, n):
                    out = []
                    s = 0.0
                    q = []
                    for v in seq:
                        q.append(v); s += v
                        if len(q) > n:
                            s -= q.pop(0)
                        out.append(s / len(q))
                    return out
                def ema(seq, n):
                    out = []
                    k = 2.0 / (n + 1.0)
                    prev = None
                    for v in seq:
                        prev = v if prev is None else (v * k + prev * (1 - k))
                        out.append(prev)
                    return out
                f = ma if self.thumb_overlay_type == "ma" else ema
                p1 = max(1, int(self.thumb_overlay_p1))
                p2 = max(1, int(self.thumb_overlay_p2))
                l1 = f(closes, p1)
                l2 = f(closes, p2)
                def draw_line(vals, color: QtGui.QColor):
                    pen = QtGui.QPen(color)
                    pen.setWidth(max(1, int(H * 0.006)))
                    p.setPen(pen)
                    prev_pt = None
                    for i, v in enumerate(vals):
                        x = int(left + i * cw + cw / 2)
                        y = y_of(v)
                        pt = QtCore.QPoint(x, y)
                        if prev_pt is not None:
                            p.drawLine(prev_pt, pt)
                        prev_pt = pt
                def _col(name: str) -> QtGui.QColor:
                    mapping = {
                        'gold': QtGui.QColor(255,215,0), 'skyblue': QtGui.QColor(135,206,235),
                        'orange': QtGui.QColor(255,165,0), 'lime': QtGui.QColor(0,255,0),
                        'white': QtGui.QColor(255,255,255), 'yellow': QtGui.QColor(255,255,0),
                        'red': QtGui.QColor(255,0,0), 'green': QtGui.QColor(0,200,0),
                        'cyan': QtGui.QColor(0,255,255), 'magenta': QtGui.QColor(255,0,255),
                    }
                    return mapping.get((name or '').lower(), QtGui.QColor(255,215,0))
                draw_line(l1, _col(getattr(self, 'thumb_overlay_color1', 'gold')))
                draw_line(l2, _col(getattr(self, 'thumb_overlay_color2', 'skyblue')))

            # sub-indicator
            if sub_enabled and sub_top is not None and sub_bottom is not None:
                sub_h = sub_bottom - sub_top
                p.setClipRect(QtCore.QRect(left, sub_top, width, sub_h))
                # subtle border line
                p.setPen(QtGui.QPen(QtGui.QColor(80, 80, 80, 180), max(1, int(H * 0.008))))
                p.drawLine(left, sub_top, right, sub_top)
                if self.thumb_sub_indicator == "rsi":
                    rsi_vals = self._calc_rsi_series(closes, 14)
                    self._draw_line_series(p, rsi_vals, left, cw, sub_top, sub_bottom, 0, 100, QtGui.QColor(173, 216, 230))
                    # RSI 0/30/70/100 + labels on right
                    pen_grid = QtGui.QPen(QtGui.QColor(200, 200, 200, 220))
                    pen_grid.setStyle(QtCore.Qt.DashLine)
                    pen_grid.setWidth(max(1, int(H * 0.01)))
                    try:
                        pen_grid.setCosmetic(True)
                    except Exception:
                        pass
                    p.setPen(pen_grid)
                    def y_from_pct(pct: float) -> int:
                        return int(sub_top + (1 - pct/100.0) * (sub_bottom - sub_top))
                    y0 = y_from_pct(0.0); y30 = y_from_pct(30.0); y70 = y_from_pct(70.0); y100 = y_from_pct(100.0)
                    p.drawLine(left, y30, right-2, y30)
                    p.drawLine(left, y70, right-2, y70)
                    pen_axis = QtGui.QPen(QtGui.QColor(190, 190, 190, 220))
                    pen_axis.setWidth(max(1, int(H*0.008)))
                    try:
                        pen_axis.setCosmetic(True)
                    except Exception:
                        pass
                    p.setPen(pen_axis)
                    p.drawLine(left, y0, right-2, y0)
                    p.drawLine(left, y100, right-2, y100)
                    font_px_sub = max(7, int(min(H * 0.07, (W - right - pad) * 0.45)))
                    font_px_sub = max(6, int(font_px_sub * 0.6))
                    f = p.font(); f.setPointSize(font_px_sub); p.setFont(f)
                    try:
                        p.setClipping(False)
                    except Exception:
                        pass
                    p.setPen(QtGui.QPen(QtGui.QColor(255,255,255,240)))
                    for val, yy in [(100, y100), (70, y70), (30, y30), (0, y0)]:
                        p.drawText(right + 4, yy + int(font_px_sub * 0.35), str(val))
                    try:
                        p.setClipRect(QtCore.QRect(left, sub_top, width, sub_h))
                    except Exception:
                        pass
                elif self.thumb_sub_indicator == "kdj":
                    # KDJ gridlines (dashed) at 20/50/80 with labels
                    try:
                        pen_grid_kdj = QtGui.QPen(QtGui.QColor(200, 200, 200, 220))
                        pen_grid_kdj.setStyle(QtCore.Qt.DashLine)
                        pen_grid_kdj.setWidth(max(1, int(H * 0.01)))
                        try:
                            pen_grid_kdj.setCosmetic(True)
                        except Exception:
                            pass
                        p.setPen(pen_grid_kdj)
                        def yk_from_pct(pct: float) -> int:
                            return int(sub_top + (1 - pct/100.0) * (sub_bottom - sub_top))
                        y20 = yk_from_pct(20.0); y50 = yk_from_pct(50.0); y80 = yk_from_pct(80.0)
                        p.drawLine(left, y20, right-2, y20)
                        p.drawLine(left, y50, right-2, y50)
                        p.drawLine(left, y80, right-2, y80)
                        font_px_kdj = max(7, int(min(H * 0.07, (W - right - pad) * 0.45)))
                        font_px_kdj = max(6, int(font_px_kdj * 0.6))
                        f = p.font(); f.setPointSize(font_px_kdj); p.setFont(f)
                        try:
                            p.setClipping(False)
                        except Exception:
                            pass
                        p.setPen(QtGui.QPen(QtGui.QColor(255,255,255,240)))
                        for val, yy in [(80, y80), (50, y50), (20, y20)]:
                            p.drawText(right + 4, yy + int(font_px_kdj * 0.35), str(val))
                        try:
                            p.setClipRect(QtCore.QRect(left, sub_top, width, sub_h))
                        except Exception:
                            pass
                    except Exception:
                        pass
                    # KDJ lines (K,D)
                    k, d, j = self._calc_kdj_series(ohlc, 9, 3, 3)
                    self._draw_line_series(p, k, left, cw, sub_top, sub_bottom, 0, 100, QtGui.QColor(255, 215, 0))
                    self._draw_line_series(p, d, left, cw, sub_top, sub_bottom, 0, 100, QtGui.QColor(135, 206, 235))
                elif self.thumb_sub_indicator == "macd":
                    macd, signal, hist = self._calc_macd_series(closes, 12, 26, 9)
                    # scale to symmetric range using all series so线不出界
                    mx = max(1e-9,
                             max(abs(min(hist)), abs(max(hist)),
                                 abs(min(macd)), abs(max(macd)),
                                 abs(min(signal)), abs(max(signal))))
                    for i, hval in enumerate(hist):
                        x = int(left + i * cw + cw / 2)
                        yh = int(sub_top + (sub_bottom - sub_top) * (0.5 - 0.45 * (hval / mx)))
                        y0 = int(sub_top + (sub_bottom - sub_top) * 0.5)
                        col = QtGui.QColor(0, 200, 0) if hval >= 0 else QtGui.QColor(220, 0, 0)
                        p.setPen(QtGui.QPen(col, max(1, int(bw * 0.6))))
                        p.drawLine(x, y0, x, yh)
                    self._draw_line_series(p, macd, left, cw, sub_top, sub_bottom, -mx, mx, QtGui.QColor(173, 216, 230))
                    self._draw_line_series(p, signal, left, cw, sub_top, sub_bottom, -mx, mx, QtGui.QColor(255, 215, 0))

            # Top sub-indicator (dual)
            if dual and top_top is not None and ohlc2 and (self.thumb_dual_sub_indicator in ("rsi","macd","kdj")):
                sub2_top = top_bot + 2
                sub2_bottom = min(H - pad, sub2_top + max(10, int((main_bottom - main_top) * 0.45)))
                if sub2_bottom - sub2_top > 10:
                    cw2 = width / max(1, len(ohlc2))
                    p.setClipRect(QtCore.QRect(left, sub2_top, width, sub2_bottom - sub2_top))
                    pen_sep2 = QtGui.QPen(QtGui.QColor(80, 80, 80, 180))
                    pen_sep2.setWidth(max(1, int(H * 0.008)))
                    try:
                        pen_sep2.setCosmetic(True)
                    except Exception:
                        pass
                    p.setPen(pen_sep2)
                    p.drawLine(left, sub2_top, right, sub2_top)
                    closes2 = [c for (_, _, _, c) in ohlc2]
                    if self.thumb_dual_sub_indicator == 'rsi' and closes2:
                        rsi2 = self._calc_rsi_series(closes2, 14)
                        self._draw_line_series(p, rsi2, left, cw2, sub2_top, sub2_bottom, 0, 100, QtGui.QColor(173, 216, 230))
                        # RSI 0/30/70/100 线与刻度
                        pen_grid2 = QtGui.QPen(QtGui.QColor(120, 120, 120, 160))
                        pen_grid2.setStyle(QtCore.Qt.DashLine)
                        pen_grid2.setWidth(max(1, int(H * 0.008)))
                        try:
                            pen_grid2.setCosmetic(True)
                        except Exception:
                            pass
                        p.setPen(pen_grid2)
                        def y2_from_pct(pct: float) -> int:
                            return int(sub2_top + (1 - pct/100.0) * (sub2_bottom - sub2_top))
                        y0 = y2_from_pct(0.0); y30 = y2_from_pct(30.0); y70 = y2_from_pct(70.0); y100 = y2_from_pct(100.0)
                        p.drawLine(left, y30, right-2, y30)
                        p.drawLine(left, y70, right-2, y70)
                        pen_axis2 = QtGui.QPen(QtGui.QColor(140,140,140,180))
                        pen_axis2.setWidth(max(1, int(H*0.008)))
                        try:
                            pen_axis2.setCosmetic(True)
                        except Exception:
                            pass
                        p.setPen(pen_axis2)
                        p.drawLine(left, y0, right-2, y0)
                        p.drawLine(left, y100, right-2, y100)
                        font_px_sub2 = max(7, int(min(H * 0.07, (W - right - pad) * 0.45)))
                        font_px_sub2 = max(6, int(font_px_sub2 * 0.6))
                        f2 = p.font(); f2.setPointSize(font_px_sub2); p.setFont(f2)
                        p.setPen(QtGui.QPen(QtGui.QColor(220,220,220,220)))
                        for val, yy in [(100, y100), (70, y70), (30, y30), (0, y0)]:
                            p.drawText(right + 4, yy + int(font_px_sub2 * 0.35), str(val))
                    elif self.thumb_dual_sub_indicator == 'kdj':
                        # KDJ gridlines (dashed) at 20/50/80 with labels for dual sub
                        try:
                            pen_grid_kdj2 = QtGui.QPen(QtGui.QColor(120, 120, 120, 160))
                            pen_grid_kdj2.setStyle(QtCore.Qt.DashLine)
                            pen_grid_kdj2.setWidth(max(1, int(H * 0.008)))
                            try:
                                pen_grid_kdj2.setCosmetic(True)
                            except Exception:
                                pass
                            p.setPen(pen_grid_kdj2)
                            def yk2_from_pct(pct: float) -> int:
                                return int(sub2_top + (1 - pct/100.0) * (sub2_bottom - sub2_top))
                            y20 = yk2_from_pct(20.0); y50 = yk2_from_pct(50.0); y80 = yk2_from_pct(80.0)
                            p.drawLine(left, y20, right-2, y20)
                            p.drawLine(left, y50, right-2, y50)
                            p.drawLine(left, y80, right-2, y80)
                            font_px_kdj2 = max(7, int(min(H * 0.07, (W - right - pad) * 0.45)))
                            font_px_kdj2 = max(6, int(font_px_kdj2 * 0.6))
                            f2 = p.font(); f2.setPointSize(font_px_kdj2); p.setFont(f2)
                            p.setPen(QtGui.QPen(QtGui.QColor(220,220,220,220)))
                            for val, yy in [(80, y80), (50, y50), (20, y20)]:
                                p.drawText(right + 4, yy + int(font_px_kdj2 * 0.35), str(val))
                        except Exception:
                            pass
                        # KDJ lines (K,D)
                        k2, d2, j2 = self._calc_kdj_series(ohlc2, 9, 3, 3)
                        self._draw_line_series(p, k2, left, cw2, sub2_top, sub2_bottom, 0, 100, QtGui.QColor(255, 215, 0))
                        self._draw_line_series(p, d2, left, cw2, sub2_top, sub2_bottom, 0, 100, QtGui.QColor(135, 206, 235))
                    elif self.thumb_dual_sub_indicator == 'macd' and closes2:
                        m2, s2, h2 = self._calc_macd_series(closes2, 12, 26, 9)
                        mx2 = max(1e-9, max(abs(min(h2)), abs(max(h2)), abs(min(m2)), abs(max(m2)), abs(min(s2)), abs(max(s2))))
                        for i, hv in enumerate(h2):
                            x = int(left + i * cw2 + cw2 / 2)
                            yh = int(sub2_top + (sub2_bottom - sub2_top) * (0.5 - 0.45 * (hv / mx2)))
                            y0 = int(sub2_top + (sub2_bottom - sub2_top) * 0.5)
                            col = QtGui.QColor(0, 200, 0) if hv >= 0 else QtGui.QColor(220, 0, 0)
                            p.setPen(QtGui.QPen(col, max(1, int(bw * 0.6))))
                            p.drawLine(x, y0, x, yh)
                        self._draw_line_series(p, m2, left, cw2, sub2_top, sub2_bottom, -mx2, mx2, QtGui.QColor(173, 216, 230))
                        self._draw_line_series(p, s2, left, cw2, sub2_top, sub2_bottom, -mx2, mx2, QtGui.QColor(255, 215, 0))
        finally:
            p.end()
        return pm

    def _render_kline_pixmap_dual_window(self, ohlc: list[tuple[float, float, float, float]], w: int = 240, h: int = 120) -> QtGui.QPixmap | None:
        """Render standalone dual popup using dual-specific config (overlay/sub)."""
        # Save current options, temporarily swap to dual params
        saved = (
            self.thumb_overlay_type,
            self.thumb_overlay_p1,
            self.thumb_overlay_p2,
            getattr(self, 'thumb_overlay_color1', 'gold'),
            getattr(self, 'thumb_overlay_color2', 'skyblue'),
            self.thumb_sub_indicator,
            getattr(self, 'thumb_dual_enabled', False),
        )
        try:
            self.thumb_overlay_type = getattr(self, 'thumb_dual_overlay_type', 'ma')
            self.thumb_overlay_p1 = int(getattr(self, 'thumb_dual_overlay_p1', 5))
            self.thumb_overlay_p2 = int(getattr(self, 'thumb_dual_overlay_p2', 20))
            self.thumb_overlay_color1 = getattr(self, 'thumb_dual_overlay_color1', 'orange')
            self.thumb_overlay_color2 = getattr(self, 'thumb_dual_overlay_color2', 'lime')
            self.thumb_sub_indicator = getattr(self, 'thumb_dual_sub_indicator', 'macd')
            self.thumb_dual_enabled = False
            return self._render_kline_pixmap(ohlc, w, h, ohlc2=None)
        finally:
            (
                self.thumb_overlay_type,
                self.thumb_overlay_p1,
                self.thumb_overlay_p2,
                self.thumb_overlay_color1,
                self.thumb_overlay_color2,
                self.thumb_sub_indicator,
                self.thumb_dual_enabled,
            ) = saved

    def _draw_line_series(self, painter: QtGui.QPainter, vals: list[float], left: int, cw: float, top: int, bottom: int, vmin: float, vmax: float, color: QtGui.QColor):
        if not vals:
            return
        pen = QtGui.QPen(color)
        # Thinner lines for subcharts: reduce ~30% from previous default
        try:
            base_w = 2
            pen.setWidth(max(1, int(round(base_w * 0.7))))
            pen.setCapStyle(QtCore.Qt.RoundCap)
            pen.setJoinStyle(QtCore.Qt.RoundJoin)
        except Exception:
            pen.setWidth(1)
        painter.setPen(pen)
        def y_of(v: float) -> int:
            if vmax == vmin:
                return int((top + bottom) / 2)
            return int(top + (1.0 - (v - vmin) / (vmax - vmin)) * (bottom - top))
        prev = None
        for i, v in enumerate(vals):
            x = int(left + i * cw + cw / 2)
            y = y_of(v)
            pt = QtCore.QPoint(x, y)
            if prev is not None:
                painter.drawLine(prev, pt)
            prev = pt

    def _calc_rsi_series(self, closes: list[float], period: int = 14) -> list[float]:
        out = []
        gains, losses = 0.0, 0.0
        prev = None
        for i, c in enumerate(closes):
            if prev is None:
                out.append(50.0)
            else:
                ch = c - prev
                gains = (gains * (period - 1) + max(0.0, ch)) / period
                losses = (losses * (period - 1) + max(0.0, -ch)) / period
                rs = (gains / losses) if losses > 1e-12 else 999.0
                rsi = 100.0 - (100.0 / (1.0 + rs))
                out.append(rsi)
            prev = c
        return out

    def _calc_macd_series(self, closes: list[float], fast: int = 12, slow: int = 26, signal_p: int = 9):
        def ema(seq, n):
            out = []
            k = 2.0 / (n + 1.0)
            prev = None
            for v in seq:
                prev = v if prev is None else (v * k + prev * (1 - k))
                out.append(prev)
            return out
        ema_fast = ema(closes, fast)
        ema_slow = ema(closes, slow)
        macd = [a - b for a, b in zip(ema_fast, ema_slow)]
        signal = ema(macd, signal_p)
        hist = [a - b for a, b in zip(macd, signal)]
        return macd, signal, hist

    def _calc_kdj_series(self, ohlc: list[tuple[float, float, float, float]], n: int = 9, k_p: int = 3, d_p: int = 3):
        closes = [c for (_, _, _, c) in ohlc]
        highs = [h for (_, h, _, _) in ohlc]
        lows = [l for (_, _, l, _) in ohlc]
        rsv_list = []
        for i in range(len(closes)):
            lo = min(lows[max(0, i - n + 1): i + 1])
            hi = max(highs[max(0, i - n + 1): i + 1])
            if hi == lo:
                rsv = 50.0
            else:
                rsv = (closes[i] - lo) / (hi - lo) * 100.0
            rsv_list.append(rsv)
        def sma(seq, p):
            out = []
            prev = None
            a = 2.0 / (p + 1.0)
            for v in seq:
                prev = v if prev is None else (a * v + (1 - a) * prev)
                out.append(prev)
            return out
        k = sma(rsv_list, k_p)
        d = sma(k, d_p)
        j = [3 * kk - 2 * dd for kk, dd in zip(k, d)]
        return k, d, j

    def show_thumbnail(self, index: int, click_pos: QtCore.QPoint | None = None):
        if not bool(self.thumb_enabled):
            return
        if getattr(self, '_menu_open', False):
            return
        if not (0 <= index < len(self.slots)):
            return
        pair = self._slot_to_pair(self.slots[index])
        if not pair:
            return
        pair_l = pair.lower()
        self._thumb_pair_current = pair_l
        tf_key = ((self.thumb_tf or "1h") or "1h").lower()
        if self._thumb_line_selected and (self._thumb_line_selected[0] != pair_l or self._thumb_line_selected[1] != tf_key):
            self._thumb_line_selected = None
        bars = int(max(10, min(200, int(self.thumb_bars or 50))))
        # Fast local preview first to avoid UI卡顿
        ohlc = self._ohlc_from_local(pair_l, bars, tf=(self.thumb_tf or "1h"))
        # Prepare size by scale
        # Apply configured scale for initial size
        try:
            scale = max(50, min(300, int(self.thumb_scale_percent or 100))) / 100.0
        except Exception:
            scale = 1.0
        base_w, base_h = 240, 120
        self._thumb_popup.resize(int(base_w * scale), int(base_h * scale))
        # 清空旧图，优先显示加载占位，避免黑屏
        try:
            self._thumb_popup.set_pixmap(None)
        except Exception:
            pass
        self._thumb_ohlc_main = ohlc
        # Dual popup always shows fixed 4h preview (no extra config)
        bars2 = int(max(10, min(200, int(self.thumb_bars2 or bars))))
        ohlc2 = self._ohlc_from_local(pair_l, bars2, tf=(self.thumb_tf2 or "4h"))
        self._thumb_ohlc_top = ohlc2
        if ohlc:
            self._thumb_popup.set_data_and_owner(ohlc, self, ohlc2=None, tf_label=(self.thumb_tf or ""))
        pos = (click_pos if click_pos is not None else QtGui.QCursor.pos()) + QtCore.QPoint(12, 12)
        self._thumb_popup.move(pos)
        self._thumb_popup.show()
        try:
            self._thumb_popup.activateWindow()
            self._thumb_popup.raise_()
            self._thumb_popup.setFocus(QtCore.Qt.MouseFocusReason)
        except Exception:
            pass
        # Position and show dual popup above widget
        try:
            # apply dual scale (separate), default to main scale if missing
            try:
                dual_scale = max(50, min(300, int(getattr(self, 'thumb_dual_scale_percent', self.thumb_scale_percent) or self.thumb_scale_percent))) / 100.0
            except Exception:
                dual_scale = scale
            self._dual_popup.resize(int(base_w * dual_scale), int(base_h * dual_scale))
            dual_w, dual_h = self._dual_popup.width(), self._dual_popup.height()
            base = self.mapToGlobal(QtCore.QPoint(0, 0))
            dual_pos = QtCore.QPoint(base.x(), max(0, base.y() - dual_h - 8))
            # Clear then set data to show loading quickly
            self._dual_popup.set_pixmap(None)
            self._dual_popup.move(dual_pos)
            if ohlc2:
                self._dual_popup.set_data_and_owner(ohlc2, self, tf_label=(self.thumb_tf2 or ""))
            self._dual_popup.show()
            try:
                self._dual_popup.activateWindow()
                self._dual_popup.raise_()
                self._dual_popup.setFocus(QtCore.Qt.MouseFocusReason)
            except Exception:
                pass
        except Exception:
            pass
        # Async fetch from Binance to update after show
        if bool(self.thumb_fetch_from_binance):
            def _worker(pair_l_, bars_, tf_, dual_, bars2_, tf2_):
                try:
                    data = self._fetch_klines_binance(pair_l_, interval=tf_, limit=int(bars_))
                    if data:
                        try:
                            self.thumb_data_ready.emit(pair_l_, tf_, data)
                        except Exception:
                            pass
                    # Always fetch dual (top) in background as separate popup
                        d2 = self._fetch_klines_binance(pair_l_, interval=tf2_, limit=int(bars2_))
                        if d2:
                            try:
                                self.thumb_data_ready.emit(pair_l_, tf2_, d2)
                            except Exception:
                                pass
                except Exception:
                    pass
            t = threading.Thread(target=_worker, args=(pair_l, bars, (self.thumb_tf or "1h"), bool(self.thumb_dual_enabled), int(self.thumb_bars2 or bars), (self.thumb_tf2 or "4h")), daemon=True)
            t.start()

    @QtCore.Slot(str, str, list)
    def _on_thumb_data_ready(self, pair_l: str, tf: str, ohlc: list):
        if pair_l != getattr(self, '_thumb_pair_current', None):
            return
        if not ohlc:
            return
        try:
            # Update which buffer to use
            if tf == (self.thumb_tf or "1h"):
                self._thumb_ohlc_main = ohlc
                self._thumb_popup.set_data_and_owner(self._thumb_ohlc_main or [], self, ohlc2=None)
            # Use a separate check (not elif) so if tf == tf2, both popups update
            if tf == (self.thumb_tf2 or "4h"):
                self._thumb_ohlc_top = ohlc
                self._dual_popup.set_data_and_owner(self._thumb_ohlc_top or [], self)
        except Exception:
            pass

    def move_thumbnail(self, global_pos: QtCore.QPoint):
        # No-op since we switched to click-to-show
        return

    def _thumb_context(self, mode: str = 'main') -> tuple[str | None, str]:
        pair = getattr(self, '_thumb_pair_current', None)
        if not pair:
            return None, ""
        tf_main = ((self.thumb_tf or "1h") or "1h").lower()
        if mode == 'dual':
            tf_alt = ((self.thumb_tf2 or tf_main) or tf_main).lower()
            return pair, tf_alt
        return pair, tf_main

    def _normalize_thumb_line(self, start: QtCore.QPoint, end: QtCore.QPoint, popup: 'ThumbnailPopup') -> dict | tuple | None:
        W = popup.width(); H = popup.height()
        if W <= 0 or H <= 0:
            return None
        dx = end.x() - start.x(); dy = end.y() - start.y()
        if math.hypot(dx, dy) < 4.0:
            return None
        # Prefer time+price format for cross-timeframe alignment
        left, right, main_top, main_bottom, v_min, v_max = self._chart_area_for_popup(popup)
        width = max(1, right - left); height = max(1, main_bottom - main_top)
        def clamp(v, a, b):
            return max(a, min(b, v))
        if v_max != v_min:
            # time offset from right edge (seconds)
            n = max(1, len(getattr(popup, '_ohlc', []) or []))
            cw = float(width) / max(1, n)
            tf_sec = max(1, self._popup_tf_seconds(popup))
            def to_toff(px):
                frac = (clamp(px, left, right) - left) / float(width)
                idx = frac * max(1, n - 1)
                idx_r = max(0.0, (n - 1) - idx)
                return float(idx_r * tf_sec)
            t1_off = to_toff(start.x()); t2_off = to_toff(end.x())
            p1 = v_max - (clamp(start.y(), main_top, main_bottom) - main_top) / height * (v_max - v_min)
            p2 = v_max - (clamp(end.y(), main_top, main_bottom) - main_top) / height * (v_max - v_min)
            return {'t1_off': float(t1_off), 'p1': float(p1), 't2_off': float(t2_off), 'p2': float(p2), 'fmt': 'time'}
        # Fallback legacy (no scale info)
        x1 = max(0.0, min(1.0, start.x() / W))
        y1 = max(0.0, min(1.0, start.y() / H))
        x2 = max(0.0, min(1.0, end.x() / W))
        y2 = max(0.0, min(1.0, end.y() / H))
        return (x1, y1, x2, y2)

    def _chart_area_for_popup(self, popup: 'ThumbnailPopup') -> tuple[int, int, int, int, float, float]:
        # returns (left, right, main_top, main_bottom, v_min, v_max)
        W, H = popup.width(), popup.height()
        pad = 6
        gutter = max(40, int(W * 0.14))
        left, right = pad, W - pad - gutter
        # compute v_min/v_max from popup ohlc
        highs = [x[1] for x in (popup._ohlc or [])]
        lows = [x[2] for x in (popup._ohlc or [])]
        v_max = max(highs) if highs else 1.0
        v_min = min(lows) if lows else 0.0
        if v_max == v_min:
            v_max += 1.0
            v_min -= 1.0
        owner = getattr(popup, '_owner_ref', None)
        mode = getattr(popup, '_mode', 'main')
        dual_active = False
        sub_enabled = False
        try:
            if owner is not None:
                if mode == 'main':
                    dual_active = bool(getattr(owner, 'thumb_dual_enabled', False) and bool(getattr(popup, '_ohlc2', None)))
                    sub_enabled = str(getattr(owner, 'thumb_sub_indicator', 'none')).lower() in ("rsi","macd","kdj")
                else:
                    sub_enabled = str(getattr(owner, 'thumb_dual_sub_indicator', 'none')).lower() in ("rsi","macd","kdj")
        except Exception:
            pass
        avail_h = H - 2 * pad
        if dual_active and sub_enabled:
            top_h = int(avail_h * 0.38)
            bot_h = int(avail_h * 0.38)
            top_top, top_bot = pad, pad + top_h
            main_top, main_bottom = top_bot + 2, top_bot + 2 + bot_h
        elif dual_active:
            top_h = int(avail_h * 0.48)
            top_top, top_bot = pad, pad + top_h
            main_top, main_bottom = top_bot + 2, H - pad
        elif sub_enabled:
            main_top, main_bottom = pad, pad + int(avail_h * 0.62)
        else:
            main_top, main_bottom = pad, H - pad
        return left, right, main_top, main_bottom, float(v_min), float(v_max)

    def _popup_tf_seconds(self, popup: 'ThumbnailPopup') -> int:
        # Determine timeframe seconds for this popup's mode
        try:
            owner = getattr(popup, '_owner_ref', None)
            mode = getattr(popup, '_mode', 'main')
            label = None
            if popup._tf_label:
                label = str(popup._tf_label).lower()
            else:
                if owner is not None:
                    label = (owner.thumb_tf if mode == 'main' else owner.thumb_tf2)
            tf_map = getattr(self, '_tf_seconds', {"1m":60, "5m":300, "15m":900, "1h":3600, "4h":14400})
            return int(tf_map.get(str(label).lower(), 3600))
        except Exception:
            return 3600

    def _thumb_line_pixels(self, pair: str, tf: str, popup: 'ThumbnailPopup') -> list[tuple[int, int, int, int]]:
        lines = self._thumb_lines.get(pair, {}).get(tf, [])
        if not lines:
            return []
        W = popup.width(); H = popup.height()
        if W <= 0 or H <= 0:
            return []
        left, right, main_top, main_bottom, v_min, v_max = self._chart_area_for_popup(popup)
        width = max(1, right - left)
        height = max(1, main_bottom - main_top)
        px_lines: list[tuple[int, int, int, int]] = []
        n = max(1, len(getattr(popup, '_ohlc', []) or []))
        cw = max(1.0, (right - left) / max(1, n))
        for line in lines:
            if isinstance(line, dict) and line.get('fmt') == 'time' and ('t1' in line or 't1_off' in line):
                try:
                    # support key 't1' or 't1_off' in seconds from right edge
                    t1 = float(line.get('t1_off', line.get('t1', 0.0)))
                    t2 = float(line.get('t2_off', line.get('t2', 0.0)))
                    tf_sec = max(1, self._popup_tf_seconds(popup))
                    idx1_r = t1 / tf_sec
                    idx2_r = t2 / tf_sec
                    # map from index-from-right to x pixels
                    x1 = int(left + (max(0.0, n - 1 - idx1_r)) * cw + cw / 2)
                    x2 = int(left + (max(0.0, n - 1 - idx2_r)) * cw + cw / 2)
                    p1 = float(line.get('p1', v_min)); p2 = float(line.get('p2', v_min))
                    if v_max == v_min:
                        y1 = y2 = int((main_top + main_bottom) / 2)
                    else:
                        y1 = int(main_top + (v_max - p1) / (v_max - v_min) * height)
                        y2 = int(main_top + (v_max - p2) / (v_max - v_min) * height)
                    px_lines.append((x1, y1, x2, y2))
                except Exception:
                    continue
            elif isinstance(line, dict) and 'x1f' in line:
                try:
                    x1f = float(line.get('x1f', 0.0)); x2f = float(line.get('x2f', 0.0))
                    p1 = float(line.get('p1', v_min)); p2 = float(line.get('p2', v_min))
                    # map to pixels via current axis
                    px1 = int(left + max(0.0, min(1.0, x1f)) * width)
                    px2 = int(left + max(0.0, min(1.0, x2f)) * width)
                    if v_max == v_min:
                        py1 = py2 = int((main_top + main_bottom) / 2)
                    else:
                        py1 = int(main_top + (v_max - p1) / (v_max - v_min) * height)
                        py2 = int(main_top + (v_max - p2) / (v_max - v_min) * height)
                    px_lines.append((px1, py1, px2, py2))
                except Exception:
                    continue
            else:
                try:
                    x1, y1, x2, y2 = float(line[0]), float(line[1]), float(line[2]), float(line[3])
                    px1 = max(0, min(W - 1, int(round(x1 * W))))
                    py1 = max(0, min(H - 1, int(round(y1 * H))))
                    px2 = max(0, min(W - 1, int(round(x2 * W))))
                    py2 = max(0, min(H - 1, int(round(y2 * H))))
                    px_lines.append((px1, py1, px2, py2))
                except Exception:
                    continue
        return px_lines

    def _thumb_line_pixels_other_timeframes(self, pair: str, exclude_tf: str, popup: 'ThumbnailPopup') -> list[tuple[int, int, int, int]]:
        tf_map = self._thumb_lines.get(pair, {})
        if not tf_map:
            return []
        W = popup.width(); H = popup.height()
        if W <= 0 or H <= 0:
            return []
        left, right, main_top, main_bottom, v_min, v_max = self._chart_area_for_popup(popup)
        width = max(1, right - left)
        height = max(1, main_bottom - main_top)
        out: list[tuple[int, int, int, int]] = []
        n = max(1, len(getattr(popup, '_ohlc', []) or []))
        cw = max(1.0, (right - left) / max(1, n))
        for tf_key, lines in tf_map.items():
            if not lines or str(tf_key) == str(exclude_tf):
                continue
            for line in lines:
                if isinstance(line, dict) and line.get('fmt') == 'time' and ('t1' in line or 't1_off' in line):
                    try:
                        t1 = float(line.get('t1_off', line.get('t1', 0.0)))
                        t2 = float(line.get('t2_off', line.get('t2', 0.0)))
                        tf_sec = max(1, self._popup_tf_seconds(popup))
                        idx1_r = t1 / tf_sec
                        idx2_r = t2 / tf_sec
                        x1 = int(left + (max(0.0, n - 1 - idx1_r)) * cw + cw / 2)
                        x2 = int(left + (max(0.0, n - 1 - idx2_r)) * cw + cw / 2)
                        p1 = float(line.get('p1', v_min)); p2 = float(line.get('p2', v_min))
                        if v_max == v_min:
                            y1 = y2 = int((main_top + main_bottom) / 2)
                        else:
                            y1 = int(main_top + (v_max - p1) / (v_max - v_min) * height)
                            y2 = int(main_top + (v_max - p2) / (v_max - v_min) * height)
                        out.append((x1, y1, x2, y2))
                    except Exception:
                        continue
                elif isinstance(line, dict) and 'x1f' in line:
                    try:
                        x1f = float(line.get('x1f', 0.0)); x2f = float(line.get('x2f', 0.0))
                        p1 = float(line.get('p1', v_min)); p2 = float(line.get('p2', v_min))
                        px1 = int(left + max(0.0, min(1.0, x1f)) * width)
                        px2 = int(left + max(0.0, min(1.0, x2f)) * width)
                        if v_max == v_min:
                            py1 = py2 = int((main_top + main_bottom) / 2)
                        else:
                            py1 = int(main_top + (v_max - p1) / (v_max - v_min) * height)
                            py2 = int(main_top + (v_max - p2) / (v_max - v_min) * height)
                        out.append((px1, py1, px2, py2))
                    except Exception:
                        continue
                else:
                    try:
                        x1, y1, x2, y2 = float(line[0]), float(line[1]), float(line[2]), float(line[3])
                        px1 = max(0, min(W - 1, int(round(x1 * W))))
                        py1 = max(0, min(H - 1, int(round(y1 * H))))
                        px2 = max(0, min(W - 1, int(round(x2 * W))))
                        py2 = max(0, min(H - 1, int(round(y2 * H))))
                        out.append((px1, py1, px2, py2))
                    except Exception:
                        continue
        return out

    def _distance_point_to_segment(self, px: int, py: int, x1: int, y1: int, x2: int, y2: int) -> float:
        dx = x2 - x1
        dy = y2 - y1
        if dx == 0 and dy == 0:
            return math.hypot(px - x1, py - y1)
        t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
        t = max(0.0, min(1.0, t))
        projx = x1 + t * dx
        projy = y1 + t * dy
        return math.hypot(px - projx, py - projy)

    def _find_thumb_line_index_at_point(self, pair: str, tf: str, popup: 'ThumbnailPopup', pos: QtCore.QPoint) -> int | None:
        px_lines = self._thumb_line_pixels(pair, tf, popup)
        best_idx = None
        best_dist = 1e9
        for idx, (x1, y1, x2, y2) in enumerate(px_lines):
            dist = self._distance_point_to_segment(pos.x(), pos.y(), x1, y1, x2, y2)
            if dist <= 6.0 and dist < best_dist:
                best_dist = dist
                best_idx = idx
        return best_idx

    def _select_thumb_line_at(self, popup: 'ThumbnailPopup', pos: QtCore.QPoint, mode: str = 'main') -> bool:
        pair, tf = self._thumb_context(mode=mode)
        if not pair or not tf:
            return False
        idx = self._find_thumb_line_index_at_point(pair, tf, popup, pos)
        if idx is None:
            self._thumb_line_selected = None
            return False
        self._thumb_line_selected = (pair, tf, idx)
        return True

    def _add_thumb_line_from_popup(self, popup: 'ThumbnailPopup', start: QtCore.QPoint, end: QtCore.QPoint, mode: str = 'main') -> bool:
        pair, tf = self._thumb_context(mode=mode)
        if not pair or not tf:
            return False
        norm = self._normalize_thumb_line(start, end, popup)
        if norm is None:
            return False
        lines = self._thumb_lines.setdefault(pair, {}).setdefault(tf, [])
        lines.append(norm)
        self._thumb_line_selected = (pair, tf, len(lines) - 1)
        self._save_config()
        return True

    def _delete_selected_thumb_line(self) -> bool:
        sel = self._thumb_line_selected
        if sel is None:
            return False
        pair, tf, idx = sel
        lines = self._thumb_lines.get(pair, {}).get(tf)
        if not lines or not (0 <= idx < len(lines)):
            self._thumb_line_selected = None
            return False
        lines.pop(idx)
        if not lines:
            self._thumb_lines.get(pair, {}).pop(tf, None)
            if not self._thumb_lines.get(pair):
                self._thumb_lines.pop(pair, None)
        self._thumb_line_selected = None
        self._save_config()
        return True

    def _draw_thumb_lines_overlay(self, popup: 'ThumbnailPopup', painter: QtGui.QPainter, preview_line: tuple[int, int, int, int] | None = None, mode: str = 'main') -> None:
        pair, tf = self._thumb_context(mode=mode)
        if not pair or not tf:
            return
        # First draw lines from other timeframes (fainter), then current timeframe lines (with selection highlight)
        other_px = self._thumb_line_pixels_other_timeframes(pair, tf, popup)
        if other_px:
            for (x1, y1, x2, y2) in other_px:
                pen_o = QtGui.QPen(QtGui.QColor(135, 206, 235, 130))  # faint skyblue
                pen_o.setStyle(QtCore.Qt.DashLine)
                pen_o.setWidth(1)
                pen_o.setCapStyle(QtCore.Qt.RoundCap)
                painter.setPen(pen_o)
                painter.drawLine(x1, y1, x2, y2)
        px_lines = self._thumb_line_pixels(pair, tf, popup)
        selected = self._thumb_line_selected
        for idx, (x1, y1, x2, y2) in enumerate(px_lines):
            pen = QtGui.QPen(QtGui.QColor(255, 215, 0, 220) if selected and selected[0] == pair and selected[1] == tf and selected[2] == idx else QtGui.QColor(173, 216, 230, 200))
            pen.setWidth(2 if selected and selected[0] == pair and selected[1] == tf and selected[2] == idx else 1)
            pen.setCapStyle(QtCore.Qt.RoundCap)
            painter.setPen(pen)
            painter.drawLine(x1, y1, x2, y2)
        if preview_line is not None:
            pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 200))
            pen.setStyle(QtCore.Qt.DashLine)
            pen.setWidth(1)
            pen.setCapStyle(QtCore.Qt.RoundCap)
            painter.setPen(pen)
            painter.drawLine(*preview_line)

    def _selected_line_endpoints_pixels(self, popup: 'ThumbnailPopup', mode: str = 'main') -> tuple[tuple[int,int], tuple[int,int]] | None:
        sel = self._thumb_line_selected
        if sel is None:
            return None
        pair, tf = self._thumb_context(mode=mode)
        if not pair or not tf:
            return None
        spair, stf, sidx = sel
        if spair != pair or stf != tf:
            return None
        px_lines = self._thumb_line_pixels(pair, tf, popup)
        if not (0 <= sidx < len(px_lines)):
            return None
        x1, y1, x2, y2 = px_lines[sidx]
        return (x1, y1), (x2, y2)

    def _selected_line_endpoint_at(self, popup: 'ThumbnailPopup', pos: QtCore.QPoint, mode: str = 'main') -> int | None:
        eps = self._selected_line_endpoints_pixels(popup, mode=mode)
        if eps is None:
            return None
        (x1, y1), (x2, y2) = eps
        d1 = math.hypot(pos.x() - x1, pos.y() - y1)
        d2 = math.hypot(pos.x() - x2, pos.y() - y2)
        thr = 8.0
        if d1 <= thr or d2 <= thr:
            return 0 if d1 <= d2 else 1
        return None

    def _update_selected_thumb_line_endpoint(self, popup: 'ThumbnailPopup', end_index: int, pos: QtCore.QPoint, mode: str = 'main', commit: bool = False) -> bool:
        sel = self._thumb_line_selected
        if sel is None:
            return False
        pair, tf = self._thumb_context(mode=mode)
        if not pair or not tf:
            return False
        spair, stf, sidx = sel
        if spair != pair or stf != tf:
            return False
        lines = self._thumb_lines.get(pair, {}).get(tf)
        if not lines or not (0 <= sidx < len(lines)):
            return False
        line = lines[sidx]
        if isinstance(line, dict) and line.get('fmt') == 'time' and ('t1' in line or 't1_off' in line):
            left, right, main_top, main_bottom, v_min, v_max = self._chart_area_for_popup(popup)
            width = max(1, right - left); height = max(1, main_bottom - main_top)
            n = max(1, len(getattr(popup, '_ohlc', []) or []))
            tf_sec = max(1, self._popup_tf_seconds(popup))
            def clamp(v, a, b):
                return max(a, min(b, v))
            if v_max == v_min:
                return False
            # compute idx-from-right at cursor, then seconds
            frac = (clamp(pos.x(), left, right) - left) / float(max(1, width))
            idx = frac * max(1, n - 1)
            idx_r = max(0.0, (n - 1) - idx)
            t_off = float(idx_r * tf_sec)
            if end_index == 0:
                line['t1_off'] = t_off
                line['p1'] = float(v_max - (clamp(pos.y(), main_top, main_bottom) - main_top) / height * (v_max - v_min))
            else:
                line['t2_off'] = t_off
                line['p2'] = float(v_max - (clamp(pos.y(), main_top, main_bottom) - main_top) / height * (v_max - v_min))
            lines[sidx] = line
        elif isinstance(line, dict) and 'x1f' in line:
            left, right, main_top, main_bottom, v_min, v_max = self._chart_area_for_popup(popup)
            width = max(1, right - left); height = max(1, main_bottom - main_top)
            def clamp(v, a, b):
                return max(a, min(b, v))
            if v_max == v_min:
                return False
            xf = clamp((clamp(pos.x(), left, right) - left) / width, 0.0, 1.0)
            pv = v_max - (clamp(pos.y(), main_top, main_bottom) - main_top) / height * (v_max - v_min)
            if end_index == 0:
                line['x1f'] = float(xf); line['p1'] = float(pv)
            else:
                line['x2f'] = float(xf); line['p2'] = float(pv)
            lines[sidx] = line
        else:
            w = max(1, popup.width())
            h = max(1, popup.height())
            nx = max(0.0, min(1.0, pos.x() / w))
            ny = max(0.0, min(1.0, pos.y() / h))
            x1, y1, x2, y2 = line
            if end_index == 0:
                x1, y1 = nx, ny
            else:
                x2, y2 = nx, ny
            lines[sidx] = (x1, y1, x2, y2)
        if commit:
            self._save_config()
        return True

    def _consume_drag_flag(self) -> bool:
        was = bool(self._drag_started)
        self._drag_started = False
        return was

    def _move_popups_by_delta(self, delta: QtCore.QPoint) -> None:
        if delta.isNull():
            return
        for popup in (getattr(self, '_thumb_popup', None), getattr(self, '_dual_popup', None)):
            if popup and popup.isVisible():
                try:
                    popup.move(popup.pos() + delta)
                except Exception:
                    pass

    def _cycle_timeframe(self, kind: str, step: int) -> None:
        try:
            tfs = ["1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d", "3d"]
            if kind == 'dual':
                cur = (self.thumb_tf2 or "4h")
                try:
                    idx = tfs.index(cur)
                except Exception:
                    idx = tfs.index("4h")
                new_tf = tfs[(idx + step) % len(tfs)]
                self.thumb_tf2 = new_tf
                pair_l = getattr(self, '_thumb_pair_current', None)
                if pair_l:
                    bars2 = int(max(10, min(200, int(self.thumb_bars2 or self.thumb_bars))))
                    ohlc2 = self._ohlc_from_local(pair_l, bars2, tf=new_tf)
                    self._thumb_ohlc_top = ohlc2
                    if getattr(self, '_dual_popup', None):
                        self._dual_popup.set_data_and_owner(ohlc2 or [], self, tf_label=new_tf)
                        self._dual_popup.update()
                # kick async refresh from exchange if enabled
                if bool(self.thumb_fetch_from_binance) and pair_l:
                    def _w2(pair_l_, tf_):
                        try:
                            d2 = self._fetch_klines_binance(pair_l_, interval=tf_, limit=int(self.thumb_bars2 or self.thumb_bars))
                            if d2:
                                try:
                                    self.thumb_data_ready.emit(pair_l_, tf_, d2)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                    threading.Thread(target=_w2, args=(pair_l, new_tf), daemon=True).start()
                self._save_config()
            else:
                cur = (self.thumb_tf or "1h")
                try:
                    idx = tfs.index(cur)
                except Exception:
                    idx = tfs.index("1h")
                new_tf = tfs[(idx + step) % len(tfs)]
                self.thumb_tf = new_tf
                pair_l = getattr(self, '_thumb_pair_current', None)
                if pair_l:
                    bars = int(max(10, min(200, int(self.thumb_bars or 50))))
                    ohlc = self._ohlc_from_local(pair_l, bars, tf=new_tf)
                    self._thumb_ohlc_main = ohlc
                    if getattr(self, '_thumb_popup', None):
                        self._thumb_popup.set_data_and_owner(ohlc or [], self, ohlc2=None, tf_label=new_tf)
                        self._thumb_popup.update()
                # async refresh main from exchange if enabled
                if bool(self.thumb_fetch_from_binance) and pair_l:
                    def _w1(pair_l_, tf_):
                        try:
                            d = self._fetch_klines_binance(pair_l_, interval=tf_, limit=int(self.thumb_bars or 50))
                            if d:
                                try:
                                    self.thumb_data_ready.emit(pair_l_, tf_, d)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                    threading.Thread(target=_w1, args=(pair_l, new_tf), daemon=True).start()
                self._save_config()
        except Exception:
            pass

    def _cycle_sub_indicator(self, kind: str, step: int) -> None:
        try:
            opts = ['rsi','macd','kdj']
            if kind == 'dual':
                cur = (self.thumb_dual_sub_indicator or 'rsi').lower()
                try:
                    idx = opts.index(cur)
                except Exception:
                    idx = 0
                self.thumb_dual_sub_indicator = opts[(idx + step) % len(opts)]
                if getattr(self, '_dual_popup', None):
                    self._dual_popup._render_now()
            else:
                cur = (self.thumb_sub_indicator or 'rsi').lower()
                try:
                    idx = opts.index(cur)
                except Exception:
                    idx = 0
                self.thumb_sub_indicator = opts[(idx + step) % len(opts)]
                if getattr(self, '_thumb_popup', None):
                    self._thumb_popup._render_now()
            self._save_config()
        except Exception:
            pass

    # Single-click handler from PriceLabel
    def on_label_click(self, index: int, global_pos: QtCore.QPoint):
        if getattr(self, '_menu_open', False):
            return
        # toggle if same index is visible
        if self._thumb_index_visible == index and (self._thumb_popup.isVisible() or self._dual_popup.isVisible()):
            self.hide_thumbnail()
            self._thumb_index_visible = None
            return
        # otherwise show for the clicked index at cursor pos
        try:
            self.show_thumbnail(index, click_pos=global_pos)
            self._thumb_index_visible = index
        except Exception:
            pass

    def hide_thumbnail(self):
        try:
            self._thumb_popup.hide()
            self._dual_popup.hide()
        except Exception:
            pass

    def _install_drag_filters(self):
        # Allow dragging even when clicking on the toggle button or inner frame
        for w in (self, getattr(self, 'root_frame', None), getattr(self, 'btn_toggle', None), getattr(self, 'prices_widget', None)):
            if w is not None:
                w.installEventFilter(self)

    def eventFilter(self, obj, event):
        et = event.type()
        if et == QtCore.QEvent.MouseButtonPress and isinstance(event, QtGui.QMouseEvent):
            if event.button() == QtCore.Qt.LeftButton:
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        elif et == QtCore.QEvent.MouseMove and isinstance(event, QtGui.QMouseEvent):
            if event.buttons() & QtCore.Qt.LeftButton and self._drag_pos is not None:
                prev = self.pos()
                new_pos = event.globalPosition().toPoint() - self._drag_pos
                self.move(new_pos)
                delta = new_pos - prev
                if not delta.isNull():
                    self._drag_started = True
                    self._move_popups_by_delta(delta)
        elif et == QtCore.QEvent.MouseButtonRelease:
            self._drag_pos = None
            self._drag_started = False
        return super().eventFilter(obj, event)

    def _start_ws(self):
        pairs_slots = [self._slot_to_pair(s) for s in self.slots]
        pairs_watch = [self._slot_to_pair(s) for s in self.alert_watchlist]
        all_pairs = [p for p in set([*(p for p in pairs_slots if p), *(p for p in pairs_watch if p)])]
        # Avoid duplicate connections; manage with a flag to skip noisy warnings
        if self._price_signal_connected:
            try:
                self.ws.price_update.disconnect(self._on_price_update)
            except Exception:
                pass
        self.ws.price_update.connect(self._on_price_update)
        self._price_signal_connected = True
        try:
            if not all_pairs:
                print("[WS] empty subscription from slots/watchlist")
            else:
                print("[WS] preparing subscription for " + ", ".join(all_pairs))
        except Exception:
            pass
        # Split pairs by preference and availability
        spot_pairs: list[str] = []
        futures_pairs: list[str] = []
        try:
            fut_set = self.fetcher.fetch_binance_futures_symbols_set()
        except Exception:
            fut_set = set()
        try:
            spot_set = self.fetcher.fetch_binance_spot_symbols_set()
        except Exception:
            spot_set = set()
        pref = getattr(self, 'prefer_price_source', 'spot')
        for p in all_pairs:
            pl = (p or '').lower()
            if pref == 'futures':
                if pl in fut_set:
                    futures_pairs.append(pl)
                else:
                    spot_pairs.append(pl)
            else:  # prefer spot
                if (not spot_set) or (pl in spot_set):
                    spot_pairs.append(pl)
                else:
                    futures_pairs.append(pl)
        try:
            print("[WS] subscribe SPOT:", ", ".join(spot_pairs) or "<none>")
            print("[WS] subscribe FUTURES:", ", ".join(futures_pairs) or "<none>")
        except Exception:
            pass
        self.ws.connect_pairs(spot_pairs, futures_pairs)

    def _restart_ws(self):
        # Show placeholders while reconnecting
        self._show_placeholders()
        try:
            self.ws.close()
        except Exception:
            pass
        self._start_ws()

    # Context-menu Refresh: manually rebuild the WS connection
    def refresh(self):
        self._restart_ws()

    def _show_placeholders(self):
        for lbl in getattr(self, 'labels', []):
            try:
                lbl.setText("…")
                lbl.setToolTip("Reconnecting…")
            except Exception:
                pass

    def _lock_to_width(self, width: int):
        self.setMinimumWidth(int(width))
        self.setMaximumWidth(int(width))

    def _lock_height(self, height: int):
        self.setMinimumHeight(int(height))
        self.setMaximumHeight(int(height))

    @QtCore.Slot(list)
    def _update_prices(self, data: List[Dict]):
        # Only show prices; put details in tooltip
        by_id = {c.get("id"): c for c in data} if data else {}
        for idx, cid in enumerate(self.slots):
            lbl = self.labels[idx]
            c = by_id.get(cid)
            if not c:
                lbl.setText("…")
                lbl.setToolTip("Double-click to set coin (e.g., btcusdt/btc)")
                continue

            sym = (c.get("symbol") or "?").upper()

            # 当前价
            price = c.get("price", 0.0) or c.get("current_price", 0.0) or 0.0
            price = float(price)

            # CoinGecko 给的 24h 涨跌百分比（近似视为：当前价 vs 昨收）
            pct_24h = c.get("price_change_percentage_24h")
            if pct_24h is None:
                pct_24h = c.get("change", 0.0)
            pct_24h = float(pct_24h or 0.0)

            # 用 current = prev * (1 + pct/100) 反推昨日收盘价
            prev_close = None
            if price > 0 and abs(pct_24h) < 1000:
                try:
                    prev_close = price / (1.0 + pct_24h / 100.0)
                except ZeroDivisionError:
                    prev_close = None

            # 绑定到对应交易对
            pair = self._slot_to_pair(cid)
            if prev_close is not None and pair:
                self.prev_close[pair.lower()] = float(prev_close)

            # 使用统一函数：优先基于昨日收盘价计算百分比
            pct_for_tip = self._percent_from_prev_close(pair, price, pct_24h)

            # 主文本：只显示价格
            lbl.setText(self._format_price(price))
            if not self.thumb_enabled:
                sign = "+" if pct_for_tip >= 0 else ""
                tip = f"{sym}  ${price:,.2f}  ({sign}{pct_for_tip:.2f}%)"
                lbl.setToolTip(tip)
            else:
                lbl.setToolTip("")

    @QtCore.Slot(str, float, float)
    def _on_price_update(self, pair: str, price: float, pct: float):
        pair_l = pair.lower()
        idx = self.pair_index.get(pair_l)
        # Clear expired indicator for this pair if any
        try:
            iv = self._alert_indicator.get(pair_l)
            if iv is not None:
                lvl, exp = iv
                if time.time() >= float(exp):
                    self._alert_indicator.pop(pair_l, None)
                    if idx is not None and idx < len(getattr(self, 'dot_labels', [])):
                        self._set_indicator_level(idx, 0)
        except Exception:
            pass
        if idx is None or idx >= len(self.labels):
            # Not a visible pair; still record and maybe alert
            self.last_ws_price[pair_l] = float(price)
            self._ingest_series(pair, price)
            info = self._maybe_alert(pair, price, pct)
            if isinstance(info, dict) and info.get("period"):
                self._set_indicator_for_pair(pair_l, info["period"])  # store/update for hidden pair
            return

        lbl = self.labels[idx]
        lbl.setText(self._format_price(price))

        # 使用昨日收盘价来计算浮窗里的百分比（fallback 为 WS 自带 pct）
        pct_from_prev = self._percent_from_prev_close(pair, price, pct)

        if not self.thumb_enabled:
            sym = pair[:-4].upper()  # e.g., btcusdt -> BTC
            sign = "+" if pct_from_prev >= 0 else ""
            lbl.setToolTip(f"{sym}  ${price:,.2f}  ({sign}{pct_from_prev:.2f}%)")
        else:
            lbl.setToolTip("")

        self.last_ws_price[pair_l] = float(price)
        self._ingest_series(pair, price)
        info = self._maybe_alert(pair, price, pct)
        if isinstance(info, dict) and info.get("period"):
            self._set_indicator_for_pair(pair_l, info["period"])  # update visible
        
        rsi_map = getattr(self, '_rsi_values_tf', {}).get(pair_l)
        if rsi_map:
            sel = self.alert.pick_rsi_for_style(rsi_map)
            if sel is not None:
                self.alert.apply_rsi_style(lbl, sel)

    # 统一的“昨日收盘价百分比”计算函数
    def _percent_from_prev_close(self, pair: str | None, price: float, fallback_pct: float) -> float:
        """
        优先用 self.prev_close[pair] 来计算 (price / prev - 1) * 100，
        如果没有 prev_close，则回退到 fallback_pct。
        """
        try:
            if not pair:
                return float(fallback_pct)
            p = pair.lower()
            prev = self.prev_close.get(p)
            price = float(price)
            if prev is not None and prev > 0:
                return (price / float(prev) - 1.0) * 100.0
        except Exception:
            pass
        return float(fallback_pct)

    # --- HTTP audit vs WS every 3 minutes ---
    def _start_http_audit(self):
        if self.audit_worker is not None and self.audit_worker.isRunning():
            return
        self.audit_worker = FetchThread(self.fetcher, self.slots)
        try:
            self.audit_worker.data_ready.disconnect(self._on_http_audit_result)
        except Exception:
            pass
        self.audit_worker.data_ready.connect(self._on_http_audit_result)
        self.audit_worker.start()

    @QtCore.Slot(list)
    def _on_http_audit_result(self, data: List[Dict]):
        try:
            by_id = {c.get("id"): c for c in data if c and c.get("id")}
            needs_restart = False
            for cid in self.slots:
                pair = self._slot_to_pair(cid)
                if not pair:
                    continue
                ws_price = self.last_ws_price.get(pair.lower())
                c = by_id.get(cid)
                http_price = None
                if c is not None:
                    http_price = c.get("current_price") or c.get("price")
                if ws_price is None or not http_price:
                    continue
                http_price = float(http_price)
                if http_price <= 0:
                    continue
                diff = abs(ws_price - http_price) / http_price
                if diff >= self._audit_threshold:
                    needs_restart = True
                    break
            if needs_restart:
                self._restart_ws()
        except Exception:
            try:
                print("[AUDIT] error")
            except Exception:
                pass

    # --- Alerts ---
    def _watch_pairs_set(self) -> set[str]:
        return self.alert.watch_pairs_set()

    def _maybe_alert(self, pair: str, price: float, pct: float):
        return self.alert.maybe_alert(pair, price, pct)

    def _set_indicator_level(self, index: int, level: int):
        try:
            if index is None or index < 0:
                return
            if index >= len(getattr(self, 'dot_labels', [])):
                return
            lbl = self.dot_labels[index]
            if level <= 0:
                lbl.setText("")
                lbl.setVisible(False)
            else:
                dots = "\u2022" * int(min(3, max(1, int(level))))
                lbl.setText(dots)
                lbl.setVisible(True)
        except Exception:
            pass

    def _period_to_level(self, label: str) -> int:
        try:
            periods = [p for p in (self.alert_periods or []) if self._period_seconds(p)]
            if not periods:
                return 0
            uniq = []
            seen = set()
            for p in periods:
                if p not in seen:
                    uniq.append(p)
                    seen.add(p)
            uniq.sort(key=lambda x: self._period_seconds(x) or 0)
            n = len(uniq)
            if label not in uniq:
                return 0
            i = uniq.index(label)
            level = 1 + int(i * 3 / n)
            return int(max(1, min(3, level)))
        except Exception:
            return 0

    def _set_indicator_for_pair(self, pair_l: str, period_label: str):
        try:
            lvl = self._period_to_level(period_label)
            if lvl <= 0:
                return
            self._alert_indicator[pair_l] = (lvl, time.time() + float(self._alert_cooldown_sec))
            idx = self.pair_index.get(pair_l)
            if idx is not None:
                self._set_indicator_level(idx, lvl)
        except Exception:
            pass

    def _ingest_series(self, pair: str, price: float):
        try:
            p = pair.lower()
            prev_price = self._price_series[p][-1] if self._price_series[p] else None
            self._price_series[p].append(float(price))
            vol = float(self.ws.get_quote_volume(p)) if self.ws else 0.0
            self._vol_series[p].append(vol)
            now = time.time()
            self._price_ts[p].append((now, float(price)))
            
            # Keep legacy tick-based buffers
            if prev_price is not None:
                change = float(price) - float(prev_price)
                if change > 0:
                    self._rsi_gains[p].append(change)
                    self._rsi_losses[p].append(0.0)
                else:
                    self._rsi_gains[p].append(0.0)
                    self._rsi_losses[p].append(abs(change))
            
            # Multi-timeframe bar closes
            for tf in list(self.rsi_timeframes):
                sec = self._tf_seconds.get(tf)
                if not sec:
                    continue
                self._rsi_last_bar_close_tf[p][tf] = float(price)
                last_ts = self._rsi_last_bar_ts_tf[p].get(tf, 0.0)
                if now - last_ts >= float(sec):
                    self._rsi_last_bar_ts_tf[p][tf] = now
                    close = self._rsi_last_bar_close_tf[p].get(tf)
                    if close is not None:
                        self._rsi_closes_tf[p][tf].append(close)
                        if len(self._rsi_closes_tf[p][tf]) >= self.rsi_period + 1:
                            self._calculate_rsi_from_closes_tf(p, tf)
        except Exception:
            try:
                print("[SERIES] ingest error")
            except Exception:
                pass
            
    def _calculate_rsi(self, pair: str):
        """Calculate RSI-6 from tick-based buffers (fallback)."""
        try:
            p = pair.lower()
            if len(self._rsi_gains[p]) < self.rsi_period or len(self._rsi_losses[p]) < self.rsi_period:
                return
                
            avg_gain = sum(self._rsi_gains[p]) / self.rsi_period
            avg_loss = sum(self._rsi_losses[p]) / self.rsi_period
            
            if avg_loss == 0:
                rsi = 100
            else:
                rs = avg_gain / avg_loss
                rsi = 100 - (100 / (1 + rs))
                
            self._rsi_values_tf[p]["tick"] = rsi
            
            # Update label style based on RSI
            idx = self.pair_index.get(p)
            if idx is not None and idx < len(self.labels):
                sel = self.alert.pick_rsi_for_style(self._rsi_values_tf.get(p, {})) or rsi
                self.alert.apply_rsi_style(self.labels[idx], sel)
        except Exception:
            pass

    def _calculate_rsi_from_closes_tf(self, pair: str, tf: str):
        try:
            p = pair.lower()
            closes = list(self._rsi_closes_tf[p][tf])
            if len(closes) < self.rsi_period + 1:
                return
            gains, losses = [], []
            for i in range(1, len(closes)):
                ch = closes[i] - closes[i - 1]
                gains.append(max(ch, 0.0))
                losses.append(max(-ch, 0.0))
            gains = gains[-self.rsi_period:]
            losses = losses[-self.rsi_period:]
            if len(gains) < self.rsi_period or len(losses) < self.rsi_period:
                return
            avg_gain = sum(gains) / float(self.rsi_period)
            avg_loss = sum(losses) / float(self.rsi_period)
            rsi = 100.0 if avg_loss == 0 else (100.0 - 100.0 / (1.0 + (avg_gain / avg_loss)))
            self._rsi_values_tf[p][tf] = rsi
            idx = self.pair_index.get(p)
            if idx is not None and idx < len(self.labels):
                sel = self.alert.pick_rsi_for_style(self._rsi_values_tf.get(p, {})) or rsi
                self.alert.apply_rsi_style(self.labels[idx], sel)
        except Exception:
            pass
            
    def _update_rsi_values(self):
        """Update RSI values for all pairs"""
        for pair in list(self._price_series.keys()):
            p = pair.lower()
            done_any = False
            for tf in list(self.rsi_timeframes):
                if len(self._rsi_closes_tf[p][tf]) >= self.rsi_period + 1:
                    self._calculate_rsi_from_closes_tf(p, tf)
                    done_any = True
            if not done_any:
                self._calculate_rsi(p)

    def _volatility_stats(self, pair_l: str):
        return self.alert.volatility_stats(pair_l)

    def _volume_zscore(self, pair_l: str):
        return self.alert.volume_zscore(pair_l)

    def _period_seconds(self, label: str) -> int | None:
        return self.alert.period_seconds(label)

    def _percent_change_over(self, pair_l: str, seconds: int) -> float | None:
        return self.alert.percent_change_over(pair_l, seconds)

    def _notify(self, title: str, message: str):
        return self.alert.notify(title, message)

    # --- Alerts Settings Dialog ---
    def _open_alerts_settings(self):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Alerts Settings")
        dlg.setModal(True)
        layout = QtWidgets.QVBoxLayout(dlg)
        chk = QtWidgets.QCheckBox("Enable alerts")
        chk.setChecked(self.alerts_enabled)
        layout.addWidget(chk)

        # Method
        rowm = QtWidgets.QHBoxLayout()
        rowm.addWidget(QtWidgets.QLabel("Method"))
        cmb_method = QtWidgets.QComboBox()
        cmb_method.addItems([
            "Percent Change",
            "Volatility (returns)",
            "Volume Breakout",
            "Bullish Alignment",
        ])
        method_map = {0: "pct", 1: "vol", 2: "volume", 3: "bull"}
        inv_map = {v: k for k, v in method_map.items()}
        cmb_method.setCurrentIndex(inv_map.get(self.alert_method, 0))
        rowm.addWidget(cmb_method)
        layout.addLayout(rowm)

        # Thresholds rows
        rowp = QtWidgets.QHBoxLayout()
        rowp.addWidget(QtWidgets.QLabel("Pct Threshold (%)"))
        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(0.1, 100.0)
        spin.setSingleStep(0.1)
        spin.setValue(float(self.alert_threshold_percent))
        rowp.addWidget(spin)
        cont_rowp = QtWidgets.QWidget()
        cont_rowp.setLayout(rowp)
        layout.addWidget(cont_rowp)

        # Periods multi-select (dot style)
        periods_title = QtWidgets.QLabel("Periods (multi-select):")
        periods_row = QtWidgets.QHBoxLayout()
        period_labels = ["1m", "5m", "15m", "1h", "4h", "24h"]
        cb_periods = []
        for lab in period_labels:
            cb = QtWidgets.QCheckBox(lab)
            cb.setChecked(lab in (self.alert_periods or []))
            # Make checkbox appear circular
            cb.setStyleSheet("QCheckBox::indicator { width: 12px; height: 12px; border-radius: 6px; }")
            periods_row.addWidget(cb)
            cb_periods.append(cb)
        periods_row.addStretch(1)
        cont_periods = QtWidgets.QWidget()
        cont_periods.setLayout(periods_row)
        layout.addWidget(periods_title)
        layout.addWidget(cont_periods)

        rowv = QtWidgets.QHBoxLayout()
        rowv.addWidget(QtWidgets.QLabel("Vol Window"))
        sp_vw = QtWidgets.QSpinBox()
        sp_vw.setRange(5, 2000)
        sp_vw.setValue(int(self.vol_window_samples))
        rowv.addWidget(sp_vw)
        rowv.addWidget(QtWidgets.QLabel("Vol Sigma"))
        sp_vs = QtWidgets.QDoubleSpinBox()
        sp_vs.setRange(0.5, 10.0)
        sp_vs.setSingleStep(0.1)
        sp_vs.setValue(float(self.vol_threshold_sigma))
        rowv.addWidget(sp_vs)
        cont_rowv = QtWidgets.QWidget()
        cont_rowv.setLayout(rowv)
        layout.addWidget(cont_rowv)

        rowq = QtWidgets.QHBoxLayout()
        rowq.addWidget(QtWidgets.QLabel("Volume Window"))
        sp_qw = QtWidgets.QSpinBox()
        sp_qw.setRange(5, 2000)
        sp_qw.setValue(int(self.volume_window_samples))
        rowq.addWidget(sp_qw)
        rowq.addWidget(QtWidgets.QLabel("Volume Sigma"))
        sp_qs = QtWidgets.QDoubleSpinBox()
        sp_qs.setRange(0.5, 10.0)
        sp_qs.setSingleStep(0.1)
        sp_qs.setValue(float(self.volume_threshold_sigma))
        rowq.addWidget(sp_qs)
        cont_rowq = QtWidgets.QWidget()
        cont_rowq.setLayout(rowq)
        layout.addWidget(cont_rowq)

        # Bullish alignment params
        rowb = QtWidgets.QHBoxLayout()
        rowb.addWidget(QtWidgets.QLabel("Bull min change (%)"))
        sp_bull = QtWidgets.QDoubleSpinBox()
        sp_bull.setRange(0.1, 100.0)
        sp_bull.setSingleStep(0.1)
        sp_bull.setValue(float(self.bull_min_change_percent))
        rowb.addWidget(sp_bull)
        chk_mono = QtWidgets.QCheckBox("Monotonic ↑")
        chk_mono.setChecked(bool(self.bull_require_monotonic))
        rowb.addWidget(chk_mono)
        cont_rowb = QtWidgets.QWidget()
        cont_rowb.setLayout(rowb)
        layout.addWidget(cont_rowb)

        layout.addWidget(QtWidgets.QLabel("Watchlist (comma-separated ids or pairs):"))
        le = QtWidgets.QLineEdit()
        le.setPlaceholderText("btc, eth, sol or btcusdt, ethusdt…")
        le.setText(", ".join(self.alert_watchlist))
        layout.addWidget(le)

        # Top-coins helper
        helper = QtWidgets.QHBoxLayout()
        helper.addWidget(QtWidgets.QLabel("Top Coins:"))
        cbn = QtWidgets.QComboBox()
        for n in (10, 20, 30, 40, 50, 100):
            cbn.addItem(str(n))
        helper.addWidget(cbn)
        btn_copy_ids = QtWidgets.QPushButton("Copy IDs")
        btn_fill = QtWidgets.QPushButton("Fill Watchlist")
        helper.addWidget(btn_copy_ids)
        helper.addWidget(btn_fill)
        layout.addLayout(helper)

        def do_fetch_top():
            try:
                n = int(cbn.currentText())
                ids = self._fetch_top_coin_ids(n)
                return ids
            except Exception:
                return []

        def on_copy_ids():
            ids = do_fetch_top()
            if ids:
                cb = QtWidgets.QApplication.clipboard()
                cb.setText(", ".join(ids))

        def on_fill():
            ids = do_fetch_top()
            if ids:
                le.setText(", ".join(ids))

        btn_copy_ids.clicked.connect(on_copy_ids)
        btn_fill.clicked.connect(on_fill)

        def apply_method_visibility():
            code = method_map.get(cmb_method.currentIndex(), "pct")
            cont_rowp.setVisible(code == "pct")
            periods_title.setVisible(code in ("pct", "bull"))
            cont_periods.setVisible(code in ("pct", "bull"))
            cont_rowv.setVisible(code == "vol")
            cont_rowq.setVisible(code == "volume")
            cont_rowb.setVisible(code == "bull")

        cmb_method.currentIndexChanged.connect(lambda _: apply_method_visibility())
        apply_method_visibility()

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        layout.addWidget(btns)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            self.alerts_enabled = chk.isChecked()
            self.alert_method = method_map.get(cmb_method.currentIndex(), "pct")
            self.alert_threshold_percent = float(spin.value())
            self.vol_window_samples = int(sp_vw.value())
            self.vol_threshold_sigma = float(sp_vs.value())
            self.volume_window_samples = int(sp_qw.value())
            self.volume_threshold_sigma = float(sp_qs.value())
            self.bull_min_change_percent = float(sp_bull.value())
            self.bull_require_monotonic = bool(chk_mono.isChecked())
            # reconfigure series buffers maxlen
            self._series_maxlen = max(self.vol_window_samples, self.volume_window_samples, 120)
            self._price_series = defaultdict(lambda: deque(maxlen=self._series_maxlen), self._price_series)
            self._vol_series = defaultdict(lambda: deque(maxlen=self._series_maxlen), self._vol_series)
            # collect selected periods
            sel = [cb.text() for cb in cb_periods if cb.isChecked()]
            if not sel:
                sel = ["24h"]
            self.alert_periods = sel
            # Parse watchlist
            raw = le.text()
            items = [t.strip().lower() for t in raw.replace("\n", ",").split(",")]
            self.alert_watchlist = [t for t in items if t]
            self._save_config()
            # Rebuild WS to include updated watchlist (does not change UI slots)
            self._restart_ws()

    # --- Announcer (Windows / edge-tts) ---
    def _open_announcer_settings(self):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Announcer Settings")
        dlg.setModal(True)
        layout = QtWidgets.QVBoxLayout(dlg)

        chk = QtWidgets.QCheckBox("Enable timed price broadcast")
        chk.setChecked(bool(self.tts_enabled))
        layout.addWidget(chk)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Interval (minutes)"))
        sp = QtWidgets.QSpinBox()
        sp.setRange(1, 120)
        sp.setValue(int(max(1, int(self.tts_interval_min or 1))))
        row.addWidget(sp)
        cont = QtWidgets.QWidget()
        cont.setLayout(row)
        layout.addWidget(cont)

        # 币种勾选区：仅勾选的才播报（空表示全部）
        group = QtWidgets.QGroupBox("Coins to announce (checked only)")
        v = QtWidgets.QVBoxLayout(group)
        cb_items: list[tuple[str, QtWidgets.QCheckBox]] = []
        sel_list = self.tts_include_slots
        all_mode = (sel_list is None)
        include_set = set(sel_list or [])
        for cid in self.slots:
            pair = self._slot_to_pair(cid) or cid
            label = (pair[:-4].upper() if pair.endswith("usdt") else pair.upper())
            cb = QtWidgets.QCheckBox(label)
            checked = (all_mode) or (cid in include_set)
            cb.setChecked(checked)
            v.addWidget(cb)
            cb_items.append((cid, cb))
        layout.addWidget(group)

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        layout.addWidget(btns)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            self.tts_enabled = bool(chk.isChecked())
            self.tts_interval_min = int(max(1, int(sp.value())))
            sel = [cid for (cid, cb) in cb_items if cb.isChecked()]
            if len(sel) == len(cb_items):
                self.tts_include_slots = None  # 全选 -> 视为全部
            else:
                self.tts_include_slots = sel  # 可为空 -> 表示不播报任何
            self._save_config()
            self._restart_tts_timer()

    def _restart_tts_timer(self):
        try:
            self._tts_timer.stop()
        except Exception:
            pass
        if not self.tts_enabled:
            return
        # Fail fast: non-Windows requires edge-tts available (delegated to AlertManager)
        if platform.system() != "Windows":
            try:
                ok = bool(self.alert.has_edge_tts())
            except Exception:
                ok = False
            if not ok:
                QtWidgets.QMessageBox.critical(self, "Announcer", "edge-tts 未安装，已禁用。请先 pip install edge-tts。")
                self.tts_enabled = False
                return
        interval_ms = int(max(1, int(self.tts_interval_min)) * 60_000)
        self._tts_timer.start(interval_ms)

    def _speak_prices_if_ready(self):
        if not self.tts_enabled:
            return
        # Build message for current visible slots with available prices
        parts: list[str] = []
        for cid in self.slots:
            # 若设置了限定列表，则只播报勾选的
            if (self.tts_include_slots is not None) and (cid not in self.tts_include_slots):
                continue
            pair = self._slot_to_pair(cid)
            if not pair:
                continue
            p = self.last_ws_price.get(pair.lower())
            if p is None:
                continue
            sym = pair[:-4].upper()
            parts.append(f"{sym} 当前为 {self._format_price(p)} 。")
        if not parts:
            return
        if platform.system() == "Windows":
            # Windows 使用本地 SAPI，逐条播报并间隔 2s（放到线程中避免阻塞 UI）
            def _win_seq():
                try:
                    for i, seg in enumerate(parts):
                        self._speak_windows(seg)
                        if i + 1 < len(parts):
                            time.sleep(2.0)
                except Exception:
                    pass
            threading.Thread(target=_win_seq, daemon=True).start()
        else:
            # 非 Windows：edge-tts 顺序播报并间隔 2s
            try:
                self.alert.speak_edge_sequence(parts, pause_s=1.0)
            except Exception:
                pass

    def _speak_windows(self, text: str):
        # Use .NET SpeechSynthesizer via PowerShell
        # 先将文本中的数字与小数点转为中文读法
        try:
            text = self._to_cn_digits(text)
        except Exception:
            pass
        # 播报前去掉逗号
        text = text.replace(",", "")
        esc = text.replace("'", "''")
        ps = (
            "Add-Type -AssemblyName System.Speech; "
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$s.Rate=-1; $s.Volume=100; "
            f"$s.Speak('{esc}');"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)

    # edge-tts 实现在 alert.AlertManager 中

    def _format_price(self, price: float) -> str:
        # 基于价格大小，控制小数位，让相对精度大致在 0.1% 左右
        if price >= 1000:
            return f"{price:,.0f}"
        elif price >= 100:
            return f"{price:,.1f}"
        elif price >= 10:
            return f"{price:,.2f}"
        elif price >= 1:
            return f"{price:,.3f}"
        else:
            return f"{price:.4f}"

    def _to_cn_digits(self, text: str) -> str:
        """将文本中的阿拉伯数字与小数点逐字符转换为中文读法。
        规则：0-9 -> 零一二三四五六七八九，其中 1 -> 幺，'.' -> 点。
        仅替换数字与点，其他字符保持不变。
        """
        mapping = {
            '0': '零', '1': '幺', '2': '二', '3': '三', '4': '四',
            '5': '五', '6': '六', '7': '七', '8': '八', '9': '九', '.': '点'
        }
        if not text:
            return text
        return "".join(mapping.get(ch, ch) for ch in text)

    # --- Editing + Persistence ---
    def _open_ui_settings(self):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("UI Settings")
        dlg.setModal(True)
        layout = QtWidgets.QVBoxLayout(dlg)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        chk_thumb = QtWidgets.QCheckBox("Show thumbnail chart on hover")
        chk_thumb.setChecked(bool(self.thumb_enabled))
        layout.addWidget(chk_thumb)

        chk_fetch = QtWidgets.QCheckBox("Fetch from Binance (recommended)")
        chk_fetch.setChecked(bool(self.thumb_fetch_from_binance))
        layout.addWidget(chk_fetch)

        # Compact: timeframe + bars + style + scale in one row
        tfs = ["1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d", "3d"]
        row_main = QtWidgets.QHBoxLayout(); row_main.setSpacing(6)
        lbl_tf = QtWidgets.QLabel("TF"); lbl_tf.setFixedWidth(18)
        row_main.addWidget(lbl_tf)
        cmb_tf = QtWidgets.QComboBox(); cmb_tf.addItems(tfs)
        try:
            cmb_tf.setCurrentIndex(max(0, tfs.index((self.thumb_tf or "1h"))))
        except Exception:
            cmb_tf.setCurrentIndex(tfs.index("1h"))
        row_main.addWidget(cmb_tf)
        row_main.addSpacing(8)
        row_main.addWidget(QtWidgets.QLabel("Bars"))
        sp_bars = QtWidgets.QSpinBox(); sp_bars.setRange(10, 200)
        sp_bars.setValue(int(max(10, min(200, int(self.thumb_bars or 50)))))
        sp_bars.setFixedWidth(72)
        row_main.addWidget(sp_bars)

        # Overlay color combo builder (used by both main and dual sections)
        def _build_color_combo(selected: str) -> QtWidgets.QComboBox:
            colors = [
                ("gold", QtGui.QColor(255,215,0)),
                ("skyblue", QtGui.QColor(135,206,235)),
                ("orange", QtGui.QColor(255,165,0)),
                ("lime", QtGui.QColor(0,255,0)),
                ("white", QtGui.QColor(255,255,255)),
                ("yellow", QtGui.QColor(255,255,0)),
                ("red", QtGui.QColor(255,0,0)),
                ("green", QtGui.QColor(0,200,0)),
                ("cyan", QtGui.QColor(0,255,255)),
                ("magenta", QtGui.QColor(255,0,255)),
            ]
            cb = QtWidgets.QComboBox()
            for name, col in colors:
                pix = QtGui.QPixmap(36, 12); pix.fill(col)
                cb.addItem(QtGui.QIcon(pix), name, userData=name)
            try:
                idx = max(0, [cb.itemData(i) for i in range(cb.count())].index(selected))
            except Exception:
                idx = 0
            cb.setCurrentIndex(idx)
            return cb

        # Section: Dual-chart mode
        grp_dual = QtWidgets.QGroupBox("Dual Chart")
        grp_dual.setCheckable(True)
        grp_dual.setChecked(bool(self.thumb_dual_enabled))
        lay_dual = QtWidgets.QVBoxLayout(grp_dual)
        lay_dual.setContentsMargins(8,6,8,6)
        lay_dual.setSpacing(6)
        row_dual_top = QtWidgets.QHBoxLayout()
        row_dual_top.setSpacing(6)
        row_dual_top.addWidget(QtWidgets.QLabel("Timeframe"))
        cmb_tf2 = QtWidgets.QComboBox(); cmb_tf2.addItems(tfs)
        try:
            cmb_tf2.setCurrentIndex(max(0, tfs.index((self.thumb_tf2 or "4h"))))
        except Exception:
            cmb_tf2.setCurrentIndex(tfs.index("4h"))
        row_dual_top.addWidget(cmb_tf2)
        # dual bars count
        row_dual_top.addWidget(QtWidgets.QLabel("Bars"))
        sp_bars2 = QtWidgets.QSpinBox(); sp_bars2.setRange(10, 200)
        sp_bars2.setValue(int(max(10, min(200, int(self.thumb_bars2 if hasattr(self, 'thumb_bars2') else (self.thumb_bars or 50))))))
        sp_bars2.setFixedWidth(72)
        row_dual_top.addWidget(sp_bars2)
        row_dual_top.addWidget(QtWidgets.QLabel("Scale"))
        cmb_dual_scale = QtWidgets.QComboBox()
        scale_opts = [75, 100, 125, 150, 200]
        for s in scale_opts:
            cmb_dual_scale.addItem(f"{s}%", s)
        try:
            idx_dscale = scale_opts.index(int(self.thumb_dual_scale_percent or self.thumb_scale_percent or 100))
        except Exception:
            idx_dscale = 1
        cmb_dual_scale.setCurrentIndex(idx_dscale)
        row_dual_top.addWidget(cmb_dual_scale)
        cont_dual_top = QtWidgets.QWidget(); cont_dual_top.setLayout(row_dual_top)
        lay_dual.addWidget(cont_dual_top)

        # Top overlay settings (dual)
        grp_overlay2 = QtWidgets.QGroupBox("Dual Overlay")
        lay_ov2 = QtWidgets.QHBoxLayout(grp_overlay2)
        lay_ov2.setContentsMargins(6,4,6,4)
        lay_ov2.setSpacing(6)
        rb2_none = QtWidgets.QRadioButton("None")
        rb2_ma = QtWidgets.QRadioButton("MA")
        rb2_ema = QtWidgets.QRadioButton("EMA")
        t2 = (self.thumb_dual_overlay_type or "ma").lower()
        if t2 == "ema": rb2_ema.setChecked(True)
        elif t2 == "none": rb2_none.setChecked(True)
        else: rb2_ma.setChecked(True)
        lay_ov2.addWidget(rb2_none); lay_ov2.addWidget(rb2_ma); lay_ov2.addWidget(rb2_ema)
        lay_ov2.addWidget(QtWidgets.QLabel("P1"))
        sp2_p1 = QtWidgets.QSpinBox(); sp2_p1.setRange(1, 100); sp2_p1.setValue(int(self.thumb_dual_overlay_p1 or 5))
        lay_ov2.addWidget(sp2_p1)
        lay_ov2.addWidget(QtWidgets.QLabel("P2"))
        sp2_p2 = QtWidgets.QSpinBox(); sp2_p2.setRange(1, 200); sp2_p2.setValue(int(self.thumb_dual_overlay_p2 or 20))
        lay_ov2.addWidget(sp2_p2)
        lay_ov2.addWidget(QtWidgets.QLabel("Color1"))
        cb2_col1 = _build_color_combo(self.thumb_dual_overlay_color1)
        lay_ov2.addWidget(cb2_col1)
        lay_ov2.addWidget(QtWidgets.QLabel("Color2"))
        cb2_col2 = _build_color_combo(self.thumb_dual_overlay_color2)
        lay_ov2.addWidget(cb2_col2)
        lay_dual.addWidget(grp_overlay2)

        # Dual sub-indicator
        row_sub2 = QtWidgets.QHBoxLayout()
        row_sub2.addWidget(QtWidgets.QLabel("Dual sub-indicator"))
        cmb_sub2 = QtWidgets.QComboBox(); cmb_sub2.addItems(["none", "rsi", "macd", "kdj"])
        cur2 = (self.thumb_dual_sub_indicator or "none").lower()
        idx2 = max(0, ["none","rsi","macd","kdj"].index(cur2) if cur2 in ["none","rsi","macd","kdj"] else 0)
        cmb_sub2.setCurrentIndex(idx2)
        row_sub2.addWidget(cmb_sub2)
        cont_sub2 = QtWidgets.QWidget(); cont_sub2.setLayout(row_sub2)
        lay_dual.addWidget(cont_sub2)
        layout.addWidget(grp_dual)

        # Main overlay settings including colors
        grp_overlay = QtWidgets.QGroupBox("Overlay (choose one)")
        lay_ov = QtWidgets.QHBoxLayout(grp_overlay)
        lay_ov.setContentsMargins(6,4,6,4)
        lay_ov.setSpacing(6)
        rb_none = QtWidgets.QRadioButton("None")
        rb_ma = QtWidgets.QRadioButton("MA")
        rb_ema = QtWidgets.QRadioButton("EMA")
        t = (self.thumb_overlay_type or "ma").lower()
        if t == "ema": rb_ema.setChecked(True)
        elif t == "none": rb_none.setChecked(True)
        else: rb_ma.setChecked(True)
        lay_ov.addWidget(rb_none); lay_ov.addWidget(rb_ma); lay_ov.addWidget(rb_ema)
        lay_ov.addWidget(QtWidgets.QLabel("P1"))
        sp_p1 = QtWidgets.QSpinBox(); sp_p1.setRange(1, 100); sp_p1.setValue(int(self.thumb_overlay_p1 or 5))
        lay_ov.addWidget(sp_p1)
        lay_ov.addWidget(QtWidgets.QLabel("P2"))
        sp_p2 = QtWidgets.QSpinBox(); sp_p2.setRange(1, 200); sp_p2.setValue(int(self.thumb_overlay_p2 or 20))
        lay_ov.addWidget(sp_p2)
        lay_ov.addWidget(QtWidgets.QLabel("Color1"))
        cb_col1 = _build_color_combo(self.thumb_overlay_color1)
        lay_ov.addWidget(cb_col1)
        lay_ov.addWidget(QtWidgets.QLabel("Color2"))
        cb_col2 = _build_color_combo(self.thumb_overlay_color2)
        lay_ov.addWidget(cb_col2)
        layout.addWidget(grp_overlay)

        # Style + Scale (continue same compact row)
        row_main.addSpacing(10)
        row_main.addWidget(QtWidgets.QLabel("Style"))
        cmb_style = QtWidgets.QComboBox(); styles = ["candle", "line"]
        cmb_style.addItems(["Candle", "Line"])
        cur_style = (self.thumb_chart_style or "candle").lower()
        cmb_style.setCurrentIndex(0 if cur_style == "candle" else 1)
        row_main.addWidget(cmb_style)
        row_main.addSpacing(8)
        row_main.addWidget(QtWidgets.QLabel("Scale"))
        cmb_scale = QtWidgets.QComboBox(); scale_opts = [75, 100, 125, 150, 200]
        for s in scale_opts:
            cmb_scale.addItem(f"{s}%", s)
        try:
            idx_scale = scale_opts.index(int(self.thumb_scale_percent or 100))
        except Exception:
            idx_scale = 1
        cmb_scale.setCurrentIndex(idx_scale)
        row_main.addWidget(cmb_scale)
        cont_row_main = QtWidgets.QWidget(); cont_row_main.setLayout(row_main)
        layout.addWidget(cont_row_main)

        # Top chart overlay settings
        grp_overlay2 = QtWidgets.QGroupBox("Top overlay")
        lay_ov2 = QtWidgets.QHBoxLayout(grp_overlay2)
        rb2_none = QtWidgets.QRadioButton("None")
        rb2_ma = QtWidgets.QRadioButton("MA")
        rb2_ema = QtWidgets.QRadioButton("EMA")
        t2 = (self.thumb_dual_overlay_type or "ma").lower()
        if t2 == "ema": rb2_ema.setChecked(True)
        elif t2 == "none": rb2_none.setChecked(True)
        else: rb2_ma.setChecked(True)
        lay_ov2.addWidget(rb2_none); lay_ov2.addWidget(rb2_ma); lay_ov2.addWidget(rb2_ema)
        lay_ov2.addWidget(QtWidgets.QLabel("P1"))
        sp2_p1 = QtWidgets.QSpinBox(); sp2_p1.setRange(1, 100); sp2_p1.setValue(int(self.thumb_dual_overlay_p1 or 5))
        lay_ov2.addWidget(sp2_p1)
        lay_ov2.addWidget(QtWidgets.QLabel("P2"))
        sp2_p2 = QtWidgets.QSpinBox(); sp2_p2.setRange(1, 200); sp2_p2.setValue(int(self.thumb_dual_overlay_p2 or 20))
        lay_ov2.addWidget(sp2_p2)
        lay_ov2.addWidget(QtWidgets.QLabel("Color1"))
        cb2_col1 = _build_color_combo(self.thumb_dual_overlay_color1)
        lay_ov2.addWidget(cb2_col1)
        lay_ov2.addWidget(QtWidgets.QLabel("Color2"))
        cb2_col2 = _build_color_combo(self.thumb_dual_overlay_color2)
        lay_ov2.addWidget(cb2_col2)
        layout.addWidget(grp_overlay2)

        # Sub indicator choice (main)
        row_sub = QtWidgets.QHBoxLayout()
        row_sub.addWidget(QtWidgets.QLabel("Sub-indicator"))
        cmb_sub = QtWidgets.QComboBox()
        cmb_sub.addItems(["none", "rsi", "macd", "kdj"])
        cur = (self.thumb_sub_indicator or "none").lower()
        idx = max(0, ["none","rsi","macd","kdj"].index(cur) if cur in ["none","rsi","macd","kdj"] else 0)
        cmb_sub.setCurrentIndex(idx)
        row_sub.addWidget(cmb_sub)
        cont_sub = QtWidgets.QWidget(); cont_sub.setLayout(row_sub)
        layout.addWidget(cont_sub)

        # Toggle visibility of dual params when enable/disable
        def _toggle_dual(checked: bool):
            for w in (cont_dual_top, grp_overlay2, cont_sub2):
                w.setVisible(bool(checked))
        grp_dual.toggled.connect(_toggle_dual)
        _toggle_dual(bool(self.thumb_dual_enabled))

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        layout.addWidget(btns)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            self.thumb_enabled = bool(chk_thumb.isChecked())
            self.thumb_fetch_from_binance = bool(chk_fetch.isChecked())
            self.thumb_tf = tfs[int(cmb_tf.currentIndex())]
            self.thumb_bars = int(sp_bars.value())
            self.thumb_dual_enabled = bool(grp_dual.isChecked())
            self.thumb_tf2 = tfs[int(cmb_tf2.currentIndex())]
            try:
                self.thumb_bars2 = int(sp_bars2.value())
            except Exception:
                self.thumb_bars2 = int(self.thumb_bars)
            self.thumb_overlay_type = "none" if rb_none.isChecked() else ("ema" if rb_ema.isChecked() else "ma")
            self.thumb_overlay_p1 = int(sp_p1.value())
            self.thumb_overlay_p2 = int(sp_p2.value())
            self.thumb_sub_indicator = ["none","rsi","macd","kdj"][int(cmb_sub.currentIndex())]
            self.thumb_chart_style = styles[int(cmb_style.currentIndex())]
            self.thumb_scale_percent = int(cmb_scale.currentData())
            self.thumb_overlay_color1 = str(cb_col1.currentData())
            self.thumb_overlay_color2 = str(cb_col2.currentData())
            self.thumb_dual_overlay_type = "none" if rb2_none.isChecked() else ("ema" if rb2_ema.isChecked() else "ma")
            self.thumb_dual_overlay_p1 = int(sp2_p1.value())
            self.thumb_dual_overlay_p2 = int(sp2_p2.value())
            self.thumb_dual_overlay_color1 = str(cb2_col1.currentData())
            self.thumb_dual_overlay_color2 = str(cb2_col2.currentData())
            self.thumb_dual_sub_indicator = ["none","rsi","macd","kdj"][int(cmb_sub2.currentIndex())]
            self.thumb_dual_scale_percent = int(cmb_dual_scale.currentData())
            self._save_config()

    def edit_slot(self, index: int):
        current_id = self.slots[index] if 0 <= index < len(self.slots) else ""
        text, ok = QtWidgets.QInputDialog.getText(
            self,
            "Edit Coin",
            "Enter coin symbol or pair (e.g., btc / btcusdt):",
            text=current_id,
        )
        if not ok or not text:
            return
        new_id = self._resolve_coin_id(text)
        if not new_id:
            QtWidgets.QMessageBox.warning(self, "Unknown", f"Cannot resolve '{text}'. Try: btc, eth, ada, dot, link")
            return
        self.slots[index] = new_id
        self._save_config()
        self._rebuild_pair_index()
        self._start_ws()

    def _resolve_coin_id(self, text: str) -> str | None:
        t = (text or "").strip().lower()
        if not t:
            return None
        # Allow any symbol with usdt suffix
        if t.endswith("usdt"):
            # Return the raw input for direct pair usage
            return t
        # Check for known mappings
        SYMBOL_TO_ID = {
            "btc": "bitcoin",
            "eth": "eth",
            "ada": "ada",
            "dot": "polkadot",
            "link": "link",
            "sol": "solana",
            "avax": "avalanche-2",
            "sui": "sui",
            "xrp": "xrp",
            "doge": "dogecoin",
            "bch": "bitcoin-cash",
            "ton": "ton",
        }
        if t in SYMBOL_TO_ID:
            return SYMBOL_TO_ID[t]
        # Accept direct CoinGecko id
        ALLOWED_IDS = {
            "bitcoin", "eth", "cardano", "polkadot", "chainlink",
            "solana", "avalanche-2", "sui", "ripple", "dogecoin", "bitcoin-cash", "the-open-network",
        }
        if t in ALLOWED_IDS:
            return t
        # For any other input, treat as a direct trading pair symbol
        return t + "usdt"

    def _slot_to_pair(self, slot: str) -> str | None:
        s = (slot or "").strip().lower()
        if not s:
            return None
        # If user provided direct pair like btcusdt
        if s.endswith("usdt"):
            return s
        # Map common symbols -> ids -> default USDT pair
        SYMBOL_TO_ID = {
            "btc": "bitcoin",
            "eth": "eth",
            "ada": "cardano",
            "dot": "polkadot",
            "link": "chainlink",
            "sol": "solana",
            "avax": "avalanche-2",
            "sui": "sui",
            "xrp": "ripple",
            "doge": "dogecoin",
            "bch": "bitcoin-cash",
            "ton": "the-open-network",
        }
        COIN_ID_TO_BINANCE = {
            "bitcoin": "btcusdt",
            "eth": "ethusdt",
            "cardano": "adausdt",
            "polkadot": "dotusdt",
            "chainlink": "linkusdt",
            "solana": "solusdt",
            "avalanche-2": "avaxusdt",
            "sui": "suiusdt",
            "ripple": "xrpusdt",
            "dogecoin": "dogeusdt",
            "bitcoin-cash": "bchusdt",
            "the-open-network": "tonusdt",
        }
        if s in SYMBOL_TO_ID:
            return COIN_ID_TO_BINANCE.get(SYMBOL_TO_ID[s])
        if s in COIN_ID_TO_BINANCE:
            return COIN_ID_TO_BINANCE[s]
        # For any other input, treat as a direct symbol and append usdt
        return s + "usdt"

    def _rebuild_pair_index(self):
        self.pair_index = {}
        for idx, slot in enumerate(self.slots):
            p = self._slot_to_pair(slot)
            if p:
                self.pair_index[p.lower()] = idx

    def _config_path(self) -> str:
        base = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.AppConfigLocation)
        if not base:
            base = os.path.expanduser("~/.config")
        path = os.path.join(base, "crypto-widget-qt")
        os.makedirs(path, exist_ok=True)
        return os.path.join(path, "config.json")

    def _load_config(self) -> dict:
        try:
            p = self._config_path()
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _thumb_lines_serialized(self) -> dict:
        out: dict[str, dict[str, list]] = {}
        for pair, tf_map in self._thumb_lines.items():
            if not pair or not isinstance(tf_map, dict):
                continue
            serialized_tf: dict[str, list] = {}
            for tf, lines in tf_map.items():
                if not tf or not isinstance(lines, list):
                    continue
                serialized_lines: list = []
                for line in lines:
                    # Keep dict form if present; otherwise legacy 4-float list
                    if isinstance(line, dict) and 'x1f' in line:
                        try:
                            serialized_lines.append({
                                'x1f': float(line.get('x1f', 0.0)),
                                'p1': float(line.get('p1', 0.0)),
                                'x2f': float(line.get('x2f', 0.0)),
                                'p2': float(line.get('p2', 0.0)),
                                'fmt': 'data',
                            })
                        except Exception:
                            continue
                    else:
                        try:
                            if not isinstance(line, (list, tuple)) or len(line) < 4:
                                continue
                            serialized_lines.append([
                                float(line[0]), float(line[1]), float(line[2]), float(line[3])
                            ])
                        except Exception:
                            continue
                if serialized_lines:
                    serialized_tf[tf] = serialized_lines
            if serialized_tf:
                out[pair] = serialized_tf
        return out

    def _handle_thumb_line_delete_shortcut(self):
        if self._delete_selected_thumb_line():
            for popup in (getattr(self, '_thumb_popup', None), getattr(self, '_dual_popup', None)):
                if popup:
                    popup.update()

    def _save_config(self):
        try:
            g = self.geometry()
            cfg = {
                "slots": self.slots,
                "geometry": [int(g.x()), int(g.y()), int(g.width()), int(g.height())],
                "collapsed": bool(self._collapsed),
                "prefer_price_source": str(getattr(self, 'prefer_price_source', 'spot')),
                "ui_scale": float(self.ui_scale),
                "thumb_enabled": bool(self.thumb_enabled),
                "thumb_fetch_from_binance": bool(self.thumb_fetch_from_binance),
                "thumb_tf": self.thumb_tf,
                "thumb_bars": int(self.thumb_bars),
                "thumb_dual_enabled": bool(getattr(self, 'thumb_dual_enabled', False)),
                "thumb_tf2": str(getattr(self, 'thumb_tf2', '4h')),
                "thumb_bars2": int(getattr(self, 'thumb_bars2', int(self.thumb_bars))),
                "thumb_overlay_type": self.thumb_overlay_type,
                "thumb_overlay_p1": int(self.thumb_overlay_p1),
                "thumb_overlay_p2": int(self.thumb_overlay_p2),
                "thumb_sub_indicator": self.thumb_sub_indicator,
                "thumb_chart_style": str(getattr(self, 'thumb_chart_style', 'candle')),
                "thumb_scale_percent": int(getattr(self, 'thumb_scale_percent', 100)),
                "thumb_dual_scale_percent": int(getattr(self, 'thumb_dual_scale_percent', int(self.thumb_scale_percent))),
                "thumb_dual_enabled": bool(getattr(self, 'thumb_dual_enabled', False)),
                "thumb_tf2": str(getattr(self, 'thumb_tf2', '4h')),
                "thumb_bars2": int(getattr(self, 'thumb_bars2', int(self.thumb_bars))),
                "thumb_overlay_color1": str(getattr(self, 'thumb_overlay_color1', 'gold')),
                "thumb_overlay_color2": str(getattr(self, 'thumb_overlay_color2', 'skyblue')),
                "thumb_dual_overlay_type": str(getattr(self, 'thumb_dual_overlay_type', 'ma')),
                "thumb_dual_overlay_p1": int(getattr(self, 'thumb_dual_overlay_p1', 5)),
                "thumb_dual_overlay_p2": int(getattr(self, 'thumb_dual_overlay_p2', 20)),
                "thumb_dual_overlay_color1": str(getattr(self, 'thumb_dual_overlay_color1', 'orange')),
                "thumb_dual_overlay_color2": str(getattr(self, 'thumb_dual_overlay_color2', 'lime')),
                "thumb_dual_sub_indicator": str(getattr(self, 'thumb_dual_sub_indicator', 'none')),
                "thumb_lines": self._thumb_lines_serialized(),
                "alerts_enabled": bool(self.alerts_enabled),
                "alert_threshold_percent": float(self.alert_threshold_percent),
                "alert_method": self.alert_method,
                "vol_window_samples": int(self.vol_window_samples),
                "vol_threshold_sigma": float(self.vol_threshold_sigma),
                "volume_window_samples": int(self.volume_window_samples),
                "volume_threshold_sigma": float(self.volume_threshold_sigma),
                "alert_periods": self.alert_periods,
                "alert_watchlist": self.alert_watchlist,
                "tts_enabled": bool(self.tts_enabled),
                "tts_interval_min": int(self.tts_interval_min),
                "tts_include_slots": (self.tts_include_slots if self.tts_include_slots is not None else None),
            }
            with open(self._config_path(), "w", encoding="utf-8") as f:
                json.dump(cfg, f)
        except Exception:
            pass

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._save_config()
        try:
            self.ws.close()
        except Exception:
            pass
        return super().closeEvent(event)

    # --- Helpers for Alerts Settings ---
    def _fetch_top_coin_ids(self, n: int) -> List[str]:
        try:
            url = "https://api.coingecko.com/api/v3/coins/markets"
            params = {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": int(n),
                "page": 1,
                "sparkline": "false",
            }
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json() or []
            return [c.get("id") for c in data if c and c.get("id")]
        except Exception:
            return []


def main():
    # Enable high-DPI pixmaps and scaling to reduce blur on Retina/4K
    try:
        QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    except Exception:
        pass
    try:
        QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    except Exception:
        pass
    app = QtWidgets.QApplication(sys.argv)
    w = CryptoWidgetQt()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

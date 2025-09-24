import sys
import time
import os
import json
import subprocess
import platform
from collections import deque, defaultdict
import math
from typing import List, Dict
import requests

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtCore import QUrl


class CryptoDataFetcher:
    def __init__(self):
        # No fixed list; the UI supplies desired CoinGecko IDs per slot
        self.crypto_data: List[Dict] = []

    def fetch_crypto_prices(self) -> List[Dict]:
        try:
            ids = "bitcoin,ethereum,cardano,polkadot,chainlink"
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
                {"id": "ethereum", "symbol": "eth", "current_price": 4000.0, "price_change_percentage_24h": 1.2},
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
        self.ws: QWebSocket | None = None
        self.pairs: list[str] = []
        self._reconnect_timer = QtCore.QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._reconnect)
        self.last_quote_volume: dict[str, float] = {}

    def connect_pairs(self, pairs: list[str]):
        self.pairs = [p.lower() for p in pairs if isinstance(p, str) and p]
        self._open()

    def close(self):
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            try:
                self.ws.deleteLater()
            except Exception:
                pass
            self.ws = None

    def _open(self):
        self.close()
        if not self.pairs:
            return
        url = "wss://stream.binance.com:9443/stream?streams=" + "/".join(f"{p}@miniTicker" for p in self.pairs)
        self.ws = QWebSocket()
        self.ws.textMessageReceived.connect(self._on_msg)
        self.ws.errorOccurred.connect(self._on_error)
        # Use disconnected() signal; QWebSocket has no 'closed'
        self.ws.disconnected.connect(self._on_closed)
        self.ws.open(QUrl(url))

    def _reconnect(self):
        self._open()

    def _on_closed(self):
        # try reconnect after short delay
        self._reconnect_timer.start(2000)

    def _on_error(self, err):
        # backoff reconnect
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
            pass

    def get_quote_volume(self, pair: str) -> float:
        return float(self.last_quote_volume.get(pair.lower(), 0.0))


class PriceLabel(QtWidgets.QLabel):
    def __init__(self, owner: 'CryptoWidgetQt', index: int, parent=None):
        super().__init__(parent)
        self._owner = owner
        self._index = index

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        self._owner.edit_slot(self._index)
        super().mouseDoubleClickEvent(event)


class CryptoWidgetQt(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("CryptoWidgetQt")

        # Window flags: borderless + always-on-top + translucent
        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.resize(240, 30)

        # Drag state
        self._drag_pos = None
        self._collapsed = False
        self._expanded_size = QtCore.QSize(240, 30)
        self._collapsed_width = 28

        # Load config (coins, geometry, collapsed)
        cfg = self._load_config()
        self.slots: List[str] = cfg.get("slots") or [
            "bitcoin",
            "ethereum",
            "cardano",
            "polkadot",
            "chainlink",
            "solana",
            "avalanche-2",
            "sui",
        ]
        self._cfg_geometry = cfg.get("geometry")  # [x, y, w, h]
        self._collapsed = bool(cfg.get("collapsed", False))
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

        # Data
        self.fetcher = CryptoDataFetcher()
        self.worker: FetchThread | None = None
        self.ws = PriceWS(self)
        self._price_signal_connected = False
        self.last_ws_price: dict[str, float] = {}
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
        
        # RSI calculation data
        self._rsi_period = 6  # RSI period length
        self._rsi_interval = 15 * 60  # 15 minutes in seconds
        self._rsi_values: dict[str, float] = {}  # Current RSI values
        self._rsi_last_calc: dict[str, float] = {}  # Last calculation timestamp
        self._rsi_gains: dict[str, deque] = defaultdict(lambda: deque(maxlen=self._rsi_period))
        self._rsi_losses: dict[str, deque] = defaultdict(lambda: deque(maxlen=self._rsi_period))
        self._rsi_timer = QtCore.QTimer(self)
        self._rsi_timer.setInterval(60000)  # Check every minute
        self._rsi_timer.timeout.connect(self._update_rsi_values)
        self._rsi_timer.start()

        # UI
        self._build_ui()
        self._apply_style()
        self._install_drag_filters()

        # First paint
        self._update_prices([])  # placeholders until first update
        # Ensure stays on top visually
        self.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint, True)
        self.raise_()

        # Apply geometry from config
        if isinstance(self._cfg_geometry, list) and len(self._cfg_geometry) == 4:
            x, y, w, h = self._cfg_geometry
            self.setGeometry(x, y, w, h)
            self._expanded_size = QtCore.QSize(w, h)
        # Start expanded for reliability; user can collapse manually.
        # Lock height and width to prevent manual stretching; only our code changes size.
        self._collapsed = False
        self._lock_height(self._expanded_size.height())
        self._lock_to_width(self._expanded_size.width())

        # Start WebSocket for live prices
        self._start_ws()

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

        # Create labels for each slot (only price text, tooltip holds details)
        self.labels: List[PriceLabel] = []
        self.pair_index: dict[str, int] = {}
        for i, _id in enumerate(self.slots):
            lbl = PriceLabel(self, i, self.prices_widget)
            lbl.setObjectName("price")
            lbl.setAlignment(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft)
            lbl.setText("…")
            lbl.setCursor(QtCore.Qt.PointingHandCursor)
            self.prices_layout.addWidget(lbl)
            self.labels.append(lbl)
        self._rebuild_pair_index()

        # Set layout
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(root)

    def _apply_style(self):
        self.setStyleSheet(
            """
            #CryptoWidgetQt { background: transparent; }
            QFrame#root { background: rgba(255,255,255,200); border-radius: 8px; border: 1px solid rgba(203,213,225,220); }
            QLabel#price { color: #111827; font: 11px 'Helvetica Neue', Arial, sans-serif; }
            QToolButton { color: #111827; background: transparent; border: none; border-radius: 5px; padding: 0px; }
            QToolButton:hover { background: transparent; color: #111827; }
            QToolTip { color: #111827; background-color: #FFFFFF; border: 1px solid #CBD5E1; }
            """
        )

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        menu = QtWidgets.QMenu(self)
        act_refresh = menu.addAction("Refresh Prices")
        act_alerts = menu.addAction("Alerts Settings…")
        menu.addSeparator()
        menu.addAction("Quit")
        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return
        if chosen.text() == "Refresh Prices":
            self.refresh()
        elif chosen.text() == "Alerts Settings…":
            self._open_alerts_settings()
        elif chosen.text() == "Quit":
            QtWidgets.QApplication.quit()

    def mousePressEvent(self, e: QtGui.QMouseEvent) -> None:
        if e.button() == QtCore.Qt.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e: QtGui.QMouseEvent) -> None:
        if self._drag_pos is not None and e.buttons() & QtCore.Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)
            e.accept()

    def mouseReleaseEvent(self, e: QtGui.QMouseEvent) -> None:
        self._drag_pos = None

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
            return max(self._collapsed_width, base + 2)
        except Exception:
            return max(self._collapsed_width, 32)

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
                self.move(event.globalPosition().toPoint() - self._drag_pos)
        elif et == QtCore.QEvent.MouseButtonRelease:
            self._drag_pos = None
        return super().eventFilter(obj, event)

    def _start_ws(self):
        pairs_slots = [self._slot_to_pair(s) for s in self.slots]
        pairs_watch = [self._slot_to_pair(s) for s in self.alert_watchlist]
        pairs = [p for p in set([*(p for p in pairs_slots if p), *(p for p in pairs_watch if p)])]
        # Avoid duplicate connections; manage with a flag to skip noisy warnings
        if self._price_signal_connected:
            try:
                self.ws.price_update.disconnect(self._on_price_update)
            except Exception:
                pass
        self.ws.price_update.connect(self._on_price_update)
        self._price_signal_connected = True
        self.ws.connect_pairs(pairs)

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
                lbl.setToolTip("Double‑click to set coin (e.g., btcusdt/btc)")
                continue
            sym = (c.get("symbol") or "?").upper()
            price = c.get("price", 0.0) or c.get("current_price", 0.0) or 0.0
            chg = c.get("change") or c.get("price_change_percentage_24h") or 0.0
            # Main text: only numeric price, no currency symbol
            lbl.setText(self._format_price(float(price)))
            sign = "+" if float(chg) >= 0 else ""
            # Tooltip shows full details (symbol + $ + change)
            tip = f"{sym}  ${float(price):,.2f}  ({sign}{float(chg):.2f}%)"
            lbl.setToolTip(tip)

    @QtCore.Slot(str, float, float)
    def _on_price_update(self, pair: str, price: float, pct: float):
        idx = self.pair_index.get(pair.lower())
        if idx is None or idx >= len(self.labels):
            # Not a visible pair; still record and maybe alert
            self.last_ws_price[pair.lower()] = float(price)
            self._ingest_series(pair, price)
            self._maybe_alert(pair, price, pct)
            return
        lbl = self.labels[idx]
        lbl.setText(self._format_price(price))
        sign = "+" if pct >= 0 else ""
        sym = pair[:-4].upper()  # e.g., btcusdt -> BTC
        lbl.setToolTip(f"{sym}  ${price:,.2f}  ({sign}{pct:.2f}%)")
        self.last_ws_price[pair.lower()] = float(price)
        self._ingest_series(pair, price)
        self._maybe_alert(pair, price, pct)
        
        # Apply RSI styling if available
        rsi = getattr(self, '_rsi_values', {}).get(pair.lower())
        if rsi is not None:
            self._apply_rsi_style(lbl, rsi)
            
    def _apply_rsi_style(self, label, rsi):
        """Apply styling based on RSI value
        
        Strong levels: 68, 75, 83, 94
        Weak levels: 25, 20, 15, 10
        
        Level 1: Bold
        Level 2: Red
        Level 3: Red + Bold
        Level 4: Flashing Red + Bold
        """
        # Reset styles first
        label.setStyleSheet("")
        
        # Stop any existing animation
        for animation in label.findChildren(QtCore.QPropertyAnimation):
            animation.stop()
            animation.deleteLater()
        
        # Apply styles based on RSI levels
        if rsi >= 68:  # Strong levels
            if rsi >= 94:  # Level 4
                self._apply_flashing_style(label, "red", True)
            elif rsi >= 83:  # Level 3
                label.setStyleSheet("color: red; font-weight: bold;")
            elif rsi >= 75:  # Level 2
                label.setStyleSheet("color: red;")
            else:  # Level 1 (68-75)
                label.setStyleSheet("font-weight: bold;")
        elif rsi <= 25:  # Weak levels
            if rsi <= 10:  # Level 4
                self._apply_flashing_style(label, "red", True)
            elif rsi <= 15:  # Level 3
                label.setStyleSheet("color: red; font-weight: bold;")
            elif rsi <= 20:  # Level 2
                label.setStyleSheet("color: red;")
            else:  # Level 1 (20-25)
                label.setStyleSheet("font-weight: bold;")
                
    def _apply_flashing_style(self, label, color, bold=False):
        """Apply flashing animation to label"""
        # Set initial style
        bold_style = "font-weight: bold;" if bold else ""
        label.setStyleSheet(f"color: {color}; {bold_style}")
        
        # Create opacity effect
        effect = QtWidgets.QGraphicsOpacityEffect(label)
        label.setGraphicsEffect(effect)
        
        # Create animation
        animation = QtCore.QPropertyAnimation(effect, b"opacity")
        animation.setDuration(500)  # 500ms per cycle
        animation.setStartValue(1.0)
        animation.setEndValue(0.3)
        animation.setLoopCount(-1)  # Infinite loop
        animation.setEasingCurve(QtCore.QEasingCurve.InOutSine)
        
        # Start animation
        animation.start()

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
            pass

    # --- Alerts ---
    def _watch_pairs_set(self) -> set[str]:
        pairs = [self._slot_to_pair(s) for s in self.alert_watchlist]
        return {p.lower() for p in pairs if p}

    def _maybe_alert(self, pair: str, price: float, pct: float):
        try:
            if not self.alerts_enabled:
                return
            pair_l = pair.lower()
            if pair_l not in self._watch_pairs_set():
                return
            triggered = False
            title = ""
            detail = ""
            if self.alert_method == "pct":
                threshold = float(self.alert_threshold_percent)
                # Check across configured periods
                period_hit = None
                for lab in (self.alert_periods or ["24h"]):
                    sec = self._period_seconds(lab)
                    if not sec:
                        continue
                    if lab == "24h":
                        val = abs(float(pct))
                    else:
                        pc = self._percent_change_over(pair_l, sec)
                        if pc is None:
                            continue
                        val = abs(float(pc))
                    if val >= threshold:
                        period_hit = (lab, val)
                        break
                if period_hit:
                    triggered = True
                    lab, val = period_hit
                    title = f"Change {lab}"
                    detail = f"{val:+.2f}%"
            elif self.alert_method == "vol":
                k = float(self.vol_threshold_sigma)
                std, last_ret = self._volatility_stats(pair_l)
                triggered = (std is not None and last_ret is not None and abs(last_ret) >= k * std)
                title = "Volatility spike"
                if std is not None and last_ret is not None:
                    detail = f"ret={last_ret:+.4f}, σ={std:.4f}"
                else:
                    return
            elif self.alert_method == "volume":
                k = float(self.volume_threshold_sigma)
                z = self._volume_zscore(pair_l)
                triggered = (z is not None and z >= k)
                if z is None:
                    return
                title = "Volume breakout"
                detail = f"z={z:.2f}"
            if not triggered:
                return
            now = time.time()
            last = self.last_alert_time.get(pair_l, 0)
            if now - last < self._alert_cooldown_sec:
                return
            self.last_alert_time[pair_l] = now
            sym = pair[:-4].upper()
            self._notify(f"{sym} abnormal: {title}", f"{sym} {detail}  price: ${price:,.2f}")
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
            
            # Update RSI data
            if prev_price is not None:
                change = price - prev_price
                if change > 0:
                    self._rsi_gains[p].append(change)
                    self._rsi_losses[p].append(0)
                else:
                    self._rsi_gains[p].append(0)
                    self._rsi_losses[p].append(abs(change))
                
                # Calculate RSI if enough time has passed since last calculation
                last_calc = self._rsi_last_calc.get(p, 0)
                if now - last_calc >= 60:  # Recalculate at most once per minute
                    self._calculate_rsi(p)
                    self._rsi_last_calc[p] = now
        except Exception:
            pass
            
    def _calculate_rsi(self, pair: str):
        """Calculate RSI-6 for the given pair"""
        try:
            p = pair.lower()
            if len(self._rsi_gains[p]) < self._rsi_period or len(self._rsi_losses[p]) < self._rsi_period:
                return
                
            avg_gain = sum(self._rsi_gains[p]) / self._rsi_period
            avg_loss = sum(self._rsi_losses[p]) / self._rsi_period
            
            if avg_loss == 0:
                rsi = 100
            else:
                rs = avg_gain / avg_loss
                rsi = 100 - (100 / (1 + rs))
                
            self._rsi_values[p] = rsi
            
            # Update label style based on RSI
            idx = self.pair_index.get(p)
            if idx is not None and idx < len(self.labels):
                self._apply_rsi_style(self.labels[idx], rsi)
        except Exception:
            pass
            
    def _update_rsi_values(self):
        """Update RSI values for all pairs"""
        for pair in list(self._price_series.keys()):
            self._calculate_rsi(pair)

    def _volatility_stats(self, pair_l: str):
        try:
            prices = self._price_series[pair_l]
            if len(prices) < max(3, int(self.vol_window_samples)):
                return None, None
            N = int(self.vol_window_samples)
            seq = list(prices)[-N:]
            rets = []
            for i in range(1, len(seq)):
                if seq[i-1] > 0:
                    rets.append((seq[i] - seq[i-1]) / seq[i-1])
            if len(rets) < 2:
                return None, None
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            std = math.sqrt(var)
            last_ret = rets[-1]
            return std, last_ret
        except Exception:
            return None, None

    def _volume_zscore(self, pair_l: str):
        try:
            vols = self._vol_series[pair_l]
            if len(vols) < max(5, int(self.volume_window_samples)):
                return None
            N = int(self.volume_window_samples)
            seq = list(vols)[-N:]
            mean = sum(seq) / len(seq)
            var = sum((v - mean) ** 2 for v in seq) / (len(seq) - 1)
            std = math.sqrt(var) if var > 0 else 0.0
            if std == 0:
                return None
            z = (seq[-1] - mean) / std
            return z
        except Exception:
            return None

    def _period_seconds(self, label: str) -> int | None:
        mapping = {
            "1m": 60,
            "3m": 180,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "4h": 14400,
            "24h": 86400,
        }
        return mapping.get(label)

    def _percent_change_over(self, pair_l: str, seconds: int) -> float | None:
        dq = self._price_ts.get(pair_l)
        if not dq or len(dq) < 2:
            return None
        now = time.time()
        cutoff = now - float(seconds)
        # find earliest sample >= cutoff
        base_price = None
        for (ts, pr) in dq:
            if ts >= cutoff:
                base_price = pr
                break
        if base_price is None:
            # all samples earlier than cutoff; use oldest
            base_price = dq[0][1]
        last_price = dq[-1][1]
        if base_price <= 0:
            return None
        return (last_price - base_price) / base_price * 100.0

    def _notify(self, title: str, message: str):
        # macOS notification via AppleScript; fallback to console
        try:
            if platform.system() == "Darwin":
                # Escape quotes for AppleScript
                safe_title = title.replace('"', '\\"')
                safe_msg = message.replace('"', '\\"')
                script = f'display notification "{safe_msg}" with title "{safe_title}"'
                subprocess.run(["osascript", "-e", script], check=False)
            else:
                print(f"[NOTIFY] {title}: {message}")
        except Exception:
            print(f"[NOTIFY] {title}: {message}")

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

    def _format_price(self, price: float) -> str:
        # Compact price: reduce decimals for large numbers
        if price >= 1000:
            return f"{price:,.0f}"
        elif price >= 1:
            return f"{price:,.2f}"
        else:
            return f"{price:.4f}"

    # --- Editing + Persistence ---
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
            "eth": "ethereum",
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
        if t in SYMBOL_TO_ID:
            return SYMBOL_TO_ID[t]
        # Accept direct CoinGecko id
        ALLOWED_IDS = {
            "bitcoin", "ethereum", "cardano", "polkadot", "chainlink",
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
            "eth": "ethereum",
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
            "ethereum": "ethusdt",
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
                self.pair_index[p] = idx

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

    def _save_config(self):
        try:
            g = self.geometry()
            cfg = {
                "slots": self.slots,
                "geometry": [int(g.x()), int(g.y()), int(g.width()), int(g.height())],
                "collapsed": bool(self._collapsed),
                "alerts_enabled": bool(self.alerts_enabled),
                "alert_threshold_percent": float(self.alert_threshold_percent),
                "alert_method": self.alert_method,
                "vol_window_samples": int(self.vol_window_samples),
                "vol_threshold_sigma": float(self.vol_threshold_sigma),
                "volume_window_samples": int(self.volume_window_samples),
                "volume_threshold_sigma": float(self.volume_threshold_sigma),
                "alert_periods": self.alert_periods,
                "alert_watchlist": self.alert_watchlist,
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


def main():
    app = QtWidgets.QApplication(sys.argv)
    w = CryptoWidgetQt()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

import time
import math
import platform
import subprocess
import tempfile
import threading
import asyncio
import os
from PySide6 import QtCore, QtWidgets

try:
    import edge_tts  # type: ignore
    _EDGE_TTS_AVAILABLE = True
except Exception:
    _EDGE_TTS_AVAILABLE = False

STYLE_RSI_LEVELS_STRONG = [76, 76, 84, 92]
STYLE_RSI_LEVELS_WEAK = [25, 20, 15, 10]
STYLE_FLASH_DURATION_MS = 500
STYLE_FLASH_MIN_OPACITY = 0.3
STYLE_PRESETS = {
    "normal": {"css": "color: #111827; font-weight: normal;"},
    "bold": {"css": "color: #111827; font-weight: bold;"},
    "red": {"css": "color: brown; font-weight: normal;"},
    "red_bold": {"css": "color: brown; font-weight: bold;"},
    "flash_red": {"css": "color: brown; font-weight: normal;", "flash_color": "brown", "flash_bold": False},
    "flash_red_bold": {"css": "color: red; font-weight: bold;", "flash_color": "red", "flash_bold": True},
    "flash_bold": {"css": "font-weight: bold;", "flash_color": "brown", "flash_bold": True},
    "blue": {"css": "color: #00008B; font-weight: normal;"},
    "blue_bold": {"css": "color: #00008B; font-weight: bold;"},
    "blue_bold_2": {"css": "color: blue; font-weight: bold;"}
}
STYLE_LEVEL_MAP_STRONG = ["bold", "bold", "flash_bold", "flash_red_bold"]
STYLE_LEVEL_MAP_WEAK = ["bold", "blue", "blue_bold", "blue_bold_2"]

class AlertManager:
    def __init__(self, widget):
        self.w = widget
        # 简单语音配置（macOS，edge-tts 无需 API Key）
        self.edge_tts_enabled = getattr(widget, "edge_tts_enabled", False)
        self.edge_tts_voice = getattr(widget, "edge_tts_voice", "zh-CN-XiaoxiaoNeural")

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

    def has_edge_tts(self) -> bool:
        return bool(_EDGE_TTS_AVAILABLE)

    def watch_pairs_set(self) -> set[str]:
        pairs = [self.w._slot_to_pair(s) for s in self.w.alert_watchlist]
        return {p.lower() for p in pairs if p}

    def period_seconds(self, label: str) -> int | None:
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

    def percent_change_over(self, pair_l: str, seconds: int) -> float | None:
        dq = self.w._price_ts.get(pair_l)
        if not dq or len(dq) < 2:
            return None
        now = time.time()
        cutoff = now - float(seconds)
        base_price = None
        for (ts, pr) in dq:
            if ts >= cutoff:
                base_price = pr
                break
        if base_price is None:
            base_price = dq[0][1]
        last_price = dq[-1][1]
        if base_price <= 0:
            return None
        return (last_price - base_price) / base_price * 100.0

    def volatility_stats(self, pair_l: str):
        try:
            prices = self.w._price_series[pair_l]
            if len(prices) < max(3, int(self.w.vol_window_samples)):
                return None, None
            N = int(self.w.vol_window_samples)
            seq = list(prices)[-N:]
            rets = []
            for i in range(1, len(seq)):
                if seq[i - 1] > 0:
                    rets.append((seq[i] - seq[i - 1]) / seq[i - 1])
            if len(rets) < 2:
                return None, None
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            std = math.sqrt(var)
            last_ret = rets[-1]
            return std, last_ret
        except Exception:
            return None, None

    def volume_zscore(self, pair_l: str):
        try:
            vols = self.w._vol_series[pair_l]
            if len(vols) < max(5, int(self.w.volume_window_samples)):
                return None
            N = int(self.w.volume_window_samples)
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

    def notify(self, title: str, message: str):
        try:
            if platform.system() == "Darwin":
                safe_title = title.replace('"', '\\"')
                safe_msg = message.replace('"', '\\"')
                script = f'display notification "{safe_msg}" with title "{safe_title}"'
                subprocess.run(["osascript", "-e", script], check=False)
            else:
                print(f"[NOTIFY] {title}: {message}")
        except Exception:
            print(f"[NOTIFY] {title}: {message}")

    def _try_speak_fallback(self, text: str) -> bool:
        if platform.system() == "Darwin":
            try:
                # Use macOS 'say' command
                subprocess.run(["say", text], check=True)
                return True
            except Exception:
                pass
        return False

    def speak_edge(self, text: str):
        if not _EDGE_TTS_AVAILABLE:
            # Try fallback immediately if lib not available
            if self._try_speak_fallback(text):
                return
            raise RuntimeError("edge-tts not available")
        
        voice = self.edge_tts_voice or "zh-CN-XiaoxiaoNeural"
        text = self._to_cn_digits(text).replace(",", "")

        def _worker():
            try:
                async def _amain():
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
                        out = f.name
                    try:
                        try:
                            comm = edge_tts.Communicate(text, voice, rate="-10%")
                        except TypeError:
                            comm = edge_tts.Communicate(text, voice)
                        await comm.save(out)
                        sysname = platform.system()
                        if sysname == "Darwin":
                            subprocess.run(["afplay", out], check=True)
                        else:
                            subprocess.run(["aplay", out], check=True)
                    finally:
                        try:
                            os.unlink(out)
                        except Exception:
                            pass

                asyncio.run(_amain())
            except Exception:
                print(f"[Alert] edge-tts failed, trying fallback...", flush=True)
                self._try_speak_fallback(text)

        th = threading.Thread(target=_worker, daemon=True)
        th.start()

    def speak_edge_sequence(self, parts: list[str], pause_s: float = 2.0):
        if not _EDGE_TTS_AVAILABLE:
             # Try fallback loop
            def _worker_fallback():
                for i, seg in enumerate(parts):
                    self._try_speak_fallback(seg)
                    if i + 1 < len(parts):
                        time.sleep(max(0.0, float(pause_s)))
            threading.Thread(target=_worker_fallback, daemon=True).start()
            return

        voice = self.edge_tts_voice or "zh-CN-XiaoxiaoNeural"

        def _worker_seq():
            # Try full sequence with edge-tts; if individual fails, fallback for that part
            for i, seg in enumerate(parts):
                success = False
                try:
                    txt = self._to_cn_digits(seg).replace(",", "")
                    async def _amain_one(t: str):
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
                            out = f.name
                        try:
                            try:
                                comm = edge_tts.Communicate(t, voice, rate="-15%")
                            except TypeError:
                                comm = edge_tts.Communicate(t, voice)
                            await comm.save(out)
                            sysname = platform.system()
                            if sysname == "Darwin":
                                subprocess.run(["afplay", out], check=True)
                            else:
                                subprocess.run(["aplay", out], check=True)
                        finally:
                            try:
                                os.unlink(out)
                            except Exception:
                                pass
                    asyncio.run(_amain_one(txt))
                    success = True
                except Exception:
                     print(f"[Alert] edge-tts failed for part, trying fallback...", flush=True)
                
                if not success:
                    self._try_speak_fallback(seg)

                if i + 1 < len(parts):
                    time.sleep(max(0.0, float(pause_s)))

        th = threading.Thread(target=_worker_seq, daemon=True)
        th.start()

    def apply_rsi_style(self, label, rsi):
        label.setStyleSheet("color: #111827; font-weight: normal;")
        for animation in label.findChildren(QtCore.QPropertyAnimation):
            animation.stop()
            animation.deleteLater()
        if label.graphicsEffect() is not None:
            label.setGraphicsEffect(None)
        strong = STYLE_RSI_LEVELS_STRONG
        weak = STYLE_RSI_LEVELS_WEAK
        if rsi >= strong[0]:
            if rsi >= strong[3]:
                self._apply_style_preset(label, STYLE_LEVEL_MAP_STRONG[3])
            elif rsi >= strong[2]:
                self._apply_style_preset(label, STYLE_LEVEL_MAP_STRONG[2])
            elif rsi >= strong[1]:
                self._apply_style_preset(label, STYLE_LEVEL_MAP_STRONG[1])
            else:
                self._apply_style_preset(label, STYLE_LEVEL_MAP_STRONG[0])
        elif rsi <= weak[0]:
            if rsi <= weak[3]:
                self._apply_style_preset(label, STYLE_LEVEL_MAP_WEAK[3])
            elif rsi <= weak[2]:
                self._apply_style_preset(label, STYLE_LEVEL_MAP_WEAK[2])
            elif rsi <= weak[1]:
                self._apply_style_preset(label, STYLE_LEVEL_MAP_WEAK[1])
            else:
                self._apply_style_preset(label, STYLE_LEVEL_MAP_WEAK[0])

    def _apply_style_preset(self, label, name: str):
        preset = STYLE_PRESETS.get(name) or STYLE_PRESETS["normal"]
        css = preset.get("css")
        if css:
            label.setStyleSheet(css)
        fcolor = preset.get("flash_color")
        if fcolor is not None:
            self._apply_flashing_style(label, fcolor, bool(preset.get("flash_bold")))

    def pick_rsi_for_style(self, rsi_map: dict[str, float]) -> float | None:
        if not rsi_map:
            return None
        def level(r: float) -> int:
            s = STYLE_RSI_LEVELS_STRONG
            w = STYLE_RSI_LEVELS_WEAK
            if r >= s[3]:
                return 3
            if r >= s[2]:
                return 2
            if r >= s[1]:
                return 1
            if r >= s[0]:
                return 0
            if r <= w[3]:
                return 3
            if r <= w[2]:
                return 2
            if r <= w[1]:
                return 1
            if r <= w[0]:
                return 0
            return -1
        best = None
        best_score = -1
        for tf, r in rsi_map.items():
            lv = level(r)
            score = lv
            if score > best_score:
                best_score = score
                best = r
        return best

    def _apply_flashing_style(self, label, color, bold=False):
        bold_style = "font-weight: bold;" if bold else ""
        label.setStyleSheet(f"color: {color}; {bold_style}")
        effect = QtWidgets.QGraphicsOpacityEffect(label)
        label.setGraphicsEffect(effect)
        animation = QtCore.QPropertyAnimation(effect, b"opacity")
        animation.setDuration(int(STYLE_FLASH_DURATION_MS))
        animation.setStartValue(1.0)
        animation.setEndValue(float(STYLE_FLASH_MIN_OPACITY))
        animation.setLoopCount(-1)
        animation.setEasingCurve(QtCore.QEasingCurve.InOutSine)
        animation.start()

    def maybe_alert(self, pair: str, price: float, pct: float):
        try:
            if not self.w.alerts_enabled:
                return None
            pair_l = pair.lower()
            if pair_l not in self.watch_pairs_set():
                return None
            triggered = False
            title = ""
            detail = ""
            period_label = None
            if self.w.alert_method == "pct":
                threshold = float(self.w.alert_threshold_percent)
                period_hit = None
                for lab in (self.w.alert_periods or ["24h"]):
                    sec = self.period_seconds(lab)
                    if not sec:
                        continue
                    if lab == "24h":
                        val = abs(float(pct))
                    else:
                        pc = self.percent_change_over(pair_l, sec)
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
                    period_label = lab
                    detail = f"{val:+.2f}%"
            elif self.w.alert_method == "vol":
                k = float(self.w.vol_threshold_sigma)
                std, last_ret = self.volatility_stats(pair_l)
                triggered = (std is not None and last_ret is not None and abs(last_ret) >= k * std)
                title = "Volatility spike"
                if std is not None and last_ret is not None:
                    detail = f"ret={last_ret:+.4f}, σ={std:.4f}"
                else:
                    return
            elif self.w.alert_method == "volume":
                k = float(self.w.volume_threshold_sigma)
                z = self.volume_zscore(pair_l)
                triggered = (z is not None and z >= k)
                if z is None:
                    return
                title = "Volume breakout"
                detail = f"z={z:.2f}"
            if not triggered:
                return None
            now = time.time()
            last = self.w.last_alert_time.get(pair_l, 0)
            if now - last < self.w._alert_cooldown_sec:
                return None
            self.w.last_alert_time[pair_l] = now
            sym = pair[:-4].upper()
            self.notify(f"{sym} abnormal: {title}", f"{sym} {detail}  price: ${price:,.2f}")
            # 简单语音播报（可通过 widget.edge_tts_enabled 控制开关；默认关闭）
            if bool(self.edge_tts_enabled):
                cn_title = {
                    "Volatility spike": "波动率异常",
                    "Volume breakout": "成交量爆发",
                }.get(title, title)
                say = f"{sym}，{cn_title}，{detail.replace('%', '百分比')}，当前价格 {price:,.2f} 美元。"
                self.speak_edge(say)
            # Return alert info so UI can mark indicator
            return {"triggered": True, "period": period_label}
        except Exception:
            return None

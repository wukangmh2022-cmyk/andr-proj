import os
import sys
import traceback

# 1. 极简的错误显示入口，不依赖任何第三方库（防止 import 就崩）
def show_crash(e_text):
    """
    一个极其坚固的报错界面，只依赖 Kivy 基础
    """
    from kivy.app import App
    from kivy.uix.label import Label
    from kivy.uix.scrollview import ScrollView
    from kivy.core.window import Window
    from kivy.utils import platform

    class CrashApp(App):
        def build(self):
            # 强制深红背景，醒目
            Window.clearcolor = (0.2, 0, 0, 1)
            
            scroll = ScrollView()
            label = Label(
                text=f"CRITICAL STARTUP ERROR:\n\n{e_text}",
                font_size='18sp',
                color=(1, 1, 1, 1),
                size_hint_y=None,
                text_size=(Window.width * 0.9, None),  # 留边距
                halign='left', 
                valign='top'
            )
            # 自动调整高度
            label.bind(texture_size=label.setter('size'))
            scroll.add_widget(label)
            return scroll

    try:
        CrashApp().run()
    except Exception:
        # 如果连报错界面都崩了，那就真的没办法了
        print("CRITICAL: Crash handler failed to UI.")
        pass

# 2. 全局捕获：甚至包括 Import 阶段
try:
    # --- 正常的 Imports ---
    import threading
    import time
    import requests
    from kivy.app import App
    from kivy.clock import Clock
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.label import Label
    from kivy.uix.button import Button
    from kivy.uix.recycleview import RecycleView
    from kivy.uix.recycleview.views import RecycleDataViewBehavior
    from kivy.properties import BooleanProperty, StringProperty
    from kivy.uix.recycleboxlayout import RecycleBoxLayout
    from kivy.uix.behaviors import FocusBehavior
    from kivy.uix.viewclass import ViewClass
    from kivy.core.window import Window
    from kivy.utils import platform

    # --- Configuration ---
    SLOTS = ["btcusdt", "ethusdt", "solusdt", "dogeusdt", "suiusdt", "avaxusdt"]
    API_URL = "https://api.binance.com/api/v3/ticker/price"
    REFRESH_RATE = 2.0  # Seconds

    class SelectableRecycleBoxLayout(FocusBehavior, RecycleBoxLayout):
        pass

    class Row(RecycleDataViewBehavior, BoxLayout):
        text = StringProperty("")
        price = StringProperty("")
        color_class = StringProperty("normal")

    class CryptoApp(App):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.data_store = {s: {"price": 0.0, "status": "ok"} for s in SLOTS}
            self.lock = threading.Lock()
            self.running = True

        def build(self):
            Window.clearcolor = (0.1, 0.1, 0.1, 1) # Dark background
            
            root = BoxLayout(orientation='vertical', padding=10, spacing=10)
            
            # Header
            header = BoxLayout(size_hint_y=None, height=50)
            self.status_label = Label(text="Status: Connecting...", size_hint_x=0.8)
            header.add_widget(self.status_label)
            
            # Floating Window Permission Button
            if platform == 'android':
                perm_btn = Button(text="Float Perm", size_hint_x=0.2)
                perm_btn.bind(on_press=self.request_overlay_permission)
                header.add_widget(perm_btn)
            
            root.add_widget(header)

            # List
            self.rv = RecycleView()
            self.rv.viewclass = 'Row'
            self.rv.data = [{'text': s.upper(), 'price': 'Loading...', 'color_class': 'normal'} for s in SLOTS]
            
            # Layout for list
            layout = SelectableRecycleBoxLayout(default_size=(None, 56), default_size_hint=(1, None), size_hint_y=None, orientation='vertical')
            layout.bind(minimum_height=layout.setter('height'))
            self.rv.add_widget(layout)
            root.add_widget(self.rv)

            return root

        def on_start(self):
            threading.Thread(target=self.fetch_loop, daemon=True).start()
            Clock.schedule_interval(self.update_ui, 1.0)

        def on_stop(self):
            self.running = False

        def request_overlay_permission(self, instance):
            if platform == 'android':
                from jnius import autoclass
                PythonActivity = autoclass('org.kivy.android.PythonActivity')
                Settings = autoclass('android.provider.Settings')
                Intent = autoclass('android.content.Intent')
                Uri = autoclass('android.net.Uri')
                
                activity = PythonActivity.mActivity
                if not Settings.canDrawOverlays(activity):
                    intent = Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                                    Uri.parse("package:" + activity.getPackageName()))
                    activity.startActivityForResult(intent, 0)
                else:
                    self.status_label.text = "Overlay Permission already granted"

        def fetch_loop(self):
            """Robust fetching loop with double-check mechanism"""
            while self.running:
                try:
                    # 1. Primary Fetch (Simulating WS/Fast Polling)
                    ids_param = str([s.upper() for s in SLOTS]).replace("'", '"') 
                    
                    symbols_arg = "[" + ",".join([f'"{s.upper()}"' for s in SLOTS]) + "]"
                    resp = requests.get(API_URL, params={"symbols": symbols_arg}, timeout=5)
                    
                    if resp.status_code == 200:
                        data = resp.json()
                        with self.lock:
                            for item in data:
                                sym = item['symbol'].lower()
                                price = float(item['price'])
                                if sym in self.data_store:
                                    self.data_store[sym]['price'] = price
                                    self.data_store[sym]['status'] = 'ok'
                    else:
                        self.update_status(f"HTTP Error: {resp.status_code}")
                    
                    time.sleep(REFRESH_RATE)
                    
                except Exception as e:
                    self.update_status(f"Net Error: {str(e)[:20]}...")
                    time.sleep(5)

        def update_status(self, text):
            Clock.schedule_once(lambda dt: setattr(self.status_label, 'text', text))

        def update_ui(self, dt):
            with self.lock:
                new_data = []
                for s in SLOTS:
                    info = self.data_store.get(s, {})
                    p = info.get('price', 0)
                    fmt = f"${p:,.2f}" if p > 0 else "..."
                    new_data.append({
                        'text': s[:-4].upper(),
                        'price': fmt,
                        'color_class': 'normal'
                    })
                self.rv.data = new_data

    # Use Builder only if imports succeeded
    from kivy.lang import Builder
    Builder.load_string('''
<Row>:
    canvas.before:
        Color:
            rgba: 0.2, 0.2, 0.2, 1
        Rectangle:
            pos: self.pos
            size: self.size
    orientation: 'horizontal'
    padding: 10
    Label:
        text: root.text
        font_size: '20sp'
        bold: True
        size_hint_x: 0.3
        color: 1, 1, 1, 1
    Label:
        text: root.price
        font_size: '20sp'
        size_hint_x: 0.7
        halign: 'right'
        color: (0, 1, 0, 1) if root.price != "..." else (0.7, 0.7, 0.7, 1)
''')

    if __name__ == '__main__':
        CryptoApp().run()

except Exception:
    # 3. 终极捕获：无论哪里出错（包括 import 缺失），都会跳到这里
    err_msg = traceback.format_exc()
    # 尝试在 Logcat 打印一份，以防 UI 起不来
    print("CRITICAL ERROR:\n", err_msg)
    # 启动报错界面
    show_crash(err_msg)

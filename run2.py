import sys
from PySide6 import QtWidgets
from run import CryptoWidgetQt

def main():
    print("[Run2] start")
    app = QtWidgets.QApplication(sys.argv)
    w = CryptoWidgetQt(use_mock_ws=True)
    try:
        print("[Run2] slots: " + ", ".join(w.slots))
    except Exception:
        pass
    try:
        print(f"[Run2] alerts_enabled={w.alerts_enabled} method={w.alert_method}")
    except Exception:
        pass
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()

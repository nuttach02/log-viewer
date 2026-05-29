"""PyInstaller entry point — sets CWD before importing app modules."""
import sys
import os
import socket

# When frozen by PyInstaller, __file__ paths inside modules point to _MEIPASS.
# Change working directory to the exe folder so relative paths (templates/, static/)
# resolve correctly at runtime.
if getattr(sys, 'frozen', False):
    os.chdir(sys._MEIPASS)  # _internal/ folder where templates/ and static/ are bundled

from main import app
import uvicorn


def _get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    import threading
    import webbrowser

    try:
        ip = _get_local_ip()
        print("=" * 44)
        print("  Log Viewer starting...")
        print(f"  This PC  : http://localhost:8000")
        print(f"  Other PCs: http://{ip}:8000")
        print("=" * 44)

        threading.Timer(1.5, lambda: webbrowser.open("http://localhost:8000")).start()

        uvicorn.run(app, host="0.0.0.0", port=8000)

    except Exception as e:
        import traceback
        print("\n[ERROR] โปรแกรมเกิดข้อผิดพลาด:")
        traceback.print_exc()

    input("\nกด Enter เพื่อปิด...")

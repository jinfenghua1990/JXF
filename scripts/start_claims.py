"""Start the existing local service and open its page from the desktop launcher."""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8021/"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def ready() -> bool:
    try:
        with HTTP.open(URL + "healthz", timeout=1) as response:
            health = json.load(response)
        with HTTP.open(URL + "api/page-version", timeout=1) as response:
            page = json.load(response)
        return health.get("ok") is True and bool(page.get("revision"))
    except (OSError, ValueError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not ready():
        if not (ROOT / ".env").is_file() or not (ROOT / "backend/app/main.py").is_file():
            print("启动失败：系统文件或 .env 配置缺失。")
            return 1
        with socket.socket() as connection:
            connection.settimeout(1)
            if connection.connect_ex(("127.0.0.1", 8021)) == 0:
                print("启动失败：8021 端口正在被其他服务占用，请联系排查。")
                return 1
        print("正在启动售后理赔系统，请稍候……", flush=True)
        with (ROOT / "service.log").open("ab") as output, (ROOT / "service.err.log").open("ab") as errors:
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0",
                 "--port", "8021", "--app-dir", "backend"],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        (ROOT / "service.pid").write_text(str(process.pid), encoding="ascii")
        deadline = time.monotonic() + 25
        while not ready():
            if process.poll() is not None or time.monotonic() >= deadline:
                print("启动未成功，请查看系统目录中的 service.err.log。")
                return 1
            time.sleep(0.4)
    print("售后理赔系统已运行：" + URL)
    if not args.no_browser:
        webbrowser.open(URL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

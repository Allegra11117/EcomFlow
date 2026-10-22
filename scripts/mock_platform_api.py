"""模拟电商平台开放接口（零依赖，标准库实现）。

用途：演示"接口对接"能力——把平台导出文件包装成 HTTP 分页接口，
      把 config.yaml 里 ingest.api.enabled 改成 true 即可切换采集方式。

启动：
    python scripts/mock_platform_api.py --port 8801
    curl "http://127.0.0.1:8801/api/v1/orders?page=1&page_size=3"
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "raw"
ENDPOINTS = {"orders": "orders.csv", "inventory": "inventory.csv",
             "traffic": "traffic.csv", "products": "products.csv"}


def _load(name: str) -> list[dict]:
    path = DATA_DIR / ENDPOINTS[name]
    if not path.exists():
        return []
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
    df = df.where(pd.notna(df), None)
    return df.to_dict("records")


CACHE: dict[str, list[dict]] = {name: _load(name) for name in ENDPOINTS}


class Handler(BaseHTTPRequestHandler):
    server_version = "MockPlatformAPI/1.0"

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        parsed = urlparse(self.path)
        if parsed.path in ("/health", "/"):
            self._json({"status": "ok", "endpoints": [f"/api/v1/{k}" for k in ENDPOINTS]})
            return

        name = parsed.path.rstrip("/").split("/")[-1]
        if name not in CACHE:
            self._json({"error": "not found", "path": parsed.path}, 404)
            return

        query = parse_qs(parsed.query)
        try:
            page = max(1, int(query.get("page", ["1"])[0]))
            page_size = min(2000, max(1, int(query.get("page_size", ["500"])[0])))
        except ValueError:
            self._json({"error": "page/page_size 必须为整数"}, 400)
            return

        rows = CACHE[name]
        start = (page - 1) * page_size
        chunk = rows[start:start + page_size]
        self._json({
            "source": name,
            "page": page,
            "page_size": page_size,
            "total": len(rows),
            "has_next": start + page_size < len(rows),
            "data": chunk,
        })

    def log_message(self, fmt: str, *args) -> None:  # 静默默认访问日志
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="模拟平台开放接口")
    parser.add_argument("--port", type=int, default=8801)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"模拟平台接口已启动: http://127.0.0.1:{args.port}/api/v1/orders?page=1&page_size=3")
    print("例如: /api/v1/orders  /api/v1/inventory  /api/v1/traffic  /api/v1/products")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

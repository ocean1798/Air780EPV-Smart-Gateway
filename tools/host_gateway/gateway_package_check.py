"""Explicit no-device packaged-Web check for EXE build verification."""
import hashlib
import http.client
import json
from pathlib import Path
import secrets
import sys
import threading
import time

import gateway_runtime as runtime


def verify_web(output_file):
    output = Path(output_file).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    data = output.parent / "verify-data"
    data.mkdir(exist_ok=True)
    runtime.configure({"dataDir": str(data), "cacheDir": str(data), "logDir": str(data),
                       "webBasePath": "/", "credential": secrets.token_urlsafe(32)})
    from gateway_web import WebServer, BUNDLE_DIR
    server = WebServer(host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.running:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("packaged_web_start_failed")
            time.sleep(0.02)
        port = server.server.server_address[1]
        def get(path):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
            conn.request("GET", path, headers={"Authorization": "Bearer " + runtime.settings["credential"]})
            response = conn.getresponse()
            status, body = response.status, response.read()
            conn.close()
            if status != 200:
                raise RuntimeError("packaged_web_request_failed")
            return body
        html = get("/")
        config = get("/runtime-config.js").decode()
        calls = json.loads(get("/api/calls"))
        if runtime.BUSINESS_VERSION not in config or not calls.get("ok"):
            raise RuntimeError("packaged_business_version_mismatch")
        build_file = Path(BUNDLE_DIR) / "build-info.json"
        result = {"frozen": bool(getattr(sys, "frozen", False)), "deviceOpened": False,
                  "businessVersion": runtime.BUSINESS_VERSION, "rootHttpStatus": 200, "callsHttpStatus": 200,
                  "webSha256": hashlib.sha256(html).hexdigest(), "runtimeConfig": config,
                  "build": json.loads(build_file.read_text(encoding="utf-8")) if build_file.is_file() else None}
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        server.stop()
        thread.join(timeout=2)


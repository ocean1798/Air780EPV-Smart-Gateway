"""Managed v0.4 entry: prepare read-only Web, activate once, stop on control EOF."""
import json
import os
from pathlib import Path
import queue
import re
import signal
import sys
import threading
import time

import gateway_runtime as runtime

MAX_FRAME = 64 * 1024


class ControlInput:
    """One bounded reader, interruptible even when the host keeps stdin open."""
    def __init__(self, stream):
        self.fd = stream.fileno()
        os.set_blocking(self.fd, False)
        self.pending = bytearray()

    def read_frame(self, stopped):
        while not stopped.is_set():
            end = self.pending.find(b"\n")
            if end >= 0:
                if end + 1 > MAX_FRAME:
                    raise ValueError("invalid_control_frame")
                raw = bytes(self.pending[:end + 1])
                del self.pending[:end + 1]
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("invalid_control_frame")
                return value
            if len(self.pending) >= MAX_FRAME:
                raise ValueError("invalid_control_frame")
            try:
                raw = os.read(self.fd, min(4096, MAX_FRAME - len(self.pending)))
            except BlockingIOError:
                stopped.wait(0.05)
                continue
            if not raw:
                if self.pending:
                    raise ValueError("invalid_control_frame")
                return None
            self.pending.extend(raw)
        return None


def validate_initialize(value):
    if sys.version_info[:2] != (3, 12):
        raise ValueError("python_abi_mismatch")
    if not value or value.get("type") != "initialize" or value.get("device") is not None:
        raise ValueError("initialize_required")
    identity = value.get("identity")
    fields = ("workspaceId", "pluginId", "packageDigest", "runtimeEpoch", "runtimeId", "ownerLeaseId", "grantVersion")
    if not isinstance(identity, dict) or any(identity.get(k) in (None, "") for k in fields):
        raise ValueError("invalid_runtime_identity")
    if not isinstance(value.get("nonce"), str) or len(value["nonce"]) < 16:
        raise ValueError("invalid_nonce")
    if not isinstance(value.get("allowedPermissions", []), list) or any(not isinstance(p, str) for p in value.get("allowedPermissions", [])):
        raise ValueError("invalid_allowed_permissions")
    if value.get("host", "127.0.0.1") != "127.0.0.1" or type(value.get("port")) is not int or not 1 <= value["port"] <= 65535:
        raise ValueError("invalid_loopback_endpoint")
    base = value.get("webBasePath", "")
    if not re.fullmatch(r"/(?:[A-Za-z0-9_.-]+/)*", base) or any(p in (".", "..") for p in base.split("/")):
        raise ValueError("invalid_base_path")
    code = Path(__file__).resolve().parent
    if Path(value["codeDir"]).resolve() != code:
        raise ValueError("code_directory_mismatch")
    for field in ("dataDir", "cacheDir", "logDir"):
        path = Path(value[field])
        if not path.is_absolute() or not path.is_dir() or path.resolve().is_relative_to(code):
            # Source development may place isolated runtime directories beneath codeDir.
            if not (path.is_absolute() and path.is_dir() and ".runtime" in path.resolve().parts):
                raise ValueError("invalid_runtime_directory")
    credential_file = Path(value["credentialFile"])
    if os.name != "nt" and credential_file.stat().st_mode & 0o077:
        raise ValueError("credential_permissions_invalid")
    if credential_file.stat().st_size > 4096:
        raise ValueError("invalid_credential")
    credential = credential_file.read_text(encoding="utf-8").strip()
    if len(credential) < 24 or any(c.isspace() for c in credential):
        raise ValueError("invalid_credential")
    return {**value, "credential": credential}


def main():
    control = sys.stdout
    sys.stdout = sys.stderr  # Imported business code cannot contaminate NDJSON stdout.
    stopped = threading.Event()
    messages = queue.Queue()
    web = hub = None
    control_thread = None
    threads = []

    def emit(value):
        control.write(json.dumps(value, ensure_ascii=False) + "\n")
        control.flush()

    def launch(target, name):
        def guarded():
            try:
                target()
            except BaseException:
                messages.put({"type": "internal_failure", "code": name + "_failed"})
            finally:
                if not stopped.is_set():
                    messages.put({"type": "internal_failure", "code": name + "_exited"})
        thread = threading.Thread(target=guarded, name=name, daemon=True)
        threads.append(thread)
        thread.start()

    def inputs():
        try:
            while not stopped.is_set():
                frame = control_input.read_frame(stopped)
                if frame is None:
                    stopped.set()
                    return
                messages.put(frame)
        except Exception:
            messages.put({"type": "internal_failure", "code": "invalid_control_frame"})

    def wait_until(predicate):
        deadline = time.monotonic() + 10
        while not predicate():
            if stopped.is_set() or time.monotonic() >= deadline:
                raise RuntimeError("startup_interrupted_or_timeout")
            if not messages.empty():
                frame = messages.get_nowait()
                if frame.get("type") == "internal_failure":
                    raise RuntimeError(frame["code"])
                messages.put(frame)
            time.sleep(0.02)

    try:
        control_input = ControlInput(sys.stdin)
        runtime.configure(validate_initialize(control_input.read_frame(stopped)))
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stopped.set())
        control_thread = threading.Thread(target=inputs, name="control-input")
        control_thread.start()
        from gateway_web import WebServer
        from gateway_hub import GatewayHub
        value = runtime.settings
        ready = {"type": "ready", "identity": value["identity"], "nonce": value["nonce"],
                 "host": "127.0.0.1", "port": value["port"], "basePath": value["webBasePath"],
                 "businessVersion": runtime.BUSINESS_VERSION}
        value["ready"] = ready
        web = WebServer(host="127.0.0.1", port=value["port"])
        launch(web.start, "web")
        wait_until(lambda: web.running)
        emit(ready)
        while not stopped.is_set():
            try:
                frame = messages.get(timeout=0.1)
            except queue.Empty:
                continue
            kind = frame.get("type")
            if kind == "shutdown":
                break
            if kind == "internal_failure":
                raise RuntimeError(frame["code"])
            if kind != "activate" or frame.get("identity") != value["identity"] or frame.get("nonce") != value["nonce"]:
                raise ValueError("invalid_activation_identity")
            if not runtime.active:
                device = frame.get("device")
                if device is not None and (not isinstance(device, dict) or not device.get("deviceId") or not device.get("path")):
                    raise ValueError("invalid_selected_device")
                runtime.active = True
                hub = GatewayHub(host="127.0.0.1", port=0, com=None, device=device)
                launch(hub.start, "hub")
                wait_until(lambda: hub.running)
                web.backend.port = hub.port
                web.backend.start()
            emit({**ready, "type": "active"})
    except Exception as error:
        emit({"type": "fatal", "code": str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__})
        return 1
    finally:
        stopped.set()
        if control_thread is not None:
            control_thread.join()
        runtime.active = False
        if web is not None:
            web.stop()
        if hub is not None:
            hub.stop()
        for thread in threads:
            thread.join(timeout=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

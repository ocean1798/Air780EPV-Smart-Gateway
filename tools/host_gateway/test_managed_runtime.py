"""No-device integration proof of the original Web + Hub managed lifecycle."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import queue
import secrets
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent
OUTPUT = Path(os.environ.get("GATEWAY_TEST_OUTPUT", ROOT / ".runtime" / "native-app-r1" / "tests"))


class Candidate:
    def __init__(self, directory=None):
        OUTPUT.mkdir(parents=True, exist_ok=True)
        self.directory = directory or Path(tempfile.mkdtemp(prefix="run-", dir=OUTPUT))
        for kind in ("data", "cache", "logs"):
            (self.directory / kind).mkdir(exist_ok=True)
        self.credential = secrets.token_urlsafe(32)
        credential_file = self.directory / "credential"
        credential_file.write_text(self.credential, encoding="utf-8")
        credential_file.chmod(0o600)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.identity = dict(workspaceId="test", pluginId="com.smartgateway.cellular", packageDigest="sha256:test",
                             runtimeEpoch="epoch", runtimeId="runtime", ownerLeaseId="lease", grantVersion=1)
        self.nonce = secrets.token_hex(16)
        self.base = "/_plugin-apps/com.smartgateway.cellular/epoch/"
        self.stderr = open(self.directory / "logs" / "stderr.log", "w", encoding="utf-8")
        self.process = subprocess.Popen([sys.executable, "-s", "-m", "gateway_managed"], cwd=ROOT,
                                        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr)
        self.frames = queue.Queue()
        def collect():
            for line in self.process.stdout:
                self.frames.put(json.loads(line))
        threading.Thread(target=collect, daemon=True).start()
        self.send(dict(type="initialize", identity=self.identity, nonce=self.nonce, host="127.0.0.1", port=self.port,
                       codeDir=str(ROOT), dataDir=str(self.directory / "data"), cacheDir=str(self.directory / "cache"),
                       logDir=str(self.directory / "logs"), webBasePath=self.base, credentialFile=str(credential_file)))
        self.ready = self.frames.get(timeout=12)

    def send(self, value):
        self.process.stdin.write((json.dumps(value) + "\n").encode())
        self.process.stdin.flush()

    def request(self, path, body=None, credential=True, absolute=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Authorization": "Bearer " + self.credential} if credential else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn.request("POST" if body is not None else "GET", path if absolute else self.base + path,
                     json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        result = response.status, response.read()
        conn.close()
        return result

    def activate(self, nonce=None):
        self.send(dict(type="activate", identity=self.identity, nonce=nonce or self.nonce, device=None))
        return self.frames.get(timeout=12)

    def close(self):
        if not self.process.stdin.closed:
            self.process.stdin.close()  # EOF is part of the contract, not only explicit shutdown.
        self.process.wait(timeout=8)
        self.process.stdout.close()
        self.stderr.close()


class ManagedRuntimeTests(unittest.TestCase):
    def test_shutdown_with_stdin_still_open(self):
        for active in (False, True):
            with self.subTest(active=active):
                c = Candidate()
                try:
                    self.assertEqual(c.ready["type"], "ready", c.ready)
                    if active:
                        self.assertEqual(c.activate()["type"], "active")
                    c.send({"type": "shutdown"})
                    code = c.process.wait(timeout=8)  # The host still owns the open pipe.
                    self.assertFalse(c.process.stdin.closed)
                    stderr = (c.directory / "logs" / "stderr.log").read_text(encoding="utf-8", errors="replace")
                    self.assertEqual(code, 0, stderr)
                    self.assertNotIn("Fatal Python error", stderr)
                    with socket.socket() as sock:
                        self.assertNotEqual(sock.connect_ex(("127.0.0.1", c.port)), 0)
                finally:
                    c.close()

    @unittest.skipUnless(sys.platform == "linux", "Managed runtime signal contract is Linux")
    def test_sigterm_interrupts_partial_control_frame(self):
        c = Candidate()
        try:
            self.assertEqual(c.ready["type"], "ready", c.ready)
            self.assertEqual(c.activate()["type"], "active")
            c.process.stdin.write(b'{"type":')
            c.process.stdin.flush()
            c.process.send_signal(signal.SIGTERM)
            self.assertEqual(c.process.wait(timeout=8), 0)
            self.assertFalse(c.process.stdin.closed)
            self.assertNotIn("Fatal Python error", (c.directory / "logs" / "stderr.log").read_text())
            with socket.socket() as sock:
                self.assertNotEqual(sock.connect_ex(("127.0.0.1", c.port)), 0)
        finally:
            c.close()

    def test_prepare_activate_repeat_sse_eof_and_persistence(self):
        c = Candidate()
        try:
            self.assertEqual(c.ready["type"], "ready", c.ready)
            self.assertEqual(c.ready["identity"], c.identity)
            self.assertEqual(json.loads(c.request("api/runtime-identity")[1])["phase"], "prepared")
            self.assertEqual(c.request("", credential=False)[0], 401)
            self.assertEqual(c.request("/api/status", absolute=True)[0], 404)
            self.assertEqual(c.request("api/config/notify", {})[0], 409)
            self.assertEqual(list((c.directory / "data").iterdir()), [])
            self.assertIn(b"runtime-config.js", c.request("")[1])
            self.assertEqual(c.request("api/calls")[0], 200)
            first = c.activate()
            self.assertEqual(first["type"], "active")
            self.assertEqual(first, c.activate())
            self.assertEqual(json.loads(c.request("api/runtime-identity")[1])["phase"], "active")
            self.assertFalse(json.loads(c.request("api/status")[1])["online"])
            self.assertEqual(c.request("api/control/fota", {})[0], 501)
            self.assertEqual(c.request("api/config/notify/test", {"channel": "webhook", "config": {"url": "https://invalid.invalid"}})[0], 403)
            saved = c.request("api/config/notify", {"feishu": {"enable": 0, "url": ""}})
            self.assertEqual(saved[0], 200)
            self.assertFalse(json.loads(saved[1])["board_synced"])
            for _ in range(2):
                conn = http.client.HTTPConnection("127.0.0.1", c.port, timeout=3)
                conn.request("GET", c.base + "api/events", headers={"Authorization": "Bearer " + c.credential})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn(b"event: status_update", response.readline())
                response.close()
                conn.close()
            (c.directory / "result.json").write_text(json.dumps({"ready": c.ready, "active": first,
                "configSaved": True, "sseReconnects": 2, "device": None}, indent=2), encoding="utf-8")
        finally:
            c.close()
        self.assertEqual(c.process.returncode, 0)
        with socket.socket() as sock:
            self.assertNotEqual(sock.connect_ex(("127.0.0.1", c.port)), 0)
        before = hashlib.sha256((c.directory / "data" / "gateway_config.json").read_bytes()).hexdigest()
        restarted = Candidate(c.directory)
        try:
            self.assertEqual(restarted.ready["type"], "ready")
            self.assertEqual(restarted.request("api/config/notify")[0], 200)
            self.assertEqual(hashlib.sha256((c.directory / "data" / "gateway_config.json").read_bytes()).hexdigest(), before)
        finally:
            restarted.close()

    def test_wrong_activation_never_starts_business(self):
        c = Candidate()
        try:
            self.assertEqual(c.ready["type"], "ready", c.ready)
            self.assertEqual(c.activate("wrong-nonce")["type"], "fatal")
        finally:
            c.close()
        self.assertEqual(c.process.returncode, 1)
        self.assertEqual(list((c.directory / "data").iterdir()), [])

    def test_business_events_and_selected_device_do_not_scan(self):
        import gateway_runtime as runtime
        OUTPUT.mkdir(parents=True, exist_ok=True)
        target = Path(tempfile.mkdtemp(prefix="frames-", dir=OUTPUT))
        runtime.configure({"dataDir": str(target)})
        import gateway_web
        import gateway_hub
        runtime.active = True
        try:
            backend = gateway_web.HubBackendClient()
            backend._dispatch_frame(json.dumps({"type": "event", "event": "device_connected", "data": {}}))
            self.assertTrue(backend.is_hardware_connected)
            backend._dispatch_frame(json.dumps({"type": "event", "event": "call_rx", "data": {"from": "test-only", "time": "fixture"}}))
            self.assertEqual(json.loads(Path(backend.history_path).read_text())["calls"][0]["phone"], "test-only")
            backend._dispatch_frame(json.dumps({"type": "event", "event": "device_disconnected", "data": {}}))
            self.assertFalse(backend.is_hardware_connected)
            for _ in range(8):
                self.assertIsNotNone(backend.register_sse_listener())
            self.assertIsNone(backend.register_sse_listener())
            with patch.object(gateway_hub, "find_cellular_vuart_port") as scan, patch.object(gateway_hub.serial, "Serial") as opened:
                hub = gateway_hub.GatewayHub(com=None, device=None)
                self.assertFalse(hub._ensure_serial_connected())
                hub.device = {"deviceId": "selected", "path": "/definitely-not-a-device", "identity": {}}
                self.assertFalse(hub._ensure_serial_connected())
                scan.assert_not_called()
                opened.assert_not_called()
            with patch.object(gateway_hub.urllib.request, "urlopen") as external:
                hub._dispatch_host_proxy_push("sms_rx", {"content": "test only"})
                external.assert_not_called()
        finally:
            runtime.managed, runtime.active, runtime.settings = False, True, {}


class SmsRealtimeTests(unittest.TestCase):
    def test_numeric_event_and_legacy_cache_keep_inbox_order(self):
        import gateway_runtime as runtime
        import gateway_web

        OUTPUT.mkdir(parents=True, exist_ok=True)
        target = Path(tempfile.mkdtemp(prefix="sms-realtime-", dir=OUTPUT))
        timestamp = 1789437600
        old_items = [{"from": f"fixture-old-{i}", "content": "synthetic stored message",
                      "time": timestamp - 86400 + i} for i in range(15)]
        history_path = target / "gateway_history.json"
        history_path.write_text(json.dumps({"sms": [old_items[0]], "calls": []}), encoding="utf-8")
        peers = queue.Queue()
        requests = []

        class HubHandler(socketserver.StreamRequestHandler):
            def handle(self):
                json.loads(self.rfile.readline())  # Isolated managed fixture authentication.
                peers.put(self.request)
                for line in self.rfile:
                    command = json.loads(line)
                    requests.append(command["cmd"])
                    reply = {"type": "res", "id": command["id"], "code": 0 if len(requests) == 1 else 1,
                             "data": {"items": old_items, "total": len(old_items)}}
                    self.wfile.write((json.dumps(reply) + "\n").encode())
                    self.wfile.flush()

        class HubServer(socketserver.ThreadingTCPServer):
            daemon_threads = True

        with patch.object(runtime, "managed", True), patch.object(runtime, "active", True), \
                patch.object(runtime, "settings", {"credential": "fixture-only"}), \
                patch.object(gateway_web, "DATA_DIR", str(target)):
            hub = HubServer(("127.0.0.1", 0), HubHandler)
            hub_thread = threading.Thread(target=hub.serve_forever, daemon=True)
            hub_thread.start()
            backend = gateway_web.HubBackendClient(port=hub.server_address[1])
            handler = type("FixtureHandler", (gateway_web.GatewayWebHandler,), {"backend": backend})
            web = gateway_web.ThreadedHTTPServer(("127.0.0.1", 0), handler)
            web.gateway_running = True
            web_thread = threading.Thread(target=web.serve_forever, daemon=True)
            web_thread.start()
            stream = response = peer = None

            def history():
                conn = http.client.HTTPConnection("127.0.0.1", web.server_port, timeout=3)
                try:
                    conn.request("GET", "/api/history", headers={"Authorization": "Bearer fixture-only"})
                    result = conn.getresponse()
                    self.assertEqual(result.status, 200)
                    return json.loads(result.read())
                finally:
                    conn.close()

            def event():
                name = response.readline().decode().strip().removeprefix("event: ")
                data = json.loads(response.readline().removeprefix(b"data: "))
                self.assertEqual(response.readline().strip(), b"")
                return {"event": name, "data": data}

            try:
                backend.start()
                peer = peers.get(timeout=2)
                initial = history()["data"]["list"]
                self.assertTrue(all(isinstance(item["time"], str) for item in initial))
                stream = http.client.HTTPConnection("127.0.0.1", web.server_port, timeout=3)
                stream.request("GET", "/api/events", headers={"Authorization": "Bearer fixture-only"})
                response = stream.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(event()["event"], "status_update")
                peer.sendall((json.dumps({"type": "event", "event": "sms_rx", "data": {
                    "from": "fixture-new", "content": "synthetic incoming message", "time": timestamp}}) + "\n").encode())
                incoming = event()
                self.assertEqual(incoming["event"], "sms_received")
                expected = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
                self.assertEqual(incoming["data"]["time"], expected)
                saved = history_path.read_bytes()
                self.assertEqual(json.loads(saved)["sms"][0]["time"], expected)
                fallback = history()
                self.assertTrue(fallback["cached"])
                self.assertTrue(all(isinstance(item["time"], str) for item in fallback["data"]["list"]))
                self.assertEqual(history_path.read_bytes(), saved, "Read normalization must not rewrite private history")
                self.assertIsInstance(backend.recent_sms_events[-1]["time"], int)

                # Run the original page's actual SSE handler and renderer; synthetic DOM only.
                js = r'''
const fs=require('fs'),vm=require('vm');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const source=fs.readFileSync(process.argv[1],'utf8').match(/<script>\s*([\s\S]*?)<\/script>/)[1];
function node(){return {children:[],style:{},classList:{add(){},remove(){},toggle(){}},appendChild(child){this.children.push(child)},set innerHTML(value){this.html=value;this.children=[]},get innerHTML(){return this.html||''},textContent:'',checked:false,disabled:false,setAttribute(){},removeAttribute(){},addEventListener(){},remove(){}}}
const nodes=new Map(),listeners={},errors=[];
const context={window:{GATEWAY_RUNTIME:{basePath:'/',businessVersion:'1.3.0'},addEventListener(){}},document:{title:'fixture',getElementById(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id)},createElement:node,querySelectorAll(){return []}},localStorage:{getItem(){return null}},console:{log(){},error(...x){errors.push(x.map(String).join(' '))}},setTimeout(){return 0},clearTimeout(){},setInterval(){return 0},EventSource:class {addEventListener(name,fn){listeners[name]=fn}},Date,JSON,Intl,URL,URLSearchParams};
vm.createContext(context);vm.runInContext(source,context);context.initial=input.initial;
vm.runInContext('state.smsList=initial;state.smsTotal=15;renderSmsList();setupEventSource();',context);
listeners[input.incoming.event]({data:JSON.stringify(input.incoming.data)});
function index(){return nodes.get('smsContainer').children.filter(x=>x.className==='sms-item').findIndex(x=>x.innerHTML.includes('fixture-new'))}
const descendingIndex=index();
vm.runInContext('state.sortOrder="asc";renderSmsList();',context);
const ascendingIndex=index(),count=vm.runInContext('state.smsList.length',context),order=vm.runInContext('state.sortOrder',context);
process.stdout.write(JSON.stringify({descendingIndex,ascendingIndex,count,order,errors}));
if(descendingIndex!==0||ascendingIndex!==15||count!==16||order!=='asc'||errors.length)process.exit(1);
'''
                rendered = subprocess.run(["node", "-e", js, str(ROOT / "web" / "index.html")],
                    input=json.dumps({"initial": initial, "incoming": incoming}), text=True, capture_output=True, timeout=10)
                self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
                for time_fields in ({"ts": timestamp}, {"time": expected}):
                    peer.sendall((json.dumps({"type": "event", "event": "sms_rx", "data": {
                        "from": "fixture-alias", "content": "synthetic time variant", **time_fields}}) + "\n").encode())
                    self.assertEqual(event()["data"]["time"], expected)
                result = {"initial": initial, "incoming": incoming, "render": json.loads(rendered.stdout),
                          "managedPersistence": True, "legacyCacheNormalizedWithoutRewrite": True,
                          "timeVariants": ["integer", "ts_alias", "formatted_string"],
                          "commands": requests, "realSerialOpened": False}
                (target / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
                print("SMS_REALTIME_EVIDENCE=" + str(target / "result.json"))
                self.assertEqual(requests, ["get_history", "get_history"])
            finally:
                web.gateway_running = False
                backend.broadcast_sse("test_finished", {})
                if response is not None:
                    for _ in range(128):
                        line = response.readline()
                        if not line or line.strip() == b"event: test_finished":
                            break
                    response.readline()
                    response.readline()
                    response.close()
                if stream is not None:
                    stream.close()
                if peer is not None:
                    peer.shutdown(socket.SHUT_RDWR)
                backend.stop()
                if backend.rx_thread:
                    backend.rx_thread.join(timeout=2)
                web.shutdown()
                web.server_close()
                hub.shutdown()
                hub.server_close()
                web_thread.join(timeout=2)
                hub_thread.join(timeout=2)


class StatusRecoveryTests(unittest.TestCase):
    def test_http_timeout_then_refresh_recovers_without_serial_reopen(self):
        import gateway_runtime as runtime
        import gateway_web

        requests = []
        clients = []
        streams = []

        class HubHandler(socketserver.StreamRequestHandler):
            def handle(self):
                clients.append(self.request)
                self.wfile.write(b'{"type":"event","event":"status","data":{"online":true}}\n')
                self.wfile.flush()
                for line in self.rfile:
                    command = json.loads(line)
                    requests.append(command)
                    if len(requests) == 1:
                        continue  # One missing reply; the fixture Hub and connection remain online.
                    if len(requests) == 4:
                        reply = {"type": "response", "id": command["id"], "ok": False,
                                 "online": False, "error": "fixture_device_disconnected"}
                    else:
                        reply = {"type": "res", "id": command["id"], "code": 0,
                                 "data": {"bsp": "Air780EPV", "version": "fixture", "csq": 25}}
                    self.wfile.write((json.dumps(reply) + "\n").encode())
                    self.wfile.flush()

        class HubServer(socketserver.ThreadingTCPServer):
            daemon_threads = True

        with patch.object(runtime, "managed", False), patch.object(runtime, "settings", {}):
            hub = HubServer(("127.0.0.1", 0), HubHandler)
            hub_thread = threading.Thread(target=hub.serve_forever, daemon=True)
            hub_thread.start()
            backend = gateway_web.HubBackendClient(port=hub.server_address[1])
            handler = type("FixtureHandler", (gateway_web.GatewayWebHandler,), {"backend": backend})
            web = gateway_web.ThreadedHTTPServer(("127.0.0.1", 0), handler)
            web.gateway_running = True
            web_thread = threading.Thread(target=web.serve_forever, daemon=True)
            web_thread.start()

            def status():
                conn = http.client.HTTPConnection("127.0.0.1", web.server_port, timeout=5)
                try:
                    conn.request("GET", "/api/status")
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    return json.loads(response.read())
                finally:
                    conn.close()

            def initial_sse():
                conn = http.client.HTTPConnection("127.0.0.1", web.server_port, timeout=3)
                try:
                    conn.request("GET", "/api/events")
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.readline().strip(), b"event: status_update")
                    snapshot = json.loads(response.readline().removeprefix(b"data: "))
                    streams.append((conn, response))
                    return snapshot
                except BaseException:
                    conn.close()
                    raise

            listener = backend.register_sse_listener()
            try:
                backend.start()
                deadline = time.monotonic() + 2
                while not backend.is_hardware_connected and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(backend.is_hardware_connected)
                while not listener.empty():
                    listener.get_nowait()

                timed_out = status()
                self.assertFalse(timed_out["ok"])
                self.assertIsNone(timed_out["online"])
                self.assertIn("超时", timed_out["error"])
                self.assertTrue(backend.is_hardware_connected, "A missing reply cannot prove physical disconnect")
                self.assertIsNone(listener.get(timeout=1)["data"]["online"])
                self.assertIsNone(initial_sse()["online"])

                recovered = status()
                self.assertTrue(recovered["ok"])
                self.assertTrue(recovered["online"])
                self.assertEqual(recovered["data"]["csq"], 25)
                self.assertTrue(listener.get(timeout=1)["data"]["online"])
                self.assertTrue(initial_sse()["online"])
                self.assertNotIn("error", backend.latest_status)

                # Reproduce a legacy false Web flag while the same Hub remains usable.
                backend._update_status_cache({"online": False})
                self.assertTrue(status()["ok"])
                self.assertTrue(backend.is_hardware_connected)
                disconnected = status()
                self.assertFalse(disconnected["online"])
                self.assertEqual(disconnected["error"], "fixture_device_disconnected")
                self.assertFalse(backend.is_hardware_connected)
                self.assertFalse(initial_sse()["online"])
                self.assertTrue(status()["online"])
                self.assertEqual([r["cmd"] for r in requests], ["get_status"] * 5)
                self.assertEqual(len({r["id"] for r in requests}), 5)
                self.assertEqual(len(clients), 1, "Recovery must reuse the existing Hub connection")
            finally:
                web.gateway_running = False
                backend.broadcast_sse("test_finished", {})
                for conn, response in streams:
                    try:
                        for _ in range(128):
                            line = response.readline()
                            if not line or line.strip() == b"event: test_finished":
                                break
                        response.readline()
                        response.readline()
                    except (TimeoutError, OSError):
                        # Shutdown can end the SSE handler before the final fixture event.
                        pass
                    finally:
                        response.close()
                        conn.close()
                for client in clients:
                    try:
                        client.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                backend.stop()
                if backend.rx_thread:
                    backend.rx_thread.join(timeout=2)
                web.shutdown()
                web.server_close()
                hub.shutdown()
                hub.server_close()
                web_thread.join(timeout=2)
                hub_thread.join(timeout=2)


class UpstreamParityTests(unittest.TestCase):
    """Original HTTP/SSE/Hub/page paths with synthetic data and no serial or OS side effects."""

    def setUp(self):
        import gateway_runtime as runtime
        import gateway_web
        import gateway_hub
        self.web_module, self.hub_module = gateway_web, gateway_hub
        OUTPUT.mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="parity-09-", dir=OUTPUT))
        self.config = self.directory / "gateway_config.json"
        self.patches = [patch.object(runtime, "managed", True), patch.object(runtime, "active", True),
                        patch.object(runtime, "settings", {"credential": "fixture-only"}),
                        patch.object(gateway_web, "DATA_DIR", str(self.directory)),
                        patch.object(gateway_web, "GATEWAY_CONFIG_PATH", str(self.config)),
                        patch.object(gateway_hub, "GATEWAY_CONFIG_PATH", str(self.config)),
                        patch.object(gateway_web, "_log"), patch.object(gateway_hub, "log")]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.backend = gateway_web.HubBackendClient()
        self.backend.execute_cmd = Mock(return_value={"ok": True, "data": {}})
        self.backend.send_raw_command = Mock()
        handler = type("ParityHandler", (gateway_web.GatewayWebHandler,), {"backend": self.backend})
        self.web = gateway_web.ThreadedHTTPServer(("127.0.0.1", 0), handler)
        self.web.gateway_running = True
        self.thread = threading.Thread(target=self.web.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.web.gateway_running = False
        self.backend.broadcast_sse("test_finished", {})
        self.web.shutdown()
        self.web.server_close()
        self.thread.join(timeout=2)

    def request(self, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.web.server_port, timeout=3)
        try:
            conn.request("GET" if body is None else "POST", path,
                         None if body is None else json.dumps(body),
                         {"Authorization": "Bearer fixture-only", "Content-Type": "application/json"})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            return json.loads(response.read())
        finally:
            conn.close()

    def frame(self, name, data):
        self.backend._dispatch_frame(json.dumps({"type": "event", "event": name, "data": data}))

    def page(self, actions):
        # Same original-page VM technique as SmsRealtimeTests; no browser/clipboard API exposed.
        js = r'''
const fs=require('fs'),vm=require('vm'),actions=JSON.parse(fs.readFileSync(0,'utf8'));
const source=fs.readFileSync(process.argv[1],'utf8').match(/<script>\s*([\s\S]*?)<\/script>/)[1];
function node(){return {children:[],style:{},classList:{add(){},remove(){},toggle(){}},appendChild(x){this.children.push(x)},set innerHTML(v){this.html=v;this.children=[]},get innerHTML(){return this.html||''},textContent:'',checked:false,disabled:false,setAttribute(){},removeAttribute(){},addEventListener(){},remove(){}}}
const nodes=new Map(),listeners={},errors=[],context={window:{GATEWAY_RUNTIME:{basePath:'/',businessVersion:'1.3.0'},addEventListener(){}},document:{title:'fixture',getElementById(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id)},createElement:node,querySelectorAll(){return []}},localStorage:{getItem(){return null}},console:{log(){},error(){errors.push('page-error')}},setTimeout(){return 0},clearTimeout(){},setInterval(){return 0},EventSource:class{addEventListener(n,f){listeners[n]=f}},Date,JSON,Intl,URL,URLSearchParams};
vm.createContext(context);vm.runInContext(source,context);vm.runInContext('setupEventSource()',context);
const views=[];
for(const action of actions){context.payload=action.data;if(action.event)listeners[action.event]({data:JSON.stringify(action.data)});else vm.runInContext('renderStatus(payload)',context);views.push({phone:nodes.get('simPhoneVal').textContent,version:nodes.get('fotaCurrentVersionBadge').textContent});}
process.stdout.write(JSON.stringify({views,errors,calls:(nodes.get('callList')?.children||[]).map(x=>x.innerHTML)}));
'''
        result = subprocess.run(["node", "-e", js, str(ROOT / "web" / "index.html")],
                                input=json.dumps(actions).encode("utf-8"), capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(result["errors"], [])
        for action in actions:
            if action.get("event") == "call_incoming":
                self.assertTrue(any(action["data"]["time"] in row for row in result["calls"]))
        return result["views"]

    def test_status_http_sse_initial_incremental_and_clear(self):
        from http.client import HTTPConnection
        listener = self.backend.register_sse_listener()
        self.backend.execute_cmd.return_value = {"ok": True, "data": {
            "bsp": "Air780EPV", "number": "+8613800000000", "version": "fixture-09", "csq": 25,
            "blackbox_count": 3, "uptime_seconds": 42, "rndis_enable": True}}
        http = self.request("/api/status")["data"]
        broadcast = listener.get(timeout=1)["data"]
        self.assertEqual(http, broadcast)
        self.assertEqual(http["raw"]["number"], http["number"])
        self.assertEqual((http["model"], http["sms_count"], http["uptime"], http["rndis"]), ("Air780EPV", 3, 42, True))
        conn = HTTPConnection("127.0.0.1", self.web.server_port, timeout=3)
        conn.connect()
        client_socket = conn.sock
        conn.request("GET", "/api/events", headers={"Authorization": "Bearer fixture-only"})
        response = conn.getresponse()
        try:
            self.assertEqual(response.readline().strip(), b"event: status_update")
            initial = json.loads(response.readline().removeprefix(b"data: "))
            self.assertEqual(initial, http)
        finally:
            client_socket.shutdown(socket.SHUT_WR)
            response.close()
            conn.close()
        self.frame("status", {"online": True, "csq": 26})
        incremental = listener.get(timeout=1)["data"]
        self.assertEqual(incremental["number"], http["number"])
        self.assertEqual(incremental["version"], http["version"])
        self.backend.execute_cmd.return_value = {"ok": True, "data": {"csq": 27}}
        missing = self.request("/api/status")["data"]
        self.assertEqual(missing, listener.get(timeout=1)["data"])
        self.assertEqual(missing["number"], http["number"])
        for order in ((http, broadcast), (broadcast, http)):
            views = self.page([{"data": order[0]}, {"event": "status_update", "data": order[1]},
                               {"event": "status_update", "data": initial},
                               {"event": "status_update", "data": incremental}, {"data": missing}])
            self.assertTrue(all(v == views[0] for v in views))
            self.assertEqual(views[0], {"phone": "138 0000 0000", "version": "vfixture-09 就绪"})
        unknown = self.backend._update_status_cache({"online": None, "error": "fixture timeout"})
        self.assertEqual(unknown["number"], http["number"])
        self.assertTrue(self.backend.is_hardware_connected)
        for empty in ("", None, "invalid", False):
            cleared = self.backend._update_status_cache({"online": True, "number": empty})
            self.assertEqual(cleared["raw"]["number"], "")
            self.assertEqual(self.page([{"data": http}, {"data": cleared}])[-1]["phone"], "未读出号码")
        self.backend._update_status_cache(http)
        disconnected = self.backend._update_status_cache({"online": False})
        self.assertEqual(disconnected["number"], "")
        self.assertEqual(self.page([{"data": http}, {"data": disconnected}])[-1]["phone"], "--")
        self.backend._update_status_cache(http)
        self.frame("device_connected", {"port": "fixture-new-device"})
        fresh = dict(self.backend.latest_status)
        self.assertEqual((fresh["number"], fresh["version"]), ("", ""))
        self.assertEqual(self.page([{"data": http}, {"event": "device_connected", "data": fresh}])[-1],
                         {"phone": "未读出号码", "version": "--"})
        self.assertEqual(self.web_module.HubBackendClient().latest_status["number"], "")
        self.backend._update_status_cache(http)
        self.frame("gateway_ready", {"version": "fixture-new-boot"})
        self.assertEqual(self.backend.latest_status["number"], "")
        self.assertTrue(self.backend.latest_status["online"])
        (self.directory / "result.json").write_text(json.dumps({"httpSseInitialEqual": True,
            "bothArrivalOrdersStable": True, "absenceRetained": True, "explicitEmptyDisconnectNewConnectionCleared": True}), encoding="utf-8")

    def test_notify_save_reload_restart_and_copy_preference(self):
        self.config.write_text(json.dumps({"system": {"auto_copy_otp": True, "fixture_other": "keep"},
                                          "bark": {"enable": 0, "group": "fixture-keep"}}), encoding="utf-8")
        hub = self.hub_module.GatewayHub()
        hub._broadcast = Mock()
        self.backend.send_raw_command.side_effect = hub._handle_client_send
        def execute(cmd, params=None, **kwargs):
            self.assertEqual(cmd, "set_notify_config")
            hub._handle_client_send(json.dumps({"cmd": cmd, "data": params, "id": "fixture"}))
            return {"ok": False, "error": "fixture has no serial"}
        self.backend.execute_cmd.side_effect = execute
        with patch.object(self.hub_module, "set_windows_clipboard", return_value=True) as clipboard, \
                patch.object(self.hub_module, "show_windows_toast") as toast, \
                patch.object(hub, "_dispatch_host_proxy_push"):
            for flag, count in ((False, 0), (True, 1), (0, 0)):
                clipboard.reset_mock()
                result = self.request("/api/config/notify", {"system": {"auto_copy_otp": flag}})
                self.assertFalse(result["board_synced"])
                self.assertEqual(self.request("/api/config/notify")["data"]["system"]["auto_copy_otp"], flag)
                self.assertEqual(hub.notify_config["system"], {"auto_copy_otp": flag, "fixture_other": "keep"})
                self.assertEqual(hub.notify_config["bark"]["group"], "fixture-keep")
                self.assertEqual(self.hub_module.GatewayHub().notify_config["system"]["auto_copy_otp"], flag)
                hub._process_incoming_frame({"type": "event", "event": "sms_rx", "data": {
                    "code": "123456", "from": "fixture", "content": "synthetic"}})
                self.assertEqual(clipboard.call_count, count)
            self.assertIsNone(hub.ser)

    def test_call_event_and_legacy_output_do_not_rewrite_cache(self):
        timestamp = 1789437600
        expected = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
        listener = self.backend.register_sse_listener()
        for fields in ({"time": timestamp}, {"ts": timestamp}, {"time": expected}):
            self.frame("call_rx", {"from": "fixture", **fields})
            event = listener.get(timeout=1)
            self.assertEqual(event["data"]["time"], expected)
            self.page([{"data": {"online": True, "version": ""}}, event])
        legacy = {"phone": "fixture-old", "time": timestamp}
        history = Path(self.backend.history_path)
        history.write_text(json.dumps({"sms": [], "calls": [legacy]}), encoding="utf-8")
        self.backend.recent_calls = [legacy]
        before = history.read_bytes()
        calls = self.request("/api/calls")["data"]["list"]
        self.assertEqual(calls[0]["time"], expected)
        self.assertEqual(history.read_bytes(), before)
        self.assertEqual(legacy["time"], timestamp)

    def test_history_cursor_whitelist_and_limit(self):
        from urllib.parse import quote
        for cursor in ("", "0", "null", "undefined", "bad", "h:1:-2", "h:１:２", " h:1:2", "h:1:2\n", "h:7:0"):
            self.request("/api/history?limit=50&cursor=" + quote(cursor))
            cmd, params = self.backend.execute_cmd.call_args.args
            self.assertEqual(cmd, "get_history")
            self.assertEqual(params["limit"], 15)
            self.assertEqual(params.get("cursor"), "h:7:0" if cursor == "h:7:0" else None)


class SmsSendLifecycleTests(unittest.TestCase):
    """Verify two-stage SMS send lifecycle, queued-not-popped behavior, error mappings and recovery."""

    def setUp(self):
        import gateway_runtime as runtime
        import gateway_web
        self.web_module = gateway_web
        self.backend = gateway_web.HubBackendClient()
        self.backend._ensure_connected = Mock(return_value=True)
        self.backend.sock = Mock()
        self.backend.broadcast_sse = Mock()

    def test_wait_terminal_does_not_pop_on_queued_and_resolves_on_sent_ok(self):
        req_id = "test_req_01"
        evt = threading.Event()
        entry = {"event": evt, "response": None, "wait_terminal": True, "cmd": "send_sms"}
        self.backend.pending_requests[req_id] = entry

        # First frame: QUEUED acceptance
        frame_queued = json.dumps({"type": "res", "id": req_id, "code": 0, "msg": "QUEUED", "data": {"queue_position": 0}})
        self.backend._dispatch_frame(frame_queued)

        # Must still be pending, event not yet set, queued marked true
        self.assertIn(req_id, self.backend.pending_requests)
        self.assertFalse(evt.is_set())
        self.assertTrue(entry.get("queued"))
        self.backend.broadcast_sse.assert_called_with("sms_status", {"id": req_id, "state": "QUEUED", "data": {"queue_position": 0}})

        # Second frame: terminal SENT_OK
        frame_ok = json.dumps({"type": "res", "id": req_id, "code": 0, "msg": "SENT_OK", "data": {"success": True}})
        self.backend._dispatch_frame(frame_ok)

        # Must now be popped and event set with ok=True
        self.assertNotIn(req_id, self.backend.pending_requests)
        self.assertTrue(evt.is_set())
        self.assertTrue(entry["response"]["ok"])
        self.assertEqual(entry["response"]["msg"], "SENT_OK")

    def test_immediate_failure_frame_pops_even_with_wait_terminal(self):
        req_id = "test_req_fail"
        evt = threading.Event()
        entry = {"event": evt, "response": None, "wait_terminal": True, "cmd": "send_sms"}
        self.backend.pending_requests[req_id] = entry

        # Error frame (-409 SMS_RESULT_UNKNOWN)
        frame_err = json.dumps({"type": "res", "id": req_id, "code": -409, "msg": "SMS_RESULT_UNKNOWN",
                                "data": {"reason": "previous_modem_result_pending"}})
        self.backend._dispatch_frame(frame_err)

        self.assertNotIn(req_id, self.backend.pending_requests)
        self.assertTrue(evt.is_set())
        self.assertFalse(entry["response"]["ok"])
        self.assertEqual(entry["response"]["code"], -409)

    def test_error_formatting_preserves_specific_reasons(self):
        formatter = self.web_module.GatewayWebHandler._format_error_message

        # -409上一条未决
        msg_409 = formatter({"code": -409, "msg": "SMS_RESULT_UNKNOWN", "data": {"reason": "previous_modem_result_pending"}})
        self.assertIn("上一条短信发送结果未决", msg_409)
        self.assertNotIn("超时", msg_409)

        # -429队列满
        msg_429 = formatter({"code": -429, "msg": "QUEUE_FULL"})
        self.assertIn("队列已满", msg_429)

        # -101参数错误
        msg_101 = formatter({"code": -101, "msg": "PARAM_ERR"})
        self.assertIn("参数错误", msg_101)

        # -102发射失败
        msg_102 = formatter({"code": -102, "msg": "SEND_FAILED"})
        self.assertIn("射频发射失败", msg_102)

        # -1基站失败
        msg_1 = formatter({"code": -1, "msg": "SENT_FAILED"})
        self.assertIn("基站发送失败", msg_1)

        # -408超时未决
        msg_408 = formatter({"code": -408, "msg": "UNKNOWN", "data": {"reason": "modem_result_timeout"}})
        self.assertIn("结果未知", msg_408)

    def test_consecutive_and_queued_requests_resolve_independently(self):
        # Test two concurrent in-flight SMS requests
        req1 = "req_sms_1"
        req2 = "req_sms_2"
        evt1 = threading.Event()
        evt2 = threading.Event()
        self.backend.pending_requests[req1] = {"event": evt1, "response": None, "wait_terminal": True}
        self.backend.pending_requests[req2] = {"event": evt2, "response": None, "wait_terminal": True}

        # req1 gets QUEUED
        self.backend._dispatch_frame(json.dumps({"type": "res", "id": req1, "code": 0, "msg": "QUEUED"}))
        self.assertIn(req1, self.backend.pending_requests)
        self.assertFalse(evt1.is_set())

        # req2 gets QUEUED (queue_position 1)
        self.backend._dispatch_frame(json.dumps({"type": "res", "id": req2, "code": 0, "msg": "QUEUED", "data": {"queue_position": 1}}))
        self.assertIn(req2, self.backend.pending_requests)
        self.assertFalse(evt2.is_set())

        # req1 finishes SENT_OK
        self.backend._dispatch_frame(json.dumps({"type": "res", "id": req1, "code": 0, "msg": "SENT_OK", "data": {"success": True}}))
        self.assertNotIn(req1, self.backend.pending_requests)
        self.assertTrue(evt1.is_set())
        self.assertIn(req2, self.backend.pending_requests)
        self.assertFalse(evt2.is_set())

        # req2 finishes SENT_OK
        self.backend._dispatch_frame(json.dumps({"type": "res", "id": req2, "code": 0, "msg": "SENT_OK", "data": {"success": True}}))
        self.assertNotIn(req2, self.backend.pending_requests)
        self.assertTrue(evt2.is_set())

    def test_http_api_sms_send_and_reset(self):
        # Test HTTP layer endpoints
        handler_cls = type("TestHandler", (self.web_module.GatewayWebHandler,), {"backend": self.backend})
        server = self.web_module.ThreadedHTTPServer(("127.0.0.1", 0), handler_cls)
        server.gateway_running = True
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)

            # 1. Success case
            self.backend.execute_cmd = Mock(return_value={"ok": True, "code": 0, "msg": "SENT_OK", "data": {"success": True}})
            conn.request("POST", "/api/sms/send", json.dumps({"phone": "10010", "content": "CXLL"}),
                         {"Content-Type": "application/json"})
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            data = json.loads(res.read())
            self.assertTrue(data["ok"])
            self.assertEqual(data["status"], "sent")
            self.backend.execute_cmd.assert_called_with(
                "send_sms", {"to": "10010", "text": "CXLL", "phone": "10010", "content": "CXLL"},
                timeout=12.0, wait_terminal=True
            )

            # 2. Rejection with -409
            self.backend.execute_cmd = Mock(return_value={"ok": False, "code": -409, "msg": "SMS_RESULT_UNKNOWN",
                                                          "data": {"reason": "previous_modem_result_pending"}})
            conn.request("POST", "/api/sms/send", json.dumps({"phone": "10010", "content": "CXLL"}),
                         {"Content-Type": "application/json"})
            res = conn.getresponse()
            self.assertEqual(res.status, 500)
            err_data = json.loads(res.read())
            self.assertFalse(err_data["ok"])
            self.assertEqual(err_data["code"], -409)
            self.assertIn("上一条短信发送结果未决", err_data["error"])

            # 3. Reset SMS recovery endpoint
            self.backend.execute_cmd = Mock(return_value={"ok": True, "data": {"status": "sms_queue_cleared"}})
            conn.request("POST", "/api/sms/reset", "{}", {"Content-Type": "application/json"})
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            reset_data = json.loads(res.read())
            self.assertTrue(reset_data["ok"])
            self.assertEqual(reset_data["msg"], "短信发送状态已重置")
            self.backend.execute_cmd.assert_called_with("reset_sms", {}, timeout=5.0)

            conn.close()
        finally:
            server.gateway_running = False
            server.shutdown()
            server.server_close()
            t.join(timeout=2)


class HardwareIdentityAndTraceabilityTests(unittest.TestCase):
    """Verify dynamic device model identification, unique IMEI formatting, and notification footer traceability."""

    def test_format_device_desc_variations(self):
        import gateway_hub
        hub = gateway_hub.GatewayHub.__new__(gateway_hub.GatewayHub)
        hub.latest_status = {"online": True, "model": "Air780EC", "bsp": "EC618", "imei": "862534065678901"}

        # 1. Direct from latest_status
        desc1 = hub._format_device_desc()
        self.assertEqual(desc1, "Air780EC · IMEI: 862534065678901")

        # 2. With slot_tag
        desc2 = hub._format_device_desc(slot_tag="Slot 1")
        self.assertEqual(desc2, "[Slot 1] Air780EC · IMEI: 862534065678901")

        # 3. Overridden by incoming event data
        data_override = {"model": "Air780EPV", "imei": "864509071234567"}
        desc3 = hub._format_device_desc(data_override, slot_tag="Slot 2")
        self.assertEqual(desc3, "[Slot 2] Air780EPV · IMEI: 864509071234567")

        # 4. Fallback without IMEI
        hub.latest_status = {"online": True, "model": "Air780E"}
        desc4 = hub._format_device_desc({})
        self.assertEqual(desc4, "Air780E")

    def test_test_channel_push_payload_includes_dynamic_device_desc(self):
        import gateway_hub
        intercepted = {}

        def mock_urlopen(req, timeout=None):
            body = req.data.decode("utf-8") if isinstance(req.data, bytes) else str(req.data)
            intercepted["url"] = req.full_url
            intercepted["body"] = json.loads(body)
            mock_resp = Mock()
            mock_resp.status = 200
            mock_resp.read.return_value = b'{"code":0,"msg":"success"}'
            mock_resp.__enter__ = Mock(return_value=mock_resp)
            mock_resp.__exit__ = Mock(return_value=False)
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res = gateway_hub.test_channel_push(
                "feishu",
                {"url": "https://open.feishu.cn/mock", "secret": ""},
                device_desc="[Slot 1] Air780EC · IMEI: 862534065678901"
            )
            self.assertTrue(res["ok"])
            elements = intercepted["body"]["card"]["body"]["elements"]
            footer = elements[-1]["text"]["content"]
            self.assertIn("[Slot 1] Air780EC · IMEI: 862534065678901", footer)
            self.assertNotIn("Air780EPV", footer)


class DataLifecycleAndCompartmentTests(unittest.TestCase):
    """AIR-27 数据全生命周期闭环与多卡分舱测试"""

    def test_operator_derivation_and_badge(self):
        from storage_manager import derive_operator_and_badge
        # 1. 中国联通 + 手机号
        r1 = derive_operator_and_badge("89860125801523307793", "+8613243832515")
        self.assertEqual(r1["operator"], "中国联通")
        self.assertEqual(r1["badge_text"], "📱 联通 · 2515")
        self.assertEqual(r1["tail"], "2515")

        # 2. 中国移动 (无手机号，自动截取 ICCID 尾号)
        r2 = derive_operator_and_badge("89860012345678909876", None)
        self.assertEqual(r2["operator"], "中国移动")
        self.assertEqual(r2["badge_text"], "📱 移动 · 9876")
        self.assertEqual(r2["tail"], "9876")

        # 3. 中国电信
        r3 = derive_operator_and_badge("89860312345678901122", "")
        self.assertEqual(r3["operator"], "中国电信")
        self.assertEqual(r3["badge_text"], "📱 电信 · 1122")

        # 4. 中国广电
        r4 = derive_operator_and_badge("89861512345678903344", "")
        self.assertEqual(r4["operator"], "中国广电")
        self.assertEqual(r4["badge_text"], "📱 广电 · 3344")

    def test_tombstone_prevents_deleted_sms_from_resurrecting(self):
        import tempfile
        from storage_manager import StorageManager
        with tempfile.TemporaryDirectory() as tmp_dir:
            mgr = StorageManager(tmp_dir)
            iccid = "89860125801523307793"
            comp = mgr.get_compartment(iccid)

            msg_id = "boot_1_1789480000_sms_1"
            msg = {
                "id": msg_id,
                "phone": "10010",
                "content": "验证码 123456",
                "time": "2026-09-15 12:00:00"
            }
            # 1. 正常存入
            self.assertTrue(comp.append_message(msg))
            self.assertEqual(len(comp.load_messages()), 1)

            # 2. 加入墓碑
            comp.add_tombstone(msg_id, "10010", "验证码 123456", "2026-09-15 12:00:00")
            self.assertTrue(comp.is_deleted(msg_id))
            # messages.json 中已剔除
            self.assertEqual(len(comp.load_messages()), 0)

            # 3. 再次拉取或追加已被墓碑标记的同一短信，直接静默拦截，杜绝死而复生
            self.assertFalse(comp.append_message(msg))

    def test_compartment_physical_isolation(self):
        import tempfile
        from storage_manager import StorageManager
        with tempfile.TemporaryDirectory() as tmp_dir:
            mgr = StorageManager(tmp_dir)
            iccid_a = "89860100000000000001"
            iccid_b = "89860000000000000002"

            comp_a = mgr.get_compartment(iccid_a)
            comp_b = mgr.get_compartment(iccid_b)

            msg_a = {"id": "sms_a", "phone": "10010", "content": "联通消息"}
            msg_b = {"id": "sms_b", "phone": "10086", "content": "移动消息"}

            comp_a.append_message(msg_a)
            comp_b.append_message(msg_b)

            # 物理路径严格隔离
            self.assertTrue(os.path.isdir(os.path.join(tmp_dir, "cards", iccid_a)))
            self.assertTrue(os.path.isdir(os.path.join(tmp_dir, "cards", iccid_b)))

            # 删除 A 不影响 B
            comp_a.add_tombstone("sms_a")
            self.assertTrue(comp_a.is_deleted("sms_a"))
            self.assertFalse(comp_b.is_deleted("sms_a"))
            self.assertEqual(len(comp_b.load_messages()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

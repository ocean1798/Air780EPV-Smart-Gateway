"""Produce an immutable source-matched native plugin; never copy user configuration."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import zipfile

from gateway_runtime import BUSINESS_VERSION

ROOT = Path(__file__).resolve().parent
SHARED = ["gateway_hub.py", "gateway_runtime.py", "gateway_web.py", "web/index.html"]
SOURCE = sorted(SHARED + ["gateway_managed.py", "gateway_app.py", "gateway_package_check.py", "build_exe.py", "build_native.py",
                          "requirements-linux.lock", "requirements-windows.lock", "app.ico"])


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source_identity():
    files = [{"path": name, "sha256": digest((ROOT / name).read_bytes())} for name in SOURCE]
    def summarize(rows):
        return digest("".join(row["path"] + "\0" + row["sha256"] + "\n" for row in rows).encode())
    return {"sourceRevision": summarize(files), "sourceRevisionType": "sha256-source-set",
            "sharedSourceDigest": "sha256:" + summarize([f for f in files if f["path"] in SHARED]),
            "businessVersion": BUSINESS_VERSION, "files": files,
            "baseGitRevision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "workingTree": True}


def build(output, wheelhouse, windows=False):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    wheel = wheelhouse.resolve() / "pyserial-3.5-py2.py3-none-any.whl"
    expected = "c4451db6ba391ca6ca299fb3ec7bae67a5c55dde170964c7a14ceefec02f2cf0"
    if digest(wheel.read_bytes()) != expected:
        raise ValueError("pyserial_wheel_hash_mismatch")
    identity = source_identity()
    manifest = {"packageFormat": 2, "pluginId": "com.smartgateway.cellular", "name": "Air780 单卡上位机",
                "version": BUSINESS_VERSION, "apiVersion": "1.1.0", "hostCompatibility": ">=0.2.0 <0.3.0",
                "sourceRevision": identity["sourceRevision"], "sharedSourceDigest": identity["sharedSourceDigest"],
                "runtime": {"kind": "managed-python-web-v1", "platform": "linux-x86_64", "pythonAbi": "cp312",
                            "entryModule": "gateway_managed", "dependencyLock": "requirements.lock", "wheelhouse": "wheelhouse"},
                "dataSchemaVersion": 1, "migrationPolicy": "none",
                "permissions": ["application.runtime", "application.web", "plugin.storage", "hardware.serial", "network.outbound"],
                "contributions": [{"contributionId": "main", "kind": "application", "target": "workbench.main",
                                   "label": "单卡上位机", "icon": "plug", "startPath": ""}],
                "hostProtocol": "host-capabilities/v1", "hostCapabilities": {"required": [], "optional": []}}
    build_info = output / "build-info.json"
    build_info.write_text(json.dumps(identity, ensure_ascii=False, indent=2), encoding="utf-8")
    archive = output / f"Air780EPV-Gateway-{BUSINESS_VERSION}.se-plugin"
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as package:
        for name in sorted(SHARED + ["gateway_managed.py"]):
            package.write(ROOT / name, name)
        package.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        package.write(build_info, "build-info.json")
        package.write(ROOT / "requirements-linux.lock", "requirements.lock")
        package.write(wheel, "wheelhouse/" + wheel.name)
        package.write(ROOT.parent.parent / "LICENSE", "LICENSE")
    result = {**identity, "plugin": {"path": str(archive), "sha256": digest(archive.read_bytes()), "bytes": archive.stat().st_size},
              "linuxDependencies": {wheel.name: expected}, "windows": None}
    if windows:
        if sys.prefix == sys.base_prefix:
            raise RuntimeError("windows_build_requires_dedicated_venv")
        for line in (ROOT / "requirements-windows.lock").read_text().splitlines():
            if line and not line.startswith("#"):
                name, version = line.split()[0].split("==")
                if importlib.metadata.version(name) != version:
                    raise RuntimeError("windows_dependency_lock_mismatch: " + name)
        subprocess.run([sys.executable, str(ROOT / "build_exe.py"), "--output-dir", str(output / "windows"),
                        "--build-info", str(build_info)], check=True)
        exe = output / "windows" / "dist" / "Air780EPV-Gateway.exe"
        result["windows"] = {"path": str(exe), "sha256": digest(exe.read_bytes()), "bytes": exe.stat().st_size,
                              "python": sys.version, "dependencies": {name: importlib.metadata.version(name)
                              for name in ("pyserial", "pystray", "pillow", "pyinstaller", "pyinstaller-hooks-contrib", "six", "altgraph", "pefile", "pywin32-ctypes", "packaging", "setuptools")},
                              "dependencyLockSha256": digest((ROOT / "requirements-windows.lock").read_bytes())}
    if source_identity()["sourceRevision"] != identity["sourceRevision"]:
        raise RuntimeError("source_changed_during_build")
    (output / "artifacts.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--windows", action="store_true")
    args = parser.parse_args()
    build(args.output_dir, args.wheelhouse, args.windows)

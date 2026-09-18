"""Shared paths and activation state for desktop and managed application entry points."""
import os
import json
import stat
import sys
from pathlib import Path

BUSINESS_VERSION = "1.3.0"
managed = False
active = True
settings = {}


def configure(value):
    global managed, active, settings
    managed, active, settings = True, False, dict(value)


def directory(kind):
    configured = settings.get(kind) or os.environ.get("GATEWAY_" + {
        "dataDir": "DATA_DIR", "cacheDir": "CACHE_DIR", "logDir": "LOG_DIR"}[kind])
    if configured:
        return str(Path(configured).resolve())
    return str(Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent)


def base_path():
    return settings.get("webBasePath", "/")


def outbound_allowed():
    return not managed or (active and "network.outbound" in settings.get("allowedPermissions", []))


def verify_selected_device(device):
    """Validate only the selected character device and its sysfs ancestors; never scan."""
    if not device or sys.platform != "linux":
        raise OSError("selected_device_unavailable")
    expected = device.get("identity") or {}
    keys = ("serialNumber", "vid", "pid", "interface")
    if any(expected.get(k) in (None, "") for k in keys):
        raise OSError("selected_device_identity_incomplete")
    node = Path(device["path"]).resolve(strict=True)
    node_stat = node.stat()
    if not stat.S_ISCHR(node_stat.st_mode):
        raise OSError("selected_device_not_character_device")
    numbers = (os.major(node_stat.st_rdev), os.minor(node_stat.st_rdev))
    sysnode = Path("/sys/dev/char") / f"{numbers[0]}:{numbers[1]}"
    parent = sysnode.resolve(strict=True)
    actual = {}
    for ancestor in (parent, *parent.parents):
        for source, field in (("idVendor", "vid"), ("idProduct", "pid"),
                              ("serial", "serialNumber"), ("bInterfaceNumber", "interface")):
            candidate = ancestor / source
            if field not in actual and candidate.is_file():
                actual[field] = candidate.read_text().strip()
    def normalize(key, value):
        if key == "serialNumber":
            return str(value)
        return value if isinstance(value, int) else int(str(value).removeprefix("0x"), 16)
    if any(normalize(k, actual.get(k, "-1")) != normalize(k, expected[k]) for k in keys):
        raise OSError("selected_device_identity_mismatch")
    manifest_path = device.get("identityManifestPath") or expected.get("identityManifestPath")
    if manifest_path:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        if (manifest.get("major"), manifest.get("minor")) != numbers:
            raise OSError("selected_device_mapping_changed")
        if manifest.get("bootId") != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
            raise OSError("selected_device_stale_mapping")
        recorded = manifest.get("identity") or {}
        if any(normalize(k, recorded.get(k, "-1")) != normalize(k, expected[k]) for k in keys):
            raise OSError("selected_device_mapping_identity_mismatch")
    return str(node), node_stat.st_rdev

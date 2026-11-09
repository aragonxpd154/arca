"""Descoberta de discos/cartões via lsblk e /sys (não precisa de root)."""
from __future__ import annotations

import json
import os
import subprocess

SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var", "[SWAP]"}


def _b(v) -> bool:
    return v in (True, 1, "1", "true")


def _mounts(node) -> list:
    out = []
    for key in ("mountpoints", "mountpoint"):
        v = node.get(key)
        if isinstance(v, list):
            out += [m for m in v if m]
        elif v:
            out.append(v)
    for ch in node.get("children") or []:
        out += _mounts(ch)
    return sorted(set(out))


def _fslist(node) -> list:
    out = []
    for ch in node.get("children") or []:
        out.append({"path": ch.get("path") or f"/dev/{ch.get('name')}",
                    "fstype": ch.get("fstype") or "", "label": ch.get("label") or "",
                    "size": int(ch.get("size") or 0), "mounts": _mounts(ch)})
    return out


def list_disks() -> list:
    cols = "NAME,PATH,SIZE,TYPE,TRAN,RM,HOTPLUG,MODEL,SERIAL,VENDOR,MOUNTPOINT,FSTYPE,LABEL,LOG-SEC,RO"
    try:
        raw = subprocess.run(["lsblk", "-J", "-b", "-o", cols], capture_output=True,
                             text=True, check=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    disks = []
    loops = os.environ.get("ARCA_LOOP") == "1"   # só para testes com dispositivos loop
    for d in json.loads(raw).get("blockdevices", []):
        name = d.get("name", "")
        if loops and d.get("type") == "loop":
            pass
        elif d.get("type") != "disk" or name.startswith(("loop", "zram", "ram", "sr")):
            continue
        mounts = _mounts(d)
        system = bool(set(mounts) & SYSTEM_MOUNTS)
        tran = (d.get("tran") or "").lower()
        removable = _b(d.get("rm")) or _b(d.get("hotplug")) or tran in ("usb", "mmc", "pcmcia") \
            or name.startswith("mmcblk")
        model = " ".join(x.strip() for x in (d.get("vendor") or "", d.get("model") or "") if x and x.strip())
        disks.append({
            "name": name, "path": d.get("path") or f"/dev/{name}",
            "size": int(d.get("size") or 0), "model": model or "Disco sem nome",
            "serial": (d.get("serial") or "").strip(), "tran": tran or "?",
            "removable": removable, "system": system, "readonly": _b(d.get("ro")),
            "sector": int(d.get("log-sec") or 512), "mounts": mounts, "parts": _fslist(d),
        })
    disks.sort(key=lambda x: (x["system"], not x["removable"], x["name"]))
    return disks


def get_disk(path: str) -> dict | None:
    real = os.path.realpath(path)
    for d in list_disks():
        if os.path.realpath(d["path"]) == real:
            return d
    return None


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or u == "TB":
            s = f"{n:.0f} {u}" if u == "B" else f"{n:.2f} {u}"
            return s.replace(".", ",")
        n /= 1000
    return str(n)


def human_bin(n: float) -> str:
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or u == "TiB":
            s = f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
            return s.replace(".", ",")
        n /= 1024
    return str(n)

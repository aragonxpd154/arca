"""Análise das estruturas de boot: MBR, tabela de partições, boot sector (VBR) e áreas FAT.

Funciona sobre qualquer função read(pos, size) -> bytes (imagem ou dispositivo).
"""
from __future__ import annotations

import struct

PART_TYPES = {
    0x00: "Vazia", 0x01: "FAT12", 0x04: "FAT16 (<32 MB)", 0x05: "Estendida",
    0x06: "FAT16", 0x07: "NTFS/exFAT", 0x0B: "FAT32 (CHS)", 0x0C: "FAT32 (LBA)",
    0x0E: "FAT16 (LBA)", 0x0F: "Estendida (LBA)", 0x11: "FAT12 oculta",
    0x14: "FAT16 oculta", 0x16: "FAT16 oculta", 0x1B: "FAT32 oculta",
    0x1C: "FAT32 oculta (LBA)", 0x1E: "FAT16 oculta (LBA)", 0x82: "Linux swap",
    0x83: "Linux", 0x8E: "Linux LVM", 0xA5: "FreeBSD", 0xA6: "OpenBSD",
    0xDA: "Dados (não-FS)", 0xEE: "GPT protetora", 0xEF: "EFI",
}


def _u8(b, o): return b[o]
def _u16(b, o): return struct.unpack_from("<H", b, o)[0]
def _u32(b, o): return struct.unpack_from("<I", b, o)[0]


def _txt(b: bytes) -> str:
    return b.decode("ascii", "replace").replace("\x00", " ").strip()


def parse_vbr(sec: bytes) -> dict | None:
    """Interpreta um boot sector FAT12/16/32. Retorna None se não parecer FAT."""
    if len(sec) < 512:
        return None
    if sec[3:11] == b"NTFS    ":
        return {"fs": "NTFS", "oem": "NTFS"}
    if sec[3:11] == b"EXFAT   ":
        return {"fs": "exFAT", "oem": "EXFAT"}
    if sec[0] not in (0xEB, 0xE9):
        return None
    bps = _u16(sec, 11)
    spc = _u8(sec, 13)
    if bps not in (512, 1024, 2048, 4096) or spc == 0 or spc & (spc - 1):
        return None
    reserved = _u16(sec, 14)
    nfats = _u8(sec, 16)
    root_ent = _u16(sec, 17)
    tot16 = _u16(sec, 19)
    fatsz16 = _u16(sec, 22)
    tot32 = _u32(sec, 32)
    if reserved == 0 or nfats == 0:
        return None
    total = tot16 or tot32
    if fatsz16 == 0:
        fatsz = _u32(sec, 36)
        label, fstype = _txt(sec[71:82]), _txt(sec[82:90])
        fs = "FAT32"
    else:
        fatsz = fatsz16
        label, fstype = _txt(sec[43:54]), _txt(sec[54:62])
        root_secs = (root_ent * 32 + bps - 1) // bps
        clusters = (total - reserved - nfats * fatsz - root_secs) // spc if spc else 0
        fs = "FAT12" if clusters < 4085 else "FAT16"
    return {
        "fs": fs, "oem": _txt(sec[3:11]), "label": label or "(sem rótulo)",
        "fstype_field": fstype, "bytes_per_sector": bps, "sectors_per_cluster": spc,
        "reserved_sectors": reserved, "fats": nfats, "fat_sectors": fatsz,
        "root_entries": root_ent, "total_sectors": total,
        "signature_ok": sec[510:512] == b"\x55\xAA",
    }


def _fat_regions(name: str, base_lba: int, vbr: dict, sector: int):
    """Regiões críticas de um volume FAT, em bytes absolutos."""
    bps = vbr["bytes_per_sector"]
    f = bps / sector  # setores lógicos do FS -> setores do disco
    regs = [{"name": f"{name} · área reservada / boot sector", "kind": "vbr",
             "start": base_lba * sector, "size": int(vbr["reserved_sectors"] * bps)}]
    fat_start = base_lba + int(vbr["reserved_sectors"] * f)
    for k in range(vbr["fats"]):
        regs.append({"name": f"{name} · FAT nº {k + 1}", "kind": "fat",
                     "start": (fat_start + int(k * vbr["fat_sectors"] * f)) * sector,
                     "size": int(vbr["fat_sectors"] * bps)})
    if vbr["fs"] != "FAT32" and vbr["root_entries"]:
        root_lba = fat_start + int(vbr["fats"] * vbr["fat_sectors"] * f)
        regs.append({"name": f"{name} · diretório raiz", "kind": "root",
                     "start": root_lba * sector, "size": vbr["root_entries"] * 32})
    return regs


def analyze(read, total_size: int, sector: int = 512) -> dict:
    """Analisa o disco/imagem. read(pos, size) -> bytes."""
    info = {"scheme": "desconhecido", "partitions": [], "regions": [],
            "min_size": total_size, "notes": []}
    try:
        s0 = read(0, sector)
    except OSError as e:
        info["notes"].append(f"Setor 0 ilegível: {e}")
        return info
    if len(s0) < 512 or not any(s0):
        info["notes"].append("Setor 0 vazio (zerado ou ainda não recuperado).")
        info["regions"].append({"name": "MBR (setor 0)", "kind": "mbr", "start": 0, "size": sector})
        return info

    vbr0 = parse_vbr(s0)
    parts = []
    sig = s0[510:512] == b"\x55\xAA"
    if sig:
        for n in range(4):
            e = s0[446 + 16 * n: 446 + 16 * (n + 1)]
            ptype, lba, cnt = e[4], _u32(e, 8), _u32(e, 12)
            if ptype == 0 or cnt == 0:
                continue
            parts.append({"index": n + 1, "boot": e[0] == 0x80, "flag": e[0], "type": ptype,
                          "type_name": PART_TYPES.get(ptype, f"0x{ptype:02X}"),
                          "start": lba, "sectors": cnt})
        valid = parts and all(e["flag"] in (0x00, 0x80) and e["start"] > 0 and
                              (e["start"] + e["sectors"]) * sector <= total_size * 1.01
                              for e in parts)
        if vbr0 and not valid:
            parts = []  # é um boot sector FAT direto, não uma tabela de partições

    if parts and any(p["type"] == 0xEE for p in parts):
        info["scheme"] = "GPT"
        info["regions"] += [{"name": "MBR protetora (setor 0)", "kind": "mbr", "start": 0, "size": sector},
                            {"name": "Cabeçalho + tabela GPT", "kind": "gpt", "start": sector, "size": 33 * sector}]
        info["notes"].append("Disco GPT: apenas o cabeçalho é analisado.")
        info["min_size"] = total_size
        return info

    if parts:
        info["scheme"] = "MBR"
        info["disk_signature"] = f"{_u32(s0, 440):08X}"
        info["regions"].append({"name": "MBR + tabela de partições (setor 0)", "kind": "mbr",
                                "start": 0, "size": sector})
        end_max = 0
        for p in parts:
            end_max = max(end_max, (p["start"] + p["sectors"]) * sector)
            try:
                vb = parse_vbr(read(p["start"] * sector, max(sector, 512)))
            except OSError:
                vb = None
            p["fs"] = vb
            name = f"Partição {p['index']}"
            if vb and vb.get("fs", "").startswith("FAT"):
                info["regions"] += _fat_regions(name, p["start"], vb, sector)
            else:
                info["regions"].append({"name": f"{name} · boot sector", "kind": "vbr",
                                        "start": p["start"] * sector, "size": sector})
        info["partitions"] = parts
        info["min_size"] = end_max
        if end_max < total_size:
            info["notes"].append(f"Há {total_size - end_max:,} bytes não particionados no fim.".replace(",", "."))
    elif vbr0:
        info["scheme"] = "Sem tabela (FAT direto / superfloppy)"
        info["partitions"] = [{"index": 0, "boot": False, "type": None, "type_name": vbr0["fs"],
                               "start": 0, "sectors": vbr0.get("total_sectors", total_size // sector),
                               "fs": vbr0}]
        if vbr0["fs"].startswith("FAT"):
            info["regions"] += _fat_regions("Volume", 0, vbr0, sector)
            info["min_size"] = vbr0["total_sectors"] * vbr0["bytes_per_sector"]
    else:
        info["scheme"] = "MBR sem partições" if sig else "Desconhecido"
        info["regions"].append({"name": "Setor 0", "kind": "mbr", "start": 0, "size": sector})
        info["notes"].append("Não foi encontrada tabela de partições nem sistema FAT conhecido. "
                             "A cópia setor a setor continua válida.")
    return info

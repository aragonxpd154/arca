"""Gravação de uma imagem num cartão novo, com verificação por SHA-256."""
from __future__ import annotations

import fcntl
import hashlib
import mmap
import os
import stat
import time

CHUNK = 1 << 20
BLKFLSBUF = 0x1261


def fit(image_size: int, target_size: int, boot: dict | None) -> dict:
    """Diz se a imagem cabe no cartão de destino.

    Cartões de mesma capacidade nominal (ex.: "1 GB") de marcas diferentes
    quase sempre têm números de setores diferentes, por isso a checagem.
    """
    need = (boot or {}).get("min_size") or image_size
    need = min(need, image_size)
    diff = target_size - image_size
    if diff == 0:
        return {"level": "exact", "write_len": image_size,
                "msg": "Tamanho idêntico: cópia exata setor a setor."}
    if diff > 0:
        return {"level": "larger", "write_len": image_size,
                "msg": f"O cartão novo é {_h(diff)} maior. A cópia fica idêntica; "
                       "o espaço extra fica sem uso (dá para expandir depois no GParted)."}
    if target_size >= need:
        return {"level": "smaller_ok", "write_len": target_size,
                "msg": f"O cartão novo é {_h(-diff)} menor, mas todas as partições cabem. "
                       "Só a área não particionada do fim será descartada."}
    return {"level": "too_small", "write_len": 0,
            "msg": f"Não cabe: faltam {_h(need - target_size)} para as partições. "
                   "Use um cartão de capacidade maior."}


def _h(n: int) -> str:
    for u in ("bytes", "KiB", "MiB", "GiB"):
        if abs(n) < 1024 or u == "GiB":
            return f"{n:.0f} {u}" if u == "bytes" else f"{n:.1f} {u}".replace(".", ",")
        n /= 1024
    return str(n)


def _open_target(path: str) -> int:
    flags = os.O_WRONLY
    st = os.stat(path)
    if stat.S_ISBLK(st.st_mode):
        flags |= os.O_EXCL  # falha com EBUSY se algo estiver montado
    return os.open(path, flags)


def _size(path: str) -> int:
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


def write_image(image: str, target: str, emit, verify: bool = True,
                length: int | None = None, cancel=None) -> dict:
    cancel = cancel or (lambda: False)
    length = length or os.path.getsize(image)
    tsize = _size(target)
    if length > tsize:
        raise ValueError("a imagem é maior que o destino")
    emit({"ev": "wstart", "image": image, "target": target, "length": length, "target_size": tsize})

    h_img = hashlib.sha256()
    t0 = time.monotonic()
    last = 0.0
    with open(image, "rb") as fi:
        fo = _open_target(target)
        try:
            done = 0
            while done < length:
                if cancel():
                    emit({"ev": "log", "level": "warn", "msg": "Gravação cancelada: o cartão ficou incompleto."})
                    return {"ev": "wdone", "ok": False, "cancelled": True}
                buf = fi.read(min(CHUNK, length - done))
                if not buf:
                    raise IOError("imagem terminou antes do esperado")
                h_img.update(buf)
                mv = memoryview(buf)
                while mv:
                    n = os.write(fo, mv)
                    mv = mv[n:]
                done += len(buf)
                now = time.monotonic()
                if now - last > 0.25 or done == length:
                    last = now
                    el = now - t0
                    emit({"ev": "wprogress", "phase": "write", "done": done, "total": length,
                          "rate": done / el if el else 0})
            emit({"ev": "log", "level": "info", "msg": "Esvaziando cache para o cartão (sync)…"})
            os.fsync(fo)
            try:
                fcntl.ioctl(fo, BLKFLSBUF, 0)
            except OSError:
                pass
        finally:
            os.close(fo)

    sha_img = h_img.hexdigest()
    if not verify:
        return {"ev": "wdone", "ok": True, "verified": False, "sha_img": sha_img}

    # --------------------------------------------------------- leitura de volta
    h_dev = hashlib.sha256()
    first_bad = None
    mism = 0
    buf = mmap.mmap(-1, CHUNK)
    try:
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_DIRECT", 0))
        direct = True
    except OSError:
        fd = os.open(target, os.O_RDONLY)
        direct = False
    t0, last = time.monotonic(), 0.0
    try:
        with open(image, "rb") as fi:
            done = 0
            while done < length:
                if cancel():
                    return {"ev": "wdone", "ok": False, "cancelled": True, "sha_img": sha_img}
                n = min(CHUNK, length - done)
                want = fi.read(n)
                if direct and n % 512 == 0:
                    view = memoryview(buf)[:n]
                    got = bytes(view[:os.preadv(fd, [view], done)])
                else:
                    got = os.pread(fd, n, done)
                h_dev.update(got)
                if got != want:
                    mism += 1
                    if first_bad is None:
                        first_bad = done + next((i for i in range(min(len(got), len(want)))
                                                 if got[i] != want[i]), min(len(got), len(want)))
                done += n
                now = time.monotonic()
                if now - last > 0.25 or done == length:
                    last = now
                    el = now - t0
                    emit({"ev": "wprogress", "phase": "verify", "done": done, "total": length,
                          "rate": done / el if el else 0})
    finally:
        os.close(fd)
    sha_dev = h_dev.hexdigest()
    return {"ev": "wdone", "ok": sha_dev == sha_img, "verified": True, "match": sha_dev == sha_img,
            "sha_img": sha_img, "sha_dev": sha_dev, "first_mismatch": first_bad,
            "bad_chunks": mism, "length": length}


def sha256_file(path: str, length: int | None = None) -> str:
    h = hashlib.sha256()
    left = length if length is not None else os.path.getsize(path)
    with open(path, "rb") as f:
        while left > 0:
            b = f.read(min(CHUNK, left))
            if not b:
                break
            h.update(b)
            left -= len(b)
    return h.hexdigest()

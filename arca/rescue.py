"""Motor de resgate setor a setor (estratégia inspirada no GNU ddrescue).

Ordem de trabalho:
  0. Estruturas críticas primeiro: setor 0, boot sectors, FATs e diretório raiz.
  1. Cópia rápida em blocos, pulando áreas com erro (para não estressar o cartão).
  2. Cópia reversa do que foi pulado, sem pular.
  3. Aparo (trim): lê setor a setor pelas bordas de cada bloco com erro.
  4. Raspagem (scrape): lê setor a setor o miolo das áreas com erro.
  5. Novas tentativas nos setores ruins, alternando a direção.

O mapa é salvo continuamente, então o resgate pode ser interrompido e retomado.
"""
from __future__ import annotations

import errno
import mmap
import os
import time

from . import bootinfo
from .mapfile import (BAD, FINISHED, NON_SCRAPED, NON_TRIED, NON_TRIMMED, MapFile)

LOST_ERRNOS = {errno.ENODEV, errno.ENXIO, errno.ENOENT, getattr(errno, "ENOMEDIUM", 123)}

PHASES = {
    "boot": "Estruturas de boot",
    "copy": "Cópia rápida",
    "copy2": "Cópia reversa",
    "trim": "Aparando bordas",
    "scrape": "Raspando setor a setor",
    "retry": "Novas tentativas",
    "done": "Concluído",
}


class DeviceLost(Exception):
    pass


class Cancelled(Exception):
    pass


class DeviceReader:
    """Leitura direta (O_DIRECT) para não mascarar erros com o cache do kernel."""

    def __init__(self, path: str, sector: int = 512, max_io: int = 1 << 20):
        self.path = path
        self.sector = sector
        self.direct = True
        try:
            self.fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECT", 0))
        except OSError:
            self.fd = os.open(path, os.O_RDONLY)
            self.direct = False
        self.buf = mmap.mmap(-1, max_io)  # alinhado em página

    def read(self, pos: int, size: int) -> bytes:
        if self.direct:
            try:
                view = memoryview(self.buf)[:size]
                n = os.preadv(self.fd, [view], pos)
                return bytes(view[:n])
            except OSError as e:
                if e.errno != errno.EINVAL:
                    raise
                self.direct = False  # dispositivo não aceita O_DIRECT
                os.close(self.fd)
                self.fd = os.open(self.path, os.O_RDONLY)
        try:
            os.posix_fadvise(self.fd, pos, size, os.POSIX_FADV_DONTNEED)
        except (AttributeError, OSError):
            pass
        return os.pread(self.fd, size, pos)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


def device_size(path: str) -> int:
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


class Rescuer:
    def __init__(self, reader, total: int, image_path: str, map_path: str,
                 sector: int = 512, cluster: int = 64 * 1024, retries: int = 3,
                 emit=None, cancel=None, resume: bool = True, boot_first: bool = True):
        self.reader = reader
        self.total = total
        self.sector = sector
        self.cluster = max(sector, cluster - cluster % sector)
        self.retries = retries
        self.emit = emit or (lambda ev: None)
        self.cancel = cancel or (lambda: False)
        self.image_path = image_path
        self.map_path = map_path
        self.boot_first = boot_first
        self.resumed = False
        if resume and os.path.exists(map_path) and os.path.exists(image_path):
            self.map = MapFile.load(map_path, total)
            self.resumed = True
        else:
            self.map = MapFile(total)
        self.img = os.open(image_path, os.O_RDWR | os.O_CREAT, 0o644)
        if os.fstat(self.img).st_size < total:
            os.ftruncate(self.img, total)
        self.phase = "boot"
        self.pos = 0
        self.read_errors = 0
        self.t0 = time.monotonic()
        self.bytes_this_run = 0
        self.last_ok = time.monotonic()
        self._t_prog = self._t_map = self._t_save = 0.0
        self._rate_win = []  # (t, bytes)
        self.boot = None

    # --------------------------------------------------------------- utilidades
    def _read_img(self, pos, size):
        return os.pread(self.img, size, pos)

    def _check(self):
        if self.cancel():
            raise Cancelled()

    def _try(self, pos: int, size: int) -> bool:
        self._check()
        size = min(size, self.total - pos)
        self.pos = pos
        try:
            data = self.reader.read(pos, size)
        except OSError as e:
            if e.errno in LOST_ERRNOS:
                raise DeviceLost(str(e))
            self.read_errors += 1
            self._tick()
            return False
        if len(data) < size:
            if not data:
                self.read_errors += 1
                self._tick()
                return False
            size = len(data) - len(data) % self.sector or len(data)
            data = data[:size]
            os.pwrite(self.img, data, pos)
            self.map.set(pos, size, FINISHED)
            self._account(size)
            return False
        os.pwrite(self.img, data, pos)
        self.map.set(pos, size, FINISHED)
        self._account(size)
        return True

    def _account(self, n):
        now = time.monotonic()
        self.bytes_this_run += n
        self.last_ok = now
        self._rate_win.append((now, n))
        self._tick()

    def _rate(self):
        now = time.monotonic()
        self._rate_win = [x for x in self._rate_win if now - x[0] < 3.0]
        if not self._rate_win:
            return 0.0
        span = max(now - self._rate_win[0][0], 0.5)
        return sum(x[1] for x in self._rate_win) / span

    def stats(self) -> dict:
        c = self.map.counts()
        el = time.monotonic() - self.t0
        avg = self.bytes_this_run / el if el > 0 else 0
        remaining = c[NON_TRIED] + c[NON_TRIMMED] + c[NON_SCRAPED]
        rate = self._rate()
        return {
            "ev": "progress", "phase": self.phase, "phase_name": PHASES[self.phase],
            "pos": self.pos, "total": self.total, "rescued": c[FINISHED],
            "pending": c[NON_TRIED], "nontrimmed": c[NON_TRIMMED], "nonscraped": c[NON_SCRAPED],
            "bad": c[BAD], "bad_areas": self.map.error_areas(), "read_errors": self.read_errors,
            "rate": rate, "avg_rate": avg, "elapsed": el,
            "since_ok": time.monotonic() - self.last_ok,
            "eta": (remaining / rate) if rate > 0 and self.phase in ("copy", "copy2", "boot") else None,
        }

    def _tick(self, force=False):
        now = time.monotonic()
        if force or now - self._t_prog > 0.25:
            self._t_prog = now
            self.emit(self.stats())
        if force or now - self._t_map > 0.6:
            self._t_map = now
            self.emit({"ev": "map", "total": self.total, "iv": self.map.as_list(), "pos": self.pos})
        if force or now - self._t_save > 3.0:
            self._t_save = now
            self.save()

    def save(self):
        st = {"boot": "?", "copy": "?", "copy2": "?", "trim": "*", "scrape": "/",
              "retry": "-", "done": "+"}[self.phase]
        self.map.save(self.map_path, self.pos, st, 1)

    def _set_phase(self, p):
        self.phase = p
        self.emit({"ev": "phase", "phase": p, "phase_name": PHASES[p]})
        self._tick(force=True)

    # ------------------------------------------------------------------ passadas
    def _copy(self, lo=0, hi=None, skip=True, reverse=False):
        hi = self.total if hi is None else hi
        areas = self.map.intervals({NON_TRIED}, lo, hi)
        if reverse:
            areas.reverse()
        skip0 = max(self.cluster, 64 * 1024)
        max_skip = max(skip0, min(self.total // 100, 16 << 20))
        for p, s, _ in areas:
            end, skipsz = p + s, skip0
            if not reverse:
                pos = p
                while pos < end:
                    n = min(self.cluster - pos % self.cluster or self.cluster, end - pos)
                    if self._try(pos, n):
                        skipsz = skip0
                    else:
                        for a, z, t in self.map.intervals({NON_TRIED}, pos, pos + n):
                            self.map.set(a, z, NON_TRIMMED)
                        if skip:
                            pos += n
                            jump = min(skipsz, end - pos)
                            pos += jump
                            skipsz = min(skipsz * 2, max_skip)
                            continue
                    pos += n
            else:
                pos = end
                while pos > p:
                    a = max(p, pos - (pos % self.cluster or self.cluster))
                    n = pos - a
                    if not self._try(a, n):
                        for b, z, t in self.map.intervals({NON_TRIED}, a, pos):
                            self.map.set(b, z, NON_TRIMMED)
                    pos = a

    def _trim(self, lo=0, hi=None):
        sec = self.sector
        for p, s, _ in self.map.intervals({NON_TRIMMED}, lo, hi):
            pos, end = p, p + s
            while pos < end:
                if self._try(pos, sec):
                    pos += sec
                else:
                    self.map.set(pos, sec, BAD)
                    pos += sec
                    break
            e = end
            while e > pos:
                a = max(pos, e - sec)
                if self._try(a, e - a):
                    e = a
                else:
                    self.map.set(a, e - a, BAD)
                    e = a
                    break
            if e > pos:
                self.map.set(pos, e - pos, NON_SCRAPED)

    def _scrape(self, lo=0, hi=None):
        sec = self.sector
        for p, s, _ in self.map.intervals({NON_SCRAPED}, lo, hi):
            pos = p
            while pos < p + s:
                n = min(sec, p + s - pos)
                if not self._try(pos, n):
                    self.map.set(pos, n, BAD)
                pos += n

    def _retry(self):
        sec = self.sector
        for k in range(self.retries):
            areas = self.map.intervals({BAD})
            if not areas:
                return
            self.emit({"ev": "log", "level": "info",
                       "msg": f"Nova tentativa {k + 1}/{self.retries} em {len(areas)} área(s) ruim(ns)"})
            if k % 2:
                areas.reverse()
            for p, s, _ in areas:
                rng = range(p, p + s, sec)
                for pos in (reversed(rng) if k % 2 else rng):
                    self._try(pos, min(sec, p + s - pos))

    def _region(self, lo, hi):
        """Resolve uma região por completo: blocos, depois setor a setor."""
        self._copy(lo, hi, skip=False)
        self._trim(lo, hi)
        self._scrape(lo, hi)

    def _boot_pass(self):
        """Lê primeiro o que é crítico: setor 0, VBRs, FATs e diretório raiz."""
        self._region(0, self.sector)                       # MBR antes de tudo
        self._region(0, min(self.total, 1 << 20))         # primeiro 1 MiB
        self.boot = bootinfo.analyze(self._read_img, self.total, self.sector)
        self.emit({"ev": "boot", "info": self.boot})
        for r in self.boot["regions"]:
            self._region(r["start"], r["start"] + r["size"])
        self.boot = bootinfo.analyze(self._read_img, self.total, self.sector)
        self.emit({"ev": "boot", "info": self.boot})

    def run(self) -> dict:
        self.emit({"ev": "start", "total": self.total, "sector": self.sector,
                   "resumed": self.resumed, "image": self.image_path, "map": self.map_path})
        self._tick(force=True)
        try:
            if self.boot_first:
                self._set_phase("boot")
                self._boot_pass()
            self._set_phase("copy")
            self._copy(skip=True)
            self._set_phase("copy2")
            self._copy(skip=False, reverse=True)
            self._set_phase("trim")
            self._trim()
            self._set_phase("scrape")
            self._scrape()
            if self.retries > 0:
                self._set_phase("retry")
                self._retry()
            self._set_phase("done")
            status = "done"
        except Cancelled:
            status = "cancelled"
        except DeviceLost as e:
            status = "lost"
            self.emit({"ev": "log", "level": "error",
                       "msg": f"O cartão sumiu do sistema ({e}). Reconecte e use Retomar."})
        finally:
            self.save()
            os.fsync(self.img)
        self.boot = bootinfo.analyze(self._read_img, self.total, self.sector)
        self.emit({"ev": "boot", "info": self.boot})
        self._tick(force=True)
        res = self.stats()
        res.update({"ev": "finished", "status": status})
        return res

    def close(self):
        try:
            os.close(self.img)
        except OSError:
            pass

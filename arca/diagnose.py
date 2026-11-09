"""Diagnóstico de saúde da unidade: teste de leitura/escrita, latência e degradação.

Modos:
  leitura  — lê o cartão inteiro N vezes. Não altera nada. Mede a velocidade e a
             latência de cada bloco e compara o CRC entre as passadas: se o mesmo
             bloco devolve dados diferentes, a leitura está instável (célula fraca).
  escrita  — APAGA o cartão. Em cada passada grava o cartão inteiro com um padrão
             (aleatório, 0x55, 0xAA, 0xFF, 0x00), lê tudo de volta e confere.
             Cada setor leva um carimbo com o próprio número (LBA), o que detecta:
               • dados corrompidos (bits trocados);
               • escrita perdida (o cartão aceitou, mas manteve o dado antigo);
               • capacidade falsa (setores que "dão a volta" e caem em outro lugar).

No fim calcula uma nota de 0 a 100 e explica o porquê.
"""
from __future__ import annotations

import errno
import fcntl
import mmap
import os
import random
import stat
import struct
import time
import zlib

MAGIC = b"ARCADIAG"
STAMP = struct.Struct("<8sIIQ")          # magic, semente, passada, LBA  (24 bytes)
BLKFLSBUF = 0x1261
LOST = {errno.ENODEV, errno.ENXIO, errno.ENOENT, getattr(errno, "ENOMEDIUM", 123)}

# bits do estado de cada bloco (acumulam entre passadas)
F_RERR, F_WERR, F_CORRUPT, F_UNSTABLE, F_ALIAS, F_LOSTW = 1, 2, 4, 8, 16, 32

PATTERNS = ("aleatório", "0x55", "0xAA", "0xFF", "0x00")
FILLS = (None, 0x55, 0xAA, 0xFF, 0x00)


class DeviceLost(Exception):
    pass


class Cancelled(Exception):
    pass


class DeviceIO:
    """Acesso direto (O_DIRECT) ao cartão, para medir o cartão e não o cache."""

    def __init__(self, path: str, write: bool, max_io: int):
        self.path = path
        mode = os.O_RDWR if write else os.O_RDONLY
        if write and stat.S_ISBLK(os.stat(path).st_mode):
            mode |= os.O_EXCL               # recusa se estiver montado
        self.direct = True
        try:
            self.fd = os.open(path, mode | getattr(os, "O_DIRECT", 0))
        except OSError as e:
            if e.errno != errno.EINVAL:
                raise
            self.fd = os.open(path, mode)
            self.direct = False
        self.buf = mmap.mmap(-1, max_io)

    def read(self, pos: int, size: int) -> bytes:
        view = memoryview(self.buf)[:size]
        n = os.preadv(self.fd, [view], pos)
        return bytes(view[:n])

    def write(self, pos: int, data: bytes) -> None:
        self.buf[:len(data)] = data
        view = memoryview(self.buf)[:len(data)]
        n = os.pwritev(self.fd, [view], pos)
        if n != len(data):
            raise OSError(errno.EIO, "escrita incompleta")

    def flush(self) -> None:
        os.fsync(self.fd)
        try:
            fcntl.ioctl(self.fd, BLKFLSBUF, 0)
        except OSError:
            pass
        if not self.direct:
            try:
                os.posix_fadvise(self.fd, 0, 0, os.POSIX_FADV_DONTNEED)
            except (AttributeError, OSError):
                pass

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


def _bk(b):
    return f"{b // (1 << 20)} MiB" if b % (1 << 20) == 0 else f"{b // 1024} KiB"


def _pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = min(len(sorted_vals) - 1, max(0, int(round(p / 100 * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


class Diagnoser:
    def __init__(self, io, total: int, mode: str = "leitura", passes: int = 1,
                 block: int = 1 << 20, emit=None, cancel=None, until_fail: bool = False,
                 sector: int = 512, seed: int | None = None):
        if mode not in ("leitura", "escrita"):
            raise ValueError("modo deve ser 'leitura' ou 'escrita'")
        self.io, self.total, self.mode = io, total, mode
        self.passes = max(1, passes)
        self.until_fail = until_fail
        self.sector = sector
        self.block = max(sector, block - block % sector)
        self.n = -(-total // self.block)
        self.emit = emit or (lambda e: None)
        self.cancel = cancel or (lambda: False)
        self.seed = seed if seed is not None else random.getrandbits(32)
        self.flags = [0] * self.n
        self.rlat = [None] * self.n          # última latência de leitura (ms)
        self.wlat = [None] * self.n          # última latência de escrita (ms)
        self.rworst = [0.0] * self.n         # pior leitura vista em qualquer passada
        self.crc = [None] * self.n
        self.summaries = []
        self.written = self.readb = 0
        self.t0 = time.monotonic()
        self._batch = []
        self._t_batch = self._t_prog = 0.0
        self._phase_bytes = 0
        self._phase_t0 = time.monotonic()
        self.alias_examples = []
        self.lost = False

    # --------------------------------------------------------------- padrões
    def _span(self, i):
        pos = i * self.block
        return pos, min(self.block, self.total - pos)

    def pattern(self, p: int, i: int) -> bytes:
        pos, size = self._span(i)
        fill = FILLS[(p - 1) % len(FILLS)]
        if fill is None:
            buf = bytearray(random.Random((self.seed << 32) ^ (p << 48) ^ i).randbytes(size))
        else:
            buf = bytearray(bytes((fill,)) * size)
        lba0 = pos // self.sector
        for k in range(size // self.sector):
            STAMP.pack_into(buf, k * self.sector, MAGIC, self.seed, p, lba0 + k)
        return bytes(buf)

    # ----------------------------------------------------------- utilitários
    def _check(self):
        if self.cancel():
            raise Cancelled()

    def _lost(self, e):
        if isinstance(e, OSError) and e.errno in LOST:
            raise DeviceLost(str(e))

    def _record(self, p, phase, i, ms, size):
        self._batch.append([i, round(ms, 3) if ms is not None else None, self.flags[i]])
        self._phase_bytes += size
        now = time.monotonic()
        if now - self._t_batch > 0.3:
            self._flush_batch(p, phase)
        if now - self._t_prog > 0.25:
            self._t_prog = now
            el = now - self._phase_t0
            self.emit({"ev": "dprogress", "pass": p, "phase": phase, "done": i + 1, "nblocks": self.n,
                       "rate": self._phase_bytes / el if el > 0 else 0, "elapsed": now - self.t0,
                       "written": self.written, "read": self.readb})

    def _flush_batch(self, p, phase):
        if self._batch:
            self.emit({"ev": "dblocks", "pass": p, "phase": phase, "items": self._batch})
            self._batch = []
        self._t_batch = time.monotonic()

    def _stats(self, lats, sizes, errors):
        ok = sorted(l for l in lats if l is not None)
        tot_ms = sum(l for l in lats if l is not None)
        tot_b = sum(s for l, s in zip(lats, sizes) if l is not None)
        med = _pct(ok, 50)
        slow_lim = max(5 * med, med + 30.0)
        vslow_lim = max(20 * med, 500.0)
        speeds = sorted(s / (l / 1000) for l, s in zip(lats, sizes) if l)
        return {
            "mbps": (tot_b / (tot_ms / 1000) / 1e6) if tot_ms else 0.0,
            "mbps_min": (speeds[0] / 1e6) if speeds else 0.0,
            "mbps_max": (speeds[-1] / 1e6) if speeds else 0.0,
            "lat_p50": med, "lat_p99": _pct(ok, 99), "lat_max": ok[-1] if ok else 0.0,
            "slow": sum(1 for l in ok if l > slow_lim),
            "very_slow": sum(1 for l in ok if l > vslow_lim),
            "errors": errors,
        }

    # ------------------------------------------------------------- passadas
    def _phase(self, p, phase):
        self._phase_bytes = 0
        self._phase_t0 = time.monotonic()
        self.emit({"ev": "dphase", "pass": p, "phase": phase,
                   "pattern": PATTERNS[(p - 1) % len(PATTERNS)] if self.mode == "escrita" else None})

    def _write_pass(self, p):
        self._phase(p, "write")
        lats, sizes, errors = [], [], 0
        for i in range(self.n):
            self._check()
            pos, size = self._span(i)
            data = self.pattern(p, i)
            t = time.perf_counter()
            try:
                self.io.write(pos, data)
                ms = (time.perf_counter() - t) * 1000
                self.written += size
            except OSError as e:
                self._lost(e)
                ms = None
                errors += 1
                self.flags[i] |= F_WERR
            self.wlat[i] = ms
            lats.append(ms)
            sizes.append(size)
            self._record(p, "write", i, ms, size)
        t = time.perf_counter()
        try:
            self.io.flush()
        except OSError as e:
            self._lost(e)
            errors += 1
        flush_ms = (time.perf_counter() - t) * 1000
        self._flush_batch(p, "write")
        st = self._stats(lats, sizes, errors)
        st["flush_ms"] = flush_ms
        return st

    def _compare(self, p, i, got, want):
        """Conta setores divergentes e descobre o tipo de falha."""
        sec = self.sector
        bad = 0
        pos, _ = self._span(i)
        for k in range(0, min(len(got), len(want)), sec):
            if got[k:k + sec] == want[k:k + sec]:
                continue
            bad += 1
            magic, seed, gp, lba = STAMP.unpack_from(got, k) if len(got) >= k + STAMP.size else (b"", 0, 0, 0)
            exp_lba = (pos + k) // sec
            if magic == MAGIC and seed == self.seed and lba != exp_lba:
                self.flags[i] |= F_ALIAS
                if len(self.alias_examples) < 5:
                    self.alias_examples.append((exp_lba, lba))
            elif magic == MAGIC and seed == self.seed and gp != p:
                self.flags[i] |= F_LOSTW
            else:
                self.flags[i] |= F_CORRUPT
        bad += abs(len(want) - len(got)) // sec
        return bad

    def _read_pass(self, p):
        self._phase(p, "read")
        lats, sizes, errors, bad_secs, unstable = [], [], 0, 0, 0
        for i in range(self.n):
            self._check()
            pos, size = self._span(i)
            t = time.perf_counter()
            try:
                data = self.io.read(pos, size)
                ms = (time.perf_counter() - t) * 1000
                self.readb += len(data)
                if len(data) < size:
                    raise OSError(errno.EIO, "leitura curta")
            except OSError as e:
                self._lost(e)
                ms, data = None, None
                errors += 1
                self.flags[i] |= F_RERR
            if data is not None:
                if self.mode == "escrita":
                    want = self.pattern(p, i)
                    if data != want:
                        bad_secs += self._compare(p, i, data, want)
                else:
                    c = zlib.crc32(data)
                    if self.crc[i] is not None and c != self.crc[i]:
                        unstable += 1
                        self.flags[i] |= F_UNSTABLE
                    self.crc[i] = c
            self.rlat[i] = ms
            if ms is not None:
                self.rworst[i] = max(self.rworst[i], ms)
            lats.append(ms)
            sizes.append(size)
            self._record(p, "read", i, ms, size)
        self._flush_batch(p, "read")
        st = self._stats(lats, sizes, errors)
        st["bad_sectors"] = bad_secs
        st["unstable_blocks"] = unstable
        return st

    # ------------------------------------------------------------- execução
    def run(self) -> dict:
        self.emit({"ev": "dstart", "total": self.total, "block": self.block, "nblocks": self.n,
                   "mode": self.mode, "passes": self.passes, "until_fail": self.until_fail,
                   "seed": self.seed, "direct": getattr(self.io, "direct", None)})
        status = "done"
        p = 0
        try:
            while True:
                p += 1
                if not self.until_fail and p > self.passes:
                    break
                s = {"pass": p, "pattern": PATTERNS[(p - 1) % len(PATTERNS)] if self.mode == "escrita" else None}
                if self.mode == "escrita":
                    s["write"] = self._write_pass(p)
                s["read"] = self._read_pass(p)
                self.summaries.append(s)
                self.emit({"ev": "dpass", **s})
                failed = (s["read"]["errors"] or s["read"].get("bad_sectors") or
                          s["read"].get("unstable_blocks") or (s.get("write") or {}).get("errors"))
                if self.until_fail and failed:
                    status = "failed"
                    break
        except Cancelled:
            status = "cancelled"
        except DeviceLost as e:
            status = "lost"
            self.lost = True
            self.emit({"ev": "log", "level": "error", "msg": f"O cartão parou de responder ({e})."})
        res = assess(self)
        res.update({"ev": "dresult", "status": status, "passes_done": len(self.summaries),
                    "elapsed": time.monotonic() - self.t0, "written": self.written, "read": self.readb,
                    "summaries": self.summaries, "mode": self.mode, "block": self.block,
                    "total": self.total})
        return res


# ====================================================================== nota
def assess(d: Diagnoser) -> dict:
    """Nota de saúde 0–100, veredito e achados em português."""
    sm = d.summaries
    fl = d.flags
    cnt = lambda bit: sum(1 for f in fl if f & bit)  # noqa: E731
    rerr, werr = cnt(F_RERR), cnt(F_WERR)
    corrupt, unstable, alias, lostw = cnt(F_CORRUPT), cnt(F_UNSTABLE), cnt(F_ALIAS), cnt(F_LOSTW)
    findings = []
    score = 100.0
    cap = 100.0

    if d.lost:
        cap = min(cap, 5)
        findings.append(("critical", "O cartão parou de responder no meio do teste."))
    if alias:
        cap = 0
        ex = ", ".join(f"setor {a:,} → {b:,}".replace(",", ".") for a, b in d.alias_examples[:3])
        findings.append(("critical", f"Capacidade falsa: setores gravados reaparecem em outro endereço ({ex}). "
                                     "O cartão é menor do que diz ser."))
    if corrupt:
        cap = min(cap, 10)
        findings.append(("critical", f"{corrupt} bloco(s) devolveram dados diferentes do que foi gravado "
                                     "(bits corrompidos)."))
    if lostw:
        cap = min(cap, 10)
        findings.append(("critical", f"{lostw} bloco(s) aceitaram a escrita mas mantiveram o dado antigo "
                                     "(escrita perdida)."))
    if unstable:
        cap = min(cap, 25)
        findings.append(("critical", f"{unstable} bloco(s) devolveram dados diferentes a cada leitura "
                                     "(leitura instável — células fracas)."))
    if rerr or werr:
        score -= 35 + min(25, 2 * (rerr + werr))
        cap = min(cap, 45)
        partes = []
        if rerr:
            partes.append(f"{rerr} com erro de leitura")
        if werr:
            partes.append(f"{werr} com erro de escrita")
        findings.append(("critical", "Blocos com erro de E/S: " + " e ".join(partes) + "."))

    if sm:
        last = sm[-1]["read"]
        n = max(1, d.n)
        slow_pct = 100 * last["slow"] / n
        if slow_pct >= 0.5:
            score -= min(25, slow_pct * 2.5)
            findings.append(("warning" if slow_pct < 5 else "serious",
                             f"{last['slow']} bloco(s) lentos na leitura ({slow_pct:.1f}%) — "
                             "o controlador está corrigindo erros ou relendo células.".replace(".", ",", 1)))
        if last["very_slow"]:
            score -= min(20, 2 * last["very_slow"])
            findings.append(("serious", f"{last['very_slow']} bloco(s) muito lentos (acima de "
                                        f"{max(500, 20 * last['lat_p50']):.0f} ms)."))
        if last["lat_p50"] and last["lat_p99"] > 8 * last["lat_p50"]:
            score -= 5
            findings.append(("warning", f"Latência irregular: p99 de {last['lat_p99']:.0f} ms contra mediana de "
                                        f"{last['lat_p50']:.0f} ms."))
        if len(sm) >= 2 and sm[0]["read"]["mbps"]:
            ch = 100 * (last["mbps"] - sm[0]["read"]["mbps"]) / sm[0]["read"]["mbps"]
            if ch <= -15:
                score -= min(15, -ch / 2)
                findings.append(("serious", f"A leitura ficou {-ch:.0f}% mais lenta da 1ª para a última passada "
                                            "(degradação durante o teste)."))
        w = [s["write"]["mbps"] for s in sm if "write" in s and s["write"]["mbps"]]
        if len(w) >= 2:
            ch = 100 * (w[-1] - w[0]) / w[0]
            if ch <= -20:
                score -= min(15, -ch / 2)
                findings.append(("serious", f"A escrita ficou {-ch:.0f}% mais lenta ao longo das passadas."))
        if not findings:
            findings.append(("good", "Nenhum erro, nenhuma divergência e latência uniforme em todo o cartão."))
        r_avg = last["mbps"]
        findings.append(("info", f"Leitura média {r_avg:.1f} MB/s".replace(".", ",") +
                         (f", escrita média {w[-1]:.1f} MB/s".replace(".", ",") if w else "") +
                         f", mediana {last['lat_p50']:.1f} ms por bloco de ".replace(".", ",", 1) + _bk(d.block) + "."))
    else:
        findings.append(("info", "Nenhuma passada completa."))

    score = max(0.0, min(score, cap))
    if alias:
        verdict = "Falsificado"
    elif score >= 85:
        verdict = "Saudável"
    elif score >= 60:
        verdict = "Atenção"
    elif score >= 30:
        verdict = "Degradado"
    else:
        verdict = "Reprovado"
    level = {"Saudável": "good", "Atenção": "warning", "Degradado": "serious"}.get(verdict, "critical")
    return {"score": round(score), "verdict": verdict, "level": level,
            "findings": [{"level": a, "text": b} for a, b in findings],
            "counts": {"read_errors": rerr, "write_errors": werr, "corrupt": corrupt,
                       "unstable": unstable, "alias": alias, "lost_writes": lostw}}


def report_text(info: dict, res: dict) -> str:
    """Relatório legível do diagnóstico."""
    L = ["ARCA — DIAGNÓSTICO DA UNIDADE", "=" * 60,
         f"Data:        {time.strftime('%d/%m/%Y %H:%M:%S')}",
         f"Unidade:     {info.get('model', '?')}  S/N {info.get('serial') or '—'}",
         f"Dispositivo: {info.get('path', '?')}  ({res['total']:,} bytes)".replace(",", "."),
         f"Modo:        {'leitura e escrita (destrutivo)' if res['mode'] == 'escrita' else 'só leitura'}"
         f" · bloco {_bk(res['block'])} · {res['passes_done']} passada(s)",
         f"Situação:    {res['status']}",
         f"Duração:     {res['elapsed'] / 60:.1f} min · lido {res['read'] / 1e9:.2f} GB · "
         f"gravado {res['written'] / 1e9:.2f} GB",
         "", f"NOTA: {res['score']}/100 — {res['verdict'].upper()}", ""]
    for f in res["findings"]:
        mark = {"good": "OK ", "info": " · ", "warning": " ! ", "serious": "!! ", "critical": "XX "}[f["level"]]
        L.append(f"  {mark} {f['text']}")
    L += ["", "Por passada:", "  nº  padrão      leitura MB/s  escrita MB/s  p50 ms  p99 ms  lentos  erros  divergências"]
    for s in res["summaries"]:
        r, w = s["read"], s.get("write")
        L.append(f"  {s['pass']:<3} {(s['pattern'] or '—'):<11} {r['mbps']:>12.2f}  "
                 f"{(w['mbps'] if w else 0):>12.2f}  {r['lat_p50']:>6.1f}  {r['lat_p99']:>6.1f}  "
                 f"{r['slow']:>6}  {r['errors'] + (w['errors'] if w else 0):>5}  "
                 f"{r.get('bad_sectors', 0) + r.get('unstable_blocks', 0):>12}")
    return "\n".join(L) + "\n"

"""Mapa de setores (formato compatível com o mapfile do GNU ddrescue).

Cada intervalo do disco tem um estado:
    ?  ainda não lido
    *  falhou na cópia em blocos, falta aparar (trim)
    /  aparado, falta raspar setor a setor (scrape)
    -  setor ruim (falhou na leitura individual)
    +  recuperado
"""
from __future__ import annotations

import os
from bisect import bisect_left, bisect_right

NON_TRIED = "?"
NON_TRIMMED = "*"
NON_SCRAPED = "/"
BAD = "-"
FINISHED = "+"
STATUSES = (NON_TRIED, NON_TRIMMED, NON_SCRAPED, BAD, FINISHED)


class MapFile:
    def __init__(self, total: int):
        if total <= 0:
            raise ValueError("tamanho do dispositivo inválido")
        self.total = total
        self.starts = [0]
        self.sizes = [total]
        self.stats = [NON_TRIED]

    # ------------------------------------------------------------------ edição
    def set(self, pos: int, size: int, st: str) -> None:
        end = min(pos + size, self.total)
        pos = max(pos, 0)
        if end <= pos:
            return
        starts, sizes, stats = self.starts, self.sizes, self.stats
        i = bisect_right(starts, pos) - 1
        j = bisect_left(starts, end)
        new = []
        s0, t0 = starts[i], stats[i]
        if s0 < pos:
            new.append([s0, pos - s0, t0])
        new.append([pos, end - pos, st])
        sl, zl, tl = starts[j - 1], sizes[j - 1], stats[j - 1]
        if sl + zl > end:
            new.append([end, sl + zl - end, tl])
        lo, hi = i, j
        if lo > 0 and stats[lo - 1] == new[0][2]:
            new[0] = [starts[lo - 1], sizes[lo - 1] + new[0][1], new[0][2]]
            lo -= 1
        if hi < len(starts) and stats[hi] == new[-1][2]:
            new[-1] = [new[-1][0], new[-1][1] + sizes[hi], new[-1][2]]
            hi += 1
        merged: list = []
        for item in new:
            if merged and merged[-1][2] == item[2]:
                merged[-1][1] += item[1]
            else:
                merged.append(item)
        starts[lo:hi] = [m[0] for m in merged]
        sizes[lo:hi] = [m[1] for m in merged]
        stats[lo:hi] = [m[2] for m in merged]

    # ---------------------------------------------------------------- consulta
    def intervals(self, wanted=None, lo: int = 0, hi: int | None = None):
        """Lista (pos, size, status) recortada em [lo, hi)."""
        hi = self.total if hi is None else min(hi, self.total)
        out = []
        i = max(bisect_right(self.starts, lo) - 1, 0)
        n = len(self.starts)
        while i < n and self.starts[i] < hi:
            s, z, t = self.starts[i], self.sizes[i], self.stats[i]
            a, b = max(s, lo), min(s + z, hi)
            if b > a and (wanted is None or t in wanted):
                out.append((a, b - a, t))
            i += 1
        return out

    def count(self, st: str) -> int:
        return sum(z for z, t in zip(self.sizes, self.stats) if t == st)

    def counts(self) -> dict:
        c = {s: 0 for s in STATUSES}
        for z, t in zip(self.sizes, self.stats):
            c[t] += z
        return c

    def error_areas(self) -> int:
        return sum(1 for t in self.stats if t in (BAD, NON_TRIMMED, NON_SCRAPED))

    def as_list(self):
        return [[s, z, t] for s, z, t in zip(self.starts, self.sizes, self.stats)]

    @classmethod
    def from_list(cls, total: int, items) -> "MapFile":
        m = cls(total)
        for s, z, t in items:
            m.set(int(s), int(z), t)
        return m

    # ------------------------------------------------------------- persistência
    def save(self, path: str, current_pos: int = 0, current_status: str = "?",
             current_pass: int = 1) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write("# Mapfile. Created by Arca (compatível com GNU ddrescue)\n")
            f.write("# current_pos  current_status  current_pass\n")
            f.write(f"0x{current_pos:08X}     {current_status}               {current_pass}\n")
            f.write("#      pos        size  status\n")
            for s, z, t in zip(self.starts, self.sizes, self.stats):
                f.write(f"0x{s:08X}  0x{z:08X}  {t}\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str, total: int) -> "MapFile":
        m = cls(total)
        header_seen = False
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if not header_seen:          # linha current_pos / status / pass
                    header_seen = True
                    continue
                if len(parts) < 3 or parts[2] not in STATUSES:
                    raise ValueError(f"linha inválida no mapa: {line}")
                pos, size = int(parts[0], 0), int(parts[1], 0)
                if pos + size > total:
                    raise ValueError("o mapa é de um dispositivo de tamanho diferente")
                m.set(pos, size, parts[2])
        return m


def region_status(m: MapFile, start: int, size: int) -> str:
    """Resumo de uma região: ok / bad / partial / pending."""
    parts = m.intervals(None, start, start + size)
    sts = {t for _, _, t in parts}
    if sts == {FINISHED}:
        return "ok"
    if sts & {BAD}:
        return "bad"
    if sts == {NON_TRIED}:
        return "pending"
    if FINISHED in sts and sts <= {FINISHED, NON_TRIED}:
        return "partial"
    return "bad" if sts & {NON_TRIMMED, NON_SCRAPED} else "partial"

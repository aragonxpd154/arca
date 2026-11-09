"""Processo privilegiado do Arca (roda como root via pkexec).

Fala com a interface por JSON, uma linha por evento, no stdout.
Para cancelar, a interface escreve "cancel" no stdin (ou fecha o stdin).

Também serve como ferramenta de linha de comando (--texto), por exemplo:
    sudo arca-worker resgatar /dev/sdb ~/cf.img --texto
    sudo arca-worker gravar ~/cf.img /dev/sdc --texto
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "arca"

from arca import bootinfo, devices, writer  # noqa: E402
from arca.mapfile import BAD, MapFile  # noqa: E402
from arca.rescue import DeviceReader, Rescuer, device_size  # noqa: E402

_cancel = threading.Event()
_text = False
_lock = threading.Lock()


def emit(ev: dict):
    with _lock:
        if _text:
            _print_text(ev)
        else:
            sys.stdout.write(json.dumps(ev, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def _print_text(ev):
    e = ev.get("ev")
    if e == "progress":
        pct = 100 * ev["rescued"] / ev["total"]
        sys.stderr.write(f"\r[{ev['phase_name']:<22}] {pct:6.2f}%  recuperado {devices.human_bin(ev['rescued'])}"
                         f"  ruins {devices.human_bin(ev['bad'])}  {devices.human_bin(ev['rate'])}/s   ")
    elif e == "wprogress":
        fase = "Gravando" if ev["phase"] == "write" else "Verificando"
        sys.stderr.write(f"\r{fase}: {100 * ev['done'] / ev['total']:6.2f}%  {devices.human_bin(ev['rate'])}/s   ")
    elif e in ("log", "error"):
        sys.stderr.write(f"\n{ev.get('level', 'erro').upper()}: {ev['msg']}\n")
    elif e == "phase":
        sys.stderr.write(f"\n→ {ev['phase_name']}\n")
    elif e in ("finished", "wdone", "report"):
        sys.stderr.write("\n" + json.dumps(ev, ensure_ascii=False, indent=2) + "\n")


def _watch_stdin():
    try:
        for line in sys.stdin:
            if line.strip() == "cancel":
                break
    except Exception:
        pass
    _cancel.set()


def _owner():
    uid = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
    if not uid:
        return None
    gid = os.environ.get("SUDO_GID")
    if gid is None:
        try:
            import pwd
            gid = pwd.getpwuid(int(uid)).pw_gid
        except Exception:
            gid = int(uid)
    return int(uid), int(gid)


def _chown(*paths):
    o = _owner()
    if not o or os.geteuid() != 0:
        return
    for p in paths:
        try:
            os.chown(p, *o)
        except OSError:
            pass


def _sysfs(dev: str):
    return f"/sys/block/{os.path.basename(os.path.realpath(dev))}"


class Tuning:
    """Ajustes temporários para resgate: sem read-ahead e timeout SCSI menor."""

    def __init__(self, dev, timeout):
        self.saved = {}
        base = _sysfs(dev)
        self.items = [(f"{base}/queue/read_ahead_kb", "0")]
        if timeout:
            self.items.append((f"{base}/device/timeout", str(timeout)))

    def __enter__(self):
        for path, val in self.items:
            try:
                with open(path) as f:
                    self.saved[path] = f.read().strip()
                with open(path, "w") as f:
                    f.write(val)
            except OSError:
                pass
        return self

    def __exit__(self, *a):
        for path, val in self.saved.items():
            try:
                with open(path, "w") as f:
                    f.write(val)
            except OSError:
                pass


def _check_block(path, allow_file):
    st = os.stat(path)
    if stat.S_ISBLK(st.st_mode):
        return True
    if allow_file and stat.S_ISREG(st.st_mode):
        return False
    raise SystemExit(f"{path} não é um dispositivo de bloco")


def _unmount(disk):
    for part in disk["parts"] + [{"path": disk["path"], "mounts": disk["mounts"]}]:
        if part["mounts"]:
            try:
                subprocess.run(["umount", part["path"]], capture_output=True, timeout=30)
            except (OSError, subprocess.SubprocessError):
                pass


def report_text(src, img, m: MapFile, sector, boot, sha, status, elapsed) -> str:
    from arca.mapfile import region_status
    c = m.counts()
    L = ["ARCA — RELATÓRIO DE RESGATE", "=" * 60,
         f"Data:            {time.strftime('%d/%m/%Y %H:%M:%S')}",
         f"Origem:          {src}", f"Imagem:          {img}",
         f"Situação:        {status}",
         f"Tamanho:         {m.total:,} bytes ({m.total // sector:,} setores de {sector} B)".replace(",", "."),
         f"Recuperado:      {c['+']:,} bytes ({100 * c['+'] / m.total:.4f}%)".replace(",", ";").replace(".", ",").replace(";", "."),
         f"Setores ruins:   {c['-'] // sector:,}".replace(",", "."),
         f"Não lido ainda:  {(c['?'] + c['*'] + c['/']):,} bytes".replace(",", "."),
         f"Tempo:           {elapsed / 60:.1f} min",
         f"SHA-256 imagem:  {sha}", "",
         f"Esquema de partição: {boot.get('scheme')}"]
    for p in boot.get("partitions", []):
        fs = p.get("fs") or {}
        L.append(f"  Partição {p['index']}: {p['type_name']}  início LBA {p['start']}  "
                 f"{p['sectors']} setores  FS={fs.get('fs', '?')}  rótulo={fs.get('label', '')}")
    L += ["", "Estruturas críticas:"]
    for r in boot.get("regions", []):
        st = {"ok": "OK", "bad": "COM SETORES RUINS", "partial": "PARCIAL", "pending": "NÃO LIDO"}[
            region_status(m, r["start"], r["size"])]
        L.append(f"  [{st:^18}] {r['name']} (setor {r['start'] // sector}, {r['size'] // sector or 1} setores)")
    bad = m.intervals({BAD})
    L += ["", f"Áreas ruins ({len(bad)}):"]
    for p, s, _ in bad[:5000]:
        L.append(f"  setor {p // sector} … {(p + s) // sector - 1}  ({s // sector} setores)")
    if len(bad) > 5000:
        L.append(f"  … e mais {len(bad) - 5000} áreas (veja o arquivo .map)")
    L += ["", "Setores ruins ficam zerados na imagem. O arquivo .map é compatível com o GNU ddrescue."]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------- comandos
def cmd_listar(a):
    print(json.dumps(devices.list_disks(), ensure_ascii=False, indent=2))


def cmd_resgatar(a):
    is_blk = _check_block(a.origem, a.permitir_arquivo)
    disk = devices.get_disk(a.origem) if is_blk else None
    if disk and disk["system"] and not a.forcar:
        raise SystemExit("Recusado: essa origem é o disco do sistema.")
    if disk and a.desmontar:
        _unmount(disk)
    img = os.path.abspath(a.imagem)
    mp = a.mapa or img + ".map"
    if a.do_zero:
        for p in (img, mp):
            if os.path.exists(p):
                os.remove(p)
    total = device_size(a.origem)
    sector = disk["sector"] if disk else 512
    reader = DeviceReader(a.origem, sector)
    emit({"ev": "device", "path": a.origem, "model": disk["model"] if disk else "arquivo",
          "serial": disk["serial"] if disk else "", "size": total, "sector": sector,
          "direct": reader.direct})
    tuning = Tuning(a.origem, a.timeout) if is_blk and os.geteuid() == 0 else None
    if tuning:
        tuning.__enter__()
    r = None
    try:
        r = Rescuer(reader, total, img, mp, sector=sector, cluster=a.bloco * 1024,
                    retries=a.tentativas, emit=emit, cancel=_cancel.is_set,
                    resume=not a.do_zero, boot_first=True)
        _chown(img, mp)
        res = r.run()
        status = {"done": "Concluído", "cancelled": "Interrompido pelo usuário (pode retomar)",
                  "lost": "Cartão desconectou (pode retomar)"}[res["status"]]
        emit({"ev": "log", "level": "info", "msg": "Calculando SHA-256 da imagem…"})
        sha = writer.sha256_file(img, total)
        rep = os.path.splitext(img)[0] + ".relatorio.txt"
        with open(rep, "w") as f:
            f.write(report_text(a.origem, img, r.map, sector, r.boot or {}, sha, status,
                                res["elapsed"]))
        _chown(img, mp, rep)
        res.update({"sha256": sha, "report": rep, "image": img, "map": mp})
        emit(res)
        return 0 if res["status"] == "done" else 3
    finally:
        if r:
            r.close()
        reader.close()
        if tuning:
            tuning.__exit__()


def cmd_gravar(a):
    is_blk = _check_block(a.destino, a.permitir_arquivo)
    img_size = os.path.getsize(a.imagem)
    if is_blk:
        disk = devices.get_disk(a.destino)
        if not disk:
            raise SystemExit("Dispositivo de destino não encontrado.")
        if disk["system"]:
            raise SystemExit("Recusado: o destino é o disco do sistema.")
        if disk["readonly"]:
            raise SystemExit("O cartão de destino está protegido contra escrita.")
        if disk["mounts"]:
            if not a.desmontar:
                raise SystemExit("O destino está montado. Desmonte antes ou use --desmontar.")
            _unmount(disk)
        tsize = device_size(a.destino)
    else:
        tsize = os.path.getsize(a.destino)
    with open(a.imagem, "rb") as f:
        boot = bootinfo.analyze(lambda p, s: (f.seek(p), f.read(s))[1], img_size)
    ft = writer.fit(img_size, tsize, boot)
    emit({"ev": "fit", **ft})
    if ft["level"] == "too_small":
        raise SystemExit(ft["msg"])
    res = writer.write_image(a.imagem, a.destino, emit, verify=not a.sem_verificar,
                             length=ft["write_len"], cancel=_cancel.is_set)
    if is_blk:  # avisa o kernel da nova tabela de partições
        for cmd in (["partprobe", a.destino], ["blockdev", "--rereadpt", a.destino]):
            try:
                if subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0:
                    break
            except (OSError, subprocess.SubprocessError):
                continue
    emit(res)
    return 0 if res.get("ok") else 4


def main(argv=None):
    global _text
    ap = argparse.ArgumentParser(prog="arca-worker", description="Arca — resgate e clonagem de cartões CF")
    ap.add_argument("--texto", action="store_true", help="saída legível no terminal")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("listar", help="lista os discos")
    r = sub.add_parser("resgatar", help="copia o cartão para uma imagem, setor a setor")
    r.add_argument("origem")
    r.add_argument("imagem")
    r.add_argument("--mapa")
    r.add_argument("--tentativas", type=int, default=3)
    r.add_argument("--bloco", type=int, default=64, help="tamanho do bloco da cópia rápida, em KiB")
    r.add_argument("--timeout", type=int, default=10, help="timeout SCSI por comando (s)")
    r.add_argument("--do-zero", action="store_true", help="ignora mapa existente e recomeça")
    r.add_argument("--desmontar", action="store_true")
    r.add_argument("--forcar", action="store_true")
    r.add_argument("--permitir-arquivo", action="store_true", help=argparse.SUPPRESS)
    g = sub.add_parser("gravar", help="grava uma imagem num cartão")
    g.add_argument("imagem")
    g.add_argument("destino")
    g.add_argument("--sem-verificar", action="store_true")
    g.add_argument("--desmontar", action="store_true")
    g.add_argument("--permitir-arquivo", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    _text = a.texto
    if a.cmd == "listar":
        return cmd_listar(a)
    try:  # só a interface (stdin em pipe) controla o cancelamento por stdin
        if stat.S_ISFIFO(os.fstat(0).st_mode):
            threading.Thread(target=_watch_stdin, daemon=True).start()
    except OSError:
        pass
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, lambda *x: _cancel.set())
    try:
        return {"resgatar": cmd_resgatar, "gravar": cmd_gravar}[a.cmd](a)
    except SystemExit as e:
        if isinstance(e.code, str):
            emit({"ev": "error", "msg": e.code})
            return 2
        raise
    except Exception as e:  # noqa: BLE001
        emit({"ev": "error", "msg": f"{type(e).__name__}: {e}"})
        return 1


if __name__ == "__main__":
    sys.exit(main() or 0)

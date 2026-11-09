"""Interface gráfica do Arca (GTK 4 + libadwaita).

A interface roda como usuário comum; leitura e gravação dos cartões são feitas
pelo processo arca-worker, elevado via pkexec (pede a senha uma vez).
"""
from __future__ import annotations

import json
import os
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, APP_NAME, VERSION, bootinfo, devices, writer  # noqa: E402
from .mapfile import BAD, FINISHED, NON_SCRAPED, NON_TRIED, NON_TRIMMED, MapFile, region_status  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALLED_WORKER = "/usr/libexec/arca/arca-worker"

BIT = {NON_TRIED: 1, NON_TRIMMED: 2, NON_SCRAPED: 4, BAD: 8, FINISHED: 16}

CSS = """
.stat-tile { padding: 12px 16px; }
.stat-value { font-size: 1.30em; font-weight: 700; font-feature-settings: "tnum"; }
.stat-label { font-size: 0.82em; opacity: 0.65; }
.big-percent { font-size: 2.6em; font-weight: 800; font-feature-settings: "tnum"; }
.phase-badge { padding: 4px 12px; border-radius: 99px; font-weight: 600;
               background: alpha(@accent_bg_color, 0.16); color: @accent_color; }
.phase-badge.done { background: alpha(@success_color, 0.18); color: @success_color; }
.phase-badge.fail { background: alpha(@error_color, 0.18); color: @error_color; }
.map-card { padding: 14px; }
.legend-dot { min-width: 11px; min-height: 11px; border-radius: 3px; }
.dot-ok { background: #2ec27e; } .dot-partial { background: alpha(#2ec27e, 0.45); }
.dot-pending { background: alpha(currentColor, 0.18); } .dot-trim { background: #f6d32d; }
.dot-scrape { background: #ff7800; } .dot-bad { background: #e01b24; }
.dot-cur { background: @accent_bg_color; }
.banner { padding: 12px 14px; border-radius: 12px; }
.banner.ok { background: alpha(@success_color, 0.14); }
.banner.warn { background: alpha(@warning_color, 0.16); }
.banner.bad { background: alpha(@error_color, 0.14); }
.mono { font-family: monospace; font-size: 0.88em; }
.hero-icon { opacity: 0.85; }
.log-view { font-family: monospace; font-size: 0.85em; background: transparent; }
"""


def fmt_time(s):
    if s is None:
        return "—"
    s = int(s)
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def rate_txt(r):
    return f"{devices.human_bin(r)}/s" if r else "—"


# ============================================================== processo worker
class Worker:
    def __init__(self):
        self.proc = None
        self.stream = None
        self.got_event = False

    @property
    def running(self):
        return self.proc is not None

    def _argv(self, args):
        worker_py = os.path.join(HERE, "worker.py")
        if os.geteuid() == 0:
            return [sys.executable, worker_py] + args
        if os.path.exists(INSTALLED_WORKER):
            return ["pkexec", INSTALLED_WORKER] + args
        return ["pkexec", sys.executable, worker_py] + args

    def start(self, args, on_event, on_exit):
        flags = (Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDIN_PIPE |
                 Gio.SubprocessFlags.STDERR_PIPE)
        self.proc = Gio.Subprocess.new(self._argv(args), flags)
        self.stream = Gio.DataInputStream.new(self.proc.get_stdout_pipe())
        self.got_event = False
        self.on_event, self.on_exit = on_event, on_exit
        self._read_next()
        self.proc.wait_async(None, self._exited)

    def _read_next(self):
        self.stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._got_line)

    def _got_line(self, stream, res):
        try:
            line, _ = stream.read_line_finish_utf8(res)
        except GLib.Error:
            line = None
        if line is None:
            return
        try:
            ev = json.loads(line)
        except ValueError:
            ev = None
        if ev:
            self.got_event = True
            self.on_event(ev)
        self._read_next()

    def _exited(self, proc, res):
        try:
            proc.wait_finish(res)
            code = proc.get_exit_status() if proc.get_if_exited() else -1
        except GLib.Error:
            code = -1
        err = ""
        try:
            data = proc.get_stderr_pipe().read_bytes(65536, None).get_data()
            err = (data or b"").decode("utf-8", "replace").strip()
        except GLib.Error:
            pass
        got = self.got_event
        self.proc = None
        # deixa as últimas linhas do stdout chegarem antes de avisar o fim
        GLib.timeout_add(250, lambda: (self.on_exit(code, got, err), False)[1])

    def cancel(self):
        if self.proc:
            try:
                self.proc.get_stdin_pipe().write_bytes(GLib.Bytes.new(b"cancel\n"), None)
                self.proc.get_stdin_pipe().close(None)
            except GLib.Error:
                pass


# ================================================================ mapa de setores
class SectorMap(Gtk.DrawingArea):
    CELL, GAP = 9, 2

    def __init__(self):
        super().__init__()
        self.total = 0
        self.iv = []
        self.pos = None
        self.marks = []       # regiões críticas (boot)
        self.sector = 512
        self._cells = None
        self._geom = None
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_content_height(250)
        self.set_draw_func(self._draw)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tooltip)

    def reset(self, total=0, sector=512):
        self.total, self.sector, self.iv, self.pos, self.marks = total, sector, [], None, []
        self._cells = None
        self.queue_draw()

    def set_map(self, total, iv, pos=None):
        self.total, self.iv, self.pos = total, iv, pos
        self._cells = None
        self.queue_draw()

    def set_marks(self, regions):
        self.marks = [(r["start"], r["size"]) for r in regions]
        self._cells = None
        self.queue_draw()

    def _layout(self, w, h):
        c, g = self.CELL, self.GAP
        cols = max(1, (w + g) // (c + g))
        rows = max(1, (h + g) // (c + g))
        n = cols * rows
        bpc = max(self.sector, -(-self.total // n)) if self.total else 1
        bpc += (-bpc) % self.sector
        used = -(-self.total // bpc) if self.total else n
        return cols, rows, bpc, used

    def _compute(self, n, bpc):
        flags = [0] * n
        for s, z, t in self.iv:
            a, b = s // bpc, min(n - 1, (s + z - 1) // bpc)
            bit = BIT[t]
            for i in range(a, b + 1):
                flags[i] |= bit
        mark = [False] * n
        for s, z in self.marks:
            for i in range(s // bpc, min(n - 1, (s + max(z, 1) - 1) // bpc) + 1):
                mark[i] = True
        return flags, mark

    def _color(self, f, dark):
        if not f or f == BIT[NON_TRIED]:
            return (1, 1, 1, 0.13) if dark else (0, 0, 0, 0.10)
        if f & BIT[BAD]:
            return (0.96, 0.38, 0.32, 1) if dark else (0.88, 0.11, 0.14, 1)
        if f & BIT[NON_SCRAPED]:
            return (1.0, 0.47, 0.0, 1)
        if f & BIT[NON_TRIMMED]:
            return (0.96, 0.83, 0.18, 1)
        if f == BIT[FINISHED]:
            return (0.18, 0.76, 0.49, 1)
        return (0.18, 0.76, 0.49, 0.45)  # parcialmente lido

    def _draw(self, area, cr, w, h):
        if not self.total:
            return
        cols, rows, bpc, used = self._layout(w, h)
        key = (cols, rows, len(self.iv), id(self.iv), len(self.marks))
        if self._cells is None or self._geom != key:
            self._cells = self._compute(used, bpc)
            self._geom = key
        flags, mark = self._cells
        dark = Adw.StyleManager.get_default().get_dark()
        c, g = self.CELL, self.GAP
        cur = self.pos // bpc if self.pos is not None else -1
        for i in range(used):
            x, y = (i % cols) * (c + g), (i // cols) * (c + g)
            cr.set_source_rgba(*self._color(flags[i], dark))
            cr.rectangle(x, y, c, c)
            cr.fill()
            if mark[i]:
                cr.set_source_rgba(1, 1, 1, 0.9) if dark or flags[i] & (BIT[FINISHED] | BIT[BAD]) \
                    else cr.set_source_rgba(0, 0, 0, 0.6)
                cr.arc(x + c / 2, y + c / 2, 1.6, 0, 6.2832)
                cr.fill()
        if 0 <= cur < used:
            x, y = (cur % cols) * (c + g), (cur // cols) * (c + g)
            cr.set_source_rgba(0.21, 0.52, 0.89, 1)
            cr.set_line_width(2)
            cr.rectangle(x - 1, y - 1, c + 2, c + 2)
            cr.stroke()

    def _tooltip(self, widget, x, y, kb, tip):
        if not self.total:
            return False
        cols, rows, bpc, used = self._layout(self.get_width(), self.get_height())
        c, g = self.CELL, self.GAP
        i = int(y // (c + g)) * cols + int(x // (c + g))
        if x // (c + g) >= cols or i >= used:
            return False
        a = i * bpc
        b = min(self.total, a + bpc)
        tmp = MapFile.from_list(self.total, self.iv) if self.iv else MapFile(self.total)
        parts = tmp.intervals(None, a, b)
        names = {FINISHED: "recuperado", NON_TRIED: "não lido", NON_TRIMMED: "erro (a aparar)",
                 NON_SCRAPED: "erro (a raspar)", BAD: "ruim"}
        agg = {}
        for _, z, t in parts:
            agg[t] = agg.get(t, 0) + z
        lines = [f"Setores {a // self.sector:,} – {b // self.sector - 1:,}".replace(",", ".")]
        for t, z in sorted(agg.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {names[t]}: {z // self.sector:,} setores".replace(",", "."))
        tip.set_text("\n".join(lines))
        return True


# ============================================================== peças de interface
def stat_tile(label):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    box.add_css_class("card")
    box.add_css_class("stat-tile")
    val = Gtk.Label(label="—", xalign=0)
    val.add_css_class("stat-value")
    lab = Gtk.Label(label=label, xalign=0)
    lab.add_css_class("stat-label")
    box.append(val)
    box.append(lab)
    box.set_hexpand(True)
    return box, val


def legend_item(css, text):
    b = Gtk.Box(spacing=6)
    dot = Gtk.Box()
    dot.add_css_class("legend-dot")
    dot.add_css_class(css)
    dot.set_valign(Gtk.Align.CENTER)
    lab = Gtk.Label(label=text)
    lab.add_css_class("caption")
    b.append(dot)
    b.append(lab)
    return b


def group(title, desc=None):
    g = Adw.PreferencesGroup()
    g.set_title(title)
    if desc:
        g.set_description(desc)
    return g


def row(title, subtitle=""):
    r = Adw.ActionRow()
    r.set_title(title)
    if subtitle:
        r.set_subtitle(subtitle)
    return r


def status_dot(state):
    css = {"ok": "dot-ok", "bad": "dot-bad", "partial": "dot-partial", "pending": "dot-pending"}[state]
    d = Gtk.Box()
    d.add_css_class("legend-dot")
    d.add_css_class(css)
    d.set_valign(Gtk.Align.CENTER)
    return d


STATE_TXT = {"ok": "Íntegro", "bad": "Com setores ruins", "partial": "Lendo…", "pending": "Aguardando"}


class DevicePicker:
    """DropDown de cartões, com atualização automática ao plugar/desplugar."""

    def __init__(self, on_change):
        self.on_change = on_change
        self.disks = []
        self.dd = Gtk.DropDown.new_from_strings(["Nenhum cartão encontrado"])
        self.dd.set_valign(Gtk.Align.CENTER)
        self.dd.connect("notify::selected", lambda *a: self.on_change(self.selected()))
        self._sig = None

    def selected(self):
        i = self.dd.get_selected()
        return self.disks[i] if 0 <= i < len(self.disks) else None

    def refresh(self, force=False):
        disks = [d for d in devices.list_disks() if not d["system"]]
        sig = [(d["path"], d["size"], tuple(d["mounts"])) for d in disks]
        if sig == self._sig and not force:
            return
        self._sig = sig
        cur = self.selected()
        self.disks = disks
        labels = [f"{d['name']} — {d['model']} · {devices.human(d['size'])}" +
                  ("" if d["removable"] else " · interno") for d in disks] or ["Nenhum cartão encontrado"]
        self.dd.set_model(Gtk.StringList.new(labels))
        idx = 0
        if cur:
            for i, d in enumerate(disks):
                if d["path"] == cur["path"]:
                    idx = i
        self.dd.set_selected(idx)
        self.on_change(self.selected())


def describe_disk(d):
    if not d:
        return "Conecte o cartão CF num leitor USB."
    secs = d["size"] // max(d["sector"], 1)
    s = (f"{devices.human(d['size'])} · {secs:,} setores de {d['sector']} B · {d['tran'].upper()}"
         .replace(",", "."))
    if d["serial"]:
        s += f" · S/N {d['serial']}"
    if d["mounts"]:
        s += f"\nMontado em: {', '.join(d['mounts'])}"
    return s


# =================================================================== página resgate
class RescuePage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.win = win
        self.worker = Worker()
        self.total = 0
        self.boot = None
        self.last_iv = []
        self.result = None

        # ---------------- coluna esquerda: controles
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for m in ("top", "bottom", "start", "end"):
            getattr(left, f"set_margin_{m}")(18)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(left)
        sc.set_size_request(390, -1)
        self.append(sc)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        g = group("Cartão de origem", "O cartão desgastado. Ele só é lido, nunca gravado.")
        self.picker = DevicePicker(self._device_changed)
        r = row("Dispositivo")
        r.add_suffix(self.picker.dd)
        btn = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                         tooltip_text="Procurar cartões")
        btn.add_css_class("flat")
        btn.connect("clicked", lambda *a: self.picker.refresh(True))
        r.add_suffix(btn)
        g.add(r)
        self.dev_row = row("Detalhes", "")
        self.dev_row.set_subtitle_lines(3) if hasattr(self.dev_row, "set_subtitle_lines") else None
        g.add(self.dev_row)
        left.append(g)

        g = group("Imagem de saída", "Arquivo .img com a cópia setor a setor (+ mapa .map e relatório).")
        self.img_row = row("Arquivo", "")
        b = Gtk.Button(label="Escolher…", valign=Gtk.Align.CENTER)
        b.connect("clicked", self._choose_image)
        self.img_row.add_suffix(b)
        g.add(self.img_row)
        self.resume_row = row("Retomada", "")
        self.resume_row.set_visible(False)
        g.add(self.resume_row)
        left.append(g)

        g = group("Opções de resgate")
        self.retries = Gtk.SpinButton.new_with_range(0, 30, 1)
        self.retries.set_value(3)
        self.retries.set_valign(Gtk.Align.CENTER)
        r = row("Novas tentativas", "Quantas vezes reler cada setor ruim no final")
        r.add_suffix(self.retries)
        g.add(r)
        self.block = Gtk.DropDown.new_from_strings(["16 KiB", "32 KiB", "64 KiB", "128 KiB", "256 KiB"])
        self.block.set_selected(2)
        self.block.set_valign(Gtk.Align.CENTER)
        r = row("Bloco da cópia rápida", "Menor = isola melhor as áreas ruins")
        r.add_suffix(self.block)
        g.add(r)
        self.sw_unmount = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
        r = row("Desmontar o cartão antes", "Evita que o sistema mexa no cartão durante a leitura")
        r.add_suffix(self.sw_unmount)
        r.set_activatable_widget(self.sw_unmount)
        g.add(r)
        self.sw_fresh = Gtk.Switch(active=False, valign=Gtk.Align.CENTER)
        r = row("Recomeçar do zero", "Ignora um resgate anterior deste arquivo")
        r.add_suffix(self.sw_fresh)
        r.set_activatable_widget(self.sw_fresh)
        g.add(r)
        left.append(g)

        bb = Gtk.Box(spacing=10, homogeneous=True)
        self.btn_start = Gtk.Button(label="Iniciar resgate")
        self.btn_start.add_css_class("suggested-action")
        self.btn_start.add_css_class("pill")
        self.btn_start.connect("clicked", self._start)
        self.btn_stop = Gtk.Button(label="Parar", sensitive=False)
        self.btn_stop.add_css_class("destructive-action")
        self.btn_stop.add_css_class("pill")
        self.btn_stop.connect("clicked", lambda *a: self._stop())
        bb.append(self.btn_start)
        bb.append(self.btn_stop)
        left.append(bb)

        self.result_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.result_box.add_css_class("banner")
        self.result_box.set_visible(False)
        self.result_title = Gtk.Label(xalign=0, wrap=True)
        self.result_title.add_css_class("heading")
        self.result_text = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.result_text.add_css_class("caption")
        rb = Gtk.Box(spacing=8)
        b1 = Gtk.Button(label="Ver relatório")
        b1.connect("clicked", lambda *a: self._open(self.result and self.result.get("report")))
        b2 = Gtk.Button(label="Gravar em outro cartão")
        b2.add_css_class("suggested-action")
        b2.connect("clicked", lambda *a: self.win.go_write(self.result and self.result.get("image")))
        rb.append(b1)
        rb.append(b2)
        for w in (self.result_title, self.result_text, rb):
            self.result_box.append(w)
        left.append(self.result_box)

        # ---------------- coluna direita: mapa e números
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for m in ("top", "bottom", "start", "end"):
            getattr(right, f"set_margin_{m}")(20)
        rs = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, hexpand=True)
        rs.set_child(right)
        self.append(rs)

        top = Gtk.Box(spacing=14)
        tbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        t = Gtk.Label(label="Mapa de setores", xalign=0)
        t.add_css_class("title-2")
        self.phase = Gtk.Label(label="Pronto", xalign=0, halign=Gtk.Align.START)
        self.phase.add_css_class("phase-badge")
        tbox.append(t)
        tbox.append(self.phase)
        self.pct = Gtk.Label(label="0%", valign=Gtk.Align.CENTER)
        self.pct.add_css_class("big-percent")
        top.append(tbox)
        top.append(self.pct)
        right.append(top)

        self.bar = Gtk.ProgressBar()
        right.append(self.bar)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.add_css_class("card")
        card.add_css_class("map-card")
        self.map = SectorMap()
        card.append(self.map)
        leg = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=8,
                          column_spacing=14, row_spacing=4)
        for css, txt in (("dot-ok", "Recuperado"), ("dot-partial", "Parcial"), ("dot-pending", "Não lido"),
                         ("dot-trim", "Erro · aparar"), ("dot-scrape", "Erro · raspar"),
                         ("dot-bad", "Setor ruim"), ("dot-cur", "Posição atual")):
            leg.append(legend_item(css, txt))
        hint = Gtk.Label(label="• ponto branco = estrutura de boot (MBR, boot sector, FATs, raiz). "
                               "Passe o mouse sobre o mapa para ver os setores.", xalign=0, wrap=True)
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        card.append(leg)
        card.append(hint)
        right.append(card)

        grid = Gtk.Grid(column_spacing=10, row_spacing=10, column_homogeneous=True)
        self.st = {}
        for i, (k, lab) in enumerate((("rescued", "Recuperado"), ("bad", "Setores ruins"),
                                      ("areas", "Áreas com erro"), ("pending", "Falta ler"),
                                      ("rate", "Velocidade"), ("elapsed", "Tempo decorrido"),
                                      ("eta", "Restante (estimado)"), ("pos", "Setor atual"))):
            tile, val = stat_tile(lab)
            self.st[k] = val
            grid.attach(tile, i % 4, i // 4, 1, 1)
        right.append(grid)

        self.boot_group = group("Estruturas de boot",
                                "Lidas primeiro, porque sem elas o cartão não monta nem dá boot.")
        self.boot_rows = []
        right.append(self.boot_group)

        exp = Gtk.Expander(label="Registro de eventos")
        self.log = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log.add_css_class("log-view")
        ls = Gtk.ScrolledWindow(min_content_height=140)
        ls.set_child(self.log)
        exp.set_child(ls)
        right.append(exp)

        self.picker.refresh(True)
        GLib.timeout_add_seconds(3, self._poll)

    # ------------------------------------------------------------------ ações
    def _poll(self):
        if not self.worker.running:
            self.picker.refresh()
        return True

    def _device_changed(self, d):
        self.dev_row.set_subtitle(describe_disk(d))
        self.btn_start.set_sensitive(bool(d) and not self.worker.running)
        if d and not getattr(self, "_img_user", False):
            safe = "".join(ch if ch.isalnum() else "-" for ch in d["model"]).strip("-")[:24] or "cartao"
            folder = os.path.join(GLib.get_home_dir(), "Arca")
            self._set_image(os.path.join(folder, f"cf-{safe}-{time.strftime('%Y%m%d-%H%M')}.img"))
        elif d:
            self._check_resume()

    def _set_image(self, path):
        self.img_path = path
        self.img_row.set_subtitle(path)
        self._check_resume()

    def _check_resume(self):
        mp = getattr(self, "img_path", "") + ".map"
        d = self.picker.selected()
        if d and os.path.exists(mp) and os.path.exists(self.img_path):
            try:
                m = MapFile.load(mp, d["size"])
                pct = 100 * m.count(FINISHED) / m.total
                self.resume_row.set_subtitle(
                    f"Resgate anterior encontrado: {pct:.2f}% já recuperado. ".replace(".", ",", 1) +
                    "Ao iniciar, continua de onde parou.")
                self.map.reset(m.total, d["sector"])
                self.map.set_map(m.total, m.as_list())
                self._update_pct(m.count(FINISHED), m.total)
                self.btn_start.set_label("Retomar resgate")
            except (OSError, ValueError) as e:
                self.resume_row.set_subtitle(f"Existe um .map, mas não é deste cartão ({e}).")
            self.resume_row.set_visible(True)
        else:
            self.resume_row.set_visible(False)
            self.btn_start.set_label("Iniciar resgate")

    def _choose_image(self, *a):
        dlg = Gtk.FileChooserNative(title="Salvar imagem do cartão", transient_for=self.win,
                                    action=Gtk.FileChooserAction.SAVE, accept_label="Usar")
        dlg.set_current_name(os.path.basename(getattr(self, "img_path", "cartao.img")))
        f = Gtk.FileFilter()
        f.set_name("Imagens de disco (*.img)")
        f.add_pattern("*.img")
        dlg.add_filter(f)

        def resp(d, r):
            if r == Gtk.ResponseType.ACCEPT:
                p = d.get_file().get_path()
                if not p.endswith(".img"):
                    p += ".img"
                self._img_user = True
                self._set_image(p)
            self._dlg = None
        dlg.connect("response", resp)
        self._dlg = dlg
        dlg.show()

    def _start(self, *a):
        d = self.picker.selected()
        if not d:
            return
        os.makedirs(os.path.dirname(self.img_path), exist_ok=True)
        args = ["resgatar", d["path"], self.img_path,
                "--tentativas", str(int(self.retries.get_value())),
                "--bloco", str((16, 32, 64, 128, 256)[self.block.get_selected()])]
        if self.sw_unmount.get_active():
            args.append("--desmontar")
        if self.sw_fresh.get_active():
            args.append("--do-zero")
        self.result_box.set_visible(False)
        self.boot = None
        self._log(f"Iniciando resgate de {d['path']} → {self.img_path}")
        self.map.reset(d["size"], d["sector"])
        self._set_running(True)
        self._set_phase("Aguardando autorização…")
        self.worker.start(args, self._event, self._exit)

    def _stop(self):
        self._set_phase("Parando com segurança…")
        self.worker.cancel()

    def _set_running(self, on):
        self.btn_start.set_sensitive(not on and bool(self.picker.selected()))
        self.btn_stop.set_sensitive(on)
        self.picker.dd.set_sensitive(not on)
        self.win.set_busy(on)

    def _set_phase(self, txt, css=None):
        self.phase.set_label(txt)
        for c in ("done", "fail"):
            self.phase.remove_css_class(c)
        if css:
            self.phase.add_css_class(css)

    def _log(self, msg):
        buf = self.log.get_buffer()
        buf.insert(buf.get_end_iter(), time.strftime("%H:%M:%S  ") + msg + "\n")

    def _update_pct(self, rescued, total):
        frac = rescued / total if total else 0
        self.bar.set_fraction(frac)
        txt = f"{100 * frac:.2f}%" if 0 < frac < 1 else f"{100 * frac:.0f}%"
        self.pct.set_label(txt.replace(".", ","))

    # --------------------------------------------------------------- eventos
    def _event(self, ev):
        e = ev.get("ev")
        if e == "device":
            self._log(f"Cartão: {ev['model']} · {ev['size']:,} bytes · setor {ev['sector']} B".replace(",", ".")
                      + ("" if ev.get("direct") else " · sem O_DIRECT"))
        elif e == "start":
            self.total = ev["total"]
            if ev.get("resumed"):
                self._log("Retomando resgate anterior a partir do mapa salvo.")
        elif e == "phase":
            self._set_phase(ev["phase_name"], "done" if ev["phase"] == "done" else None)
            self._log(f"Fase: {ev['phase_name']}")
        elif e == "progress":
            sec = self.map.sector
            self._update_pct(ev["rescued"], ev["total"])
            self.st["rescued"].set_label(devices.human_bin(ev["rescued"]))
            self.st["bad"].set_label(f"{ev['bad'] // sec:,}".replace(",", "."))
            self.st["areas"].set_label(str(ev["bad_areas"]))
            self.st["pending"].set_label(devices.human_bin(ev["pending"] + ev["nontrimmed"] + ev["nonscraped"]))
            self.st["rate"].set_label(rate_txt(ev["rate"]))
            self.st["elapsed"].set_label(fmt_time(ev["elapsed"]))
            self.st["eta"].set_label(fmt_time(ev["eta"]))
            self.st["pos"].set_label(f"{ev['pos'] // sec:,}".replace(",", "."))
        elif e == "map":
            self.last_iv = ev["iv"]
            self.map.set_map(ev["total"], ev["iv"], ev.get("pos"))
            self._refresh_boot()
        elif e == "boot":
            self.boot = ev["info"]
            self.map.set_marks(self.boot.get("regions", []))
            self._refresh_boot(rebuild=True)
        elif e == "log":
            self._log(ev["msg"])
            if ev.get("level") in ("warn", "error"):
                self.win.toast(ev["msg"])
        elif e == "error":
            self._log("ERRO: " + ev["msg"])
            self.win.toast(ev["msg"])
            self._set_phase("Erro", "fail")
        elif e == "finished":
            self.result = ev
            self._show_result(ev)

    def _exit(self, code, got, err):
        self._set_running(False)
        if not got:
            msg = {126: "Autorização cancelada.", 127: "Autorização negada."}.get(
                code, f"O processo terminou sem resposta (código {code}). {err[-300:]}")
            self.win.toast(msg)
            self._set_phase("Pronto")
            self._log(msg)
        elif code not in (0, 3) and not self.result:
            self._set_phase("Falhou", "fail")
            if err:
                self._log(err[-800:])
        self.picker.refresh(True)

    def _refresh_boot(self, rebuild=False):
        if not self.boot:
            return
        m = MapFile.from_list(self.total or self.map.total, self.last_iv) if self.last_iv else None
        regs = self.boot.get("regions", [])
        if rebuild:
            for r in self.boot_rows:
                self.boot_group.remove(r)
            self.boot_rows = []
            info = row("Esquema", self.boot.get("scheme", "?"))
            parts = []
            for p in self.boot.get("partitions", []):
                fs = p.get("fs") or {}
                parts.append(f"Partição {p['index']}: {fs.get('fs') or p['type_name']} · "
                             f"{devices.human(p['sectors'] * 512)} · rótulo {fs.get('label', '—')}")
            if parts:
                info.set_subtitle(self.boot.get("scheme", "?") + "\n" + "\n".join(parts))
            self.boot_group.add(info)
            self.boot_rows.append(info)
            self._reg_rows = []
            for rg in regs:
                r = row(rg["name"], f"setor {rg['start'] // 512:,} · {max(1, rg['size'] // 512):,} setores"
                        .replace(",", "."))
                lab = Gtk.Label(valign=Gtk.Align.CENTER)
                lab.add_css_class("caption")
                holder = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
                holder.append(lab)
                r.add_suffix(holder)
                self.boot_group.add(r)
                self.boot_rows.append(r)
                self._reg_rows.append((rg, holder, lab))
        for rg, holder, lab in getattr(self, "_reg_rows", []):
            state = region_status(m, rg["start"], rg["size"]) if m else "pending"
            child = holder.get_first_child()
            if child is not lab:
                holder.remove(child)
            holder.prepend(status_dot(state))
            lab.set_label(STATE_TXT[state])

    def _show_result(self, ev):
        st = ev["status"]
        sec = self.map.sector
        pct = 100 * ev["rescued"] / ev["total"]
        for c in ("ok", "warn", "bad"):
            self.result_box.remove_css_class(c)
        if st == "done" and ev["bad"] == 0:
            css, title = "ok", "Cartão copiado por completo"
        elif st == "done":
            css, title = "warn", f"Resgate terminado com {ev['bad'] // sec:,} setor(es) ruim(ns)".replace(",", ".")
        else:
            css = "warn"
            title = "Resgate pausado — dá para retomar" if st == "cancelled" else \
                "O cartão desconectou — reconecte e clique em Retomar"
        self.result_box.add_css_class(css)
        self.result_title.set_label(title)
        self.result_text.set_label(
            f"{pct:.4f}% recuperado".replace(".", ",") + f" em {fmt_time(ev['elapsed'])}\n"
            f"Imagem: {ev.get('image', '')}\nSHA-256: {ev.get('sha256', '')}")
        self.result_box.set_visible(True)
        self._set_phase({"done": "Concluído", "cancelled": "Pausado", "lost": "Cartão desconectado"}[st],
                        "done" if st == "done" else "fail")
        self.win.toast(title)
        self._check_resume()

    def _open(self, path):
        if path:
            Gtk.show_uri(self.win, Gio.File.new_for_path(path).get_uri(), Gdk.CURRENT_TIME)


# =================================================================== página gravar
class WritePage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.win = win
        self.worker = Worker()
        self.img = None
        self.img_size = 0
        self.img_boot = None
        self.fit = None

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for m in ("top", "bottom", "start", "end"):
            getattr(left, f"set_margin_{m}")(18)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(left)
        sc.set_size_request(390, -1)
        self.append(sc)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        g = group("Imagem", "A imagem recuperada (.img) que vai para o cartão novo.")
        self.img_row = row("Arquivo", "Nenhuma imagem escolhida")
        b = Gtk.Button(label="Escolher…", valign=Gtk.Align.CENTER)
        b.connect("clicked", self._choose)
        self.img_row.add_suffix(b)
        g.add(self.img_row)
        self.img_info = row("Conteúdo", "—")
        g.add(self.img_info)
        self.img_warn = row("Atenção", "")
        self.img_warn.set_visible(False)
        g.add(self.img_warn)
        left.append(g)

        g = group("Cartão de destino", "Tudo que estiver nele será substituído.")
        self.picker = DevicePicker(self._target_changed)
        r = row("Dispositivo")
        r.add_suffix(self.picker.dd)
        btn = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER)
        btn.add_css_class("flat")
        btn.connect("clicked", lambda *a: self.picker.refresh(True))
        r.add_suffix(btn)
        g.add(r)
        self.tgt_row = row("Detalhes", "")
        g.add(self.tgt_row)
        left.append(g)

        self.fit_box = Gtk.Box(spacing=12)
        self.fit_box.add_css_class("banner")
        self.fit_icon = Gtk.Image(icon_size=Gtk.IconSize.LARGE, valign=Gtk.Align.START)
        self.fit_lbl = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self.fit_box.append(self.fit_icon)
        self.fit_box.append(self.fit_lbl)
        self.fit_box.set_visible(False)
        left.append(self.fit_box)

        g = group("Opções")
        self.sw_verify = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
        r = row("Verificar após gravar", "Lê o cartão de volta e compara o SHA-256")
        r.add_suffix(self.sw_verify)
        r.set_activatable_widget(self.sw_verify)
        g.add(r)
        self.sw_unmount = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
        r = row("Desmontar o destino automaticamente")
        r.add_suffix(self.sw_unmount)
        r.set_activatable_widget(self.sw_unmount)
        g.add(r)
        left.append(g)

        bb = Gtk.Box(spacing=10, homogeneous=True)
        self.btn_go = Gtk.Button(label="Gravar no cartão", sensitive=False)
        self.btn_go.add_css_class("destructive-action")
        self.btn_go.add_css_class("pill")
        self.btn_go.connect("clicked", self._confirm)
        self.btn_stop = Gtk.Button(label="Parar", sensitive=False)
        self.btn_stop.add_css_class("pill")
        self.btn_stop.connect("clicked", lambda *a: self.worker.cancel())
        bb.append(self.btn_go)
        bb.append(self.btn_stop)
        left.append(bb)

        # -------- direita: progresso grande
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, valign=Gtk.Align.CENTER,
                        hexpand=True)
        for m in ("top", "bottom", "start", "end"):
            getattr(right, f"set_margin_{m}")(40)
        self.hero = Gtk.Image(icon_name=APP_ID, pixel_size=112)
        self.hero.add_css_class("hero-icon")
        self.h_title = Gtk.Label(label="Pronto para clonar")
        self.h_title.add_css_class("title-1")
        self.h_sub = Gtk.Label(label="Escolha a imagem e o cartão novo.", wrap=True,
                               justify=Gtk.Justification.CENTER)
        self.h_sub.add_css_class("dim-label")
        self.pct = Gtk.Label(label="")
        self.pct.add_css_class("big-percent")
        self.bar = Gtk.ProgressBar()
        self.bar.set_size_request(420, -1)
        self.bar.set_halign(Gtk.Align.CENTER)
        grid = Gtk.Grid(column_spacing=10, column_homogeneous=True, halign=Gtk.Align.CENTER)
        self.st = {}
        for i, (k, lab) in enumerate((("done", "Processado"), ("rate", "Velocidade"), ("eta", "Restante"))):
            tile, val = stat_tile(lab)
            tile.set_size_request(150, -1)
            self.st[k] = val
            grid.attach(tile, i, 0, 1, 1)
        self.hash_lbl = Gtk.Label(selectable=True, wrap=True, justify=Gtk.Justification.CENTER)
        self.hash_lbl.add_css_class("mono")
        for w in (self.hero, self.h_title, self.h_sub, self.pct, self.bar, grid, self.hash_lbl):
            right.append(w)
        self.append(right)

        self.picker.refresh(True)
        GLib.timeout_add_seconds(3, lambda: (self.worker.running or self.picker.refresh(), True)[1])

    def set_image(self, path):
        if not path or not os.path.exists(path):
            return
        self.img = path
        self.img_size = os.path.getsize(path)
        self.img_row.set_subtitle(path)
        with open(path, "rb") as f:
            self.img_boot = bootinfo.analyze(lambda p, s: (f.seek(p), f.read(s))[1], self.img_size)
        b = self.img_boot
        parts = [f"Partição {p['index']}: {(p.get('fs') or {}).get('fs') or p['type_name']}"
                 f" ({(p.get('fs') or {}).get('label', '—')})" for p in b.get("partitions", [])]
        self.img_info.set_subtitle(f"{devices.human(self.img_size)} · {self.img_size // 512:,} setores · "
                                   .replace(",", ".") + b.get("scheme", "?") +
                                   ("\n" + "\n".join(parts) if parts else ""))
        mp = path + ".map"
        self.img_warn.set_visible(False)
        if os.path.exists(mp):
            try:
                m = MapFile.load(mp, self.img_size)
                bad = m.count(BAD)
                left_ = m.count(NON_TRIED) + m.count(NON_TRIMMED) + m.count(NON_SCRAPED)
                if bad or left_:
                    self.img_warn.set_subtitle(
                        f"Esta imagem tem {bad // 512:,} setor(es) ruim(ns)".replace(",", ".") +
                        (f" e {devices.human_bin(left_)} não lidos" if left_ else "") +
                        " — essas áreas vão zeradas para o cartão novo.")
                    self.img_warn.set_visible(True)
            except (OSError, ValueError):
                pass
        self._update_fit()

    def _choose(self, *a):
        dlg = Gtk.FileChooserNative(title="Escolher imagem", transient_for=self.win,
                                    action=Gtk.FileChooserAction.OPEN, accept_label="Abrir")
        f = Gtk.FileFilter()
        f.set_name("Imagens de disco (*.img, *.bin, *.raw, *.dd)")
        for p in ("*.img", "*.bin", "*.raw", "*.dd", "*.IMG"):
            f.add_pattern(p)
        dlg.add_filter(f)

        def resp(d, r):
            if r == Gtk.ResponseType.ACCEPT:
                self.set_image(d.get_file().get_path())
            self._dlg = None
        dlg.connect("response", resp)
        self._dlg = dlg
        dlg.show()

    def _target_changed(self, d):
        self.tgt_row.set_subtitle(describe_disk(d) + ("\n⚠ Protegido contra escrita" if d and d["readonly"] else ""))
        self._update_fit()

    def _update_fit(self):
        d = self.picker.selected()
        if not (self.img and d):
            self.fit_box.set_visible(False)
            self.btn_go.set_sensitive(False)
            return
        self.fit = writer.fit(self.img_size, d["size"], self.img_boot)
        lvl = self.fit["level"]
        css, icon = {"exact": ("ok", "arca-ok-symbolic"), "larger": ("ok", "arca-ok-symbolic"),
                     "smaller_ok": ("warn", "arca-warning-symbolic"),
                     "too_small": ("bad", "arca-error-symbolic")}[lvl]
        for c in ("ok", "warn", "bad"):
            self.fit_box.remove_css_class(c)
        self.fit_box.add_css_class(css)
        self.fit_icon.set_from_icon_name(icon)
        self.fit_lbl.set_label(self.fit["msg"] +
                               f"\nImagem {self.img_size // 512:,} setores · cartão {d['size'] // 512:,} setores"
                               .replace(",", "."))
        self.fit_box.set_visible(True)
        self.btn_go.set_sensitive(lvl != "too_small" and not d["readonly"] and not self.worker.running)

    def _confirm(self, *a):
        d = self.picker.selected()
        if not d:
            return
        dlg = Adw.Window(transient_for=self.win, modal=True, default_width=440, resizable=False)
        dlg.set_title("Confirmar gravação")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(24)
        ic = Gtk.Image(icon_name="arca-warning-symbolic", pixel_size=48)
        tl = Gtk.Label(label="Apagar e gravar o cartão?")
        tl.add_css_class("title-2")
        msg = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        msg.set_markup(f"Tudo em <b>{GLib.markup_escape_text(d['model'])}</b> "
                       f"({d['path']}, {devices.human(d['size'])}) será substituído pela imagem.\n\n"
                       f"Digite <b>{d['name']}</b> para confirmar:")
        entry = Gtk.Entry(placeholder_text=d["name"])
        btns = Gtk.Box(spacing=10, homogeneous=True)
        cancel = Gtk.Button(label="Cancelar")
        cancel.add_css_class("pill")
        ok = Gtk.Button(label="Gravar", sensitive=False)
        ok.add_css_class("destructive-action")
        ok.add_css_class("pill")
        btns.append(cancel)
        btns.append(ok)
        for w in (ic, tl, msg, entry, btns):
            box.append(w)
        dlg.set_content(box)
        entry.connect("changed", lambda e: ok.set_sensitive(e.get_text().strip() == d["name"]))
        entry.connect("activate", lambda e: ok.get_sensitive() and ok.emit("clicked"))
        cancel.connect("clicked", lambda *x: dlg.close())

        def go(*x):
            dlg.close()
            self._start(d)
        ok.connect("clicked", go)
        dlg.present()

    def _start(self, d):
        args = ["gravar", self.img, d["path"]]
        if not self.sw_verify.get_active():
            args.append("--sem-verificar")
        if self.sw_unmount.get_active():
            args.append("--desmontar")
        self._running(True)
        self._hero("arca-write-symbolic", "accent")
        self.h_title.set_label("Aguardando autorização…")
        self.h_sub.set_label(f"{os.path.basename(self.img)} → {d['path']}")
        self.hash_lbl.set_label("")
        self.bar.set_fraction(0)
        self._t0 = time.monotonic()
        self.worker.start(args, self._event, self._exit)

    def _running(self, on):
        self.btn_go.set_sensitive(not on)
        self.btn_stop.set_sensitive(on)
        self.picker.dd.set_sensitive(not on)
        self.win.set_busy(on)

    def _event(self, ev):
        e = ev.get("ev")
        if e == "wprogress":
            frac = ev["done"] / ev["total"]
            self.h_title.set_label("Gravando no cartão…" if ev["phase"] == "write" else "Verificando a cópia…")
            self.bar.set_fraction(frac)
            self.pct.set_label(f"{100 * frac:.1f}%".replace(".", ","))
            self.st["done"].set_label(devices.human_bin(ev["done"]))
            self.st["rate"].set_label(rate_txt(ev["rate"]))
            eta = (ev["total"] - ev["done"]) / ev["rate"] if ev["rate"] else None
            self.st["eta"].set_label(fmt_time(eta))
        elif e == "log":
            self.h_sub.set_label(ev["msg"])
        elif e == "error":
            self._fail(ev["msg"])
        elif e == "wdone":
            if ev.get("cancelled"):
                self._fail("Gravação interrompida — o cartão ficou incompleto.")
            elif ev.get("verified") and ev.get("match"):
                self._hero("arca-ok-symbolic", "success")
                self.h_title.set_label("Cópia idêntica gravada")
                self.h_sub.set_label("O cartão novo foi lido de volta e é igual à imagem, bit a bit.")
                self.hash_lbl.set_label(f"SHA-256 imagem  {ev['sha_img']}\nSHA-256 cartão  {ev['sha_dev']}")
                self.win.toast("Cartão gravado e verificado")
            elif ev.get("verified"):
                off = ev.get("first_mismatch")
                self._fail("A verificação encontrou diferenças" +
                           (f" a partir do setor {off // 512:,}".replace(",", ".") if off is not None else "") +
                           ". O cartão novo pode estar com defeito.")
            else:
                self._hero("arca-ok-symbolic", "success")
                self.h_title.set_label("Gravação concluída (sem verificar)")
                self.hash_lbl.set_label(f"SHA-256 imagem  {ev['sha_img']}")

    def _hero(self, icon, css=None):
        self.hero.set_from_icon_name(icon)
        for c in ("success", "error", "accent"):
            self.hero.remove_css_class(c)
        if css:
            self.hero.add_css_class(css)

    def _fail(self, msg):
        self._hero("arca-error-symbolic", "error")
        self.h_title.set_label("Não deu certo")
        self.h_sub.set_label(msg)
        self.win.toast(msg)

    def _exit(self, code, got, err):
        self._running(False)
        if not got:
            msg = {126: "Autorização cancelada.", 127: "Autorização negada."}.get(code, err[-300:] or f"código {code}")
            self.h_title.set_label("Pronto para clonar")
            self.h_sub.set_label(msg)
        self.picker.refresh(True)
        self._update_fit()


# ======================================================================= janela
class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, default_width=1240, default_height=860)
        self.set_title(APP_NAME)
        self.busy = 0
        self.stack = Adw.ViewStack()
        self.rescue = RescuePage(self)
        self.write = WritePage(self)
        p = self.stack.add_titled(self.rescue, "resgate", "Resgatar")
        p.set_icon_name("arca-card-symbolic")
        p = self.stack.add_titled(self.write, "gravar", "Clonar para cartão novo")
        p.set_icon_name("arca-write-symbolic")

        hb = Adw.HeaderBar()
        sw = Adw.ViewSwitcherTitle()
        sw.set_stack(self.stack)
        sw.set_title(APP_NAME)
        sw.set_subtitle("Resgate e clonagem de cartões CompactFlash")
        hb.set_title_widget(sw)
        menu = Gio.Menu()
        menu.append("Sobre o Arca", "app.about")
        mb = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        hb.pack_end(mb)
        self.spinner = Gtk.Spinner()
        hb.pack_start(self.spinner)

        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(self.stack)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(hb)
        box.append(self.toasts)
        self.set_content(box)
        self.connect("close-request", self._closing)

    def toast(self, msg):
        t = Adw.Toast.new(msg)
        t.set_timeout(5)
        self.toasts.add_toast(t)

    def set_busy(self, on):
        self.busy = max(0, self.busy + (1 if on else -1))
        if self.busy:
            self.spinner.start()
        else:
            self.spinner.stop()

    def go_write(self, image):
        self.write.set_image(image)
        self.stack.set_visible_child_name("gravar")

    def _closing(self, *a):
        # o worker salva o mapa ao receber o cancelamento; o resgate pode ser retomado
        self.rescue.worker.cancel()
        self.write.worker.cancel()
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS
                         if hasattr(Gio.ApplicationFlags, "DEFAULT_FLAGS") else Gio.ApplicationFlags.FLAGS_NONE)
        act = Gio.SimpleAction.new("about", None)
        act.connect("activate", self._about)
        self.add_action(act)

    def do_activate(self):
        prov = Gtk.CssProvider()
        if hasattr(prov, "load_from_string"):
            prov.load_from_string(CSS)
        else:
            prov.load_from_data(CSS.encode())
        disp = Gdk.Display.get_default()
        Gtk.StyleContext.add_provider_for_display(disp, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        theme = Gtk.IconTheme.get_for_display(disp)
        for base in (os.path.join(os.path.dirname(HERE), "data", "icons"), "/usr/share/arca/icons"):
            if os.path.isdir(base):
                theme.add_search_path(base)
        win = self.props.active_window or Window(self)
        win.present()

    def _about(self, *a):
        kw = dict(application_name=APP_NAME, application_icon=APP_ID, version=VERSION,
                  developer_name="Marcos Santos", website="https://github.com/aragonxpd154/arca",
                  license_type=Gtk.License.MIT_X11,
                  comments="Resgate setor a setor de cartões CompactFlash desgastados e clonagem "
                           "exata para um cartão novo.")
        if hasattr(Adw, "AboutWindow"):
            Adw.AboutWindow(transient_for=self.props.active_window, **kw).present()
        else:
            Gtk.AboutDialog(transient_for=self.props.active_window, program_name=APP_NAME,
                            version=VERSION, logo_icon_name=APP_ID, comments=kw["comments"],
                            website=kw["website"], authors=["Marcos Santos"],
                            license_type=Gtk.License.MIT_X11).present()


def main():
    return App().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())

"""Aba Diagnóstico: teste de leitura/escrita, mapa de latência, degradação e histórico."""
from __future__ import annotations

import json
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, GLib, Gio, Gdk, Gtk, Pango, PangoCairo  # noqa: E402

from . import devices  # noqa: E402
from .diagnose import F_ALIAS, F_CORRUPT, F_LOSTW, F_RERR, F_UNSTABLE, F_WERR  # noqa: E402
from .gui import DevicePicker, Worker, describe_disk, group, legend_item, row, stat_tile  # noqa: E402

HISTORY = os.path.join(GLib.get_user_data_dir(), "arca", "diagnosticos.json")
BAD_DATA = F_CORRUPT | F_UNSTABLE | F_ALIAS | F_LOSTW

# paleta (status fixos + duas séries categóricas), clara / escura
PAL = {
    "good": ("#0ca30c", "#0ca30c"), "warning": ("#fab219", "#fab219"),
    "serious": ("#ec835a", "#ec835a"), "critical": ("#d03b3b", "#d03b3b"),
    "violet": ("#6250d6", "#9085e9"), "read": ("#2a78d6", "#3987e5"),
    "write": ("#eb6834", "#d95926"),
}
LEVEL_CSS = {"good": "ok", "warning": "warn", "serious": "bad", "critical": "bad", "info": "ok"}


def _dark():
    return Adw.StyleManager.get_default().get_dark()


def rgb(name, alpha=1.0):
    h = PAL[name][1 if _dark() else 0]
    return (int(h[1:3], 16) / 255, int(h[3:5], 16) / 255, int(h[5:7], 16) / 255, alpha)


def ink(alpha=1.0):
    return (0.94, 0.94, 0.92, alpha) if _dark() else (0.05, 0.05, 0.05, alpha)


def flags_txt(f):
    out = []
    for bit, txt in ((F_RERR, "erro de leitura"), (F_WERR, "erro de escrita"), (F_CORRUPT, "dados corrompidos"),
                     (F_UNSTABLE, "leitura instável"), (F_ALIAS, "endereço falso"), (F_LOSTW, "escrita perdida")):
        if f & bit:
            out.append(txt)
    return ", ".join(out)


def thresholds(vals):
    ok = sorted(v for v in vals if v)
    med = ok[len(ok) // 2] if ok else 0.0
    return med, max(5 * med, med + 30.0), max(20 * med, 500.0)


# ===================================================================== gráficos
class Chart(Gtk.DrawingArea):
    PAD_L, PAD_R, PAD_T, PAD_B = 40, 12, 22, 26

    def __init__(self, height):
        super().__init__()
        self.set_hexpand(True)
        self.set_content_height(height)
        self.set_draw_func(self._draw_wrap)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tip)

    def text(self, cr, x, y, s, align="l", size=8.5, alpha=0.62, bold=False):
        lay = self.create_pango_layout(s)
        fd = Pango.FontDescription.from_string(f"Sans {'Bold ' if bold else ''}{size}")
        lay.set_font_description(fd)
        w, h = lay.get_pixel_size()
        dx = {"l": 0, "c": -w / 2, "r": -w}[align]
        cr.set_source_rgba(*ink(alpha))
        cr.move_to(x + dx, y - h / 2)
        PangoCairo.show_layout(cr, lay)

    def grid(self, cr, w, h, ymax, unit, ticks=None):
        if ticks is None:
            import math
            mant = round(ymax / 10 ** math.floor(math.log10(ymax)), 2)
            ticks = {1.0: 5, 2.0: 4, 2.5: 5, 5.0: 5}.get(mant, 4)
        x0, x1 = self.PAD_L, w - self.PAD_R
        y0, y1 = self.PAD_T, h - self.PAD_B
        cr.set_line_width(1)
        for k in range(ticks + 1):
            v = ymax * k / ticks
            y = y1 - (y1 - y0) * k / ticks
            cr.set_source_rgba(*ink(0.10 if k else 0.28))
            cr.move_to(x0, y + 0.5)
            cr.line_to(x1, y + 0.5)
            cr.stroke()
            self.text(cr, x0 - 6, y, f"{v:g}".replace(".", ","), "r")
        if unit:
            self.text(cr, x0 - 6, 6, unit.strip(), "l", 8, 0.55)
        return x0, x1, y0, y1

    def _draw_wrap(self, area, cr, w, h):
        self.draw(cr, w, h)

    def _tip(self, *a):
        return False


def nice_max(v):
    if v <= 0:
        return 1
    for e in range(-1, 6):
        for m in (1, 2, 2.5, 5):
            c = m * 10 ** e
            if c >= v * 1.08:
                return c
    return v * 1.1


class SpeedChart(Chart):
    """Velocidade de cada bloco ao longo do cartão (passada atual)."""

    def __init__(self):
        super().__init__(190)
        self.n, self.block = 0, 1
        self.read, self.write, self.prev = [], [], []

    def reset(self, n, block):
        self.n, self.block = n, block
        self.read, self.write, self.prev = [None] * n, [None] * n, [None] * n
        self.queue_draw()

    def new_pass(self):
        if any(v is not None for v in self.read):
            self.prev = self.read
        self.read, self.write = [None] * self.n, [None] * self.n

    def put(self, phase, i, ms):
        if 0 <= i < self.n:
            (self.write if phase == "write" else self.read)[i] = (self.block / (ms / 1000) / 1e6) if ms else 0.0

    def draw(self, cr, w, h):
        if not self.n:
            self.text(cr, w / 2, h / 2, "O gráfico aparece quando o teste começar.", "c")
            return
        vals = [v for s in (self.read, self.write, self.prev) for v in s if v]
        ymax = nice_max(max(vals) if vals else 1)
        x0, x1, y0, y1 = self.grid(cr, w, h, ymax, " MB/s")
        for k in range(5):
            x = x0 + (x1 - x0) * k / 4
            v = self.n * self.block * k / 4
            lab = "0" if k == 0 else (f"{v / 1e9:.2f} GB".replace(".", ",") if v >= 1e9 else f"{v / 1e6:.0f} MB")
            self.text(cr, x, y1 + 13, lab,
                      "l" if k == 0 else ("r" if k == 4 else "c"))

        def line(series, color, alpha):
            cr.set_source_rgba(*rgb(color, alpha))
            cr.set_line_width(2 if alpha == 1 else 1.5)
            pen = False
            for i, v in enumerate(series):
                if v is None:
                    pen = False
                    continue
                x = x0 + (x1 - x0) * (i + 0.5) / self.n
                y = y1 - (y1 - y0) * min(v, ymax) / ymax
                (cr.line_to if pen else cr.move_to)(x, y)
                pen = True
            cr.stroke()
        line(self.prev, "read", 0.28)
        line(self.write, "write", 1)
        line(self.read, "read", 1)

    def _tip(self, w, x, y, kb, tip):
        if not self.n:
            return False
        x0, x1 = self.PAD_L, self.get_width() - self.PAD_R
        if not x0 <= x <= x1:
            return False
        i = min(self.n - 1, int((x - x0) / (x1 - x0) * self.n))
        f = lambda v: "—" if v is None else f"{v:.1f} MB/s".replace(".", ",")  # noqa: E731
        tip.set_text(f"Bloco {i} · {devices.human(i * self.block)}\nLeitura: {f(self.read[i])}\n"
                     f"Escrita: {f(self.write[i])}\nLeitura (passada anterior): {f(self.prev[i])}")
        return True


class PassChart(Chart):
    """Velocidade média por passada: mostra a degradação durante o teste."""

    def __init__(self):
        super().__init__(170)
        self.rows = []   # (passada, leitura, escrita|None)

    def draw(self, cr, w, h):
        if not self.rows:
            self.text(cr, w / 2, h / 2, "Cada passada concluída vira um ponto aqui.", "c")
            return
        vals = [v for r in self.rows for v in r[1:] if v]
        ymax = nice_max(max(vals) if vals else 1)
        x0, x1, y0, y1 = self.grid(cr, w, h, ymax, " MB/s")
        P = len(self.rows)
        X = lambda k: x0 + (x1 - x0) * (0.5 if P == 1 else k / (P - 1))  # noqa: E731
        Y = lambda v: y1 - (y1 - y0) * min(v, ymax) / ymax  # noqa: E731
        step = max(1, P // 8)
        for k, r in enumerate(self.rows):
            if k % step == 0 or k == P - 1:
                self.text(cr, X(k), y1 + 13, str(r[0]), "c")
        for idx, color in ((2, "write"), (1, "read")):
            pts = [(X(k), Y(r[idx])) for k, r in enumerate(self.rows) if r[idx]]
            if not pts:
                continue
            cr.set_source_rgba(*rgb(color))
            cr.set_line_width(2)
            for j, (x, y) in enumerate(pts):
                (cr.line_to if j else cr.move_to)(x, y)
            cr.stroke()
            if len(pts) <= 40:
                for x, y in pts:
                    cr.set_source_rgba(*(0.12, 0.12, 0.12, 1) if _dark() else (1, 1, 1, 1))
                    cr.arc(x, y, 5, 0, 6.2832)
                    cr.fill()
                    cr.set_source_rgba(*rgb(color))
                    cr.arc(x, y, 3.5, 0, 6.2832)
                    cr.fill()

    def _tip(self, w, x, y, kb, tip):
        if not self.rows:
            return False
        x0, x1 = self.PAD_L, self.get_width() - self.PAD_R
        P = len(self.rows)
        k = 0 if P == 1 else max(0, min(P - 1, round((x - x0) / (x1 - x0) * (P - 1))))
        p, r, wr = self.rows[k]
        tip.set_text(f"Passada {p}\nLeitura média: {r:.2f} MB/s".replace(".", ",") +
                     (f"\nEscrita média: {wr:.2f} MB/s".replace(".", ",") if wr else ""))
        return True


class HistoryChart(Chart):
    """Nota de cada diagnóstico já feito nesta mesma unidade."""

    PAD_L = 34

    def __init__(self):
        super().__init__(170)
        self.entries = []

    def draw(self, cr, w, h):
        if not self.entries:
            self.text(cr, w / 2, h / 2, "Nenhum diagnóstico anterior desta unidade.", "c")
            return
        x0, x1, y0, y1 = self.grid(cr, w, h, 100, "", ticks=4)
        n = len(self.entries)
        slot = (x1 - x0) / n
        bw = max(6, min(38, slot - 6))
        lvl = {"Saudável": "good", "Atenção": "warning", "Degradado": "serious"}
        every = max(1, int(70 // slot) + 1)
        for k, e in enumerate(self.entries):
            cx = x0 + slot * (k + 0.5)
            top = y1 - (y1 - y0) * max(e["score"], 2) / 100
            cr.set_source_rgba(*rgb(lvl.get(e["verdict"], "critical")))
            r = min(4, bw / 2)
            cr.move_to(cx - bw / 2, y1)
            cr.line_to(cx - bw / 2, top + r)
            cr.arc(cx - bw / 2 + r, top + r, r, 3.1416, 4.7124)
            cr.arc(cx + bw / 2 - r, top + r, r, 4.7124, 6.2832)
            cr.line_to(cx + bw / 2, y1)
            cr.close_path()
            cr.fill()
            if slot >= 22:
                self.text(cr, cx, top - 8, str(e["score"]), "c", 8, 0.85, bold=True)
            if k % every == 0 or k == n - 1:
                self.text(cr, cx, y1 + 13, e["date"][8:10] + "/" + e["date"][5:7], "c")

    def _tip(self, w, x, y, kb, tip):
        if not self.entries:
            return False
        x0, x1 = self.PAD_L, self.get_width() - self.PAD_R
        k = int((x - x0) / ((x1 - x0) / len(self.entries)))
        if not 0 <= k < len(self.entries):
            return False
        e = self.entries[k]
        d = e["date"]
        modo = "leitura e escrita" if e["mode"] == "escrita" else "só leitura"
        tip.set_text(f"{d[8:10]}/{d[5:7]}/{d[:4]} {d[11:16]} · {modo}, {e['passes']} passada(s)\n"
                     f"Nota {e['score']} — {e['verdict']}\nLeitura {e['read_mbps']:.1f} MB/s".replace(".", ",") +
                     (f" · escrita {e['write_mbps']:.1f} MB/s".replace(".", ",") if e.get("write_mbps") else ""))
        return True


class LatencyMap(Gtk.DrawingArea):
    """Um quadrado por bloco, colorido pela latência (ou pela falha encontrada)."""

    CELL, GAP = 11, 2

    def __init__(self):
        super().__init__()
        self.n, self.block = 0, 1
        self.rlat, self.wlat, self.flags, self.worst = [], [], [], []
        self.view = "read"
        self.cur = None
        self.set_hexpand(True)
        self.set_content_height(210)
        self.set_draw_func(self._draw)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._tip)

    def reset(self, n, block):
        self.n, self.block = n, block
        self.rlat, self.wlat = [None] * n, [None] * n
        self.flags, self.worst = [0] * n, [0.0] * n
        self.cur = None
        self.queue_draw()

    def _geom(self, w):
        c, g = self.CELL, self.GAP
        cols = max(1, (w + g) // (c + g))
        size = c
        rows = -(-self.n // cols) if self.n else 1
        avail = self.get_height()
        while rows * (size + g) > avail and size > 3:  # encolhe para caber
            size -= 1
            cols = max(1, (w + g) // (size + g))
            rows = -(-self.n // cols)
        return cols, size

    def classify(self, i):
        f = self.flags[i]
        if f & BAD_DATA:
            return "violet"
        if f & (F_RERR | F_WERR):
            return "critical"
        arr = self.rlat if self.view == "read" else self.wlat
        v = arr[i]
        if v is None:
            return None
        med, slow, vslow = self._th
        if v > vslow:
            return "serious"
        if v > slow:
            return "warning"
        return "fair" if med and v > 2 * med else "good"

    def _draw(self, area, cr, w, h):
        if not self.n:
            return
        self._th = thresholds(self.rlat if self.view == "read" else self.wlat)
        cols, c = self._geom(w)
        g = self.GAP
        for i in range(self.n):
            x, y = (i % cols) * (c + g), (i // cols) * (c + g)
            k = self.classify(i)
            if k is None:
                cr.set_source_rgba(*ink(0.10))
            elif k == "fair":
                cr.set_source_rgba(*rgb("good", 0.42))
            else:
                cr.set_source_rgba(*rgb(k))
            cr.rectangle(x, y, c, c)
            cr.fill()
        if self.cur is not None and 0 <= self.cur < self.n:
            x, y = (self.cur % cols) * (c + g), (self.cur // cols) * (c + g)
            cr.set_source_rgba(*rgb("read"))
            cr.set_line_width(2)
            cr.rectangle(x - 1, y - 1, c + 2, c + 2)
            cr.stroke()

    def _tip(self, w, x, y, kb, tip):
        if not self.n:
            return False
        cols, c = self._geom(self.get_width())
        g = self.GAP
        if x // (c + g) >= cols:
            return False
        i = int(y // (c + g)) * cols + int(x // (c + g))
        if i >= self.n:
            return False
        s0 = i * self.block // 512
        f = lambda v: "—" if v is None else f"{v:.1f} ms ({self.block / (v / 1000) / 1e6:.1f} MB/s)".replace(".", ",")  # noqa: E731
        txt = (f"Bloco {i} · setores {s0:,}–{s0 + self.block // 512 - 1:,}".replace(",", ".") +
               f"\nLeitura: {f(self.rlat[i])}\nPior leitura: {f(self.worst[i] or None)}"
               f"\nEscrita: {f(self.wlat[i])}")
        if self.flags[i]:
            txt += "\n⚠ " + flags_txt(self.flags[i])
        tip.set_text(txt)
        return True


def card(title, child, extra=None, subtitle=None):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    box.add_css_class("card")
    box.add_css_class("map-card")
    head = Gtk.Box(spacing=8)
    tl = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
    t = Gtk.Label(label=title, xalign=0)
    t.add_css_class("heading")
    tl.append(t)
    if subtitle:
        s = Gtk.Label(label=subtitle, xalign=0, wrap=True)
        s.add_css_class("caption")
        s.add_css_class("dim-label")
        tl.append(s)
    head.append(tl)
    if extra:
        head.append(extra)
    box.append(head)
    box.append(child)
    return box


def legend(items):
    fb = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=8,
                     column_spacing=14, row_spacing=4)
    for css, txt in items:
        fb.append(legend_item(css, txt))
    return fb


def confirm_typed(win, d, title, markup, action, on_ok):
    dlg = Adw.Window(transient_for=win, modal=True, default_width=460, resizable=False)
    dlg.set_title(title)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    for m in ("top", "bottom", "start", "end"):
        getattr(box, f"set_margin_{m}")(24)
    ic = Gtk.Image(icon_name="arca-warning-symbolic", pixel_size=48)
    tl = Gtk.Label(label=title)
    tl.add_css_class("title-2")
    msg = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
    msg.set_markup(markup + f"\n\nDigite <b>{d['name']}</b> para confirmar:")
    entry = Gtk.Entry(placeholder_text=d["name"])
    btns = Gtk.Box(spacing=10, homogeneous=True)
    cancel = Gtk.Button(label="Cancelar")
    cancel.add_css_class("pill")
    ok = Gtk.Button(label=action, sensitive=False)
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
        on_ok()
    ok.connect("clicked", go)
    dlg.present()


def load_history(cid=None):
    try:
        with open(HISTORY) as f:
            h = json.load(f)
    except (OSError, ValueError):
        return []
    return [e for e in h if cid is None or e.get("id") == cid]


# =================================================================== a página
class DiagnosePage(Gtk.Box):
    BLOCKS = (256, 1024, 4096)

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.win = win
        self.worker = Worker()
        self.result = None
        self.n = 0
        self.passes_total = 1
        self.until_fail = False
        self.mode = "leitura"
        self.cur_pass, self.cur_phase = 1, "read"

        # ---------------------------------------------------- controles
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for m in ("top", "bottom", "start", "end"):
            getattr(left, f"set_margin_{m}")(18)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(left)
        sc.set_size_request(390, -1)
        self.append(sc)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        g = group("Unidade", "O cartão a ser avaliado.")
        self.picker = DevicePicker(self._dev_changed)
        r = row("Dispositivo")
        r.add_suffix(self.picker.dd)
        b = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER)
        b.add_css_class("flat")
        b.connect("clicked", lambda *a: self.picker.refresh(True))
        r.add_suffix(b)
        g.add(r)
        self.dev_row = row("Detalhes", "")
        g.add(self.dev_row)
        self.hist_row = row("Histórico", "")
        g.add(self.hist_row)
        left.append(g)

        g = group("Teste")
        self.mode_dd = Gtk.DropDown.new_from_strings(["Só leitura (seguro)", "Leitura e escrita (apaga)"])
        self.mode_dd.set_valign(Gtk.Align.CENTER)
        self.mode_dd.connect("notify::selected", lambda *a: self._mode_changed())
        r = row("Modo")
        r.add_suffix(self.mode_dd)
        g.add(r)
        self.passes = Gtk.SpinButton.new_with_range(1, 1000, 1)
        self.passes.set_value(3)
        self.passes.set_valign(Gtk.Align.CENTER)
        r = row("Passadas", "Cada passada percorre o cartão inteiro")
        r.add_suffix(self.passes)
        g.add(r)
        self.sw_fail = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.sw_fail.connect("notify::active", lambda *a: self.passes.set_sensitive(not self.sw_fail.get_active()))
        r = row("Até falhar (tortura)", "Repete sem parar até o primeiro erro ou até você parar")
        r.add_suffix(self.sw_fail)
        r.set_activatable_widget(self.sw_fail)
        g.add(r)
        self.block_dd = Gtk.DropDown.new_from_strings(["256 KiB", "1 MiB", "4 MiB"])
        self.block_dd.set_selected(1)
        self.block_dd.set_valign(Gtk.Align.CENTER)
        r = row("Tamanho do bloco medido", "Menor = mapa mais detalhado")
        r.add_suffix(self.block_dd)
        g.add(r)
        left.append(g)

        self.warn = Gtk.Box(spacing=12)
        self.warn.add_css_class("banner")
        self.warn.add_css_class("bad")
        wi = Gtk.Image(icon_name="arca-warning-symbolic", icon_size=Gtk.IconSize.LARGE, valign=Gtk.Align.START)
        wl = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        wl.set_markup("<b>Este modo apaga todo o conteúdo do cartão.</b>\n"
                      "Use em cartões novos ou que vão ser descartados. Nunca no cartão gasto "
                      "antes de resgatar a imagem dele. Cada passada grava todas as células uma vez "
                      "e gasta um ciclo da vida útil delas.")
        self.warn.append(wi)
        self.warn.append(wl)
        self.warn.set_visible(False)
        left.append(self.warn)

        bb = Gtk.Box(spacing=10, homogeneous=True)
        self.btn_go = Gtk.Button(label="Iniciar diagnóstico")
        self.btn_go.add_css_class("suggested-action")
        self.btn_go.add_css_class("pill")
        self.btn_go.connect("clicked", self._start)
        self.btn_stop = Gtk.Button(label="Parar", sensitive=False)
        self.btn_stop.add_css_class("destructive-action")
        self.btn_stop.add_css_class("pill")
        self.btn_stop.connect("clicked", lambda *a: (self._badge("Parando…"), self.worker.cancel()))
        bb.append(self.btn_go)
        bb.append(self.btn_stop)
        left.append(bb)

        # veredito
        self.verdict = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.verdict.add_css_class("banner")
        self.verdict.set_visible(False)
        top = Gtk.Box(spacing=14)
        self.v_score = Gtk.Label(label="—")
        self.v_score.add_css_class("big-percent")
        vt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.v_title = Gtk.Label(xalign=0)
        self.v_title.add_css_class("title-3")
        self.v_sub = Gtk.Label(xalign=0, wrap=True)
        self.v_sub.add_css_class("caption")
        vt.append(self.v_title)
        vt.append(self.v_sub)
        top.append(self.v_score)
        top.append(vt)
        self.verdict.append(top)
        self.v_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.verdict.append(self.v_list)
        vb = Gtk.Box(spacing=8)
        b1 = Gtk.Button(label="Ver relatório")
        b1.connect("clicked", lambda *a: self._open("report"))
        b2 = Gtk.Button(label="Abrir dados (CSV)")
        b2.connect("clicked", lambda *a: self._open("csv"))
        vb.append(b1)
        vb.append(b2)
        self.verdict.append(vb)
        left.append(self.verdict)

        # ------------------------------------------------------ painel direito
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for m in ("top", "bottom", "start", "end"):
            getattr(right, f"set_margin_{m}")(20)
        rs = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, hexpand=True)
        rs.set_child(right)
        self.append(rs)

        head = Gtk.Box(spacing=14)
        hb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        t = Gtk.Label(label="Saúde da unidade", xalign=0)
        t.add_css_class("title-2")
        self.badge = Gtk.Label(label="Pronto", halign=Gtk.Align.START)
        self.badge.add_css_class("phase-badge")
        hb.append(t)
        hb.append(self.badge)
        self.pct = Gtk.Label(label="")
        self.pct.add_css_class("big-percent")
        head.append(hb)
        head.append(self.pct)
        right.append(head)
        self.bar = Gtk.ProgressBar()
        right.append(self.bar)

        grid = Gtk.Grid(column_spacing=10, row_spacing=10, column_homogeneous=True)
        self.st = {}
        for i, (k, lab) in enumerate((("read", "Leitura média"), ("write", "Escrita média"),
                                      ("p50", "Latência mediana"), ("p99", "Latência p99"),
                                      ("slow", "Blocos lentos"), ("err", "Erros de E/S"),
                                      ("bad", "Dados divergentes"), ("tbw", "Total gravado"))):
            tile, val = stat_tile(lab)
            self.st[k] = val
            grid.attach(tile, i % 4, i // 4, 1, 1)
        right.append(grid)

        self.lmap = LatencyMap()
        self.view_dd = Gtk.DropDown.new_from_strings(["Leitura", "Escrita"])
        self.view_dd.set_valign(Gtk.Align.START)
        self.view_dd.connect("notify::selected", self._view_changed)
        mbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        mbox.append(self.lmap)
        mbox.append(legend((("dot-good", "Normal"), ("dot-fair", "Acima da mediana"),
                            ("dot-warn", "Lento"), ("dot-serious", "Muito lento"),
                            ("dot-crit", "Erro de E/S"), ("dot-violet", "Dado errado / instável"),
                            ("dot-pending", "Não testado"))))
        right.append(card("Mapa de latência", mbox, self.view_dd,
                          "Um quadrado por bloco. Passe o mouse para ver tempo, velocidade e falhas."))

        self.speed = SpeedChart()
        sbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sbox.append(self.speed)
        sbox.append(legend((("dot-read", "Leitura"), ("dot-write", "Escrita"),
                            ("dot-read-faded", "Leitura da passada anterior"))))
        right.append(card("Velocidade ao longo do cartão", sbox, None,
                          "Quedas localizadas indicam regiões que o controlador está corrigindo."))

        self.pchart = PassChart()
        pbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        pbox.append(self.pchart)
        pbox.append(legend((("dot-read", "Leitura média"), ("dot-write", "Escrita média"))))
        right.append(card("Velocidade por passada", pbox, None,
                          "Se a linha cai a cada passada, o cartão está se degradando durante o uso."))

        self.hchart = HistoryChart()
        hbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        hbox.append(self.hchart)
        hbox.append(legend((("dot-good", "Saudável ≥ 85"), ("dot-warn", "Atenção ≥ 60"),
                            ("dot-serious", "Degradado ≥ 30"), ("dot-crit", "Reprovado"))))
        right.append(card("Histórico desta unidade", hbox, None,
                          "Nota de cada diagnóstico já feito neste cartão, para acompanhar o desgaste."))

        self.picker.refresh(True)
        GLib.timeout_add_seconds(3, lambda: (self.worker.running or self.picker.refresh(), True)[1])

    # ------------------------------------------------------------ helpers
    def _badge(self, txt, css=None):
        self.badge.set_label(txt)
        for c in ("done", "fail", "warn"):
            self.badge.remove_css_class(c)
        if css:
            self.badge.add_css_class(css)

    def _dev_changed(self, d):
        self.dev_row.set_subtitle(describe_disk(d))
        self.btn_go.set_sensitive(bool(d) and not self.worker.running)
        self._load_hist(d)

    def _load_hist(self, d):
        if not d:
            self.hist_row.set_subtitle("—")
            self.hchart.entries = []
            self.hchart.queue_draw()
            return
        h = load_history(devices.card_id(d, d["size"]))
        self.hchart.entries = h[-24:]
        self.hchart.queue_draw()
        if h:
            e = h[-1]
            dt = e["date"]
            self.hist_row.set_subtitle(f"{len(h)} diagnóstico(s). Último em {dt[8:10]}/{dt[5:7]}/{dt[:4]}: "
                                       f"nota {e['score']} — {e['verdict']}")
        else:
            self.hist_row.set_subtitle("Nenhum diagnóstico anterior desta unidade.")

    def _mode_changed(self):
        self.warn.set_visible(self.mode_dd.get_selected() == 1)
        self.btn_go.set_label("Iniciar teste destrutivo" if self.mode_dd.get_selected() == 1
                              else "Iniciar diagnóstico")

    def _view_changed(self, *a):
        self.lmap.view = "read" if self.view_dd.get_selected() == 0 else "write"
        self.lmap.queue_draw()

    def _open(self, key):
        p = self.result and self.result.get(key)
        if p:
            Gtk.show_uri(self.win, Gio.File.new_for_path(p).get_uri(), Gdk.CURRENT_TIME)

    # ------------------------------------------------------------ iniciar
    def _start(self, *a):
        d = self.picker.selected()
        if not d:
            return
        if self.mode_dd.get_selected() == 1:
            n = "até falhar" if self.sw_fail.get_active() else f"{int(self.passes.get_value())} passada(s)"
            confirm_typed(self.win, d, "Apagar o cartão para testar?",
                          f"O teste de escrita vai <b>apagar tudo</b> em "
                          f"<b>{GLib.markup_escape_text(d['model'])}</b> ({d['path']}, "
                          f"{devices.human(d['size'])}) e gravar o cartão inteiro {n}.",
                          "Apagar e testar", lambda: self._run(d))
        else:
            self._run(d)

    def _run(self, d):
        write = self.mode_dd.get_selected() == 1
        self.mode = "escrita" if write else "leitura"
        self.until_fail = self.sw_fail.get_active()
        self.passes_total = int(self.passes.get_value())
        args = ["diagnosticar", d["path"], "--modo", self.mode,
                "--passadas", str(self.passes_total),
                "--bloco", str(self.BLOCKS[self.block_dd.get_selected()]), "--desmontar"]
        if self.until_fail:
            args.append("--ate-falhar")
        if write:
            args.append("--confirmo-apagar")
        self.result = None
        self.verdict.set_visible(False)
        self.pchart.rows = []
        self.pchart.queue_draw()
        for v in self.st.values():
            v.set_label("—")
        self.view_dd.set_selected(0)
        self._busy(True)
        self._badge("Aguardando autorização…")
        self.bar.set_fraction(0)
        self.pct.set_label("")
        self.worker.start(args, self._event, self._exit)

    def _busy(self, on):
        self.btn_go.set_sensitive(not on)
        self.btn_stop.set_sensitive(on)
        self.picker.dd.set_sensitive(not on)
        self.mode_dd.set_sensitive(not on)
        self.win.set_busy(on)

    # ------------------------------------------------------------ eventos
    def _event(self, ev):
        e = ev.get("ev")
        if e == "dstart":
            self.n = ev["nblocks"]
            self.lmap.reset(self.n, ev["block"])
            self.speed.reset(self.n, ev["block"])
        elif e == "dphase":
            self.cur_pass, self.cur_phase = ev["pass"], ev["phase"]
            if ev["phase"] == "read" and self.mode == "leitura" or ev["phase"] == "write":
                self.speed.new_pass()
            tot = "∞" if self.until_fail else str(self.passes_total)
            fase = "Gravando" if ev["phase"] == "write" else ("Conferindo" if self.mode == "escrita" else "Lendo")
            pad = f" · padrão {ev['pattern']}" if ev.get("pattern") else ""
            self._badge(f"Passada {ev['pass']}/{tot} · {fase}{pad}")
            if self.mode == "escrita":
                self.view_dd.set_selected(1 if ev["phase"] == "write" else 0)
        elif e == "dblocks":
            ph = ev["phase"]
            for i, ms, fl in ev["items"]:
                if ph == "read":
                    self.lmap.rlat[i] = ms
                    if ms:
                        self.lmap.worst[i] = max(self.lmap.worst[i], ms)
                else:
                    self.lmap.wlat[i] = ms
                self.lmap.flags[i] = fl
                self.speed.put(ph, i, ms)
            if ev["items"]:
                self.lmap.cur = ev["items"][-1][0]
            self.lmap.queue_draw()
            self.speed.queue_draw()
        elif e == "dprogress":
            phases = 2 if self.mode == "escrita" else 1
            k = (0 if ev["phase"] == "write" or phases == 1 else 1)
            within = (k + ev["done"] / ev["nblocks"]) / phases
            if self.until_fail:
                frac = within
                self.pct.set_label(f"#{ev['pass']}")
            else:
                frac = ((ev["pass"] - 1) + within) / self.passes_total
                self.pct.set_label(f"{100 * frac:.0f}%")
            self.bar.set_fraction(min(1, frac))
            key = "write" if ev["phase"] == "write" else "read"
            self.st[key].set_label(f"{ev['rate'] / 1e6:.1f} MB/s".replace(".", ","))
            self.st["tbw"].set_label(devices.human(ev["written"]) if ev["written"] else "—")
        elif e == "dpass":
            r, w = ev["read"], ev.get("write")
            self.pchart.rows.append((ev["pass"], r["mbps"], w["mbps"] if w else None))
            self.pchart.queue_draw()
            self._tiles(r, w)
        elif e == "dresult":
            self.result = ev
            self._show(ev)
        elif e == "log":
            if ev.get("level") in ("warn", "error"):
                self.win.toast(ev["msg"])
        elif e == "error":
            self.win.toast(ev["msg"])
            self._badge("Erro", "fail")

    def _tiles(self, r, w):
        f = lambda v: f"{v:.1f}".replace(".", ",")  # noqa: E731
        self.st["read"].set_label(f"{f(r['mbps'])} MB/s")
        if w:
            self.st["write"].set_label(f"{f(w['mbps'])} MB/s")
        self.st["p50"].set_label(f"{f(r['lat_p50'])} ms")
        self.st["p99"].set_label(f"{f(r['lat_p99'])} ms")
        self.st["slow"].set_label(f"{r['slow']}" + (f" ({r['very_slow']} muito)" if r["very_slow"] else ""))

    def _show(self, ev):
        c = ev["counts"]
        self.st["err"].set_label(str(c["read_errors"] + c["write_errors"]))
        self.st["bad"].set_label(str(c["corrupt"] + c["unstable"] + c["alias"] + c["lost_writes"]))
        self.st["tbw"].set_label(devices.human(ev["written"]) if ev["written"] else "0")
        if ev["summaries"]:
            last = ev["summaries"][-1]
            self._tiles(last["read"], last.get("write"))
        for cl in ("ok", "warn", "bad"):
            self.verdict.remove_css_class(cl)
        self.verdict.add_css_class(LEVEL_CSS[ev["level"]])
        self.v_score.set_label(str(ev["score"]))
        self.v_title.set_label(ev["verdict"])
        st = {"done": "teste completo", "failed": "parou no primeiro erro (modo até falhar)",
              "cancelled": "interrompido por você", "lost": "o cartão parou de responder"}[ev["status"]]
        self.v_sub.set_label(f"Nota de 0 a 100 · {ev['passes_done']} passada(s) em "
                             f"{int(ev['elapsed'] // 60)} min {int(ev['elapsed'] % 60)} s · {st}")
        child = self.v_list.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.v_list.remove(child)
            child = nxt
        dots = {"good": "dot-good", "info": "dot-pending", "warning": "dot-warn",
                "serious": "dot-serious", "critical": "dot-crit"}
        for fnd in ev["findings"]:
            b = Gtk.Box(spacing=8)
            dot = Gtk.Box(valign=Gtk.Align.START)
            dot.set_margin_top(4)
            dot.add_css_class("legend-dot")
            dot.add_css_class(dots[fnd["level"]])
            lab = Gtk.Label(label=fnd["text"], xalign=0, wrap=True, hexpand=True)
            b.append(dot)
            b.append(lab)
            self.v_list.append(b)
        self.verdict.set_visible(True)
        self.bar.set_fraction(1 if ev["status"] == "done" else self.bar.get_fraction())
        self._badge(f"{ev['verdict']} · nota {ev['score']}",
                    {"good": "done", "warning": "warn", "serious": "warn"}.get(ev["level"], "fail"))
        self.lmap.cur = None
        blocks = ev.get("blocks")
        if blocks and len(blocks) == self.n:
            for i, (r, w, fl, worst) in enumerate(blocks):
                self.lmap.rlat[i], self.lmap.wlat[i], self.lmap.flags[i], self.lmap.worst[i] = r, w, fl, worst
        self.lmap.queue_draw()
        self.win.toast(f"Diagnóstico: {ev['verdict']} (nota {ev['score']})")
        self._load_hist(self.picker.selected())

    def _exit(self, code, got, err):
        self._busy(False)
        if not got:
            msg = {126: "Autorização cancelada.", 127: "Autorização negada."}.get(code, err[-300:] or f"código {code}")
            self.win.toast(msg)
            self._badge("Pronto")
        self.picker.refresh(True)

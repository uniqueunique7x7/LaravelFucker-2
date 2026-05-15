"""Main window — Golden Laravel Scanner. Full rewrite with Start/Stop/Pause controls."""

import itertools
import queue
import threading
import webbrowser
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Optional

import customtkinter as ctk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from tkinter import END, Menu, ttk, filedialog, messagebox
from PIL import Image

try:
    import tkinter.dnd as tkdnd
except ImportError:
    tkdnd = None

from config.theme import BACKGROUND, GOLD, GOLD_ALT, GOLD_SECONDARY, PANEL, TEXT, TEXT_SOFT, HOVER
from core.persistence import (
    clear_results,
    count_results_by_status,
    export_csv,
    export_json,
    load_settings,
    load_session,
    restore_results,
    save_session,
    save_settings,
)
from core.scanner import ScanResult, Scanner


def tip(widget: Any, text: str) -> None:
    try:
        ctk.CTkToolTip(widget, text=text)
    except Exception:
        pass


def load_ctk_image(name: str, size: tuple = (22, 22)) -> Optional[ctk.CTkImage]:
    path = Path("assets") / name
    if not path.exists():
        return None
    pil = Image.open(path).resize(size, Image.LANCZOS)
    return ctk.CTkImage(light_image=pil, dark_image=pil)




# ──────────────────────────────────────────────
# Splash Screen
# ──────────────────────────────────────────────

class SplashScreen(ctk.CTkToplevel):
    def __init__(self, parent: ctk.CTk) -> None:
        super().__init__(parent)
        self.overrideredirect(True)
        self.configure(fg_color=BACKGROUND)
        self.geometry("540x300")
        self._center()
        self.attributes("-topmost", True)

        card = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=20)
        card.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.9, relheight=0.86)

        ctk.CTkLabel(card, text="⚡  Golden Scanner",
                     font=("Segoe UI", 26, "bold"), text_color=GOLD).pack(pady=(28, 4))
        ctk.CTkLabel(card, text="Laravel .env exposure analyzer",
                     font=("Segoe UI", 12), text_color=TEXT_SOFT).pack()
        self.bar = ctk.CTkProgressBar(card, width=420, progress_color=GOLD)
        self.bar.set(0)
        self.bar.pack(pady=(22, 8))
        self.lbl = ctk.CTkLabel(card, text="Initializing…",
                                text_color=TEXT_SOFT, font=("Segoe UI", 11))
        self.lbl.pack()
        self._tick(0)

    def _center(self) -> None:
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x, y = (sw - 540) // 2, (sh - 300) // 2
        self.geometry(f"540x300+{x}+{y}")

    def _tick(self, v: float) -> None:
        if v > 1.0:
            return
        self.bar.set(min(v, 1.0))
        msgs = {0.0: "Initializing…", 0.3: "Loading engine…",
                0.6: "Building UI…", 0.9: "Almost ready…"}
        for threshold, msg in sorted(msgs.items(), reverse=True):
            if v >= threshold:
                self.lbl.configure(text=msg)
                break
        self.after(55, lambda: self._tick(v + 0.04))


# ──────────────────────────────────────────────
# Stat Card widget
# ──────────────────────────────────────────────

class StatCard(ctk.CTkFrame):
    def __init__(self, master: Any, title: str, value: str = "0",
                 color: str = GOLD, **kw: Any) -> None:
        super().__init__(master, fg_color="#1E1E1E", corner_radius=14, **kw)
        ctk.CTkLabel(self, text=title, font=("Segoe UI", 10),
                     text_color=TEXT_SOFT).pack(pady=(10, 0))
        self._val = ctk.CTkLabel(self, text=value,
                                  font=("Segoe UI", 26, "bold"), text_color=color)
        self._val.pack(pady=(0, 10))

    def set(self, value: Any) -> None:
        self._val.configure(text=str(value))


# ──────────────────────────────────────────────
# Main Application
# ──────────────────────────────────────────────

class GoldenApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.settings = load_settings()
        self.scanner = Scanner(
            max_workers=self.settings.get("thread_count", 100),
            timeout=self.settings.get("timeout", 7),
        )
        self.results: List[Dict[str, Any]] = restore_results()
        self._ui_queue: queue.Queue = queue.Queue()
        self._scan_thread: Optional[threading.Thread] = None
        self._large_file_path: Optional[Path] = None   # set when a big file is loaded
        self._large_file_total: int = 0                # pre-counted line total
        self._live_feed_counter: int = 0               # throttle live feed writes

        # Live counters
        self._total = 0
        self._completed = 0
        self._valid = 0
        self._errors = 0
        self._clean = 0

        self.title("Golden Scanner  —  Laravel .env Analyzer")
        self.geometry(
            f"{self.settings.get('window_width', 1380)}x{self.settings.get('window_height', 820)}"
        )
        self.minsize(1100, 700)
        self.configure(fg_color=BACKGROUND)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_layout()
        self._bind_keys()
        self._restore_session_state()   # repopulate stats/chart from saved results
        self._poll_queue()

    # ── Layout ────────────────────────────────
    def _restore_session_state(self) -> None:
        """Restore stat cards, progress label, and chart from saved results.
        Streams the JSONL file for counts — never loads it all into RAM.
        """
        session = load_session()
        self._completed = session.get("completed", 0)

        # Count statuses by streaming (safe for millions of lines)
        counts = count_results_by_status()
        self._valid  = counts.get("VALID", 0)
        self._errors = counts.get("ERROR", 0)
        self._clean  = counts.get("CLEAN", 0)

        if not any([self._completed, self._valid, self._errors, self._clean]):
            return

        if not self._completed:
            self._completed = self._valid + self._errors + self._clean
        self._total = self._completed

        self._stat_total.set(f"{self._completed:,}")
        self._stat_valid.set(f"{self._valid:,}")
        self._stat_clean.set(f"{self._clean:,}")
        self._stat_error.set(f"{self._errors:,}")

        self._prog_label.configure(
            text=f"{self._completed:,} scanned  ·  {self._valid:,} valid  (restored)"
        )
        self._set_status(
            f"Restored — {self._valid:,} valid  ·  "
            f"{self._errors:,} errors  ·  {self._clean:,} clean  ·  "
            f"{self._completed:,} total scanned  ·  "
            f"showing last {len(self.results):,} in table"
        )
        self._draw_chart()
    def _build_layout(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_nav()
        self._build_body()
        self._build_statusbar()

    def _build_nav(self) -> None:
        nav = ctk.CTkFrame(self, width=220, fg_color=PANEL, corner_radius=0)
        nav.grid(row=0, column=0, rowspan=2, sticky="nsew")
        nav.grid_propagate(False)
        nav.grid_rowconfigure(10, weight=1)
        nav.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(nav, text="⚡ Golden", font=("Segoe UI", 20, "bold"),
                     text_color=GOLD).grid(row=0, column=0, padx=20, pady=(24, 2), sticky="w")
        ctk.CTkLabel(nav, text="Scanner", font=("Segoe UI", 12),
                     text_color=TEXT_SOFT).grid(row=1, column=0, padx=20, pady=(0, 16), sticky="w")

        sep = ctk.CTkFrame(nav, height=1, fg_color="#2A2A2A")
        sep.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 12))

        self._nav_btns: Dict[str, ctk.CTkButton] = {}
        pages = [
            ("🔍  Scanner", "scanner"),
            ("📋  Results", "results"),
            ("📊  Insights", "insights"),
            ("⚙  Settings", "settings"),
        ]
        for i, (label, key) in enumerate(pages, start=3):
            btn = ctk.CTkButton(
                nav, text=label, anchor="w", height=40,
                command=lambda k=key: self._show_page(k),
                fg_color="transparent", hover_color="#2D2D2D",
                text_color=TEXT, font=("Segoe UI", 13), corner_radius=10,
            )
            btn.grid(row=i, column=0, padx=12, pady=3, sticky="ew")
            self._nav_btns[key] = btn

        ctk.CTkButton(
            nav, text="📁  Open RESULTS/", height=36, anchor="w",
            fg_color="#1C1C1C", hover_color="#2D2D2D",
            command=lambda: webbrowser.open("file:///" + str(Path("RESULTS").resolve())),
            corner_radius=10, font=("Segoe UI", 11),
        ).grid(row=11, column=0, padx=12, pady=(0, 20), sticky="ew")

    def _build_body(self) -> None:
        self._container = ctk.CTkFrame(self, fg_color=BACKGROUND)
        self._container.grid(row=0, column=1, sticky="nsew")
        self._container.grid_columnconfigure(0, weight=1)
        self._container.grid_rowconfigure(0, weight=1)

        self._pages: Dict[str, ctk.CTkFrame] = {}
        for key in ("scanner", "results", "insights", "settings"):
            frame = ctk.CTkFrame(self._container, fg_color=BACKGROUND)
            frame.grid(row=0, column=0, sticky="nsew")
            frame.grid_remove()
            self._pages[key] = frame

        self._build_scanner_page()
        self._build_results_page()
        self._build_insights_page()
        self._build_settings_page()
        self._show_page("scanner")

    def _build_statusbar(self) -> None:
        bar = ctk.CTkFrame(self, height=28, fg_color="#141414", corner_radius=0)
        bar.grid(row=1, column=1, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        self._status_var = ctk.StringVar(value="Ready")
        ctk.CTkLabel(bar, textvariable=self._status_var,
                     font=("Segoe UI", 10), text_color="#666"
                     ).grid(row=0, column=0, padx=16, sticky="w")

    # ── Scanner Page ─────────────────────────

    def _build_scanner_page(self) -> None:
        page = self._pages["scanner"]
        page.grid_columnconfigure(0, weight=3)
        page.grid_columnconfigure(1, weight=2)
        page.grid_rowconfigure(1, weight=1)

        # Header
        hdr = ctk.CTkFrame(page, fg_color="transparent")
        hdr.grid(row=0, column=0, columnspan=2, sticky="ew", padx=24, pady=(20, 0))
        ctk.CTkLabel(hdr, text="Scanner", font=("Segoe UI", 22, "bold"),
                     text_color=GOLD).pack(side="left")

        # ── Left panel: targets
        left = ctk.CTkFrame(page, fg_color=PANEL, corner_radius=16)
        left.grid(row=1, column=0, sticky="nsew", padx=(24, 8), pady=16)
        left.grid_rowconfigure(1, weight=1)
        left.grid_columnconfigure(0, weight=1)

        th = ctk.CTkFrame(left, fg_color="transparent")
        th.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 4))
        ctk.CTkLabel(th, text="Target URLs", font=("Segoe UI", 13, "bold"),
                     text_color=TEXT).pack(side="left")
        self._target_count_lbl = ctk.CTkLabel(th, text="0 targets",
                                               font=("Segoe UI", 10), text_color=TEXT_SOFT)
        self._target_count_lbl.pack(side="right")

        self._target_box = ctk.CTkTextbox(
            left, font=("Consolas", 11), corner_radius=10,
            fg_color="#141414", text_color="#CCCCCC",
            scrollbar_button_color=GOLD_SECONDARY,
        )
        self._target_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))
        self._target_box.insert("0.0", "https://example.com\nhttp://target.local")
        self._target_box.bind("<KeyRelease>", lambda _: self._refresh_target_count())

        # File / clipboard buttons
        br = ctk.CTkFrame(left, fg_color="transparent")
        br.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 10))
        br.grid_columnconfigure((0, 1, 2), weight=1)

        ctk.CTkButton(br, text="📂 Load file", height=36, command=self._load_file,
                      corner_radius=10, fg_color="#252525", hover_color="#303030"
                      ).grid(row=0, column=0, padx=(0, 4), sticky="ew")
        ctk.CTkButton(br, text="🗑 Clear", height=36, command=self._clear_targets,
                      corner_radius=10, fg_color="#252525", hover_color="#303030"
                      ).grid(row=0, column=1, padx=4, sticky="ew")
        ctk.CTkButton(br, text="📋 Paste", height=36, command=self._paste_targets,
                      corner_radius=10, fg_color="#252525", hover_color="#303030"
                      ).grid(row=0, column=2, padx=(4, 0), sticky="ew")

        # START / PAUSE / STOP + worker slider
        ctrl = ctk.CTkFrame(left, fg_color="transparent")
        ctrl.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 10))
        ctrl.grid_columnconfigure(3, weight=1)

        self._btn_start = ctk.CTkButton(
            ctrl, text="▶  START", height=44, width=115,
            command=self._start_scan, corner_radius=12,
            fg_color=GOLD, hover_color=GOLD_ALT,
            text_color="#0F0F0F", font=("Segoe UI", 13, "bold"),
        )
        self._btn_start.grid(row=0, column=0, padx=(0, 6))
        tip(self._btn_start, "Start scan  (Ctrl+Enter)")

        self._btn_pause = ctk.CTkButton(
            ctrl, text="⏸  PAUSE", height=44, width=105,
            command=self._toggle_pause, corner_radius=12,
            fg_color="#2B2B2B", hover_color="#3A3A3A",
            state="disabled", font=("Segoe UI", 12, "bold"),
        )
        self._btn_pause.grid(row=0, column=1, padx=6)
        tip(self._btn_pause, "Pause / Resume  (Ctrl+P)")

        self._btn_stop = ctk.CTkButton(
            ctrl, text="⏹  STOP", height=44, width=105,
            command=self._stop_scan, corner_radius=12,
            fg_color="#3D1010", hover_color="#6B1A1A",
            state="disabled", font=("Segoe UI", 12, "bold"),
        )
        self._btn_stop.grid(row=0, column=2, padx=6)
        tip(self._btn_stop, "Stop scan  (Esc)")

        sf = ctk.CTkFrame(ctrl, fg_color="transparent")
        sf.grid(row=0, column=3, padx=(12, 0), sticky="ew")
        sf.grid_columnconfigure(1, weight=1)
        self._worker_var = ctk.IntVar(value=self.settings.get("thread_count", 50))
        self._wlabel = ctk.CTkLabel(sf, text=f"Workers: {self._worker_var.get()}",
                                    font=("Segoe UI", 10), text_color=TEXT_SOFT)
        self._wlabel.grid(row=0, column=0, padx=(0, 6))
        ctk.CTkSlider(sf, from_=5, to=200, variable=self._worker_var,
                      progress_color=GOLD, button_color=GOLD_ALT,
                      command=self._on_worker_change
                      ).grid(row=0, column=1, sticky="ew")

        # Progress bar
        pb = ctk.CTkFrame(left, fg_color="transparent")
        pb.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 14))
        pb.grid_columnconfigure(0, weight=1)
        self._progress = ctk.CTkProgressBar(pb, progress_color=GOLD, corner_radius=6, height=12)
        self._progress.set(0)
        self._progress.grid(row=0, column=0, sticky="ew")
        self._prog_label = ctk.CTkLabel(pb, text="0 / 0  (0%)",
                                         font=("Segoe UI", 10), text_color=TEXT_SOFT)
        self._prog_label.grid(row=1, column=0, sticky="w", pady=(3, 0))

        # ── Right panel: live feed + stat cards
        right = ctk.CTkFrame(page, fg_color=PANEL, corner_radius=16)
        right.grid(row=1, column=1, sticky="nsew", padx=(8, 24), pady=16)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        lf_hdr = ctk.CTkFrame(right, fg_color="transparent")
        lf_hdr.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 4))
        ctk.CTkLabel(lf_hdr, text="Live Feed", font=("Segoe UI", 13, "bold"),
                     text_color=TEXT).pack(side="left")
        self._feed_rate_lbl = ctk.CTkLabel(lf_hdr, text="(all results)",
                                           font=("Segoe UI", 9), text_color=TEXT_SOFT)
        self._feed_rate_lbl.pack(side="left", padx=(8, 0))

        self._live_box = ctk.CTkTextbox(
            right, font=("Consolas", 10), corner_radius=10,
            fg_color="#111111", text_color="#AAAAAA",
            scrollbar_button_color=GOLD_SECONDARY, state="disabled",
        )
        self._live_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 10))

        sc = ctk.CTkFrame(right, fg_color="transparent")
        sc.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 14))
        sc.grid_columnconfigure((0, 1, 2, 3), weight=1)

        self._stat_total = StatCard(sc, "TOTAL", "0")
        self._stat_valid = StatCard(sc, "VALID", "0", color="#4ADE80")
        self._stat_clean = StatCard(sc, "CLEAN", "0", color=TEXT_SOFT)
        self._stat_error = StatCard(sc, "ERRORS", "0", color="#F87171")

        self._stat_total.grid(row=0, column=0, padx=4, sticky="ew")
        self._stat_valid.grid(row=0, column=1, padx=4, sticky="ew")
        self._stat_clean.grid(row=0, column=2, padx=4, sticky="ew")
        self._stat_error.grid(row=0, column=3, padx=4, sticky="ew")

    # ── Results Page ─────────────────────────

    def _build_results_page(self) -> None:
        page = self._pages["results"]
        page.grid_rowconfigure(1, weight=1)
        page.grid_columnconfigure(0, weight=1)

        hdr = ctk.CTkFrame(page, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 0))
        hdr.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(hdr, text="Results", font=("Segoe UI", 22, "bold"),
                     text_color=GOLD).grid(row=0, column=0, sticky="w")

        self._search_var = ctk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._refresh_results_tree())
        ctk.CTkEntry(hdr, placeholder_text="🔍 Filter…", textvariable=self._search_var,
                     height=36, corner_radius=10, fg_color="#1C1C1C", border_color="#333"
                     ).grid(row=0, column=1, padx=16, sticky="ew")

        ab = ctk.CTkFrame(hdr, fg_color="transparent")
        ab.grid(row=0, column=2)
        ctk.CTkButton(ab, text="JSON", width=72, height=36, corner_radius=10,
                      fg_color=GOLD, hover_color=GOLD_ALT, text_color="#0F0F0F",
                      command=self._export_json).pack(side="left", padx=3)
        ctk.CTkButton(ab, text="CSV", width=72, height=36, corner_radius=10,
                      fg_color=GOLD_SECONDARY, hover_color=GOLD_ALT, text_color="#0F0F0F",
                      command=self._export_csv).pack(side="left", padx=3)
        ctk.CTkButton(ab, text="🗑 Clear", width=80, height=36, corner_radius=10,
                      fg_color="#2B2B2B", hover_color="#3D3D3D",
                      command=self._clear_stored).pack(side="left", padx=3)

        card = ctk.CTkFrame(page, fg_color=PANEL, corner_radius=16)
        card.grid(row=1, column=0, sticky="nsew", padx=24, pady=12)
        card.grid_rowconfigure(0, weight=1)
        card.grid_columnconfigure(0, weight=1)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Gold.Treeview", background="#1A1A1A", fieldbackground="#1A1A1A",
                         foreground=TEXT, rowheight=30, borderwidth=0, font=("Segoe UI", 10))
        style.configure("Gold.Treeview.Heading", background="#222", foreground=GOLD,
                         font=("Segoe UI", 10, "bold"), relief="flat")
        style.map("Gold.Treeview", background=[("selected", GOLD_SECONDARY)],
                  foreground=[("selected", "#0F0F0F")])

        self._tree = ttk.Treeview(card, columns=("url", "cat", "status"),
                                  show="headings", style="Gold.Treeview")
        self._tree.heading("url", text="URL")
        self._tree.heading("cat", text="Category")
        self._tree.heading("status", text="Status")
        self._tree.column("url", width=500, anchor="w")
        self._tree.column("cat", width=160, anchor="center")
        self._tree.column("status", width=100, anchor="center")
        self._tree.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        self._tree.tag_configure("valid", foreground="#4ADE80")
        self._tree.tag_configure("error", foreground="#F87171")
        self._tree.tag_configure("clean", foreground=TEXT_SOFT)

        vsb = ttk.Scrollbar(card, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        vsb.grid(row=0, column=1, sticky="ns", pady=12)

        self._tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # Detail box
        det = ctk.CTkFrame(page, fg_color=PANEL, corner_radius=16)
        det.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 16))
        det.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(det, text="Captured keys", font=("Segoe UI", 11, "bold"),
                     text_color=GOLD).grid(row=0, column=0, sticky="w", padx=16, pady=(12, 0))
        self._detail_box = ctk.CTkTextbox(det, height=110, font=("Consolas", 10),
                                          corner_radius=10, fg_color="#111", state="disabled")
        self._detail_box.grid(row=1, column=0, sticky="ew", padx=12, pady=(4, 12))

        ctx = Menu(self, tearoff=0)
        ctx.add_command(label="Copy URL", command=self._copy_url)
        ctx.add_command(label="Open in browser", command=self._open_url)
        self._tree.bind("<Button-3>", lambda e: ctx.tk_popup(e.x_root, e.y_root))

        self._refresh_results_tree()

    # ── Insights Page ────────────────────────

    def _build_insights_page(self) -> None:
        page = self._pages["insights"]
        page.grid_rowconfigure(1, weight=1)
        page.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(page, text="Insights", font=("Segoe UI", 22, "bold"),
                     text_color=GOLD).grid(row=0, column=0, sticky="w", padx=24, pady=(20, 0))

        card = ctk.CTkFrame(page, fg_color=PANEL, corner_radius=16)
        card.grid(row=1, column=0, sticky="nsew", padx=24, pady=16)
        card.grid_rowconfigure(0, weight=1)
        card.grid_columnconfigure(0, weight=1)

        self._fig = Figure(figsize=(8, 4), dpi=96, facecolor=BACKGROUND)
        self._ax = self._fig.add_subplot(111, facecolor="#141414")
        self._canvas = FigureCanvasTkAgg(self._fig, master=card)
        self._canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        self._draw_chart()

    # ── Settings Page ────────────────────────

    def _build_settings_page(self) -> None:
        page = self._pages["settings"]
        page.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(page, text="Settings", font=("Segoe UI", 22, "bold"),
                     text_color=GOLD).grid(row=0, column=0, sticky="w", padx=24, pady=(20, 0))

        card = ctk.CTkFrame(page, fg_color=PANEL, corner_radius=16)
        card.grid(row=1, column=0, sticky="ew", padx=24, pady=16)
        card.grid_columnconfigure(1, weight=1)

        def add_row(r: int, label: str, widget: Any) -> None:
            ctk.CTkLabel(card, text=label, font=("Segoe UI", 12),
                         text_color=TEXT).grid(row=r, column=0, sticky="w", padx=20, pady=10)
            widget.grid(row=r, column=1, padx=20, pady=10, sticky="ew")

        self._s_workers = ctk.IntVar(value=self.settings.get("thread_count", 50))
        s_slider = ctk.CTkSlider(card, from_=5, to=200, variable=self._s_workers,
                                  progress_color=GOLD, button_color=GOLD_ALT)
        add_row(0, "Worker threads", s_slider)

        self._s_timeout = ctk.CTkEntry(card, placeholder_text="8")
        self._s_timeout.insert(0, str(self.settings.get("timeout", 8)))
        add_row(1, "Timeout per request (s)", self._s_timeout)

        self._s_autosave = ctk.CTkCheckBox(card, text="Auto-save results after scan",
                                            fg_color=GOLD, hover_color=GOLD_ALT, text_color=TEXT)
        if self.settings.get("auto_save", True):
            self._s_autosave.select()
        add_row(2, "Auto-save", self._s_autosave)

        ctk.CTkButton(card, text="💾  Save Preferences", height=42, corner_radius=12,
                      fg_color=GOLD, hover_color=GOLD_ALT, text_color="#0F0F0F",
                      font=("Segoe UI", 13, "bold"), command=self._save_settings
                      ).grid(row=3, column=0, columnspan=2, padx=20, pady=(8, 4), sticky="ew")

        acts = ctk.CTkFrame(card, fg_color="transparent")
        acts.grid(row=4, column=0, columnspan=2, padx=20, pady=(4, 20), sticky="ew")
        acts.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(acts, text="↺  Reset State", height=38, corner_radius=12,
                      fg_color="#2B2B2B", hover_color="#3D1010",
                      font=("Segoe UI", 12, "bold"), command=self._reset_scan_state
                      ).grid(row=0, column=0, padx=(0, 6), sticky="ew")
        ctk.CTkButton(acts, text="✏  Edit Progress", height=38, corner_radius=12,
                      fg_color="#2B2B2B", hover_color="#2D2D2D",
                      font=("Segoe UI", 12, "bold"), command=self._edit_progress_dialog
                      ).grid(row=0, column=1, padx=(6, 0), sticky="ew")

    # ── Navigation ───────────────────────────

    def _show_page(self, key: str) -> None:
        for k, frame in self._pages.items():
            if k == key:
                frame.grid()
            else:
                frame.grid_remove()
        for k, btn in self._nav_btns.items():
            btn.configure(
                fg_color=GOLD if k == key else "transparent",
                text_color="#0F0F0F" if k == key else TEXT,
            )

    # ── Scan controls ────────────────────────

    def _start_scan(self) -> None:
        # ── Determine source and total ────────────────
        if self._large_file_path and self._large_file_path.exists():
            # Stream the file; textbox is just a preview
            total = self._large_file_total
            source: Iterable[str] = self._file_generator(self._large_file_path)
        else:
            raw = self._target_box.get("0.0", END).strip()
            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            if not lines:
                messagebox.showwarning("No targets", "Add at least one URL.")
                return
            total = len(lines)
            source = lines

        self._total = total
        self._completed = self._valid = self._errors = self._clean = 0
        self._live_feed_counter = 0
        self._progress.set(0)
        self._prog_label.configure(text=f"0 / {self._total or '?'}  (0%)")
        self._stat_total.set(self._total or "?")
        self._stat_valid.set(0)
        self._stat_clean.set(0)
        self._stat_error.set(0)

        self._live_box.configure(state="normal")
        self._live_box.delete("0.0", END)
        self._live_box.configure(state="disabled")

        self.scanner.max_workers = self._worker_var.get()
        self._btn_start.configure(state="disabled")
        self._btn_pause.configure(state="normal")
        self._btn_stop.configure(state="normal")
        self._set_status(
            f"Scanning {self._total or '?'} targets  ·  {self.scanner.max_workers} workers…"
        )

        self._scan_thread = threading.Thread(
            target=self._run_scan, args=(source, total), daemon=True
        )
        self._scan_thread.start()

    @staticmethod
    def _file_generator(path: Path) -> Generator[str, None, None]:
        """Yield lines from a file one at a time — never loads the whole file."""
        with path.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield line

    def _run_scan(self, source: Iterable[str], total_hint: int) -> None:
        self.scanner.scan_targets(
            source,
            progress_callback=lambda c, t: self._ui_queue.put(("progress", c, t)),
            result_callback=lambda r: self._ui_queue.put(("result", r)),
            total_hint=total_hint,
        )
        self._ui_queue.put(("done", None, None))

    def _toggle_pause(self) -> None:
        if self.scanner.is_paused:
            self.scanner.resume()
            self._btn_pause.configure(text="⏸  PAUSE", fg_color="#2B2B2B",
                                       text_color=TEXT)
            self._set_status("Scan resumed")
        else:
            self.scanner.pause()
            self._btn_pause.configure(text="▶  RESUME", fg_color=GOLD,
                                       text_color="#0F0F0F")
            self._set_status("Scan paused — click RESUME to continue")

    def _stop_scan(self) -> None:
        self.scanner.stop()
        self._btn_stop.configure(state="disabled")
        self._btn_pause.configure(state="disabled")
        self._set_status("Stopping scan…")

    # ── Queue poller ─────────────────────────

    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self._ui_queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    self._on_progress(msg[1], msg[2])
                elif kind == "result":
                    self._on_result(msg[1])
                elif kind == "done":
                    self._on_scan_done()
        except queue.Empty:
            pass
        self.after(80, self._poll_queue)

    def _on_progress(self, current: int, total: int) -> None:
        pct = current / total if total else 0.0
        self._progress.set(pct)
        self._prog_label.configure(text=f"{current} / {total}  ({pct*100:.0f}%)")

    def _on_result(self, result: ScanResult) -> None:
        # STOPPED / SKIPPED are not real scan outcomes — ignore them entirely
        if result.status in ("STOPPED", "SKIPPED"):
            return

        self._completed += 1
        if result.status == "VALID":
            self._valid += 1
        elif result.status in ("ERROR", "DEAD"):
            # DEAD = DNS failed; ERROR = connection/timeout/SSL — both mean
            # "couldn't verify this domain", so they share the ERRORS bucket
            self._errors += 1
        else:
            # CLEAN (no leakage found) or EMPTY (APP_KEY present but no captures)
            self._clean += 1

        self._stat_valid.set(self._valid)
        self._stat_clean.set(self._clean)
        self._stat_error.set(self._errors)

        # Live feed:
        #   VALID / ERROR / DEAD  → always shown (most important outcomes)
        #   CLEAN                 → sampled adaptively so UI stays responsive
        #   ≤1 000 targets → all; ≤10 000 → 1 in 10; >10 000 → 1 in 100
        self._live_feed_counter += 1
        _n = self._total or 1
        _rate = 1 if _n <= 1000 else (10 if _n <= 10000 else 100)
        if _rate != getattr(self, "_live_feed_rate", None):
            self._live_feed_rate = _rate
            lbl = "(all results)" if _rate == 1 else f"(sampled 1/{_rate}  ·  VALID/ERROR/DEAD always shown)"
            self._feed_rate_lbl.configure(text=lbl)
        if result.status in ("VALID", "ERROR", "DEAD") or self._live_feed_counter % _rate == 0:
            icons = {"VALID": "🟢", "ERROR": "🔴", "CLEAN": "⚪",
                     "STOPPED": "🟡", "DEAD": "⚫"}
            icon = icons.get(result.status, "⬜")
            cats = ", ".join(result.categories) if result.categories else (result.category or "")
            cat = f"  → {cats}" if cats else ""
            line = f"{icon} [{result.status}]  {result.url}{cat}\n"
            self._live_box.configure(state="normal")
            self._live_box.insert(END, line)
            self._live_box.see(END)
            self._live_box.configure(state="disabled")

        if result.status == "VALID":
            cats_str = ", ".join(result.categories) if result.categories else (result.category or "General")
            self.results.append({
                "url": result.url,
                "category": cats_str,
                "categories": result.categories,
                "status": result.status,
                "details": result.details,
            })
            self._insert_tree_row(result)

        # Chart update every 500 results to avoid redrawing 10M times
        if self._completed % 500 == 0:
            self._draw_chart()

    def _on_scan_done(self) -> None:
        self._btn_start.configure(state="normal")
        self._btn_pause.configure(state="disabled", text="⏸  PAUSE",
                                   fg_color="#2B2B2B", text_color=TEXT)
        self._btn_stop.configure(state="disabled")
        self._progress.set(1.0)
        self._set_status(
            f"Done — {self._total} scanned  ·  {self._valid} valid  ·  {self._errors} errors"
        )
        self._show_toast(f"Scan complete  ·  {self._valid} valid results found")
        # Results are already persisted line-by-line to RESULTS/results.jsonl
        # No redundant export needed here.

    # ── Reset / Edit progress ──────────────────────────────────

    def _reset_scan_state(self) -> None:
        """Reset all in-memory counters and UI without deleting stored result files."""
        if self._scan_thread and self._scan_thread.is_alive():
            if not messagebox.askyesno(
                    "Reset while scanning?",
                    "A scan is running.  Stop it first and reset all counters?"):
                return
            self.scanner.stop()
        # Drain any pending queue messages so stale results don't land after reset
        try:
            while True:
                self._ui_queue.get_nowait()
        except queue.Empty:
            pass
        self._completed = self._valid = self._errors = self._clean = 0
        self._total = 0
        self._live_feed_counter = 0
        self._live_feed_rate = None
        self._progress.set(0)
        self._prog_label.configure(text="0 / 0  (0%)")
        self._stat_total.set(0)
        self._stat_valid.set(0)
        self._stat_clean.set(0)
        self._stat_error.set(0)
        self._live_box.configure(state="normal")
        self._live_box.delete("0.0", END)
        self._live_box.configure(state="disabled")
        self._feed_rate_lbl.configure(text="(all results)")
        self._btn_start.configure(state="normal")
        self._btn_pause.configure(state="disabled", text="⏸  PAUSE",
                                   fg_color="#2B2B2B", text_color=TEXT)
        self._btn_stop.configure(state="disabled")
        self._set_status("Reset — counters cleared  ·  stored result files untouched")
        self._show_toast("State reset  ·  stored results preserved")

    def _edit_progress_dialog(self) -> None:
        """Edit session.json directly — lets you set the resumed-from counter."""
        session = load_session()

        dialog = ctk.CTkToplevel(self)
        dialog.title("Edit session.json")
        dialog.resizable(False, False)
        dialog.configure(fg_color=BACKGROUND)
        dialog.attributes("-topmost", True)
        dialog.grab_set()
        dialog.update_idletasks()
        dw, dh = 360, 220
        px = self.winfo_x() + (self.winfo_width()  - dw) // 2
        py = self.winfo_y() + (self.winfo_height() - dh) // 2
        dialog.geometry(f"{dw}x{dh}+{px}+{py}")

        card = ctk.CTkFrame(dialog, fg_color=PANEL, corner_radius=16)
        card.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(card, text="Edit session.json",
                     font=("Segoe UI", 13, "bold"), text_color=GOLD).pack(pady=(16, 2))
        ctk.CTkLabel(card, text="Changes are written to config/session.json immediately.",
                     font=("Segoe UI", 9), text_color=TEXT_SOFT).pack(pady=(0, 12))

        # One editable row per key in session.json
        fields: Dict[str, ctk.CTkEntry] = {}
        for key, val in session.items():
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=20, pady=4)
            ctk.CTkLabel(row, text=key, font=("Consolas", 11), text_color=TEXT,
                         width=160, anchor="w").pack(side="left")
            entry = ctk.CTkEntry(row, width=110, height=28, font=("Consolas", 11),
                                 fg_color="#1C1C1C", border_color="#333")
            entry.insert(0, str(val))
            entry.pack(side="right")
            fields[key] = entry

        def _apply() -> None:
            updated: Dict[str, Any] = {}
            for key, entry in fields.items():
                raw = entry.get().strip()
                # Keep the same type as the original value
                orig = session.get(key)
                try:
                    if isinstance(orig, int):
                        updated[key] = int(raw)
                    elif isinstance(orig, float):
                        updated[key] = float(raw)
                    else:
                        updated[key] = raw
                except ValueError:
                    messagebox.showerror("Invalid input",
                                         f'"{key}" must be a {type(orig).__name__}.', parent=dialog)
                    return
            save_session(updated)
            # Also sync the in-memory completed counter so progress bar reflects it
            if "completed" in updated:
                self._completed = updated["completed"]
                pct = self._completed / self._total if self._total else 0.0
                self._progress.set(min(pct, 1.0))
                self._prog_label.configure(
                    text=f"{self._completed:,} / {self._total:,}  ({pct*100:.0f}%)")
                self._stat_total.set(f"{self._total:,}")
            self._set_status(f"session.json saved — completed={updated.get('completed', '?')}")
            self._show_toast("session.json updated")
            dialog.destroy()

        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.pack(pady=(10, 16))
        ctk.CTkButton(btns, text="Save", width=100, height=34, corner_radius=10,
                      fg_color=GOLD, hover_color=GOLD_ALT, text_color="#0F0F0F",
                      font=("Segoe UI", 12, "bold"), command=_apply
                      ).pack(side="left", padx=6)
        ctk.CTkButton(btns, text="Cancel", width=80, height=34, corner_radius=10,
                      fg_color="#2B2B2B", hover_color="#3A3A3A",
                      font=("Segoe UI", 12), command=dialog.destroy
                      ).pack(side="left", padx=6)

    # ── Results helpers ──────────────────────

    def _insert_tree_row(self, result: ScanResult) -> None:
        tag = result.status.lower() if result.status in ("VALID", "ERROR", "CLEAN") else "clean"
        cats = ", ".join(result.categories) if result.categories else (result.category or "—")
        self._tree.insert("", 0, values=(result.url, cats, result.status),
                          tags=(tag,))

    def _refresh_results_tree(self) -> None:
        self._tree.delete(*self._tree.get_children())
        q = self._search_var.get().lower().strip()
        for item in self.results:
            if q and q not in item["url"].lower() \
                    and q not in item["category"].lower() \
                    and q not in item["status"].lower():
                continue
            tag = item["status"].lower() if item["status"] in ("VALID", "ERROR", "CLEAN") else "clean"
            self._tree.insert("", END, values=(item["url"], item["category"], item["status"]),
                              tags=(tag,))

    def _on_tree_select(self, _: Any) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        url = self._tree.item(sel[0], "values")[0]
        match = next((r for r in self.results if r["url"] == url), None)
        self._detail_box.configure(state="normal")
        self._detail_box.delete("0.0", END)
        self._detail_box.insert(END, match["details"] if match else "—")
        self._detail_box.configure(state="disabled")

    def _copy_url(self) -> None:
        sel = self._tree.selection()
        if sel:
            self.clipboard_clear()
            self.clipboard_append(self._tree.item(sel[0], "values")[0])

    def _open_url(self) -> None:
        sel = self._tree.selection()
        if sel:
            webbrowser.open(self._tree.item(sel[0], "values")[0])

    def _export_json(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".json",
                                             filetypes=[("JSON", "*.json")])
        if path:
            self._set_status("Exporting JSON…")
            self.update_idletasks()
            count = export_json(Path(path))
            self._show_toast(f"Exported {count:,} results to JSON")
            self._set_status(f"Exported {count:,} results → {path}")

    def _export_csv(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                             filetypes=[("CSV", "*.csv")])
        if path:
            self._set_status("Exporting CSV…")
            self.update_idletasks()
            count = export_csv(Path(path))
            self._show_toast(f"Exported {count:,} results to CSV")
            self._set_status(f"Exported {count:,} results → {path}")

    def _clear_stored(self) -> None:
        if messagebox.askyesno("Clear results", "Delete all stored scan results?"):
            clear_results()
            self.results = []
            self._valid = self._errors = self._clean = self._completed = self._total = 0
            self._stat_total.set(0)
            self._stat_valid.set(0)
            self._stat_clean.set(0)
            self._stat_error.set(0)
            self._prog_label.configure(text="0 / 0  (0%)")
            self._progress.set(0)
            self._set_status("Results cleared.")
            self._refresh_results_tree()
            self._draw_chart()

    # ── Chart ────────────────────────────────

    def _draw_chart(self) -> None:
        cats: Dict[str, int] = {}
        for item in self.results:
            cats[item["category"]] = cats.get(item["category"], 0) + 1
        self._ax.clear()
        self._ax.set_facecolor("#141414")
        if cats:
            palette = [GOLD, GOLD_ALT, GOLD_SECONDARY, "#C8A020", TEXT_SOFT,
                       "#E8D5A3", "#FFDF4D", "#FFFFFF"]
            bars = self._ax.bar(
                list(cats.keys()), list(cats.values()),
                color=[palette[i % len(palette)] for i in range(len(cats))],
                edgecolor="none", zorder=2,
            )
            self._ax.bar_label(bars, color=TEXT_SOFT, fontsize=9, padding=3)
        self._ax.set_title("Detected categories", color=TEXT, fontsize=11, pad=10)
        self._ax.tick_params(axis="x", colors=TEXT_SOFT, rotation=28, labelsize=8)
        self._ax.tick_params(axis="y", colors=TEXT_SOFT, labelsize=8)
        for sp in self._ax.spines.values():
            sp.set_color("#2A2A2A")
        self._ax.grid(axis="y", color="#2A2A2A", linewidth=0.6, zorder=1)
        self._fig.tight_layout()
        self._canvas.draw_idle()

    # ── Target helpers ───────────────────────

    def _refresh_target_count(self) -> None:
        n = len([l for l in self._target_box.get("0.0", END).splitlines() if l.strip()])
        if self._large_file_path:
            self._target_count_lbl.configure(
                text=f"{self._large_file_total:,} targets (file)")
        else:
            self._target_count_lbl.configure(text=f"{n} targets")

    def _load_file(self) -> None:
        path = filedialog.askopenfilename(
            filetypes=[("Text files", "*.txt"), ("All", "*.*")])
        if not path:
            return
        p = Path(path)
        # Count lines without loading into memory
        self._set_status(f"Counting lines in {p.name}…")
        self.update_idletasks()
        try:
            with p.open(encoding="utf-8", errors="ignore") as fh:
                count = sum(1 for line in fh if line.strip())
        except OSError as exc:
            messagebox.showerror("File error", str(exc))
            return

        LARGE_THRESHOLD = 50_000  # lines above this: don't put in textbox
        if count > LARGE_THRESHOLD:
            self._large_file_path = p
            self._large_file_total = count
            self._target_box.delete("0.0", END)
            self._target_box.insert(END,
                f"# Large file loaded — streaming mode\n"
                f"# Path : {p}\n"
                f"# Lines: {count:,}\n"
                f"#\n"
                f"# Targets will be fed directly to the scanner,\n"
                f"# not loaded into this textbox, to save RAM.\n"
            )
            self._target_count_lbl.configure(text=f"{count:,} targets (file)")
        else:
            self._large_file_path = None
            self._large_file_total = 0
            content = p.read_text(encoding="utf-8", errors="ignore").strip()
            self._target_box.delete("0.0", END)
            self._target_box.insert(END, content)
            self._refresh_target_count()
        self._set_status(f"Loaded {p.name}  ({count:,} lines)")

    def _clear_targets(self) -> None:
        self._target_box.delete("0.0", END)
        self._large_file_path = None
        self._large_file_total = 0
        self._refresh_target_count()

    def _paste_targets(self) -> None:
        try:
            text = self.clipboard_get()
            existing = self._target_box.get("0.0", END).strip()
            self._target_box.insert(END, ("\n" if existing else "") + text)
            self._refresh_target_count()
        except Exception:
            pass

    # ── Settings ─────────────────────────────

    def _on_worker_change(self, v: float) -> None:
        n = int(v)
        self._wlabel.configure(text=f"Workers: {n}")
        self.scanner.max_workers = n
        self.settings["thread_count"] = n

    def _save_settings(self) -> None:
        try:
            timeout = max(1, int(self._s_timeout.get()))
        except ValueError:
            timeout = 8
        self.settings.update({
            "thread_count": int(self._s_workers.get()),
            "timeout": timeout,
            "auto_save": bool(self._s_autosave.get()),
            "window_width": self.winfo_width(),
            "window_height": self.winfo_height(),
        })
        save_settings(self.settings)
        self.scanner.max_workers = self.settings["thread_count"]
        self.scanner.timeout = timeout
        self._worker_var.set(self.settings["thread_count"])
        self._show_toast("Settings saved")

    # ── Keyboard shortcuts ───────────────────

    def _bind_keys(self) -> None:
        self.bind("<Control-Return>", lambda _: self._start_scan())
        self.bind("<Control-p>", lambda _: self._toggle_pause())
        self.bind("<Escape>", lambda _: self._stop_scan())
        self.bind("<Control-s>", lambda _: self._export_json())

    # ── Toast / status ───────────────────────

    def _set_status(self, msg: str) -> None:
        self._status_var.set(msg)

    def _show_toast(self, msg: str, duration: int = 2600) -> None:
        toast = ctk.CTkToplevel(self)
        toast.overrideredirect(True)
        toast.configure(fg_color=PANEL)
        toast.attributes("-topmost", True)
        x = self.winfo_x() + self.winfo_width() - 360
        y = self.winfo_y() + self.winfo_height() - 120
        toast.geometry(f"330x46+{x}+{y}")
        ctk.CTkLabel(toast, text=msg, font=("Segoe UI", 11),
                     text_color=GOLD).pack(expand=True, padx=20, pady=10)
        self.after(duration, toast.destroy)

    def _on_close(self) -> None:
        self.scanner.stop()
        self._save_settings()
        self.destroy()

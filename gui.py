"""
gui.py — Full system GUI for Homer + Bart + Marge
=================================================
  Top    :  Robot status (left col)  |  Order system (right col)
  Middle :  Live View / Tray status  |  (continued)
  Strip  :  Compact terminal log
  Bottom :  Start | Pause | Voice | Emergency Stop
"""

import tkinter as tk
from tkinter import scrolledtext, messagebox
import threading
import queue
import time
import sys
import io

# Voice ordering — optional; button is hidden if the package isn't installed
try:
    import speech_recognition as sr
    _SR_AVAILABLE = True
except ImportError:
    _SR_AVAILABLE = False

from homer  import Homer
from bart   import Bart
from marge  import Marge

# Camera — optional; panel is disabled if the MVS SDK isn't installed
try:
    from camera import DispatchCamera, _SDK_AVAILABLE as _CAM_SDK_AVAILABLE
    _CAM_AVAILABLE = True
except ImportError:
    _CAM_AVAILABLE        = False
    _CAM_SDK_AVAILABLE    = False


# ── Redirect stdout → GUI log queue ────────────────────────────────────────────
class _StdoutRedirect(io.TextIOBase):
    """Captures all print() calls from robot threads and sends them to the GUI."""
    def __init__(self, put_fn):
        self._put = put_fn
        self._buf = ""

    def write(self, text):
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._put(line.strip())
        return len(text)

    def flush(self):
        if self._buf.strip():
            self._put(self._buf.strip())
            self._buf = ""


# ══════════════════════════════════════════════════════════════════════════════
class App:
    # ── Colour palette ─────────────────────────────────────────────────────────
    BG      = "#1a1a1a"
    PANEL   = "#212121"
    BORDER  = "#383838"
    FG      = "#e0e0e0"
    DIM     = "#555555"
    GREEN   = "#27ae60"
    AMBER   = "#e67e22"
    RED     = "#c0392b"
    BLUE    = "#2980b9"
    HOMER_C = "#3498db"
    BART_C  = "#e67e22"
    MARGE_C = "#9b59b6"
    C_RED   = "#e74c3c"
    C_GRN   = "#2ecc71"
    C_BLU   = "#5dade2"

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Robotic Sorting System")
        self.root.geometry("1300x950")
        self.root.configure(bg=self.BG)
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── Robot instances ───────────────────────────────────────────────────
        self.homer:  Homer          = None
        self.bart:   Bart           = None
        self.marge:  Marge          = None
        self.camera: "DispatchCamera | None" = None

        # ── Threading primitives (mirrors run.py) ─────────────────────────────
        self.tray_lock          = threading.Lock()
        self.ok_to_sort         = threading.Event()   # Cleared while Marge works
        self.homer_at_sensor    = threading.Event()
        self.bart_ready_to_scan = threading.Event()
        self.block_on_belt      = threading.Event()
        self.stop_signal        = threading.Event()
        self.system_paused      = threading.Event()   # Cleared on user pause

        self.ok_to_sort.set()
        self.bart_ready_to_scan.set()
        self.system_paused.set()

        # ── Internal state ────────────────────────────────────────────────────
        self._log_q           = queue.Queue()
        self._sys_running     = False
        self._marge_active    = False
        self._is_estopped     = False   # True after E-stop, until resume clears it
        self._homer_block_idx = 0       # Which grid block Homer resumes from
        self._bart_cycle_count= 0       # How many sort cycles Bart has completed
        self._status = {"Homer": "Offline", "Bart": "Offline", "Marge": "Offline"}

        # ── Camera state ──────────────────────────────────────────────────────
        self._cam_verify_var  = tk.BooleanVar(value=True)  # auto-verify checkbox
        self._last_cam_result = ""                          # text shown in the panel
        self._cam_streaming   = False                       # live preview active
        self._cam_photo       = None                        # keep PhotoImage ref alive

        self._build_ui()
        self._poll_log()
        self._poll_status()

    # ══════════════════════════════════════════════════════════════════════════
    # UI CONSTRUCTION
    # ══════════════════════════════════════════════════════════════════════════

    def _card(self, parent, title, **kw) -> tk.LabelFrame:
        return tk.LabelFrame(
            parent, text=f"  {title}  ",
            bg=self.PANEL, fg=self.DIM,
            font=("Segoe UI", 8, "bold"),
            relief="flat", bd=0,
            highlightbackground=self.BORDER, highlightthickness=1,
            **kw
        )

    def _build_ui(self):
        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(self.root, bg=self.BG)
        hdr.pack(fill="x", padx=20, pady=(14, 0))
        tk.Label(hdr, text="ROBOTIC SORTING COMMAND CENTER",
                 font=("Segoe UI", 19, "bold"), bg=self.BG, fg="#00d2ff").pack(side="left")
        self._status_dot = tk.Label(hdr, text="●  READY",
                                     font=("Segoe UI", 10, "bold"), bg=self.BG, fg=self.DIM)
        self._status_dot.pack(side="right", padx=6)

        # ── Pack order: bottom-up so expand on body works correctly ───────────
        # Controls bar — anchored to very bottom first
        self._build_bottom_bar()
        # Terminal log strip — sits just above controls
        self._build_terminal()

        # ── Body (two columns, fills remaining vertical space) ─────────────────
        body = tk.Frame(self.root, bg=self.BG)
        body.pack(fill="both", expand=True, padx=20, pady=(10, 0))

        # Left column: Robot Status + Visualizer + Tray Status stacked
        left = tk.Frame(body, bg=self.BG, width=370)
        left.pack(side="left", fill="y", padx=(0, 12))
        left.pack_propagate(False)
        self._build_robot_status(left)
        self._build_visualizer(left)
        self._build_tray_status(left)

        # Right column: camera card anchored to bottom, order panel fills the rest
        right = tk.Frame(body, bg=self.BG)
        right.pack(side="left", fill="both", expand=True)
        # Pack camera card first (bottom-anchor so order panel gets expand space)
        self._build_camera_card(right)
        self._build_order_panel(right)

    # ── Terminal log strip ─────────────────────────────────────────────────────

    def _build_terminal(self):
        """Compact terminal strip packed above the controls bar."""
        outer = tk.Frame(self.root, bg="#0a0a0a", pady=2)
        outer.pack(fill="x", side="bottom")

        hdr = tk.Frame(outer, bg="#0a0a0a")
        hdr.pack(fill="x", padx=10, pady=(4, 0))
        tk.Label(hdr, text="SYSTEM LOG",
                 font=("Segoe UI", 7, "bold"), bg="#0a0a0a", fg=self.DIM).pack(side="left")

        self._log_area = scrolledtext.ScrolledText(
            outer, bg="#0a0a0a", fg="#00e676",
            font=("Consolas", 9), wrap=tk.WORD,
            relief="flat", bd=0, insertbackground="#00e676",
            height=6)
        self._log_area.pack(fill="x", padx=10, pady=(2, 6))

        self._log_area.tag_config("info",  foreground=self.C_BLU)
        self._log_area.tag_config("warn",  foreground=self.AMBER)
        self._log_area.tag_config("error", foreground=self.C_RED)
        self._log_area.tag_config("marge", foreground="#bb8fce")
        self._log_area.tag_config("ok",    foreground=self.C_GRN)

    # ── Robot status card ──────────────────────────────────────────────────────

    def _build_robot_status(self, parent):
        card = self._card(parent, "ROBOT STATUS", padx=10, pady=6)
        card.pack(fill="x", pady=(0, 8))
        self._robot_lbls:  dict[str, tk.Label]  = {}
        self._home_btns:   dict[str, tk.Button] = {}
        self._homing_active: dict[str, bool]    = {"Homer": False, "Bart": False, "Marge": False}

        for name, col in (("Homer", self.HOMER_C), ("Bart", self.BART_C), ("Marge", self.MARGE_C)):
            row = tk.Frame(card, bg=self.PANEL)
            row.pack(fill="x", pady=2)

            tk.Label(row, text=f"{name}:", width=7, anchor="w",
                     font=("Segoe UI", 9, "bold"), bg=self.PANEL, fg=col).pack(side="left")

            lbl = tk.Label(row, text="Offline", anchor="w",
                           font=("Segoe UI", 9), bg=self.PANEL, fg=self.DIM)
            lbl.pack(side="left", fill="x", expand=True)

            btn = tk.Button(
                row, text="⇱ Safe",
                command=lambda n=name: self._home_robot(n),
                bg="#2c3e50", fg="#aaa",
                font=("Segoe UI", 8), relief="flat",
                activebackground="#34495e", activeforeground="white",
                padx=6, state="disabled"
            )
            btn.pack(side="right", padx=(4, 0))

            self._robot_lbls[name] = lbl
            self._home_btns[name]  = btn

    # ── 2D Visualizer card ─────────────────────────────────────────────────────
    
    def _build_visualizer(self, parent):
        card = self._card(parent, "LIVE VIEW", padx=10, pady=8)
        card.pack(fill="x", pady=(0, 8))
        
        self.canvas = tk.Canvas(card, width=330, height=140, bg="#111", highlightthickness=0)
        self.canvas.pack(pady=5)

        # Belt and Sensor
        self.canvas.create_rectangle(70, 30, 260, 50, fill="#222", outline="#333") 
        self.canvas.create_text(165, 40, text="CONVEYOR", fill="#444", font=("Segoe UI", 7, "bold"))
        self.canvas.create_rectangle(100, 25, 120, 55, fill="", outline=self.AMBER, dash=(2,2)) 
        self.canvas.create_text(110, 15, text="SENSOR", fill=self.AMBER, font=("Segoe UI", 6))

        # Tray
        self.canvas.create_rectangle(100, 90, 230, 130, fill="#222", outline="#333")
        self.canvas.create_text(165, 110, text="TRAY", fill="#444", font=("Segoe UI", 7, "bold"))

        # Robot Indicators
        self.h_icon = self.canvas.create_oval(20, 20, 60, 60, fill="#222", outline=self.HOMER_C, width=2)
        self.canvas.create_text(40, 40, text="H", fill=self.HOMER_C, font=("Segoe UI", 12, "bold"))

        self.b_icon = self.canvas.create_oval(270, 20, 310, 60, fill="#222", outline=self.BART_C, width=2)
        self.canvas.create_text(290, 40, text="B", fill=self.BART_C, font=("Segoe UI", 12, "bold"))

        self.m_icon = self.canvas.create_oval(250, 90, 290, 130, fill="#222", outline=self.MARGE_C, width=2)
        self.canvas.create_text(270, 110, text="M", fill=self.MARGE_C, font=("Segoe UI", 12, "bold"))

        # The block that moves
        self.block_vis = self.canvas.create_rectangle(0, 0, 0, 0, fill="", outline="")

    # ── Tray status card ───────────────────────────────────────────────────────

    def _build_tray_status(self, parent):
        card = self._card(parent, "TRAY STATUS", padx=10, pady=8)
        card.pack(fill="x", pady=(0, 8))

        leg = tk.Frame(card, bg=self.PANEL)
        leg.pack(fill="x", pady=(0, 4))
        for txt, col in (("■ sorted", "#888"), ("■ dispatched", "#444"), ("□ empty", "#333")):
            tk.Label(leg, text=txt, font=("Consolas", 7),
                     bg=self.PANEL, fg=col).pack(side="left", padx=6)

        self._tray_widgets: dict[str, tuple] = {}
        for colour, hex_col in (("red", self.C_RED), ("green", self.C_GRN), ("blue", self.C_BLU)):
            row = tk.Frame(card, bg=self.PANEL)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=colour.upper(), width=6, anchor="w",
                     font=("Segoe UI", 9, "bold"), bg=self.PANEL, fg=hex_col).pack(side="left")
            sq_frame = tk.Frame(row, bg=self.PANEL)
            sq_frame.pack(side="left")
            squares = [
                tk.Label(sq_frame, width=3, height=1, bg="#2a2a2a", relief="flat", bd=1)
                for _ in range(4)
            ]
            for sq in squares:
                sq.pack(side="left", padx=1)
            count_lbl = tk.Label(row, text="0/4", width=4, anchor="w",
                                  font=("Consolas", 9), bg=self.PANEL, fg=self.DIM)
            count_lbl.pack(side="left", padx=(6, 0))
            avail_lbl = tk.Label(row, text="avail: 0", anchor="w",
                                  font=("Consolas", 9), bg=self.PANEL, fg=self.DIM)
            avail_lbl.pack(side="left", padx=4)
            self._tray_widgets[colour] = (squares, count_lbl, avail_lbl, hex_col)

    # ── Order panel ────────────────────────────────────────────────────────────

    def _build_order_panel(self, parent):
        card = self._card(parent, "ORDER SYSTEM", padx=12, pady=10)
        card.pack(fill="both", expand=True)

        ind = tk.Frame(card, bg=self.PANEL)
        ind.pack(fill="x", pady=(0, 6))
        tk.Label(ind, text="Marge:", font=("Segoe UI", 9, "bold"),
                 bg=self.PANEL, fg=self.MARGE_C).pack(side="left")
        self._marge_ind = tk.Label(ind, text="●  Idle",
                                    font=("Segoe UI", 9, "bold"), bg=self.PANEL, fg=self.DIM)
        self._marge_ind.pack(side="left", padx=5)

        tk.Frame(card, height=1, bg=self.BORDER).pack(fill="x", pady=(2, 10))

        self._order_vars:   dict[str, tk.IntVar]  = {}
        self._avail_labels: dict[str, tk.Label]   = {}

        for colour, hex_col in (("red", self.C_RED), ("green", self.C_GRN), ("blue", self.C_BLU)):
            row = tk.Frame(card, bg=self.PANEL)
            row.pack(fill="x", pady=5)

            tk.Label(row, text=colour.upper(), width=7, anchor="w",
                     font=("Segoe UI", 11, "bold"), bg=self.PANEL, fg=hex_col).pack(side="left")

            var = tk.IntVar(value=0)
            self._order_vars[colour] = var

            tk.Button(row, text="−", width=3, relief="flat",
                      bg="#333", fg="white", font=("Segoe UI", 12, "bold"),
                      activebackground="#444", activeforeground="white",
                      command=lambda c=colour: self._adj(c, -1)).pack(side="left")

            tk.Label(row, textvariable=var, width=3,
                     font=("Segoe UI", 13, "bold"), bg=self.PANEL, fg="white").pack(side="left")

            tk.Button(row, text="+", width=3, relief="flat",
                      bg="#333", fg="white", font=("Segoe UI", 12, "bold"),
                      activebackground="#444", activeforeground="white",
                      command=lambda c=colour: self._adj(c, +1)).pack(side="left")

            avail_lbl = tk.Label(row, text="(—)", anchor="e",
                                  font=("Segoe UI", 8), bg=self.PANEL, fg=self.DIM)
            avail_lbl.pack(side="right")
            self._avail_labels[colour] = avail_lbl

        tk.Frame(card, height=1, bg=self.BORDER).pack(fill="x", pady=(8, 8))

        self._place_btn = tk.Button(
            card, text="▶   PLACE ORDER",
            command=self._place_order,
            bg=self.MARGE_C, fg="white",
            font=("Segoe UI", 12, "bold"),
            height=2, relief="flat",
            activebackground="#7d3c98",
            state="disabled"
        )
        self._place_btn.pack(fill="x", pady=(0, 6))

        if _SR_AVAILABLE:
            self._voice_btn = tk.Button(
                card, text="🎤   VOICE ORDER",
                command=self._voice_order,
                bg="#1a5276", fg="white",
                font=("Segoe UI", 11, "bold"),
                height=2, relief="flat",
                activebackground="#154360",
                state="disabled"
            )
            self._voice_btn.pack(fill="x", pady=(0, 10))
            self._voice_status = tk.Label(
                card, text="", font=("Segoe UI", 8, "italic"),
                bg=self.PANEL, fg=self.DIM)
            self._voice_status.pack(anchor="w", pady=(0, 4))
        else:
            self._voice_btn    = None
            self._voice_status = None

        tk.Label(card, text="PENDING ORDERS", font=("Segoe UI", 8, "bold"),
                 bg=self.PANEL, fg=self.DIM).pack(anchor="w")

        q_bg = tk.Frame(card, bg="#0d0d0d", relief="sunken", bd=1)
        q_bg.pack(fill="x", pady=(3, 6))
        self._queue_lb = tk.Listbox(
            q_bg, bg="#0d0d0d", fg="#cccccc",
            font=("Consolas", 9), height=4,
            selectbackground=self.MARGE_C,
            relief="flat", bd=0, activestyle="none"
        )
        self._queue_lb.pack(fill="x", padx=6, pady=4)

        tk.Button(card, text="✕  Remove selected",
                  command=self._remove_order,
                  bg="#2a2a2a", fg=self.DIM,
                  font=("Segoe UI", 8), relief="flat",
                  activebackground="#333", activeforeground=self.FG
                  ).pack(anchor="e")

    # ── Bottom controls bar ────────────────────────────────────────────────────

    def _build_bottom_bar(self):
        bar = tk.Frame(self.root, bg="#111111", pady=12)
        bar.pack(fill="x", side="bottom")

        self._start_btn = tk.Button(
            bar, text="▶   START SYSTEM",
            command=self._start_or_resume,
            bg=self.GREEN, fg="white",
            font=("Segoe UI", 11, "bold"),
            width=18, height=2, relief="flat",
            activebackground="#1e8449"
        )
        self._start_btn.pack(side="left", padx=(80, 15))

        self._pause_btn = tk.Button(
            bar, text="⏸   PAUSE",
            command=self._toggle_pause,
            bg=self.AMBER, fg="white",
            font=("Segoe UI", 11, "bold"),
            width=15, height=2, relief="flat",
            state="disabled",
            activebackground="#d68910"
        )
        self._pause_btn.pack(side="left", padx=15)

        self._estop_btn = tk.Button(
            bar, text="🛑   EMERGENCY STOP",
            command=self._estop,
            bg=self.RED, fg="white",
            font=("Segoe UI", 11, "bold"),
            width=22, height=2, relief="flat",
            activebackground="#922b21"
        )
        self._estop_btn.pack(side="right", padx=(15, 80))

    # ══════════════════════════════════════════════════════════════════════════
    # LOGGING
    # ══════════════════════════════════════════════════════════════════════════

    def _log(self, msg: str, tag: str = ""):
        self._log_q.put((msg, tag))

    def _poll_log(self):
        try:
            while True:
                msg, tag = self._log_q.get_nowait()
                ts   = time.strftime("%H:%M:%S")
                line = f"[{ts}]  {msg}\n"
                if not tag:
                    low = msg.lower()
                    if "error" in low or "warning" in low or "!!!" in low:
                        tag = "warn"
                    elif "complete" in low or "online" in low or "placed" in low:
                        tag = "ok"
                    elif "[marge]" in low:
                        tag = "marge"
                self._log_area.insert(tk.END, line, tag or "")
                self._log_area.see(tk.END)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_log)

    # ══════════════════════════════════════════════════════════════════════════
    # LIVE STATUS POLLING  (runs on main thread every 400 ms)
    # ══════════════════════════════════════════════════════════════════════════

    def _poll_status(self):
        self._refresh_robot_labels()
        self._refresh_visualizer()
        self._refresh_tray_display()
        self._refresh_marge_indicator()
        self._refresh_queue_display()
        self.root.after(400, self._poll_status)

    def _refresh_robot_labels(self):
        robots = {"Homer": self.homer, "Bart": self.bart, "Marge": self.marge}
        for name in ("Homer", "Bart", "Marge"):
            text = self._status[name]
            lbl  = self._robot_lbls[name]
            if "offline" in text.lower():
                lbl.config(text=text, fg=self.DIM)
            elif "error" in text.lower():
                lbl.config(text=text, fg=self.C_RED)
            elif text.lower() in ("idle", "ready", "done (16/16)"):
                lbl.config(text=text, fg=self.C_GRN)
            else:
                lbl.config(text=text, fg=self.FG)

            # Home button: enabled any time workers aren't running.
            # Clicking it will connect the robot first if not already connected.
            workers_idle = not self._sys_running
            homing       = self._homing_active.get(name, False)
            btn = self._home_btns[name]
            if homing:
                btn.config(state="disabled", text="⇱ Moving…", fg="#aaa")
            elif workers_idle:
                btn.config(state="normal",   text="⇱ Safe",    fg="white")
            else:
                btn.config(state="disabled", text="⇱ Safe",    fg="#aaa")

    def _refresh_visualizer(self):
        """Update the 2D visual representation of the system state."""
        # Update Robot Highlights
        h_stat = self._status["Homer"].lower()
        self.canvas.itemconfig(self.h_icon, fill=self.HOMER_C if "picking" in h_stat or "presenting" in h_stat or "placing" in h_stat else "#222")
        
        b_stat = self._status["Bart"].lower()
        self.canvas.itemconfig(self.b_icon, fill=self.BART_C if "sorting" in b_stat or "scanning" in b_stat else "#222")

        m_stat = self._status["Marge"].lower()
        self.canvas.itemconfig(self.m_icon, fill=self.MARGE_C if "fulfilling" in m_stat else "#222")

        # Update Block Position
        self.canvas.coords(self.block_vis, 0, 0, 0, 0)
        self.canvas.itemconfig(self.block_vis, fill="", outline="")

        if self.homer_at_sensor.is_set():
            self.canvas.coords(self.block_vis, 105, 35, 115, 45)
            self.canvas.itemconfig(self.block_vis, fill="#fff", outline="#fff")
        elif self.block_on_belt.is_set():
            # Estimate block halfway to Bart
            self.canvas.coords(self.block_vis, 160, 35, 170, 45)
            # Try to grab the colour Bart detected
            col = getattr(self.bart, 'last_colour', None) if self.bart else None
            hex_c = self.C_RED if col == "red" else self.C_GRN if col == "green" else self.C_BLU if col == "blue" else "#fff"
            self.canvas.itemconfig(self.block_vis, fill=hex_c, outline=hex_c)

    def _refresh_tray_display(self):
        if not self.bart or not self.marge:
            return
        for colour in ("red", "green", "blue"):
            total_placed = self.bart.colour_counts.get(colour, 0)
            total_taken  = self.marge.slots_taken.get(colour, 0)
            in_tray      = max(0, total_placed - total_taken)   # currently occupied
            avail_n      = in_tray                               # all in-tray = available
            squares, count_lbl, avail_lbl, hex_col = self._tray_widgets[colour]

            # Draw the 4 physical slots using ring-buffer indices
            for i in range(4):
                # slot i is occupied if it was placed and not yet taken
                # physical_slot_placed  = total_placed % 4 reached this index
                # physical_slot_taken   = total_taken  % 4 cleared this index
                # Simple approach: colour the first `in_tray` slots starting from
                # the wrap-around position of total_taken
                slot_offset = (total_taken + i) % 4
                if i < in_tray:
                    squares[slot_offset].config(bg=hex_col)       # occupied
                else:
                    squares[slot_offset].config(bg="#2a2a2a")      # empty

            count_lbl.config(
                text=f"{in_tray}/4",
                fg=self.FG if in_tray > 0 else self.DIM
            )
            avail_lbl.config(
                text=f"avail: {avail_n}",
                fg=self.C_GRN if avail_n > 0 else self.DIM
            )
            self._avail_labels[colour].config(
                text=f"({avail_n} avail)",
                fg=self.C_GRN if avail_n > 0 else self.DIM
            )

    def _refresh_marge_indicator(self):
        if self._marge_active:
            self._marge_ind.config(text="●  Fulfilling order", fg=self.MARGE_C)
            self._status_dot.config(text="●  MARGE ACTIVE", fg=self.MARGE_C)
        elif not self._sys_running:
            self._status_dot.config(text="●  READY", fg=self.DIM)
        elif not self.system_paused.is_set():
            self._marge_ind.config(text="●  Idle", fg=self.DIM)
            self._status_dot.config(text="●  PAUSED", fg=self.AMBER)
        else:
            self._marge_ind.config(text="●  Idle", fg=self.DIM)
            self._status_dot.config(text="●  RUNNING", fg=self.C_GRN)

    def _refresh_queue_display(self):
        if not self.marge:
            return
        self._queue_lb.delete(0, tk.END)
        with self.marge.order_lock:
            for order in self.marge.order_queue:
                parts = "  ".join(f"{q}× {c}" for c, q in order.items())
                self._queue_lb.insert(tk.END, f"  {parts}")
        if self._queue_lb.size() == 0:
            self._queue_lb.insert(tk.END, "  (empty)")
            self._queue_lb.itemconfig(0, fg=self.DIM)

    # ══════════════════════════════════════════════════════════════════════════
    # ORDER SYSTEM
    # ══════════════════════════════════════════════════════════════════════════

    def _adj(self, colour: str, delta: int):
        var     = self._order_vars[colour]
        new_val = max(0, var.get() + delta)
        if self.bart and self.marge:
            avail   = max(0, self.bart.colour_counts.get(colour, 0) - self.marge.slots_taken.get(colour, 0))
            new_val = min(new_val, avail)
        var.set(new_val)
        total = sum(v.get() for v in self._order_vars.values())
        can_order = total > 0 and self._sys_running and self.marge is not None
        self._place_btn.config(state="normal" if can_order else "disabled")

    def _place_order(self):
        order = {c: v.get() for c, v in self._order_vars.items() if v.get() > 0}
        if not order:
            return
        if self.bart and self.marge:
            for colour, qty in order.items():
                avail = (self.bart.colour_counts.get(colour, 0) - self.marge.slots_taken.get(colour, 0))
                if qty > avail:
                    messagebox.showwarning(
                        "Insufficient blocks",
                        f"Only {avail} {colour} block(s) available in the tray.\nReduce quantity or wait for Bart to sort more."
                    )
                    return
        self.marge.add_order(order)
        summary = "  ".join(f"{q}× {c}" for c, q in order.items())
        self._log(f"Order queued: {summary}", "marge")
        for var in self._order_vars.values():
            var.set(0)
        self._place_btn.config(state="disabled")

    def _remove_order(self):
        sel = self._queue_lb.curselection()
        if not sel or not self.marge:
            return
        idx = sel[0]
        with self.marge.order_lock:
            if idx < len(self.marge.order_queue):
                removed = self.marge.order_queue.pop(idx)
                summary = ", ".join(f"{q}× {c}" for c, q in removed.items())
                self._log(f"Order removed: {summary}", "warn")

    # ══════════════════════════════════════════════════════════════════════════
    # DISPATCH CAMERA PANEL
    # ══════════════════════════════════════════════════════════════════════════

    def _build_camera_card(self, parent):
        """Standalone camera verification card — packed to the bottom of the right column."""
        card = self._card(parent, "DISPATCH CAMERA", padx=10, pady=8)
        card.pack(fill="x", side="bottom", pady=(6, 0))

        # ── Live preview canvas ───────────────────────────────────────────────
        self._cam_canvas = tk.Canvas(
            card, bg="#0a0a0a", height=220,
            highlightthickness=1, highlightbackground=self.BORDER
        )
        self._cam_canvas.pack(fill="x", pady=(0, 6))
        # Placeholder text — replaced by frames once connected
        self._cam_placeholder = self._cam_canvas.create_text(
            0, 110, text="No camera feed", fill="#2a2a2a",
            font=("Consolas", 11), anchor="w"
        )
        # Image item — reused every frame
        self._cam_img_item = self._cam_canvas.create_image(0, 0, anchor="nw")
        # Centre placeholder after the widget is mapped
        self._cam_canvas.bind("<Configure>",
            lambda e: self._cam_canvas.coords(
                self._cam_placeholder, e.width // 2, e.height // 2))

        # ── Status + Connect / Disconnect ─────────────────────────────────────
        conn_row = tk.Frame(card, bg=self.PANEL)
        conn_row.pack(fill="x", pady=(0, 4))

        self._cam_status_lbl = tk.Label(
            conn_row, text="● Disconnected",
            font=("Segoe UI", 9, "bold"), bg=self.PANEL, fg=self.DIM
        )
        self._cam_status_lbl.pack(side="left")

        self._cam_disc_btn = tk.Button(
            conn_row, text="Disconnect",
            command=self._cam_disconnect,
            bg="#2a2a2a", fg=self.DIM,
            font=("Segoe UI", 8), relief="flat",
            activebackground="#333", activeforeground=self.FG,
            state="disabled"
        )
        self._cam_disc_btn.pack(side="right", padx=(4, 0))

        sdk_ok = _CAM_AVAILABLE and _CAM_SDK_AVAILABLE
        self._cam_conn_btn = tk.Button(
            conn_row, text="Connect",
            command=self._cam_connect,
            bg="#1a5276" if sdk_ok else "#2a2a2a",
            fg="white"   if sdk_ok else self.DIM,
            font=("Segoe UI", 8), relief="flat",
            activebackground="#154360",
            state="normal" if sdk_ok else "disabled"
        )
        self._cam_conn_btn.pack(side="right", padx=(4, 0))

        if not sdk_ok:
            msg = ("(camera.py not found)" if not _CAM_AVAILABLE
                   else "(MVS SDK not installed)")
            tk.Label(conn_row, text=msg, font=("Segoe UI", 7, "italic"),
                     bg=self.PANEL, fg=self.DIM).pack(side="right", padx=6)

        # ── Last result display ───────────────────────────────────────────────
        self._cam_result_lbl = tk.Label(
            card, text="Last check: —",
            font=("Consolas", 9), bg=self.PANEL, fg=self.DIM, anchor="w"
        )
        self._cam_result_lbl.pack(fill="x", pady=(0, 4))

        # ── Options + Snapshot ────────────────────────────────────────────────
        opt_row = tk.Frame(card, bg=self.PANEL)
        opt_row.pack(fill="x")

        tk.Checkbutton(
            opt_row, text="Auto-verify deliveries",
            variable=self._cam_verify_var,
            bg=self.PANEL, fg=self.FG, selectcolor="#333",
            activebackground=self.PANEL, activeforeground=self.FG,
            font=("Segoe UI", 8)
        ).pack(side="left")

        self._cam_snap_btn = tk.Button(
            opt_row, text="📷 Snapshot",
            command=self._cam_snapshot,
            bg="#2a2a2a", fg=self.DIM,
            font=("Segoe UI", 8), relief="flat",
            activebackground="#333", activeforeground=self.FG,
            state="disabled"
        )
        self._cam_snap_btn.pack(side="right")

        # ── Calibration row ───────────────────────────────────────────────────
        cal_row = tk.Frame(card, bg=self.PANEL)
        cal_row.pack(fill="x", pady=(6, 0))
        tk.Label(cal_row, text="Calibrate:",
                 font=("Segoe UI", 8), bg=self.PANEL, fg=self.DIM
                 ).pack(side="left", padx=(0, 6))

        for colour, hex_col in (("red",   self.C_RED),
                                ("green", self.C_GRN),
                                ("blue",  self.C_BLU)):
            tk.Button(
                cal_row, text=colour.upper(),
                command=lambda c=colour: self._cam_calibrate(c),
                bg="#2a2a2a", fg=hex_col,
                font=("Segoe UI", 8, "bold"), relief="flat",
                activebackground="#383838", activeforeground=hex_col,
                padx=8
            ).pack(side="left", padx=2)

    # ── Camera controls ───────────────────────────────────────────────────────

    def _cam_connect(self):
        """Connect to the dispatch camera in a background thread."""
        if not _CAM_AVAILABLE:
            self._log("Camera module not available.", "error")
            return
        self._cam_conn_btn.config(state="disabled", text="Connecting…")
        self._cam_status_lbl.config(text="● Connecting…", fg=self.AMBER)
        threading.Thread(target=self._cam_connect_worker, daemon=True).start()

    def _cam_connect_worker(self):
        try:
            if self.camera is None:
                self.camera = DispatchCamera()
            ok = self.camera.connect()
            if ok:
                self.root.after(0, self._cam_on_connected)
            else:
                self.root.after(0, lambda: self._cam_on_error("Could not open camera."))
        except Exception as exc:
            self.root.after(0, lambda e=exc: self._cam_on_error(str(e)))

    def _cam_on_connected(self):
        self._cam_status_lbl.config(text="● Connected", fg=self.C_GRN)
        self._cam_conn_btn.config(state="disabled", text="Connect")
        self._cam_disc_btn.config(state="normal")
        self._cam_snap_btn.config(state="normal")
        self._log(f"Camera connected — {self.camera._frame_w}×{self.camera._frame_h}", "ok")
        # Hide placeholder text and start streaming
        self._cam_canvas.itemconfig(self._cam_placeholder, state="hidden")
        self._cam_start_stream()

    # ── Live preview streaming ─────────────────────────────────────────────────

    def _cam_start_stream(self):
        """Spawn the background thread that feeds frames to the canvas."""
        self._cam_streaming = True
        threading.Thread(target=self._cam_stream_loop, daemon=True,
                         name="CamStream").start()

    def _cam_stop_stream(self):
        """Signal the stream thread to stop."""
        self._cam_streaming = False

    def _cam_stream_loop(self):
        """Background thread: grab frames and push them to the GUI at ~12 fps."""
        try:
            from PIL import Image, ImageTk, ImageDraw
        except ImportError:
            self.root.after(0, lambda: self._log(
                "Install Pillow for live view: pip install Pillow", "warn"))
            return

        while self._cam_streaming and self.camera and self.camera.connected:
            try:
                frame = self.camera.capture_frame()
                if frame is not None:
                    self.root.after(0, lambda f=frame: self._cam_render(f))
            except Exception:
                pass
            time.sleep(0.08)   # ~12 fps

    def _cam_render(self, frame_bgr):
        """Main-thread callback: convert numpy frame → PhotoImage and update canvas."""
        try:
            from PIL import Image, ImageTk, ImageDraw

            cw = self._cam_canvas.winfo_width()
            ch = self._cam_canvas.winfo_height()
            if cw < 10 or ch < 10:
                return

            fh, fw = frame_bgr.shape[:2]
            scale  = min(cw / fw, ch / fh)
            new_w  = int(fw * scale)
            new_h  = int(fh * scale)

            # BGR → RGB, resize
            img = Image.fromarray(frame_bgr[:, :, ::-1])
            img = img.resize((new_w, new_h), Image.LANCZOS)

            # Draw ROI rectangle so user can see what area is being analysed
            if self.camera and self.camera.roi:
                draw = ImageDraw.Draw(img)
                ry, rx, rh, rw = self.camera.roi
                x0, y0 = int(rx * scale), int(ry * scale)
                x1, y1 = int((rx + rw) * scale), int((ry + rh) * scale)
                draw.rectangle([x0, y0, x1, y1], outline=(0, 220, 220), width=2)
                draw.text((x0 + 3, y0 + 3), "ROI", fill=(0, 220, 220))

            # Keep reference so the image isn't garbage-collected
            self._cam_photo = ImageTk.PhotoImage(img)
            # Place image in top-left; canvas fills the rest with black bg
            self._cam_canvas.itemconfig(self._cam_img_item,
                                        image=self._cam_photo)
            self._cam_canvas.coords(self._cam_img_item,
                                    (cw - new_w) // 2,
                                    (ch - new_h) // 2)
        except Exception:
            pass

    def _cam_on_error(self, msg: str):
        self._cam_status_lbl.config(text="● Error", fg=self.C_RED)
        self._cam_conn_btn.config(state="normal", text="Connect")
        self._log(f"Camera error: {msg}", "error")
        self.camera = None

    def _cam_disconnect(self):
        # Stop the stream thread first, give it one frame interval to exit
        self._cam_stop_stream()
        time.sleep(0.12)
        if self.camera:
            self.camera.close()
            self.camera = None
        # Reset canvas to placeholder
        self._cam_canvas.itemconfig(self._cam_img_item, image="")
        self._cam_canvas.itemconfig(self._cam_placeholder, state="normal")
        self._cam_photo = None
        self._cam_status_lbl.config(text="● Disconnected", fg=self.DIM)
        self._cam_conn_btn.config(state="normal", text="Connect")
        self._cam_disc_btn.config(state="disabled")
        self._cam_snap_btn.config(state="disabled")
        self._cam_result_lbl.config(text="Last check: —", fg=self.DIM)
        self._log("Camera disconnected.", "info")

    def _cam_snapshot(self):
        """Save a snapshot from the current frame (background thread)."""
        if not self.camera or not self.camera.connected:
            self._log("Camera not connected.", "warn")
            return
        threading.Thread(target=self._cam_snapshot_worker, daemon=True).start()

    def _cam_snapshot_worker(self):
        path = self.camera.save_snapshot()
        if path:
            self.root.after(0, lambda p=path:
                self._log(f"Snapshot saved: {p}", "ok"))
        else:
            self.root.after(0, lambda:
                self._log("Snapshot failed — check camera connection.", "error"))

    def _cam_calibrate(self, colour: str):
        """Run a calibration capture for `colour` in a background thread."""
        if not self.camera or not self.camera.connected:
            self._log(f"Camera not connected — cannot calibrate {colour}.", "warn")
            return
        self._log(f"Calibrating camera for {colour}… (block must be in dispatch zone)", "info")
        threading.Thread(
            target=self._cam_calibrate_worker, args=(colour,), daemon=True
        ).start()

    def _cam_calibrate_worker(self, colour: str):
        ok = self.camera.calibrate(colour)
        if ok:
            self.root.after(0, lambda c=colour:
                self._log(f"Camera: '{c}' calibrated successfully.", "ok"))
        else:
            self.root.after(0, lambda c=colour:
                self._log(f"Camera: calibration failed for '{c}'.", "error"))

    def _make_verify_fn(self):
        """
        Return a verify callback if camera is connected and auto-verify is on,
        otherwise return None (Marge skips verification silently).

        The callback signature matches marge.py's expectation:
            verify_fn(expected_colour) → (passed, detected, roi_frame)
        """
        if (self.camera and self.camera.connected
                and self._cam_verify_var.get()):
            cam = self.camera   # capture reference — safe to call from Marge thread

            def _verify(expected_colour):
                passed, detected, roi_frame = cam.verify(expected_colour)
                # Schedule GUI label update on the main thread
                self.root.after(0, lambda p=passed, d=detected,
                                       ec=expected_colour:
                    self._cam_update_result(p, d, ec))
                # Return the tuple marge.py expects
                return (passed, detected, roi_frame)

            return _verify
        return None

    def _cam_update_result(self, passed: bool, detected, expected):
        """Update the last-result label on the main thread."""
        if passed:
            txt = f"Last check: PASS ✓  detected={detected}"
            fg  = self.C_GRN
        else:
            txt = (f"Last check: FAIL ✗  "
                   f"expected={expected}  detected={detected or '—'}")
            fg  = self.C_RED
        self._cam_result_lbl.config(text=txt, fg=fg)
        self._log(txt, "ok" if passed else "warn")

    # ══════════════════════════════════════════════════════════════════════════
    # SYSTEM CONTROLS
    # ══════════════════════════════════════════════════════════════════════════

    def _start_or_resume(self):
        if self._is_estopped:
            self._resume()
        else:
            self._start()

    def _start(self):
        self._homer_block_idx  = 0
        self._bart_cycle_count = 0
        self._is_estopped      = False
        self.stop_signal.clear()
        self.system_paused.set()
        self.ok_to_sort.set()
        self.bart_ready_to_scan.set()
        self.block_on_belt.clear()
        self.homer_at_sensor.clear()
        self._sys_running = True

        self._start_btn.config(state="disabled", text="▶   START SYSTEM", bg=self.GREEN)
        self._pause_btn.config(state="normal", text="⏸   PAUSE", bg=self.AMBER)
        if self._voice_btn:
            self._voice_btn.config(state="normal")
        self._status_dot.config(text="●  INITIALISING...", fg=self.BLUE)

        sys.stdout = _StdoutRedirect(self._log)

        threading.Thread(target=self._homer_worker, daemon=True).start()
        threading.Thread(target=self._bart_worker,  daemon=True).start()
        threading.Thread(target=self._marge_worker, daemon=True).start()

    def _resume(self):
        # ── Safety gate ──────────────────────────────────────────────────────
        confirmed = messagebox.askyesno(
            "Resume System — Safety Check",
            "Before resuming, please confirm ALL of the following:\n\n"
            "  ✔  All robot arms are in a safe resting position\n"
            "  ✔  No people or obstacles are in the work area\n"
            "  ✔  Any dislodged blocks have been cleared\n"
            "  ✔  The conveyor belt is clear\n\n"
            "Is it safe to resume?",
            icon="warning"
        )
        if not confirmed:
            self._log("Resume cancelled by user.", "warn")
            return

        self._is_estopped = False
        self.stop_signal.clear()
        self.system_paused.set()
        self.ok_to_sort.set()
        self.bart_ready_to_scan.set()
        self.block_on_belt.clear()
        self.homer_at_sensor.clear()
        self._sys_running  = True
        self._marge_active = False

        self._start_btn.config(state="disabled", text="▶   START SYSTEM", bg=self.GREEN)
        self._pause_btn.config(state="normal", text="⏸   PAUSE", bg=self.AMBER)
        if self._voice_btn:
            self._voice_btn.config(state="normal")
        self._status_dot.config(text="●  RESUMING...", fg=self.BLUE)

        for robot in (self.homer, self.bart, self.marge):
            if robot:
                try:
                    robot.device.clear_alarms()
                    robot.device._set_queued_cmd_start_exec()
                except Exception:
                    pass

        self._log(
            f"Resuming — Homer from block {self._homer_block_idx + 1}/16, "
            f"Bart from cycle {self._bart_cycle_count + 1}/16.", "warn"
        )

        threading.Thread(target=self._homer_worker, daemon=True).start()
        threading.Thread(target=self._bart_worker,  daemon=True).start()
        threading.Thread(target=self._marge_worker, daemon=True).start()

    def _toggle_pause(self):
        if self.system_paused.is_set():
            self.system_paused.clear()
            self._pause_btn.config(text="▶   RESUME", bg=self.BLUE)
            self._log("System paused by user — robots will stop at next safe point.", "warn")
        else:
            self.system_paused.set()
            self._pause_btn.config(text="⏸   PAUSE", bg=self.AMBER)
            self._log("System resumed.", "info")

    def _estop(self):
        """
        Two-path hardware stop for maximum immediacy:
          Path A (< 1 ms): main thread sends _set_queued_cmd_stop_exec() to each
                           Dobot right now — arm decelerates to a halt immediately.
          Path B (≤ 10 ms): worker threads detect stop_signal in their polling
                            loops and call hw_stop() again for clean suction-off
                            and queue clear (harmless repeat).
        Both paths are needed: Path A is fastest but risks a brief serial race;
        Path B is thread-safe but has one polling interval of latency.
        """
        self._log("!!! EMERGENCY STOP !!!", "error")
        self.stop_signal.set()

        # Path A — fire hardware stop from main thread immediately
        for robot in (self.homer, self.bart, self.marge):
            if robot:
                try:
                    robot.device._set_queued_cmd_stop_exec()
                    robot.device._set_queued_cmd_clear()
                    robot.device.suck(False)
                    # Marge: also kill the rail stepper
                    if robot is self.marge:
                        from marge import RAIL_INTERFACE
                        robot.device._set_stepper_motor(speed=0, interface=RAIL_INTERFACE)
                except Exception:
                    pass

        # Unblock every blocking wait() so worker threads exit cleanly
        for evt in (self.ok_to_sort, self.bart_ready_to_scan,
                    self.homer_at_sensor, self.block_on_belt, self.system_paused):
            evt.set()

        self._sys_running  = False
        self._marge_active = False
        self._is_estopped  = True

        self._start_btn.config(text="↺   RESUME", bg="#16a085",
                               activebackground="#0e6655", state="normal")
        self._pause_btn.config(state="disabled")
        self._place_btn.config(state="disabled")
        if self._voice_btn:
            self._voice_btn.config(state="disabled", text="🎤   VOICE ORDER")
        self._status_dot.config(text="●  E-STOPPED", fg=self.RED)

    def _on_close(self):
        self.stop_signal.set()
        for evt in (self.ok_to_sort, self.bart_ready_to_scan,
                    self.homer_at_sensor, self.block_on_belt, self.system_paused):
            evt.set()
        sys.stdout = sys.__stdout__
        time.sleep(0.3)
        for robot in (self.homer, self.bart, self.marge):
            if robot:
                try:
                    robot.close()
                except Exception:
                    pass
        self._cam_stop_stream()
        time.sleep(0.12)
        if self.camera:
            try:
                self.camera.close()
            except Exception:
                pass
        self.root.destroy()

    # ══════════════════════════════════════════════════════════════════════════
    # SAFE-POSITION MOVES  (available whenever workers are idle)
    # ══════════════════════════════════════════════════════════════════════════

    def _home_robot(self, name: str):
        """Move a robot to its safe standby position.
        Connects first if not already connected — works before START and after E-stop."""
        if self._sys_running or self._homing_active.get(name):
            return
        fn = {"Homer": self._do_safe_homer,
              "Bart":  self._do_safe_bart,
              "Marge": self._do_safe_marge}.get(name)
        if fn:
            if sys.stdout is sys.__stdout__:
                sys.stdout = _StdoutRedirect(self._log)
            self._homing_active[name] = True
            self._status[name] = "Connecting…"
            threading.Thread(target=fn, daemon=True).start()

    def _do_safe_homer(self):
        try:
            if self.homer is None:
                self._log("Homer: connecting…", "info")
                self.homer = Homer(port='COM7')
                self.homer.stop_event = self.stop_signal
                self.homer.setup()
            self._status["Homer"] = "Moving to safe…"
            self._log("Homer: lifting to safe height…", "info")
            from homer import SAFE_Z
            pose = self.homer.device.get_pose()
            self.homer._move(pose.position.x, pose.position.y, SAFE_Z, pose.position.r)
            self._log("Homer: at safe height.", "ok")
            self._status["Homer"] = "Idle"
        except Exception as exc:
            self._log(f"Homer safe-position error: {exc}", "error")
            self._status["Homer"] = "ERROR"
            self.homer = None
        finally:
            self._homing_active["Homer"] = False

    def _do_safe_bart(self):
        try:
            if self.bart is None:
                self._log("Bart: connecting…", "info")
                self.bart = Bart(port='COM8')
                self.bart.stop_event = self.stop_signal
                self.bart.setup()
            self._status["Bart"] = "Moving to safe…"
            self._log("Bart: moving to safe position…", "info")
            self.bart.go_safe()
            self._log("Bart: at safe position.", "ok")
            self._status["Bart"] = "Idle"
        except Exception as exc:
            self._log(f"Bart safe-position error: {exc}", "error")
            self._status["Bart"] = "ERROR"
            self.bart = None
        finally:
            self._homing_active["Bart"] = False

    def _do_safe_marge(self):
        try:
            if self.marge is None:
                self._log("Marge: connecting…", "info")
                self.marge = Marge(port='COM6')
                self.marge.stop_event = self.stop_signal
                self.marge.setup(tray_lock=self.tray_lock)
            self._status["Marge"] = "Moving to safe…"
            self._log("Marge: moving arm to safe position…", "info")
            self.marge.go_to_safe()
            self._log("Marge: at safe position.", "ok")
            self._status["Marge"] = "Idle"
        except Exception as exc:
            self._log(f"Marge safe-position error: {exc}", "error")
            self._status["Marge"] = "ERROR"
            self.marge = None
        finally:
            self._homing_active["Marge"] = False

    # ══════════════════════════════════════════════════════════════════════════
    # VOICE ORDERING
    # ══════════════════════════════════════════════════════════════════════════

    def _voice_order(self):
        if not _SR_AVAILABLE or not self._voice_btn:
            return
        self._voice_btn.config(state="disabled", text="🎤   Listening…")
        self._voice_status.config(text="Speak now: e.g. \"two red, one blue\"", fg=self.C_BLU)
        threading.Thread(target=self._listen_for_order, daemon=True).start()

    def _listen_for_order(self):
        r = sr.Recognizer()
        r.energy_threshold  = 300
        r.pause_threshold   = 0.8
        try:
            with sr.Microphone() as source:
                r.adjust_for_ambient_noise(source, duration=0.4)
                try:
                    audio = r.listen(source, timeout=6, phrase_time_limit=8)
                except sr.WaitTimeoutError:
                    self.root.after(0, lambda: self._voice_done("No speech detected — try again.", "warn"))
                    return
        except OSError:
            self.root.after(0, lambda: self._voice_done("No microphone found.", "error"))
            return

        try:
            text = r.recognize_google(audio).lower()
            self._log(f"Voice heard: \"{text}\"", "info")
            order = self._parse_voice_order(text)
            if order:
                self.root.after(0, lambda o=order: self._apply_voice_order(o))
                return
            else:
                self.root.after(0, lambda: self._voice_done("Could not find colours in speech — try again.", "warn"))
        except sr.UnknownValueError:
            self.root.after(0, lambda: self._voice_done("Speech not understood — try again.", "warn"))
        except sr.RequestError as exc:
            self.root.after(0, lambda e=exc: self._voice_done(f"Recognition error: {e}", "error"))

    @staticmethod
    def _parse_voice_order(text: str) -> dict:
        _NUM = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'a': 1, 'an': 1, '1': 1, '2': 2, '3': 3, '4': 4}
        _COLOURS = {'red', 'blue', 'green'}
        words  = text.replace(',', ' ').split()
        order  = {}
        i = 0
        while i < len(words):
            w = words[i]
            if w in _COLOURS:
                qty = 1
                if i + 1 < len(words) and words[i + 1] in _NUM:
                    qty = _NUM[words[i + 1]]
                    i += 1
                order[w] = order.get(w, 0) + qty
            elif w in _NUM:
                qty = _NUM[w]
                if i + 1 < len(words) and words[i + 1] in _COLOURS:
                    colour = words[i + 1]
                    order[colour] = order.get(colour, 0) + qty
                    i += 1
            i += 1
        return order or {}

    def _apply_voice_order(self, order: dict):
        if self.bart and self.marge:
            for colour in list(order):
                avail = max(0, self.bart.colour_counts.get(colour, 0) - self.marge.slots_taken.get(colour, 0))
                order[colour] = min(order[colour], avail)
                if order[colour] == 0:
                    del order[colour]

        if not order:
            self._voice_done("No blocks available for that order.", "warn")
            return

        for colour, var in self._order_vars.items():
            var.set(order.get(colour, 0))

        summary = ", ".join(f"{q}× {c}" for c, q in order.items())
        total   = sum(v.get() for v in self._order_vars.values())
        can_order = total > 0 and self._sys_running and self.marge is not None
        self._place_btn.config(state="normal" if can_order else "disabled")
        self._voice_done(f"Voice order ready: {summary} — press PLACE ORDER to confirm", "ok")

    def _voice_done(self, msg: str, tag: str = ""):
        if self._voice_btn:
            self._voice_btn.config(state="normal", text="🎤   VOICE ORDER")
        if self._voice_status:
            colours = {"ok": self.C_GRN, "warn": self.AMBER, "error": self.C_RED, "info": self.C_BLU}
            self._voice_status.config(text=msg, fg=colours.get(tag, self.DIM))
        self._log(f"Voice: {msg}", tag)

    # ══════════════════════════════════════════════════════════════════════════
    # WORKER THREADS  (mirror run.py logic exactly)
    # ══════════════════════════════════════════════════════════════════════════

    def _homer_worker(self):
        try:
            if self.homer is None:
                self._status["Homer"] = "Connecting..."
                self.homer = Homer(port='COM7')
                self.homer.stop_event = self.stop_signal
                self.homer.setup()
                self._status["Homer"] = "Ready"
                self._log("Homer online.", "info")
            elif self._is_estopped:
                self.homer.stop_event = self.stop_signal
                self._status["Homer"] = "Resuming..."
                self._log(f"Homer resuming from block {self._homer_block_idx + 1}/16.", "warn")
            else:
                # Pre-connected via home button before START
                self.homer.stop_event = self.stop_signal
                self._status["Homer"] = "Ready"
                self._log("Homer online (pre-connected).", "info")

            start_idx = self._homer_block_idx
            for block_index in range(start_idx, 16):
                if self.stop_signal.is_set():
                    break
                self._status["Homer"] = f"Waiting  ({block_index + 1}/16)"

                self.system_paused.wait()
                self.bart_ready_to_scan.wait()
                if self.stop_signal.is_set():
                    break
                self.bart_ready_to_scan.clear()

                self._status["Homer"] = f"Picking block {block_index + 1}/16"
                self.homer.pick_block(block_index)

                self._status["Homer"] = f"Presenting #{block_index + 1} to sensor"
                self.homer.hold_over_colour_sensor(at_sensor_event=self.homer_at_sensor)

                self._status["Homer"] = f"Placing #{block_index + 1} on belt"
                self.homer.place_on_conveyor()
                self.block_on_belt.set()

                self._homer_block_idx = block_index + 1

            if not self.stop_signal.is_set():
                self._status["Homer"] = "Done (16/16)"
                self._homer_block_idx = 0
                self._log("Homer: all 16 grid blocks processed.", "ok")

        except InterruptedError:
            self._status["Homer"] = "E-stopped"
            self._log(f"Homer stopped by E-stop (will resume from block {self._homer_block_idx + 1}/16).", "warn")
        except Exception as exc:
            self._status["Homer"] = "ERROR"
            self._log(f"Homer error: {exc}", "error")

    def _bart_worker(self):
        try:
            if self.bart is None:
                self._status["Bart"] = "Connecting..."
                self.bart = Bart(port='COM8')
                self.bart.stop_event = self.stop_signal
                self.bart.setup()
                self._status["Bart"] = "Ready"
                self._log("Bart online.", "info")
            elif self._is_estopped:
                self.bart.stop_event = self.stop_signal
                self.bart.setup()   # re-enables sensor, lifts to safe Z
                self._status["Bart"] = "Resuming..."
                self._log(f"Bart resuming from cycle {self._bart_cycle_count + 1}/16.", "warn")
            else:
                # Pre-connected via home button before START
                self.bart.stop_event = self.stop_signal
                self._status["Bart"] = "Ready"
                self._log("Bart online (pre-connected).", "info")

            remaining = 16 - self._bart_cycle_count
            for _ in range(remaining):
                if self.stop_signal.is_set():
                    break
                self._status["Bart"] = "Waiting for sensor..."

                self.homer_at_sensor.wait()
                if self.stop_signal.is_set():
                    break

                self._status["Bart"] = "Scanning colour..."
                detected = None
                while self.homer_at_sensor.is_set() and not self.stop_signal.is_set():
                    color = self.bart.read_colour()
                    if color:
                        detected = color
                        break
                    time.sleep(0.1)

                self.bart.last_colour = detected or "unknown"
                label = (detected or "unknown").upper()
                self._status["Bart"] = f"Detected: {label}"

                self.block_on_belt.wait()
                self.block_on_belt.clear()

                self.system_paused.wait()
                if self.stop_signal.is_set():
                    break

                self._status["Bart"] = "Waiting for belt travel..."
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    if self.stop_signal.is_set():
                        break
                    time.sleep(0.01)
                if self.stop_signal.is_set():
                    break

                self._status["Bart"] = f"Sorting: {label}"
                self.bart.pick_from_conveyor()
                with self.tray_lock:
                    # Pass Marge's dispatch counts so Bart knows which slots are
                    # physically free and can reuse them (ring-buffer placement)
                    taken = self.marge.slots_taken if self.marge else {}
                    self.bart.place_block(marge_slots_taken=taken)
                    self.bart.go_safe()

                self._bart_cycle_count += 1
                self._status["Bart"] = "Idle"
                self.bart_ready_to_scan.set()

            if not self.stop_signal.is_set():
                self._status["Bart"] = "Done (16/16)"
                self._bart_cycle_count = 0
                self._log("Bart: all blocks sorted.", "ok")

        except InterruptedError:
            self._status["Bart"] = "E-stopped"
            self._log(f"Bart stopped by E-stop (completed {self._bart_cycle_count}/16 cycles).", "warn")
        except Exception as exc:
            self._status["Bart"] = "ERROR"
            self._log(f"Bart error: {exc}", "error")

    def _marge_worker(self):
        try:
            if self.marge is None:
                self._status["Marge"] = "Connecting..."
                self.marge = Marge(port='COM6')
                self.marge.stop_event = self.stop_signal
                self.marge.setup(tray_lock=self.tray_lock)
                self._status["Marge"] = "Idle"
                self._log("Marge online — orders accepted.", "info")
            elif self._is_estopped:
                self.marge.stop_event = self.stop_signal
                self._status["Marge"] = "Resuming..."
                self._log("Marge resuming — homing rail and returning to safe position.", "warn")
                self.marge.setup(tray_lock=self.tray_lock)   # re-homes rail, goes safe
                self._status["Marge"] = "Idle"
            else:
                # Pre-connected via home button before START
                self.marge.stop_event = self.stop_signal
                self.marge.tray_lock = self.tray_lock
                self._status["Marge"] = "Idle"
                self._log("Marge online (pre-connected) — orders accepted.", "info")

            while not self.stop_signal.is_set():
                order = None
                with self.marge.order_lock:
                    if self.marge.order_queue:
                        order = self.marge.order_queue.pop(0)

                if order:
                    summary = ", ".join(f"{q}× {c}" for c, q in order.items())
                    self._log(f"Marge: fulfilling  [{summary}]", "marge")
                    self._status["Marge"] = f"Fulfilling: {summary}"
                    self._marge_active = True

                    verify_fn = self._make_verify_fn()
                    self.marge.fulfil_order(order, self.bart.colour_counts,
                                            verify_fn=verify_fn)

                    self._marge_active = False
                    self._status["Marge"] = "Idle"
                    self._log("Marge: order complete.", "marge")
                else:
                    time.sleep(0.3)

            self._status["Marge"] = "Stopped"

        except InterruptedError:
            self._status["Marge"] = "E-stopped"
            self._marge_active = False
            self.ok_to_sort.set()
            self._log("Marge stopped by E-stop.", "warn")
        except Exception as exc:
            self._status["Marge"] = "ERROR"
            self._marge_active = False
            self.ok_to_sort.set()
            self._log(f"Marge error: {exc}", "error")


# ── Entry point ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
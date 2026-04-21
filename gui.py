"""
gui.py — Full system GUI for Homer + Bart + Marge
=================================================
  Left  :  Live system log (all robot print output)
  Right :  Robot status  /  Tray status  /  Order system
  Bottom:  Start | Pause | Emergency Stop
"""

import tkinter as tk
from tkinter import scrolledtext, messagebox
import threading
import queue
import time
import sys
import io

from homer import Homer
from bart  import Bart
from marge import Marge


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
        self.root.geometry("1300x900")
        self.root.configure(bg=self.BG)
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── Robot instances ───────────────────────────────────────────────────
        self.homer: Homer = None
        self.bart:  Bart  = None
        self.marge: Marge = None

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
        self._log_q         = queue.Queue()
        self._sys_running   = False
        self._marge_active  = False
        self._status = {"Homer": "Offline", "Bart": "Offline", "Marge": "Offline"}

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
        # Header row
        hdr = tk.Frame(self.root, bg=self.BG)
        hdr.pack(fill="x", padx=20, pady=(14, 0))
        tk.Label(hdr, text="ROBOTIC SORTING COMMAND CENTER",
                 font=("Segoe UI", 19, "bold"), bg=self.BG, fg="#00d2ff").pack(side="left")
        self._status_dot = tk.Label(hdr, text="●  READY",
                                     font=("Segoe UI", 10, "bold"), bg=self.BG, fg=self.DIM)
        self._status_dot.pack(side="right", padx=6)

        # Body
        body = tk.Frame(self.root, bg=self.BG)
        body.pack(fill="both", expand=True, padx=20, pady=10)

        self._build_log_panel(body)

        right = tk.Frame(body, bg=self.BG, width=400)
        right.pack(side="right", fill="y", padx=(12, 0))
        right.pack_propagate(False)
        self._build_robot_status(right)
        self._build_tray_status(right)
        self._build_order_panel(right)

        self._build_bottom_bar()

    # ── Log panel ──────────────────────────────────────────────────────────────

    def _build_log_panel(self, parent):
        frame = tk.Frame(parent, bg=self.BG)
        frame.pack(side="left", fill="both", expand=True)
        tk.Label(frame, text="SYSTEM LOG",
                 font=("Segoe UI", 8, "bold"), bg=self.BG, fg=self.DIM).pack(anchor="w")
        self._log_area = scrolledtext.ScrolledText(
            frame, bg="#0d0d0d", fg="#00e676",
            font=("Consolas", 9), wrap=tk.WORD,
            relief="flat", bd=0, insertbackground="#00e676")
        self._log_area.pack(fill="both", expand=True)
        # Named text tags for colour-coded lines
        self._log_area.tag_config("info",  foreground=self.C_BLU)
        self._log_area.tag_config("warn",  foreground=self.AMBER)
        self._log_area.tag_config("error", foreground=self.C_RED)
        self._log_area.tag_config("marge", foreground="#bb8fce")
        self._log_area.tag_config("ok",    foreground=self.C_GRN)

    # ── Robot status card ──────────────────────────────────────────────────────

    def _build_robot_status(self, parent):
        card = self._card(parent, "ROBOT STATUS", padx=10, pady=6)
        card.pack(fill="x", pady=(0, 8))
        self._robot_lbls: dict[str, tk.Label] = {}
        for name, col in (("Homer", self.HOMER_C), ("Bart", self.BART_C), ("Marge", self.MARGE_C)):
            row = tk.Frame(card, bg=self.PANEL)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=f"{name}:", width=7, anchor="w",
                     font=("Segoe UI", 9, "bold"), bg=self.PANEL, fg=col).pack(side="left")
            lbl = tk.Label(row, text="Offline", anchor="w",
                           font=("Segoe UI", 9), bg=self.PANEL, fg=self.DIM)
            lbl.pack(side="left", fill="x")
            self._robot_lbls[name] = lbl

    # ── Tray status card ───────────────────────────────────────────────────────

    def _build_tray_status(self, parent):
        card = self._card(parent, "TRAY STATUS", padx=10, pady=8)
        card.pack(fill="x", pady=(0, 8))

        # Legend row
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

        # Marge activity indicator
        ind = tk.Frame(card, bg=self.PANEL)
        ind.pack(fill="x", pady=(0, 6))
        tk.Label(ind, text="Marge:", font=("Segoe UI", 9, "bold"),
                 bg=self.PANEL, fg=self.MARGE_C).pack(side="left")
        self._marge_ind = tk.Label(ind, text="●  Idle",
                                    font=("Segoe UI", 9, "bold"), bg=self.PANEL, fg=self.DIM)
        self._marge_ind.pack(side="left", padx=5)

        tk.Frame(card, height=1, bg=self.BORDER).pack(fill="x", pady=(2, 10))

        # ── Per-colour quantity rows ───────────────────────────────────────────
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

        # ── Place Order button ─────────────────────────────────────────────────
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
        self._place_btn.pack(fill="x", pady=(0, 10))

        # ── Pending orders queue ───────────────────────────────────────────────
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

        # Clear order button (removes selected queued order)
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
            command=self._start,
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
        """Thread-safe: push a message to the log queue."""
        self._log_q.put((msg, tag))

    def _poll_log(self):
        """Drain the log queue onto the text widget (runs on main thread)."""
        try:
            while True:
                msg, tag = self._log_q.get_nowait()
                ts   = time.strftime("%H:%M:%S")
                line = f"[{ts}]  {msg}\n"
                # Auto-tag lines that came from the robot print() redirect
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
        self._refresh_tray_display()
        self._refresh_marge_indicator()
        self._refresh_queue_display()
        self.root.after(400, self._poll_status)

    def _refresh_robot_labels(self):
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

    def _refresh_tray_display(self):
        if not self.bart or not self.marge:
            return
        for colour in ("red", "green", "blue"):
            sorted_n  = self.bart.colour_counts.get(colour, 0)
            taken_n   = self.marge.slots_taken.get(colour, 0)
            avail_n   = max(0, sorted_n - taken_n)
            squares, count_lbl, avail_lbl, hex_col = self._tray_widgets[colour]

            for i, sq in enumerate(squares):
                if i < taken_n:
                    sq.config(bg="#3d3d3d")   # dispatched (dim)
                elif i < sorted_n:
                    sq.config(bg=hex_col)     # in tray (coloured)
                else:
                    sq.config(bg="#2a2a2a")   # empty

            count_lbl.config(
                text=f"{sorted_n}/4",
                fg=self.FG if sorted_n > 0 else self.DIM
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
            self._marge_ind.config(text="●  Fulfilling order — sorting paused",
                                    fg=self.MARGE_C)
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
        """Increment / decrement a colour quantity, capped by available blocks."""
        var     = self._order_vars[colour]
        new_val = max(0, var.get() + delta)
        if self.bart and self.marge:
            avail   = max(0, self.bart.colour_counts.get(colour, 0)
                             - self.marge.slots_taken.get(colour, 0))
            new_val = min(new_val, avail)
        var.set(new_val)
        total = sum(v.get() for v in self._order_vars.values())
        can_order = total > 0 and self._sys_running and self.marge is not None
        self._place_btn.config(state="normal" if can_order else "disabled")

    def _place_order(self):
        order = {c: v.get() for c, v in self._order_vars.items() if v.get() > 0}
        if not order:
            return
        # Final availability check before submitting
        if self.bart and self.marge:
            for colour, qty in order.items():
                avail = (self.bart.colour_counts.get(colour, 0)
                         - self.marge.slots_taken.get(colour, 0))
                if qty > avail:
                    messagebox.showwarning(
                        "Insufficient blocks",
                        f"Only {avail} {colour} block(s) available in the tray.\n"
                        f"Reduce quantity or wait for Bart to sort more."
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
    # SYSTEM CONTROLS
    # ══════════════════════════════════════════════════════════════════════════

    def _start(self):
        self.stop_signal.clear()
        self.system_paused.set()
        self.ok_to_sort.set()
        self.bart_ready_to_scan.set()
        self._sys_running = True

        self._start_btn.config(state="disabled")
        self._pause_btn.config(state="normal", text="⏸   PAUSE", bg=self.AMBER)
        self._status_dot.config(text="●  INITIALISING...", fg=self.BLUE)

        # Redirect all robot print() calls into the GUI log
        sys.stdout = _StdoutRedirect(self._log)

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
        self._log("!!! EMERGENCY STOP !!!", "error")
        self.stop_signal.set()
        # Unblock every waiting event so threads can exit
        for evt in (self.ok_to_sort, self.bart_ready_to_scan,
                    self.homer_at_sensor, self.block_on_belt, self.system_paused):
            evt.set()
        self._sys_running  = False
        self._marge_active = False
        self._start_btn.config(state="normal")
        self._pause_btn.config(state="disabled")
        self._place_btn.config(state="disabled")
        # Stop hardware command queues and cut suction on all robots immediately
        for robot in (self.homer, self.bart, self.marge):
            if robot:
                try:
                    robot.hw_stop()          # clears Dobot queue → arm decelerates to halt
                    robot.device.clear_alarms()
                except Exception:
                    pass

    def _on_close(self):
        """Clean shutdown when the window X button is pressed."""
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
        self.root.destroy()

    # ══════════════════════════════════════════════════════════════════════════
    # WORKER THREADS  (mirror run.py logic exactly)
    # ══════════════════════════════════════════════════════════════════════════

    def _homer_worker(self):
        try:
            self._status["Homer"] = "Connecting..."
            self.homer = Homer(port='COM7')
            self.homer.stop_event = self.stop_signal
            self.homer.setup()
            self._status["Homer"] = "Ready"
            self._log("Homer online.", "info")

            for block_index in range(16):
                if self.stop_signal.is_set():
                    break
                self._status["Homer"] = f"Waiting  ({block_index + 1}/16)"

                # Gate: wait for Marge to finish AND user not paused AND Bart ready
                self.ok_to_sort.wait()
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

            self._status["Homer"] = "Done (16/16)"
            self._log("Homer: all 16 grid blocks processed.", "ok")

        except Exception as exc:
            self._status["Homer"] = "ERROR"
            self._log(f"Homer error: {exc}", "error")

    def _bart_worker(self):
        try:
            self._status["Bart"] = "Connecting..."
            self.bart = Bart(port='COM8')
            self.bart.stop_event = self.stop_signal
            self.bart.setup()
            self._status["Bart"] = "Ready"
            self._log("Bart online.", "info")

            for _ in range(16):
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

                # Pause here if Marge is working or user paused
                self.ok_to_sort.wait()
                self.system_paused.wait()
                if self.stop_signal.is_set():
                    break

                self._status["Bart"] = "Waiting for belt travel..."
                # Interruptible — checks stop_signal every 50 ms
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    if self.stop_signal.is_set():
                        break
                    time.sleep(0.05)
                if self.stop_signal.is_set():
                    break

                self._status["Bart"] = f"Sorting: {label}"
                self.bart.pick_from_conveyor()
                # go_safe() is called INSIDE tray_lock — lock is not released
                # until Bart's arm is fully clear, so Marge can never enter the
                # tray area while Bart is still above it.
                with self.tray_lock:
                    self.bart.place_block()
                    self.bart.go_safe()

                self._status["Bart"] = "Idle"
                self.bart_ready_to_scan.set()

            self._status["Bart"] = "Done (16/16)"
            self._log("Bart: all blocks sorted.", "ok")

        except Exception as exc:
            self._status["Bart"] = "ERROR"
            self._log(f"Bart error: {exc}", "error")

    def _marge_worker(self):
        try:
            self._status["Marge"] = "Connecting..."
            self.marge = Marge(port='COM6')
            self.marge.stop_event = self.stop_signal
            self.marge.setup(tray_lock=self.tray_lock)
            self._status["Marge"] = "Idle"
            self._log("Marge online — orders accepted.", "info")

            # Enable the Place Order button now Marge is ready
            self.root.after(0, lambda: self._place_btn.config(state="disabled"))

            while not self.stop_signal.is_set():
                order = None
                with self.marge.order_lock:
                    if self.marge.order_queue:
                        order = self.marge.order_queue.pop(0)

                if order:
                    summary = ", ".join(f"{q}× {c}" for c, q in order.items())
                    self._log(f"Marge: fulfilling  [{summary}]  — sorting paused", "marge")
                    self._status["Marge"] = f"Fulfilling: {summary}"
                    self._marge_active = True

                    self.ok_to_sort.clear()          # Pause Homer & Bart
                    time.sleep(0.5)                  # Let any in-flight tray move finish
                    self.marge.fulfil_order(order, self.bart.colour_counts)
                    self.ok_to_sort.set()            # Resume Homer & Bart

                    self._marge_active = False
                    self._status["Marge"] = "Idle"
                    self._log(f"Marge: order complete — sorting resumed.", "marge")
                else:
                    time.sleep(0.3)

            self._status["Marge"] = "Stopped"

        except Exception as exc:
            self._status["Marge"] = "ERROR"
            self._log(f"Marge error: {exc}", "error")


# ── Entry point ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()

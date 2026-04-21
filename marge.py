import time
import threading
from pydobotplus import Dobot

# ── Constants ──────────────────────────────────────────────────────────────────
RAIL_INTERFACE  = 1
RAIL_SPEED      = 5000
PULSES_PER_MM   = 80
MAX_TRAVEL_MM   = 1065
SAFE_Z          = 20

# ── Rail positions (mm from home) ──────────────────────────────────────────────
RAIL_TRAY_MM     = 0
RAIL_DISPATCH_MM = 750

# ── Safe arm position during rail travel ───────────────────────────────────────
MARGE_SAFE_POSITION = (0, -180, 20, 0)

# ── Tray positions ─────────────────────────────────────────────────────────────
# Slot 0 — measured row 1 positions
TRAY_RED   = (229.8, -22.7, -35.5, 0.0)
TRAY_BLUE  = (207.2, -46.2, -37.7, 0.0)
TRAY_GREEN = (183.2, -66.9, -38.1, 0.0)

# Diagonal step per slot — derived from measured row 1 → row 2 difference
# Red:   (+23.7, -21.5)   Blue: (+21.9, -21.9)   Green: (+21.0, -24.1)
TRAY_STEP = {
    'red':   (23.7, -21.5, 0.0),
    'blue':  (21.9, -21.9, 0.0),
    'green': (21.0, -24.1, 0.0),
}

# ── Dispatch box ───────────────────────────────────────────────────────────────
DISPATCH_BOX = (271.3, -30.3, 22.0, 0.0)


class Marge:
    def __init__(self, port='COM6'):
        print(f"[Marge] Connecting on {port}...")
        self.device = Dobot(port=port)
        self.device.speed(velocity=40, acceleration=40)
        self.device.clear_alarms()
        self.device.suck(False)

        self.order_queue  = []
        self.order_lock   = threading.Lock()
        self.slots_taken  = {'red': 0, 'blue': 0, 'green': 0}
        self.tray_lock    = None
        self.rail_pos_mm  = 0.0
        self.stop_event   = None   # Set externally to enable interruptible moves

        print("[Marge] Connected. Suction off.")

    def setup(self, tray_lock):
        self.tray_lock = tray_lock
        print("[Marge] Initialising...")
        self.go_to_safe()
        print("[Marge] Ready.")

    # ── Interruptible primitives ─────────────────────────────────────────────────

    def _move(self, x, y, z, r):
        """
        Move to (x,y,z,r) using distance polling instead of wait=True.
        Raises InterruptedError immediately if stop_event is set.
        """
        self.device.move_to(x, y, z, r, wait=False)
        while True:
            if self.stop_event is not None and self.stop_event.is_set():
                raise InterruptedError("E-stop during move")
            try:
                pose = self.device.get_pose()
                dist = ((pose.position.x - x) ** 2 +
                        (pose.position.y - y) ** 2 +
                        (pose.position.z - z) ** 2) ** 0.5
                if dist < 2.0:
                    return
            except Exception:
                return
            time.sleep(0.05)

    def _sleep(self, seconds):
        """sleep() that wakes immediately if stop_event is set."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.stop_event is not None and self.stop_event.is_set():
                raise InterruptedError("E-stop during sleep")
            time.sleep(0.05)

    def hw_stop(self):
        """Tell the Dobot hardware to stop executing its command queue NOW."""
        try:
            self.device._set_queued_cmd_stop_exec()
            self.device._set_queued_cmd_clear()
            self.device._set_stepper_motor(speed=0, interface=RAIL_INTERFACE)
            self.device.suck(False)
        except Exception:
            pass

    # ── Queue management ─────────────────────────────────────────────────────────

    def _flush_queue(self):
        self.device._set_queued_cmd_stop_exec()
        time.sleep(0.1)
        self.device._set_queued_cmd_clear()
        time.sleep(0.1)
        self.device._set_queued_cmd_start_exec()
        time.sleep(0.1)

    # ── Safe position ────────────────────────────────────────────────────────────

    def go_to_safe(self):
        """
        Lift to SAFE_Z → travel horizontally → lower to MARGE_SAFE_POSITION.
        Must be called before every rail move.
        """
        x, y, z, r = MARGE_SAFE_POSITION
        pose = self.device.get_pose()
        self._move(pose.position.x, pose.position.y, SAFE_Z, pose.position.r)
        self._move(x, y, SAFE_Z, r)
        self._move(x, y, z, r)
        print("[Marge] Arm at safe position.")

    # ── Rail movement ────────────────────────────────────────────────────────────

    def move_rail(self, target_mm):
        """
        Move rail to target mm from home.
        Always moves arm to safe position first.
        """
        target_mm = max(0, min(target_mm, MAX_TRAVEL_MM))
        delta_mm  = target_mm - self.rail_pos_mm

        if abs(delta_mm) < 1:
            print(f"[Marge] Rail already at {target_mm:.0f}mm.")
            return

        self.go_to_safe()
        self._flush_queue()

        duration  = abs(delta_mm * PULSES_PER_MM) / RAIL_SPEED
        speed     = -RAIL_SPEED if delta_mm > 0 else RAIL_SPEED
        direction = "forward" if delta_mm > 0 else "backward"

        print(f"[Marge] Rail {direction} to {target_mm:.0f}mm ({duration:.2f}s)...")
        self.device._set_stepper_motor(speed=speed, interface=RAIL_INTERFACE)
        self._sleep(duration)
        self.device._set_stepper_motor(speed=0, interface=RAIL_INTERFACE)
        self._sleep(0.5)
        self._flush_queue()

        self.rail_pos_mm = target_mm
        print(f"[Marge] Rail at {self.rail_pos_mm:.0f}mm.")

    # ── Arm movement ────────────────────────────────────────────────────────────

    def _safe_move(self, x, y, z, r):
        """Lift to SAFE_Z → travel horizontally → lower."""
        pose = self.device.get_pose()
        self._move(pose.position.x, pose.position.y, SAFE_Z, pose.position.r)
        self._move(x, y, SAFE_Z, r)
        self._move(x, y, z, r)

    # ── Tray access ─────────────────────────────────────────────────────────────

    def _get_tray_position(self, colour, slot):
        """
        Calculate tray position for a given colour and slot.
        Uses diagonal offset — each subsequent block shifts in both X and Y.
        """
        if   colour == 'red':   base = TRAY_RED
        elif colour == 'blue':  base = TRAY_BLUE
        elif colour == 'green': base = TRAY_GREEN
        else:
            raise ValueError(f"Unknown colour: {colour}")

        dx, dy, dz = TRAY_STEP[colour]
        x = base[0] + (slot * dx)
        y = base[1] + (slot * dy)
        z = base[2] + (slot * dz)
        r = base[3]
        return x, y, z, r

    def pick_from_tray(self, colour, bart_colour_counts):
        """Pick next available block of given colour from tray."""
        available = bart_colour_counts.get(colour, 0) - self.slots_taken.get(colour, 0)
        if available <= 0:
            print(f"[Marge] No {colour} blocks available in tray.")
            return False

        slot = self.slots_taken[colour]
        x, y, z, r = self._get_tray_position(colour, slot)

        print(f"[Marge] Picking {colour} block from slot {slot + 1} at ({x:.1f}, {y:.1f}, {z:.1f})...")

        with self.tray_lock:
            self.move_rail(RAIL_TRAY_MM)
            self._safe_move(x, y, z, r)
            self.device.suck(True)
            self._sleep(0.5)
            self.go_to_safe()

        self.slots_taken[colour] += 1
        print(f"[Marge] Picked {colour} block — at safe position.")
        return True

    # ── Dispatch ────────────────────────────────────────────────────────────────

    def deliver_to_box(self):
        """Move rail to dispatch, lower into box, release block."""
        print("[Marge] Delivering to dispatch box...")
        self.move_rail(RAIL_DISPATCH_MM)
        self._safe_move(*DISPATCH_BOX)
        self.device.suck(False)
        self._sleep(0.3)
        self.go_to_safe()
        print("[Marge] Block delivered — at safe position.")

    # ── Order handling ───────────────────────────────────────────────────────────

    def add_order(self, order):
        """Add an order dict to the queue. e.g. {'red': 2, 'blue': 1}"""
        with self.order_lock:
            self.order_queue.append(order)
        print(f"[Marge] Order added: {order}")

    def fulfil_order(self, order, bart_colour_counts):
        """Pick and deliver each block in the order."""
        print(f"[Marge] Fulfilling order: {order}")
        for colour, qty in order.items():
            for i in range(qty):
                print(f"[Marge] Picking {colour} block {i + 1}/{qty}...")
                success = self.pick_from_tray(colour, bart_colour_counts)
                if not success:
                    print(f"[Marge] WARNING: No {colour} block available — skipping.")
                    continue
                self.deliver_to_box()
        print("[Marge] Order complete — returning rail to home...")
        self.move_rail(RAIL_TRAY_MM)
        print(f"[Marge] Order done: {order}")

    def run(self, bart_colour_counts):
        """Main loop — checks order queue and fulfils orders."""
        print("[Marge] Waiting for orders...")
        while True:
            order = None
            with self.order_lock:
                if self.order_queue:
                    order = self.order_queue.pop(0)
            if order:
                self.fulfil_order(order, bart_colour_counts)
                print("[Marge] Ready for next order.")
            else:
                time.sleep(0.5)

    # ── Shutdown ─────────────────────────────────────────────────────────────────

    def close(self):
        self.device.suck(False)
        self.device._set_stepper_motor(speed=0, interface=RAIL_INTERFACE)
        time.sleep(0.3)
        self._flush_queue()
        self.go_to_safe()
        self.device.close()
        print("[Marge] Closed.")


# ── Standalone test ────────────────────────────────────────────────────────────
if __name__ == '__main__':
    tray_lock = threading.Lock()
    marge = Marge(port='COM6')
    marge.setup(tray_lock=tray_lock)

    fake_bart_counts = {'red': 4, 'blue': 4, 'green': 4}

    print("\nOrder Test — type an order like:  red 2 blue 1")
    print("Type 'q' to quit.\n")

    try:
        while True:
            raw = input("Enter order: ").strip().lower()
            if raw == 'q':
                break

            parts = raw.split()
            order = {}
            i = 0
            while i < len(parts) - 1:
                colour = parts[i]
                try:
                    qty = int(parts[i + 1])
                    if colour in ('red', 'blue', 'green'):
                        order[colour] = qty
                        i += 2
                    else:
                        print(f"  Unknown colour '{colour}' — use red, blue or green.")
                        i += 1
                except ValueError:
                    print(f"  Expected a number after '{colour}'.")
                    i += 1

            if not order:
                print("  Could not parse order. Try: red 2 blue 1")
                continue

            print(f"\n  Order: {order}")
            confirm = input("  Confirm? (y/n): ").strip().lower()
            if confirm == 'y':
                marge.fulfil_order(order, fake_bart_counts)
            else:
                print("  Cancelled.")

    except KeyboardInterrupt:
        print("\nStopped.")

    finally:
        marge.close()
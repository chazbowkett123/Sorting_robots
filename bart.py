import time
import struct
from pydobotplus import Dobot
from pydobotplus.message import Message

# ── Constants ──────────────────────────────────────────────────────────────────
SAFE_Z               = 50
CONVEYOR_TRAVEL_TIME = 3.0

# ── Positions ──────────────────────────────────────────────────────────────────
CONV_PICKUP = (182.6,  55.5,  19, 0.0)

# Base positions — column 0 of each colour's row
# Tray layout (4x4):
# Row 0: RED   (top)
# Row 1: GREEN
# Row 2: BLUE  (bottom)
# Columns run left — each next block moves +30 in X
TRAY_RED    = (201.7, -208.0, 14.5, 0.0)   # red,   col 0
TRAY_GREEN  = (138.3, -209.6, 16.9, 0.0)   # green, col 0
TRAY_BLUE   = (171.4, -210.3, 15.4, 0.0)   # blue,  col 0
TRAY_HUMAN  = (268.9,   69.8, 27.6, 0.0)   # yellow / unknown

# Each subsequent block of the same colour shifts +30 in X (next column left)
COL_STEP = 30

# Standby position — clear of the tray area so Marge can pick safely
BART_SAFE_POSITION = (154.6, 82.8, 51.3, 0.0)


class Bart:
    def __init__(self, port='COM8'):
        print(f"[Bart] Connecting on {port}...")
        self.device = Dobot(port=port)
        self.device.speed(velocity=40, acceleration=40)
        self.last_colour = None
        self.colour_counts = {'red': 0, 'blue': 0, 'green': 0, 'unknown': 0}
        print("[Bart] Connected.")

    def setup(self):
        print("[Bart] Initialising...")
        self.device.clear_alarms()
        pose = self.device.get_pose()
        self.device.move_to(
            pose.position.x, pose.position.y,
            SAFE_Z, pose.position.r, wait=True
        )
        # Enable colour sensor
        msg = Message()
        msg.id     = 137
        msg.ctrl   = 0x03
        msg.params = bytearray([1, 1, 1])
        self.device._send_command(msg)
        time.sleep(1.0)
        print("[Bart] Sensor active. Standing by...")

    # ── Movement ────────────────────────────────────────────────────────────────

    def go_safe(self):
        """
        Move to BART_SAFE_POSITION — fully clear of the tray area.
        Must be called (and complete) before releasing tray_lock so that
        Marge can never enter the tray while Bart's arm is still above it.
        """
        x, y, z, r = BART_SAFE_POSITION
        pose = self.device.get_pose()
        if pose.position.z < SAFE_Z:
            self.device.move_to(
                pose.position.x, pose.position.y,
                SAFE_Z, pose.position.r, wait=True
            )
        self.device.move_to(x, y, z, r, wait=True)
        print("[Bart] At safe position.")

    def _safe_move(self, x, y, z, r):
        """Lift to SAFE_Z → travel horizontally → lower. Never diagonal."""
        pose = self.device.get_pose()
        self.device.move_to(
            pose.position.x, pose.position.y,
            SAFE_Z, pose.position.r, wait=True
        )
        self.device.move_to(x, y, SAFE_Z, r, wait=True)
        self.device.move_to(x, y, z, r, wait=True)

    # ── Colour reading ──────────────────────────────────────────────────────────

    def _single_read(self):
        msg = Message()
        msg.id     = 137
        msg.ctrl   = 0x00
        msg.params = bytearray([Dobot.PORT_GP2, 0x01, 0x01])
        resp = self.device._send_command(msg)
        r = struct.unpack_from('B', resp.params, 0)[0]
        g = struct.unpack_from('B', resp.params, 1)[0]
        b = struct.unpack_from('B', resp.params, 2)[0]
        return r, g, b

    def read_colour(self, samples=7):
        """
        Take multiple readings and return the most common valid colour.
        Discards r=0,g=0,b=0 readings (noise/no block).
        """
        readings = []
        for _ in range(samples):
            r, g, b = self._single_read()
            if   r == 1 and g == 0 and b == 0: readings.append('red')
            elif r == 0 and g == 0 and b == 1: readings.append('blue')
            elif r == 0 and g == 1 and b == 0: readings.append('green')
            else: print(f"[Bart] Raw: r={r} g={g} b={b} (discarded)")
            time.sleep(0.1)

        if not readings:
            print(f"[Bart] No valid colour readings ({samples} samples all blank)")
            self.last_colour = None
            return None

        colour = max(set(readings), key=readings.count)
        confidence = readings.count(colour)
        print(f"[Bart] Colour: {colour} ({confidence}/{len(readings)} valid, {samples} samples)")

        if confidence <= len(readings) // 2:
            print(f"[Bart] WARNING: low confidence ({confidence}/{len(readings)}) — rejecting")
            self.last_colour = None
            return None

        self.last_colour = colour
        return colour

    # ── Conveyor wait ───────────────────────────────────────────────────────────

    def wait_for_block(self):
        print(f"[Bart] Waiting {CONVEYOR_TRAVEL_TIME}s for block to arrive...")
        time.sleep(CONVEYOR_TRAVEL_TIME)
        print("[Bart] Block should be at pick position.")

    # ── Pick and place ──────────────────────────────────────────────────────────

    def pick_from_conveyor(self):
        print("[Bart] Picking from conveyor...")
        x, y, z, r = CONV_PICKUP
        self._safe_move(x, y, z, r)
        self.device.suck(True)
        time.sleep(0.5)
        self.device.move_to(x, y, SAFE_Z, r, wait=True)
        print("[Bart] Block picked.")

    def place_block(self):
        """
        Sort block into correct tray slot.
        Tray is 4x4 — each colour has a fixed row, columns fill left to right.
        Each subsequent block of same colour moves +30mm in X (next column).
        """
        colour = self.last_colour

        if   colour == 'red':     base = TRAY_RED
        elif colour == 'blue':    base = TRAY_BLUE
        elif colour == 'green':   base = TRAY_GREEN
        elif colour == 'unknown': base = TRAY_HUMAN
        else:
            print("[Bart] No colour stored — releasing block.")
            self.device.suck(False)
            return

        count = self.colour_counts[colour]

        if count >= 4:
            print(f"[Bart] WARNING: {colour} tray full (4 blocks already placed) — releasing.")
            self.device.suck(False)
            return

        # Each block moves one column to the left (+30 in X)
        x = base[0] + (count * COL_STEP)
        y = base[1]
        z = base[2]
        r = base[3]

        print(f"[Bart] Placing {colour} block #{count + 1} (col {count}) at ({x:.1f}, {y:.1f}, {z:.1f})...")

        self.device.move_to(x, y, SAFE_Z, r, wait=True)
        self.device.move_to(x, y, z, r, wait=True)
        self.device.suck(False)
        time.sleep(0.5)
        self.device.move_to(x, y, SAFE_Z, r, wait=True)

        self.colour_counts[colour] += 1
        print(f"[Bart] Placed. {colour} count: {self.colour_counts[colour]}/4")

    # ── Full cycle ──────────────────────────────────────────────────────────────

    def run_cycle(self, colour=None):
        self.wait_for_block()

        if colour:
            self.last_colour = colour
            print(f"[Bart] Colour provided: {colour}")
        else:
            result = self.read_colour()
            if result is None:
                print("[Bart] Skipping cycle — could not determine colour.")
                return

        self.pick_from_conveyor()
        self.place_block()
        print("[Bart] Cycle complete.\n")

    # ── Shutdown ────────────────────────────────────────────────────────────────

    def close(self):
        self.device.suck(False)
        msg = Message()
        msg.id     = 137
        msg.ctrl   = 0x03
        msg.params = bytearray([0, 1, 1])
        self.device._send_command(msg)
        self.device.close()
        print("[Bart] Closed.")


# ── Standalone runner ──────────────────────────────────────────────────────────
if __name__ == '__main__':
    bart = Bart(port='COM8')
    bart.setup()

    try:
        print("Bart is running. Place blocks on the conveyor.")
        print("Ctrl+C to stop.\n")
        while True:
            bart.run_cycle()

    except KeyboardInterrupt:
        print("\n[Bart] Stopped by user.")

    except Exception as e:
        print(f"\n[Bart] Error: {e}")
        raise

    finally:
        bart.close()
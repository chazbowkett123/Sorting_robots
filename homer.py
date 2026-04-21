import time
import threading
from pydobotplus import Dobot

CONVEYOR_INTERFACE = 1
RAIL_INTERFACE     = 0
RAIL_SPEED         = 10000
CONVEYOR_SPEED     = 12800
PULSES_PER_MM      = 80

# ── Positions ──────────────────────────────────────────────────────────────────
# BLOCK1_BASE is the Top-Right corner (Grid Index 0)
BLOCK1_BASE   = (188.0, 168.2, 36.5, 0.0)

# Movement logic: 
# 1. First column goes down in X (-30)
# 2. Next column is up in Y (+30)
ROW_STEP = -30  # X-axis change
COL_STEP = 30   # Y-axis change

COLOUR_SENSOR = (222.6, 45.6, 32.8,  0.0)
CONV          = (148.4, 99.9, 20.0, 0.0)

SAFE_Z             = 60
CONVEYOR_DISTANCE  = 900   


class Homer:
    def __init__(self, port='COM7'):
        print(f"[Homer] Connecting on {port}...")
        self.device = Dobot(port=port)
        self.device.speed(velocity=60, acceleration=60)

        # Synchronisation tools
        self.lock = threading.Lock()
        self.conveyor_thread = None
        self.stop_event = None   # Set externally to enable interruptible moves

        print("[Homer] Connected.")

    def setup(self):
        print("[Homer] Ready (homing skipped for testing).")

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
            self.device.suck(False)
        except Exception:
            pass

    def _move(self, x, y, z, r, wait=True):
        """Thread-safe move with distance polling. Raises InterruptedError on E-stop."""
        with self.lock:
            self.device.move_to(x, y, z, r, wait=False)

        if wait:
            while True:
                if self.stop_event is not None and self.stop_event.is_set():
                    raise InterruptedError("E-stop during move")
                with self.lock:
                    try:
                        pose = self.device.get_pose()
                    except Exception:
                        pose = None
                if pose is not None:
                    dist = ((pose.position.x - x) ** 2 +
                            (pose.position.y - y) ** 2 +
                            (pose.position.z - z) ** 2) ** 0.5
                    if dist < 2.5:
                        break
                time.sleep(0.05)

    def get_grid_position(self, index):
        """
        Calculates coordinates for the 4x4 grid.
        Pattern: Fills Column 0 (Rows 0-3), then jumps to Column 1 (Rows 0-3).
        """
        col = index // 4  # Changes every 4 blocks
        row = index % 4   # Cycles 0, 1, 2, 3

        # Apply the logic: Down in X, Next column Up in Y
        x = BLOCK1_BASE[0] + (row * ROW_STEP)
        y = BLOCK1_BASE[1] + (col * COL_STEP)
        z = BLOCK1_BASE[2]
        r = BLOCK1_BASE[3]
        
        return (x, y, z, r)

    def pick_block(self, index):
        """Calculates grid pos from index and performs the pick."""
        x, y, z, r = self.get_grid_position(index)
        
        print(f"[Homer] Picking Grid {index+1} (Row:{index%4}, Col:{index//4}) at ({x:.1f}, {y:.1f})")
        
        with self.lock:
            pose = self.device.get_pose()
            
        if pose and pose.position.z < SAFE_Z:
            self._move(pose.position.x, pose.position.y, SAFE_Z, pose.position.r, wait=True)

        self._move(x, y, SAFE_Z, r, wait=True)
        self._move(x, y, z, r, wait=True)

        with self.lock:
            self.device.suck(True)

        time.sleep(0.5)
        self._move(x, y, SAFE_Z, r, wait=True)

    def hold_over_colour_sensor(self, at_sensor_event=None):
        print("[Homer] Moving to colour sensor...")
        self._move(COLOUR_SENSOR[0], COLOUR_SENSOR[1], SAFE_Z, COLOUR_SENSOR[3], wait=True)
        self._move(*COLOUR_SENSOR, wait=True)
        # Signal that Homer is stable at the sensor — safe to scan now
        if at_sensor_event is not None:
            at_sensor_event.set()
        self._sleep(1.0)
        if at_sensor_event is not None:
            at_sensor_event.clear()
        self._move(COLOUR_SENSOR[0], COLOUR_SENSOR[1], SAFE_Z, COLOUR_SENSOR[3], wait=True)

    def place_on_conveyor(self):
        self.wait_for_conveyor()
        
        print("[Homer] Placing on conveyor...")
        self._move(CONV[0], CONV[1], SAFE_Z, CONV[3], wait=True)
        self._move(*CONV, wait=True)

        with self.lock:
            self.device.suck(False)

        time.sleep(0.3)
        self._move(CONV[0], CONV[1], SAFE_Z, CONV[3], wait=True)
        
        self._move_conveyor_async()

    def _move_conveyor_async(self):
        pulses = CONVEYOR_DISTANCE * PULSES_PER_MM
        duration = pulses / CONVEYOR_SPEED

        def conveyor_control():
            with self.lock:
                self.device._set_stepper_motor(speed=-CONVEYOR_SPEED, interface=CONVEYOR_INTERFACE)
            time.sleep(duration)
            with self.lock:
                self.device._set_stepper_motor(speed=0, interface=CONVEYOR_INTERFACE)

        self.conveyor_thread = threading.Thread(target=conveyor_control, daemon=True)
        self.conveyor_thread.start()

    def wait_for_conveyor(self):
        if self.conveyor_thread and self.conveyor_thread.is_alive():
            self.conveyor_thread.join()

    def close(self):
        self.wait_for_conveyor()
        with self.lock:
            self.device.suck(False)
            self.device._set_stepper_motor(speed=0, interface=CONVEYOR_INTERFACE)
            self.device.close()
        print("[Homer] Closed.")

if __name__ == '__main__':
    homer = Homer(port='COM7')
    homer.setup()
    try:
        # Test the first 4 blocks (the first column)
        for i in range(4):
            homer.pick_block(i)
            homer.hold_over_colour_sensor()
            homer.place_on_conveyor()
    finally:
        homer.close()
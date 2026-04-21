import time
from pydobotplus import Dobot

# ── Run this ONCE to find which direction is toward home ───────────────────────
# Watch the rail — if it moves away from home, change TOWARD_HOME_SPEED to negative

PORT            = 'COM6'
RAIL_INTERFACE  = 1
TOWARD_HOME_SPEED = 5000   # if rail moves wrong way, change to -5000
PULSES_PER_MM   = 80
MAX_TRAVEL_MM   = 1065
DURATION = (MAX_TRAVEL_MM * PULSES_PER_MM / abs(TOWARD_HOME_SPEED)) + 5.0

print(f"Connecting on {PORT}...")
device = Dobot(port=PORT)
device.clear_alarms()
print("Connected.\n")

print(f"Running rail toward home for {DURATION:.1f}s...")
print("Watch the rail — if it moves AWAY from home press Ctrl+C immediately")
print("then change TOWARD_HOME_SPEED = -5000 at the top of this file.\n")

input("Press Enter when ready...")

try:
    device._set_stepper_motor(speed=TOWARD_HOME_SPEED, interface=RAIL_INTERFACE)
    time.sleep(DURATION)
    device._set_stepper_motor(speed=0, interface=RAIL_INTERFACE)
    time.sleep(1.0)
    print("\nRail should now be at home position.")
    print("If it reached home correctly, update home.py with:")
    print(f"  TOWARD_HOME_SPEED = {TOWARD_HOME_SPEED}")

except KeyboardInterrupt:
    print("\nStopped early.")
    device._set_stepper_motor(speed=0, interface=RAIL_INTERFACE)
    print(f"Rail was moving in wrong direction.")
    print(f"Change TOWARD_HOME_SPEED to {-TOWARD_HOME_SPEED} and try again.")

finally:
    device._set_stepper_motor(speed=0, interface=RAIL_INTERFACE)
    device.close()
    print("Done.")
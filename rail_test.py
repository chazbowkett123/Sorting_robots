import time
from pydobotplus import Dobot

# ── Settings ───────────────────────────────────────────────────────────────────
PORT           = 'COM6'
RAIL_INTERFACE = 1
RAIL_SPEED     = 5000
PULSES_PER_MM  = 80
MAX_TRAVEL_MM  = 1065

MAX_PULSES     = MAX_TRAVEL_MM * PULSES_PER_MM
current_pulses = 0

print(f"Connecting on {PORT}...")
device = Dobot(port=PORT)
device.clear_alarms()
time.sleep(0.5)   # let the device settle after alarm clear
print("Connected.\n")
print(f"Rail speed: {RAIL_SPEED} | Max travel: {MAX_TRAVEL_MM}mm")
print("Commands: go <mm>, h=home, fh=force home (no blocks), q=quit")

def force_home():
    """Forces the motor to move backward regardless of current software position."""
    global current_pulses
    print("!!! Force Homing: Moving backward for 5 seconds to reset axis...")
    
    # Positive speed moves backward toward the motor/limit switch
    device._set_stepper_motor(speed=RAIL_SPEED, interface=RAIL_INTERFACE)
    time.sleep(5) 
    device._set_stepper_motor(speed=0, interface=RAIL_INTERFACE)
    
    # RESET internal tracker to 0
    current_pulses = 0
    print("Internal position reset to 0mm.")

def move_to_mm(target_mm, force=False):
    global current_pulses

    # Software Clamp (Only used if not forcing)
    if not force:
        target_mm = max(0, min(target_mm, MAX_TRAVEL_MM))
    
    target_pulses = int(target_mm * PULSES_PER_MM)
    delta_pulses = target_pulses - current_pulses

    # The "Soft Block" check
    if not force and abs(delta_pulses) < 10:
        print(f"   Already at {target_mm:.1f}mm.")
        return

    # Forward = negative speed, backward = positive speed
    speed = -RAIL_SPEED if delta_pulses > 0 else RAIL_SPEED
    duration = abs(delta_pulses) / RAIL_SPEED

    direction = "forward" if delta_pulses > 0 else "backward"
    print(f"   Moving {direction} to {target_mm:.1f}mm ({duration:.2f}s)...")

    device._set_stepper_motor(speed=speed, interface=RAIL_INTERFACE)
    time.sleep(duration)
    device._set_stepper_motor(speed=0, interface=RAIL_INTERFACE)

    current_pulses = target_pulses
    print(f"   Now at software {current_pulses / PULSES_PER_MM:.1f}mm.")

try:
    while True:
        pos_mm = current_pulses / PULSES_PER_MM
        cmd = input(f"[{pos_mm:.1f}mm] Command: ").strip().lower()

        if cmd == 'q':
            break

        elif cmd == 'fh':
            force_home()

        elif cmd == 'h':
            move_to_mm(0)

        elif cmd.startswith('go '):
            try:
                target = float(cmd[3:])
                move_to_mm(target)
            except ValueError:
                print("   Usage: go <mm>")

except KeyboardInterrupt:
    print("\nStopped.")
finally:
    device._set_stepper_motor(speed=0, interface=RAIL_INTERFACE)
    device.close()
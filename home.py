from pydobotplus import Dobot

# ── Coordinates ────────────────────────────────────────────────────────────────
HOMER_SAFE_POSITION = (166.7, 83.7, 65.0, 0)
BART_SAFE_POSITION  = (154.6, 82.8, 51.3, 0)
MARGE_SAFE_POSITION = (0, -180, 50, 0)  # Update these as needed

def home_and_park(arm, name, target_coords):
    """Utility to home an arm and immediately move it to a safe spot."""
    print(f"--- Processing {name} ---")
    arm.clear_alarms()
    
    print(f"Homing {name}...")
    arm.home()
    arm.clear_alarms()
    
    # After homing, move to safe position
    print(f"Moving {name} to safe position: {target_coords}")
    arm.move_to(*target_coords, wait=True)
    print(f"{name} is secure.\n")

# ── Main Execution ─────────────────────────────────────────────────────────────
print("Connecting to Homer, Bart, and Marge...")
homer = Dobot(port='COM7')
bart  = Dobot(port='COM8')
marge = Dobot(port='COM6')
print("Connections established.\n")

try:
    # 1. Home and Park Homer and Bart first to clear the way
    home_and_park(homer, "Homer", HOMER_SAFE_POSITION)
    home_and_park(bart, "Bart", BART_SAFE_POSITION)
    
    # 2. Now that the workspace is clear, home and park Marge
    home_and_park(marge, "Marge", MARGE_SAFE_POSITION)
    
    print("All arms are homed and in safe positions.")

except Exception as e:
    print(f"An error occurred during the sequence: {e}")

finally:
    # Always close connections to release COM ports
    homer.close()
    bart.close()
    marge.close()
    print("Connections closed.")
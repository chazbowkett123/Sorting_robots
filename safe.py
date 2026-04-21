import time
from pydobotplus import Dobot

# ── Positions ──────────────────────────────────────────────────────────────────
HOMER_SAFE_POSITION = (166.7, 83.7, 65.0, 0)
BART_SAFE_POSITION  = (154.6, 82.8, 51.3, 0)
MARGE_SAFE_POSITION = (0.0, -180.0, 54.8, 0) 
RETRACT_HEIGHT      = 0 

def move_to_safe(arm, name, target_coords, wait=True):
    """
    Retracts the arm vertically first, then moves to the target parking spot.
    """
    print(f"Securing {name}...")
    arm.clear_alarms()
    
    # Get current position to calculate the vertical lift
    pose = arm.get_pose()
    curr_x = pose.position.x
    curr_y = pose.position.y
    curr_z = pose.position.z
    curr_r = pose.position.r
    
    # 1. Lift Z first (Retraction)
    print(f"  {name} lifting to Z: {curr_z + RETRACT_HEIGHT:.1f}...")
    arm.move_to(curr_x, curr_y, curr_z + RETRACT_HEIGHT, curr_r, wait=wait)
    
    # 2. Move to final parking position
    print(f"  Moving {name} to safe position: {target_coords}")
    arm.move_to(*target_coords, wait=wait)

# ── Main Execution ─────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("Connecting to arms...")
    marge = Dobot(port='COM6')
    homer = Dobot(port='COM7')
    bart  = Dobot(port='COM8')
    print("Connections established.\n")

    try:
        # STEP 1: Process Homer and Bart simultaneously
        # Using wait=False allows the code to trigger both arms at once
        move_to_safe(homer, "Homer", HOMER_SAFE_POSITION, wait=False)
        move_to_safe(bart, "Bart", BART_SAFE_POSITION, wait=False)
        
        # Pause while the arms move in the background
        print("Waiting for Homer and Bart to clear the workspace...")
        time.sleep(8) 
        
        # STEP 2: Process Marge sequentially
        move_to_safe(marge, "Marge", MARGE_SAFE_POSITION, wait=True)
        
        print("\nSuccess: All arms (Marge, Homer, Bart) are parked.")

    except Exception as e:
        print(f"\nAn error occurred: {e}")

    finally:
        # Ensure connections are closed to release COM ports
        marge.close()
        homer.close()
        bart.close()
        print("Connections closed.")
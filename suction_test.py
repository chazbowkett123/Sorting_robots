import time
from pydobotplus import Dobot

print("Connecting to arm 1 (COM7)...")
arm1 = Dobot(port='COM7')
print("Arm 1 connected.")

print("Connecting to arm 2 (COM8)...")
arm2 = Dobot(port='COM8')
print("Arm 2 connected.")

try:
    print("\n[Arm 1] Suction ON...")
    arm1.suck(True)
    
    print("[Arm 2] Suction ON...")
    arm2.suck(True)

    print("\nSuction is currently ON for both arms.")
    print("Press Ctrl+C to stop suction and exit the script.")

    # This loop keeps the script running indefinitely
    while True:
        time.sleep(0.1)

except KeyboardInterrupt:
    print("\nStopped by user.")

except Exception as e:
    print(f"\nError: {e}")

finally:
    # This ensures the suction turns off cleanly when you exit
    print("Turning suction OFF and disconnecting...")
    arm1.suck(False)
    arm2.suck(False)
    arm1.close()
    arm2.close()
    print("Done.")
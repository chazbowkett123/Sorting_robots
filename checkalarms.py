import time
from pydobotplus import Dobot

# Connect
print("Connecting to arm 1 (COM7)...")
arm1 = Dobot(port='COM7')
print("Arm 1 connected.")

print("Connecting to arm 2 (COM8)...")
arm2 = Dobot(port='COM8')
print("Arm 2 connected.")

# Check alarms before doing anything
print("\n--- Alarms BEFORE homing ---")
print("Arm 1 alarms:", arm1.get_alarms())
print("Arm 2 alarms:", arm2.get_alarms())

# Clear alarms
arm1.clear_alarms()
arm2.clear_alarms()
print("\nAlarms cleared.")

# Lift 75mm before homing
print("\nLifting arm 1 up 75mm...")
pose = arm1.get_pose()
arm1.move_to(pose.position.x, pose.position.y, pose.position.z + 75, pose.position.r, wait=True)

print("Lifting arm 2 up 75mm...")
pose = arm2.get_pose()
arm2.move_to(pose.position.x, pose.position.y, pose.position.z + 75, pose.position.r, wait=True)

# Check alarms after lift
print("\n--- Alarms AFTER lift ---")
print("Arm 1 alarms:", arm1.get_alarms())
print("Arm 2 alarms:", arm2.get_alarms())

# Home arm 1
print("\nHoming arm 1...")
arm1.home()
time.sleep(1)
print("--- Alarms AFTER homing arm 1 ---")
print("Arm 1 alarms:", arm1.get_alarms())
arm1.clear_alarms()

# Home arm 2
print("\nHoming arm 2...")
arm2.home()
time.sleep(1)
print("--- Alarms AFTER homing arm 2 ---")
print("Arm 2 alarms:", arm2.get_alarms())
arm2.clear_alarms()

print("\nDone.")
arm1.close()
arm2.close()
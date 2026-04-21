from pydobotplus import Dobot

print("Connecting...")
device = Dobot(port='COM8')
print("Connected.\n")

pose = device.get_pose()
x = pose.position.x
y = pose.position.y
z = pose.position.z
r = pose.position.r

print("Current arm position:")
print(f"  X = {x:.2f}")
print(f"  Y = {y:.2f}")
print(f"  Z = {z:.2f}")
print(f"  R = {r:.2f}")
print(f"\nCopy this into your code:")
print(f"  ({x:.1f}, {y:.1f}, {z:.1f}, {r:.1f})")

device.close()
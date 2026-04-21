import time
import struct
from pydobotplus import Dobot
from pydobotplus.message import Message

print("Connecting to Bart (COM8)...")
device = Dobot(port='COM8')
print("Connected.\n")

def set_ir_with_version(enable, port, version):
    msg = Message()
    msg.id     = 138
    msg.ctrl   = 0x02
    msg.params = bytearray([int(enable), port, version])
    device._send_command(msg)

def get_ir(port):
    msg = Message()
    msg.id     = 138
    msg.ctrl   = 0x00
    msg.params = bytearray([port])
    resp = device._send_command(msg)
    return struct.unpack_from('B', resp.params, 0)[0]

# Enable all combinations
set_ir_with_version(True, Dobot.PORT_GP4, 0)   # GP4 V1 sensor
set_ir_with_version(True, Dobot.PORT_GP4, 1)   # GP4 V2 sensor
set_ir_with_version(True, Dobot.PORT_GP5, 0)   # GP5 V1 sensor
set_ir_with_version(True, Dobot.PORT_GP5, 1)   # GP5 V2 sensor
time.sleep(0.5)

print("Wave a block in front of the sensor. Ctrl+C to stop.")
print("Watching ALL ports and versions for ANY change.\n")
print(f"{'Time':>8} | {'GP4':>4} | {'GP5':>4} | Changes")
print("-" * 45)

prev = None

try:
    while True:
        gp4 = get_ir(Dobot.PORT_GP4)
        gp5 = get_ir(Dobot.PORT_GP5)

        row = (gp4, gp5)
        ts = f"{time.time() % 1000:.1f}"
        changed = ""

        if prev is not None:
            if gp4 != prev[0]: changed += f" GP4:{prev[0]}→{gp4}"
            if gp5 != prev[1]: changed += f" GP5:{prev[1]}→{gp5}"
            if changed: changed = " *** CHANGED ***" + changed

        prev = row
        print(f"{ts:>8} | {gp4:>4} | {gp5:>4} |{changed}")
        time.sleep(0.15)

except KeyboardInterrupt:
    print("\nStopped.")

finally:
    set_ir_with_version(False, Dobot.PORT_GP4, 0)
    set_ir_with_version(False, Dobot.PORT_GP5, 0)
    device.close()
    print("Done.")
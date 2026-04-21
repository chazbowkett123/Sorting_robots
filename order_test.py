import threading
from marge import Marge

# ── Fake Bart counts — simulates blocks already sorted into tray ───────────────
# Adjust these to match how many blocks you've actually placed in the tray
fake_bart_counts = {
    'red':   4,
    'blue':  4,
    'green': 4,
}

tray_lock = threading.Lock()
marge = Marge(port='COM6')
marge.setup(tray_lock=tray_lock)

print("\nOrder Test — Marge will pick blocks from tray and deliver to box.")
print("Type an order like:  red 2 blue 1")
print("Type 'q' to quit.\n")

try:
    while True:
        raw = input("Enter order: ").strip().lower()

        if raw == 'q':
            break

        # Parse input — e.g. "red 2 blue 1" → {'red': 2, 'blue': 1}
        parts = raw.split()
        order = {}
        i = 0
        while i < len(parts) - 1:
            colour = parts[i]
            try:
                qty = int(parts[i + 1])
                if colour in ('red', 'blue', 'green'):
                    order[colour] = qty
                    i += 2
                else:
                    print(f"  Unknown colour '{colour}' — use red, blue or green.")
                    i += 1
            except ValueError:
                print(f"  Expected a number after '{colour}'.")
                i += 1

        if not order:
            print("  Could not parse order. Try: red 2 blue 1")
            continue

        print(f"\n  Order parsed: {order}")
        confirm = input("  Confirm? (y/n): ").strip().lower()
        if confirm == 'y':
            marge.fulfil_order(order, fake_bart_counts)
        else:
            print("  Order cancelled.")

except KeyboardInterrupt:
    print("\nStopped.")

finally:
    marge.close()
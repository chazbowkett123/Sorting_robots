import time
import threading
from homer import Homer
from bart import Bart
from marge import Marge

# ── Connect all three arms ──────────────────────────────────────────────────────
homer = Homer(port='COM7')
bart  = Bart(port='COM8')
marge = Marge(port='COM6')

# ── Shared synchronisation ──────────────────────────────────────────────────────
#
#   tray_lock      — Mutex: Bart (place) and Marge (pick) must never touch the
#                    trays at the same time. Both acquire this before any tray move.
#
#   ok_to_sort     — Normally SET (green light). Marge CLEARS it when she starts
#                    an order; Homer and Bart pause at safe points until it is
#                    SET again. This drains the pipeline cleanly:
#                      • Homer won't pick the next grid block.
#                      • Bart won't start the belt-travel timer for the next block.
#                    Any block already on the belt finishes its sort via tray_lock.
#
#   homer_at_sensor    — Set by Homer's hold_over_colour_sensor() once the arm is
#                        fully lowered and stable. Cleared when Homer lifts away.
#   bart_ready_to_scan — Set by Bart after each sort. Homer waits for it before
#                        picking the next block (prevents Homer out-running Bart).
#   block_on_belt      — Set by Homer after placing on conveyor. Bart waits for it
#                        before starting the belt-travel countdown.
#   stop_signal        — Set on 'q' or Ctrl+C. All loops check this to exit.

tray_lock          = threading.Lock()
ok_to_sort         = threading.Event()
homer_at_sensor    = threading.Event()
bart_ready_to_scan = threading.Event()
block_on_belt      = threading.Event()
stop_signal        = threading.Event()

ok_to_sort.set()          # Sorting is allowed at startup
bart_ready_to_scan.set()  # Bart is ready at startup

# ── Setup ───────────────────────────────────────────────────────────────────────
homer.setup()
bart.setup()
marge.setup(tray_lock=tray_lock)


# ── Homer thread ────────────────────────────────────────────────────────────────
def run_homer():
    for block_index in range(16):
        if stop_signal.is_set():
            break

        print(f"\n[Homer] === Block {block_index + 1}/16 ===")

        # Gate — do NOT pick the next block until:
        #   1. Bart has finished placing the previous block (bart_ready_to_scan)
        #   2. Marge is not currently fulfilling an order (ok_to_sort)
        # Order matters: check ok_to_sort first so a Marge pause takes effect
        # immediately, then wait for Bart so we never out-run the sorter.
        ok_to_sort.wait()
        bart_ready_to_scan.wait()
        if stop_signal.is_set():
            break
        bart_ready_to_scan.clear()

        homer.pick_block(block_index)

        print("[Homer] Presenting to colour sensor...")
        homer.hold_over_colour_sensor(at_sensor_event=homer_at_sensor)

        homer.place_on_conveyor()
        block_on_belt.set()

    print("\n[Homer] All 16 grid blocks processed.")


# ── Bart thread ─────────────────────────────────────────────────────────────────
def run_bart():
    processed_count = 0

    while processed_count < 16 and not stop_signal.is_set():

        # Wait for Homer to lower the block over the sensor
        homer_at_sensor.wait()

        detected_color = None
        print("[Bart] Scanning colour...")
        while homer_at_sensor.is_set() and not stop_signal.is_set():
            color = bart.read_colour()
            if color:
                detected_color = color
                break
            time.sleep(0.1)

        bart.last_colour = detected_color or 'unknown'
        if detected_color:
            print(f"[Bart] Confirmed: {detected_color.upper()}")
        else:
            print("[Bart] No colour detected — routing to human bin.")

        # Wait for the block to be placed on the belt
        block_on_belt.wait()
        block_on_belt.clear()

        # If Marge is fulfilling an order, pause here — don't start the belt
        # travel timer until the trays are free again. Any block already on the
        # belt at the moment ok_to_sort was cleared will finish this wait and
        # then acquire tray_lock below to safely place without colliding with Marge.
        ok_to_sort.wait()
        if stop_signal.is_set():
            break

        print("[Bart] Waiting 5s for block to arrive...")
        time.sleep(5.0)

        bart.pick_from_conveyor()

        # Acquire tray_lock so Bart and Marge never access a tray simultaneously.
        # go_safe() is called INSIDE the lock — the lock is not released until
        # Bart's arm has fully moved clear of the tray area, so Marge can never
        # enter while Bart is still above it.
        with tray_lock:
            bart.place_block()
            bart.go_safe()

        processed_count += 1
        print(f"[Bart] {processed_count}/16 blocks sorted.\n")
        bart_ready_to_scan.set()

    print("\n[Bart] All blocks sorted.")


# ── Marge thread ─────────────────────────────────────────────────────────────────
def run_marge():
    print("[Marge] Standing by for orders...")
    while not stop_signal.is_set():
        order = None
        with marge.order_lock:
            if marge.order_queue:
                order = marge.order_queue.pop(0)

        if order:
            print(f"\n[Marge] *** Order received: {order}")
            print("[Marge] *** Pausing Homer & Bart...")

            # Clear the green light — Homer & Bart pause at their next safe point
            ok_to_sort.clear()

            # Brief settle: give Bart time to finish any tray placement that was
            # already in progress before the pause takes effect. tray_lock below
            # also protects against any race, but the sleep keeps prints clean.
            time.sleep(0.5)

            marge.fulfil_order(order, bart.colour_counts)

            # Restore the green light — Homer & Bart resume
            ok_to_sort.set()
            print("[Marge] *** Order fulfilled — Homer & Bart resumed ***\n")
        else:
            time.sleep(0.3)


# ── Order parsing helper ─────────────────────────────────────────────────────────
def parse_order(raw):
    """'red 2 blue 1'  →  {'red': 2, 'blue': 1}"""
    parts = raw.split()
    order = {}
    i = 0
    while i < len(parts) - 1:
        colour = parts[i]
        try:
            qty = int(parts[i + 1])
            if colour in ('red', 'blue', 'green'):
                order[colour] = qty
            else:
                print(f"  Unknown colour '{colour}' — use red, blue, green.")
            i += 2
        except ValueError:
            print(f"  Expected a number after '{colour}'.")
            i += 1
    return order


# ── Main ─────────────────────────────────────────────────────────────────────────
def unblock_all():
    """Unblock every waiting thread so they can see stop_signal and exit."""
    stop_signal.set()
    ok_to_sort.set()
    bart_ready_to_scan.set()
    homer_at_sensor.set()
    block_on_belt.set()


try:
    print("\n" + "=" * 52)
    print("  FULL SYSTEM  —  Homer  +  Bart  +  Marge")
    print("=" * 52)
    print("Homer & Bart will sort 16 blocks automatically.")
    print("Type an order at any time, e.g.:  red 2 blue 1")
    print("Type 'q' to stop the system.\n")

    t_homer = threading.Thread(target=run_homer, daemon=True, name="Homer")
    t_bart  = threading.Thread(target=run_bart,  daemon=True, name="Bart")
    t_marge = threading.Thread(target=run_marge, daemon=True, name="Marge")

    t_homer.start()
    t_bart.start()
    t_marge.start()

    # Main thread — order input loop
    # Runs independently of the robot threads so you can place orders at any time.
    while not stop_signal.is_set():

        # Non-blocking check: print a notice once sorting is done
        if not t_homer.is_alive() and not t_bart.is_alive():
            print("\n[System] Sorting complete — all 16 blocks are in the trays.")
            print("[System] You can still place orders. Type 'q' to shut down.\n")
            # Drop into a simpler loop — no need to keep checking thread state
            while not stop_signal.is_set():
                try:
                    raw = input("Order: ").strip().lower()
                except EOFError:
                    unblock_all()
                    break
                if raw == 'q':
                    unblock_all()
                    break
                if not raw:
                    continue
                order = parse_order(raw)
                if order:
                    confirm = input(f"  Confirm {order}? (y/n): ").strip().lower()
                    if confirm == 'y':
                        marge.add_order(order)
                        print("  Order queued.")
                    else:
                        print("  Cancelled.")
                else:
                    print("  Could not parse. Try:  red 2  blue 1")
            break

        try:
            raw = input("Order: ").strip().lower()
        except EOFError:
            unblock_all()
            break

        if raw == 'q':
            unblock_all()
            break

        if not raw:
            continue

        order = parse_order(raw)
        if order:
            confirm = input(f"  Confirm {order}? (y/n): ").strip().lower()
            if confirm == 'y':
                marge.add_order(order)
                print("  Order queued.")
            else:
                print("  Cancelled.")
        else:
            print("  Could not parse. Try:  red 2  blue 1")

    # Give threads a moment to exit gracefully before closing serial ports
    t_homer.join(timeout=10)
    t_bart.join(timeout=10)

except KeyboardInterrupt:
    print("\n[System] Emergency stop (Ctrl+C).")
    unblock_all()
    t_homer.join(timeout=5)
    t_bart.join(timeout=5)

finally:
    homer.close()
    bart.close()
    marge.close()

    # ── Final summary ──────────────────────────────────────────────────────────
    print("\n" + "=" * 40)
    print("  FINAL SUMMARY")
    print("=" * 40)
    sorted_total = 0
    for colour, count in bart.colour_counts.items():
        if count > 0:
            print(f"  {colour.upper():10} : {count} block{'s' if count != 1 else ''} sorted")
            sorted_total += count
    print("-" * 40)
    print(f"  {'SORTED':10} : {sorted_total}")
    dispatched = sum(marge.slots_taken.values())
    print(f"  {'DISPATCHED':10} : {dispatched}")
    print("=" * 40)
    print("System shut down.")

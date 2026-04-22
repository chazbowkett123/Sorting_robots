"""
camera.py — Hikvision MV-CE050-30UC dispatch verification camera
================================================================
Uses the standard GenTL interface via the 'harvesters' library.
This avoids all the permission issues of the raw Hikvision SDK wrapper
and works without Administrator rights.

Install dependencies:
    pip install harvesters genicam opencv-python pillow

The Hikvision MVS SDK must still be installed (it provides the .cti
GenTL producer file that harvesters loads).

Colour detection:  OpenCV HSV thresholding on an ROI crop.
Calibration:       HSV hue ranges saved to camera_cal.json.
"""

import os
import json
import time

import numpy as np

# ── GenTL producer path (Hikvision MVS Runtime) ───────────────────────────────
_MVS_RUNTIME   = r"C:\Program Files (x86)\Common Files\MVS\Runtime\Win64_x64"
_CTI_PATH      = os.path.join(_MVS_RUNTIME, "MvProducerU3V.cti")   # USB3 Vision
_CTI_PATH_GEV  = os.path.join(_MVS_RUNTIME, "MvProducerGEV.cti")   # GigE (backup)

# ── Check what's available ────────────────────────────────────────────────────
_HARVESTERS_AVAILABLE = False
_CV2_AVAILABLE        = False

try:
    from harvesters.core import Harvester
    _HARVESTERS_AVAILABLE = True
except ImportError:
    pass

try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:
    pass

_SDK_AVAILABLE = _HARVESTERS_AVAILABLE and os.path.isfile(_CTI_PATH)

# ── Default HSV hue ranges ────────────────────────────────────────────────────
# OpenCV HSV: H=0-179, S=0-255, V=0-255
# Format per colour: [[h_low, h_high], s_min, v_min]
DEFAULT_HSV_CAL = {
    "red":   [[-10, 10], 80, 60],   # red wraps around 0/180
    "green": [[40,  85], 80, 60],
    "blue":  [[100,135], 80, 60],
}

# ROI = (y_start, x_start, height, width) in pixels
# Adjust after physically positioning the camera above the dispatch box
DEFAULT_ROI = (200, 280, 100, 120)

_CAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camera_cal.json")


# ── Windows USB soft-reset helper ────────────────────────────────────────────
def _reenumerate_hikvision_usb() -> bool:
    """
    Ask Windows to soft-reset the Hikvision USB camera device by calling
    CM_Reenumerate_DevNode (CfgMgr32).  This is equivalent to 'Scan for
    hardware changes' in Device Manager and does NOT require admin rights.

    Returns True if the reenumeration request was submitted successfully.
    The caller should wait ~3 s for the device to come back before retrying.
    """
    try:
        import ctypes
        cfgmgr32 = ctypes.WinDLL("CfgMgr32.dll")

        # Build the device-ID filter string for CM_Get_Device_ID_List
        # The Hikvision USB3 Vision cameras all have VID 2BC5.
        _HIKVISION_FILTER = "USB\\VID_2BC5"

        # Step 1 — ask for the required buffer size
        size = ctypes.c_ulong(0)
        CM_GETIDLIST_FILTER_ENUMERATOR = 0x00000002
        cfgmgr32.CM_Get_Device_ID_List_SizeW(
            ctypes.byref(size),
            _HIKVISION_FILTER,
            CM_GETIDLIST_FILTER_ENUMERATOR,
        )
        if size.value < 2:
            print("[Camera] USB reset: no Hikvision device found in PnP list.")
            return False

        # Step 2 — retrieve the (double-null-terminated) list
        buf = ctypes.create_unicode_buffer(size.value)
        ret = cfgmgr32.CM_Get_Device_ID_ListW(
            _HIKVISION_FILTER,
            buf,
            size,
            CM_GETIDLIST_FILTER_ENUMERATOR,
        )
        if ret != 0:   # CR_SUCCESS = 0
            return False

        # Parse: null-separated, terminated by \0\0
        dev_id = None
        raw = buf.raw.decode("utf-16-le", errors="replace")
        for entry in raw.split("\x00"):
            if entry and "VID_2BC5" in entry.upper():
                dev_id = entry
                break

        if not dev_id:
            return False

        # Step 3 — locate the device node
        dev_inst = ctypes.c_uint32()
        CM_LOCATE_DEVNODE_NORMAL = 0x00000000
        ret = cfgmgr32.CM_Locate_DevNodeW(
            ctypes.byref(dev_inst),
            dev_id,
            CM_LOCATE_DEVNODE_NORMAL,
        )
        if ret != 0:
            return False

        # Step 4 — request reenumeration (soft-reset)
        CM_REENUMERATE_NORMAL = 0x00000000
        ret = cfgmgr32.CM_Reenumerate_DevNode(
            dev_inst,
            CM_REENUMERATE_NORMAL,
        )
        ok = (ret == 0)
        print(f"[Camera] USB soft-reset {'submitted' if ok else 'failed (ret=' + str(ret) + ')'}  — waiting for re-enumeration…")
        return ok

    except Exception as exc:
        print(f"[Camera] USB soft-reset skipped: {exc}")
        return False


# ═════════════════════════════════════════════════════════════════════════════
class DispatchCamera:
    """
    Colour-verification camera using the standard GenTL interface.
    No administrator rights required.
    """

    def __init__(self):
        self._harvester  = None
        self._ia         = None        # image acquirer
        self._connected  = False
        self._frame_w    = 0
        self._frame_h    = 0

        self.hsv_cal = {k: list(v) for k, v in DEFAULT_HSV_CAL.items()}
        self.roi     = DEFAULT_ROI
        self._load_cal()

    # ── Calibration persistence ───────────────────────────────────────────────

    def _load_cal(self):
        if os.path.isfile(_CAL_FILE):
            try:
                with open(_CAL_FILE) as fh:
                    data = json.load(fh)
                self.hsv_cal = data.get("hsv_cal", self.hsv_cal)
                self.roi     = tuple(data.get("roi", list(self.roi)))
            except Exception:
                pass

    def save_cal(self):
        try:
            with open(_CAL_FILE, "w") as fh:
                json.dump({"hsv_cal": self.hsv_cal,
                           "roi":     list(self.roi)}, fh, indent=2)
        except Exception as exc:
            print(f"[Camera] WARNING: could not save calibration: {exc}")

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def sdk_available(self) -> bool:
        return _SDK_AVAILABLE

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def backend(self) -> str:
        return "harvesters" if self._connected else "none"

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self, index: int = 0) -> bool:
        """
        Connect to the Hikvision camera via the GenTL interface.
        Does NOT require Administrator rights.
        Returns True on success.
        """
        if self._connected:
            return True

        if not _HARVESTERS_AVAILABLE:
            print("[Camera] ERROR: harvesters not installed.")
            print("         Run:  pip install harvesters genicam")
            return False

        # Try USB3 Vision producer first, then GigE
        cti_files = []
        if os.path.isfile(_CTI_PATH):
            cti_files.append(_CTI_PATH)
        if os.path.isfile(_CTI_PATH_GEV):
            cti_files.append(_CTI_PATH_GEV)

        if not cti_files:
            print("[Camera] ERROR: Hikvision GenTL producer (.cti) not found.")
            print(f"         Expected at: {_CTI_PATH}")
            print("         Make sure the MVS SDK is installed.")
            return False

        h = Harvester()
        for cti in cti_files:
            h.add_file(cti)
            print(f"[Camera] Loaded GenTL producer: {os.path.basename(cti)}")

        try:
            h.update()
        except Exception as exc:
            print(f"[Camera] Device enumeration failed: {exc}")
            h.reset()
            return False

        count = len(h.device_info_list)
        print(f"[Camera] Found {count} camera(s).")
        if count == 0:
            print("[Camera] No cameras found — check USB cable.")
            h.reset()
            return False
        if index >= count:
            print(f"[Camera] Index {index} out of range (found {count}).")
            h.reset()
            return False

        # h.create() reads the camera's GenICam XML over USB.  If the camera's
        # USB interface is in a bad state (e.g. from a prior failed connection)
        # the XML read returns garbage, producing a UnicodeDecodeError.
        # Strategy:
        #   1. Try h.create() normally.
        #   2. On failure: fully release the Harvester, trigger a Windows USB
        #      soft-reset (CM_Reenumerate_DevNode — no admin required), wait for
        #      the device to re-enumerate, then retry from scratch.
        #   3. After 3 failures print clear instructions.
        ia      = None
        _max_attempts = 3
        for _attempt in range(1, _max_attempts + 1):
            try:
                ia = h.create(index)
                break   # success — leave loop
            except (UnicodeDecodeError, Exception) as exc:
                err_is_unicode = isinstance(exc, UnicodeDecodeError)
                tag = "XML encoding error" if err_is_unicode else "acquirer error"
                print(f"[Camera] Attempt {_attempt}/{_max_attempts} — {tag}: {exc}")
                if _attempt < _max_attempts:
                    # Release the Harvester fully before retrying
                    try:
                        h.reset()
                    except Exception:
                        pass
                    # Attempt a Windows USB soft-reset so the camera re-enumerates
                    if err_is_unicode:
                        _reenumerate_hikvision_usb()
                    time.sleep(3)
                    # Rebuild the Harvester for the next attempt
                    h = Harvester()
                    for cti in cti_files:
                        h.add_file(cti)
                    try:
                        h.update()
                    except Exception:
                        pass

        if ia is None:
            print("[Camera] All connection attempts failed.")
            print("         The camera USB interface is stuck.  Fix:")
            print("         1. Unplug the USB cable, wait 3 s, then plug it back in.")
            print("         2. Close any MVS Viewer window and try again.")
            try:
                h.reset()
            except Exception:
                pass
            return False

        try:
            ia.start()
        except Exception as exc:
            print(f"[Camera] Could not start acquisition: {exc}")
            ia.destroy()
            h.reset()
            return False

        # Read resolution from the first frame
        try:
            with ia.fetch(timeout=3.0) as buf:
                comp = buf.payload.components[0]
                self._frame_w = comp.width
                self._frame_h = comp.height
                print(f"[Camera] Connected — {self._frame_w}×{self._frame_h}")
        except Exception as exc:
            print(f"[Camera] WARNING: Could not read frame dimensions: {exc}")
            self._frame_w = 2592
            self._frame_h = 1944

        self._harvester = h
        self._ia        = ia
        self._connected = True
        return True

    def close(self):
        if self._ia is not None:
            try:
                self._ia.stop()
                self._ia.destroy()
            except Exception:
                pass
            self._ia = None
        if self._harvester is not None:
            try:
                self._harvester.reset()
            except Exception:
                pass
            self._harvester = None
        self._connected = False
        print("[Camera] Closed.")

    # ── Frame capture ─────────────────────────────────────────────────────────

    def capture_frame(self) -> "np.ndarray | None":
        """
        Grab one frame and return a (H, W, 3) BGR uint8 numpy array.
        Returns None on error.

        IMPORTANT — do NOT call str(comp.data_format).
        The genicam C extension converts the pixel-format integer to a
        symbolic name by reading the camera's bootstrap registers via the
        GenTL node map.  The register read returns raw sensor data (actual
        Bayer pixel bytes) that are not valid UTF-8, causing a
        UnicodeDecodeError on every single frame.
        Instead we read the raw PFNC integer code directly (int() is safe)
        and compare it to known constants, falling back to size-based
        heuristics if that also fails.
        """
        if not self._connected or self._ia is None:
            return None

        # PFNC (Pixel Format Naming Convention) codes we care about
        _MONO8      = 0x01080001
        _BAYERGR8   = 0x01080008
        _BAYERRG8   = 0x01080009
        _BAYERGB8   = 0x0108000A
        _BAYERBG8   = 0x0108000B
        _RGB8       = 0x02180014
        _BGR8       = 0x02180015
        _BAYER_CODES = (_BAYERGR8, _BAYERRG8, _BAYERGB8, _BAYERBG8)
        _CV2_BAYER   = {
            _BAYERRG8: cv2.COLOR_BayerRG2BGR if _CV2_AVAILABLE else None,
            _BAYERGR8: cv2.COLOR_BayerGR2BGR if _CV2_AVAILABLE else None,
            _BAYERGB8: cv2.COLOR_BayerGB2BGR if _CV2_AVAILABLE else None,
            _BAYERBG8: cv2.COLOR_BayerBG2BGR if _CV2_AVAILABLE else None,
        }

        try:
            with self._ia.fetch(timeout=3.0) as buf:
                comp = buf.payload.components[0]
                raw  = np.frombuffer(comp.data, dtype=np.uint8)   # safe: no string decode
                h    = comp.height
                w    = comp.width

                # Safely read the PFNC integer — do NOT call str() on it
                fmt_code = 0
                try:
                    fmt_code = int(comp.data_format)
                except Exception:
                    pass

                # ── 3-byte packed (RGB8 / BGR8) ──────────────────────────────
                if raw.size == h * w * 3 or fmt_code in (_RGB8, _BGR8):
                    bgr = raw[:h * w * 3].reshape(h, w, 3).astype(np.uint8)
                    if fmt_code == _RGB8:
                        bgr = bgr[:, :, ::-1].copy()
                    return bgr.copy()

                # ── Single-channel (Mono8 or any Bayer 8-bit) ─────────────────
                if raw.size >= h * w:
                    plane = raw[:h * w].reshape(h, w).astype(np.uint8)

                    # Mono8 — stack to grey-BGR
                    if fmt_code == _MONO8:
                        return np.stack([plane, plane, plane], axis=-1)

                    # Known Bayer pattern
                    if fmt_code in _BAYER_CODES and _CV2_AVAILABLE:
                        code = _CV2_BAYER[fmt_code]
                        return cv2.cvtColor(plane, code).copy()

                    # Unknown / unreadable format — the MV-CE050-30UC ships
                    # with BayerRG8 by default so try that first.
                    if _CV2_AVAILABLE:
                        return cv2.cvtColor(plane, cv2.COLOR_BayerRG2BGR).copy()
                    # Last resort: monochrome stack
                    return np.stack([plane, plane, plane], axis=-1)

                print(f"[Camera] Unexpected buffer size {raw.size} for {w}×{h}")
                return None

        except Exception as exc:
            print(f"[Camera] Capture error: {exc}")
            return None

    # ── Colour classification ─────────────────────────────────────────────────

    def classify_frame(self, frame: "np.ndarray",
                       roi=None) -> "tuple[str | None, np.ndarray]":
        """
        Classify dominant colour in the ROI via HSV thresholding.
        Returns (colour_str, roi_bgr).
        """
        y, x, h, w = roi if roi is not None else self.roi
        y2 = min(y + h, frame.shape[0])
        x2 = min(x + w, frame.shape[1])
        crop = frame[y:y2, x:x2]

        if not _CV2_AVAILABLE or crop.size == 0:
            return None, crop

        hsv    = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        scores = {}

        for colour, cal in self.hsv_cal.items():
            hue_range, s_min, v_min = cal
            h_lo, h_hi = hue_range

            if h_lo < 0:
                # Red wraps around 0
                m1 = cv2.inRange(hsv,
                    np.array([h_lo + 180, s_min, v_min], np.uint8),
                    np.array([180,        255,   255],   np.uint8))
                m2 = cv2.inRange(hsv,
                    np.array([0,    s_min, v_min], np.uint8),
                    np.array([h_hi, 255,   255],   np.uint8))
                mask = cv2.bitwise_or(m1, m2)
            else:
                mask = cv2.inRange(hsv,
                    np.array([h_lo, s_min, v_min], np.uint8),
                    np.array([h_hi, 255,   255],   np.uint8))

            scores[colour] = int(mask.sum() // 255)

        best       = max(scores, key=scores.get)
        total_px   = crop.shape[0] * crop.shape[1]
        confidence = scores[best] / total_px if total_px > 0 else 0

        print(f"[Camera] HSV — " +
              "  ".join(f"{c}:{scores[c]}" for c in scores) +
              f"  → {best} ({confidence:.0%})")

        return (best if confidence >= 0.10 else None), crop

    # ── Verification ─────────────────────────────────────────────────────────

    def verify(self, expected_colour):
        """
        Capture a frame and verify the delivered block.
        Returns (passed, detected_colour, roi_frame).
        """
        frame = self.capture_frame()
        if frame is None:
            print("[Camera] Verify failed — no frame.")
            return (False, None, None)

        detected, roi_bgr = self.classify_frame(frame)
        passed = (detected == expected_colour) if expected_colour else (detected is not None)

        status = "PASS ✓" if passed else "FAIL ✗"
        print(f"[Camera] {status}  expected={expected_colour}  detected={detected}")
        return (passed, detected, roi_bgr)

    # ── Calibration ───────────────────────────────────────────────────────────

    def calibrate(self, colour: str, samples: int = 10) -> bool:
        """
        Measure HSV hue from real frames and save calibration for `colour`.
        Place the target block in the dispatch zone first.
        """
        if not _CV2_AVAILABLE:
            print("[Camera] Calibration requires opencv-python.")
            return False

        colour = colour.lower()
        hues   = []
        print(f"[Camera] Calibrating '{colour}' ({samples} samples)…")

        for i in range(samples):
            frame = self.capture_frame()
            if frame is None:
                continue
            y, x, h, w = self.roi
            crop = frame[y:y+h, x:x+w]
            hsv  = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            sat_mask = hsv[:, :, 1] > 80
            if sat_mask.sum() == 0:
                print(f"  Sample {i+1}: low saturation — is the block in view?")
                continue
            mean_h = float(hsv[:, :, 0][sat_mask].mean())
            hues.append(mean_h)
            print(f"  Sample {i+1}/{samples}: hue={mean_h:.1f}")
            time.sleep(0.15)

        if not hues:
            print("[Camera] Calibration failed — no valid frames.")
            return False

        mean_hue = sum(hues) / len(hues)
        margin   = 18

        if colour == "red":
            self.hsv_cal["red"] = [[-margin, margin], 80, 60]
        else:
            self.hsv_cal[colour] = [[round(mean_hue - margin),
                                     round(mean_hue + margin)], 80, 60]
        self.save_cal()
        print(f"[Camera] '{colour}' calibrated: hue≈{mean_hue:.1f} ± {margin}  (saved)")
        return True

    # ── Snapshot ──────────────────────────────────────────────────────────────

    def save_snapshot(self, path: str = None) -> "str | None":
        frame = self.capture_frame()
        if frame is None:
            return None

        if path is None:
            ts   = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                f"dispatch_snapshot_{ts}.png"
            )

        display = frame.copy()
        if _CV2_AVAILABLE:
            y, x, h, w = self.roi
            cv2.rectangle(display, (x, y), (x+w, y+h), (0, 220, 220), 2)

        try:
            if _CV2_AVAILABLE:
                cv2.imwrite(path, display)
            else:
                from PIL import Image
                Image.fromarray(frame[:, :, ::-1]).save(path)
            print(f"[Camera] Snapshot saved: {path}")
            return path
        except Exception as exc:
            print(f"[Camera] Snapshot error: {exc}")
            return None


# ── Standalone test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"harvesters available : {_HARVESTERS_AVAILABLE}")
    print(f"OpenCV available     : {_CV2_AVAILABLE}")
    print(f"CTI file exists      : {os.path.isfile(_CTI_PATH)}  ({_CTI_PATH})")
    print()

    cam = DispatchCamera()
    if not cam.connect():
        print("\nCould not connect. Checklist:")
        print("  1. Is the USB cable plugged in?")
        print("  2. Has the MVS viewer app been closed?")
        print("  3. pip install harvesters genicam")
        raise SystemExit(1)

    print(f"\nConnected via {cam.backend}  {cam._frame_w}×{cam._frame_h}")
    print("\nCommands:  v <colour>  — verify   c <colour>  — calibrate   s  — snapshot   q  — quit\n")

    try:
        while True:
            raw = input("> ").strip().lower()
            if raw == "q":
                break
            elif raw == "s":
                cam.save_snapshot()
            elif raw.startswith("c "):
                col = raw[2:].strip()
                if col in ("red", "green", "blue"):
                    cam.calibrate(col)
                else:
                    print("  Use: red  green  blue")
            elif raw.startswith("v "):
                col = raw[2:].strip()
                passed, detected, _ = cam.verify(col)
                print(f"  → {'PASS' if passed else 'FAIL'}  detected={detected}")
            else:
                print("  Unknown command.")
    except KeyboardInterrupt:
        pass
    finally:
        cam.close()

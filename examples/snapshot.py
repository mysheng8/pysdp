"""
snapshot.py — Full capture flow: connect → launch → capture → disconnect.

Usage:
    python examples/snapshot.py [device_id] [package/Activity]

Example:
    python examples/snapshot.py 192.168.1.100:5555 com.example.app/com.example.MainActivity
"""

import sys

from pysdp import SdpClient, SdpConnectionError, SdpJobError, SdpStateError, SdpTimeoutError

DEVICE_ID        = sys.argv[1] if len(sys.argv) > 1 else None
PACKAGE_ACTIVITY = sys.argv[2] if len(sys.argv) > 2 else "com.example.app/com.example.MainActivity"

client = SdpClient("http://localhost:5000", verbose=True)

try:
    # 1. Connect
    print("=== Connect ===")
    result = client.connect(device_id=DEVICE_ID)
    print(f"Connected: {result}\n")

    # 2. Launch app
    print("=== Launch ===")
    result = client.launch(PACKAGE_ACTIVITY)
    print(f"Session active: {result}\n")

    # 3. Capture
    print("=== Capture ===")
    capture = client.capture()
    print(f"Capture complete:")
    print(f"  sdpPath:   {capture.get('sdpPath')}")
    print(f"  captureId: {capture.get('captureId')}")
    print(f"  sessionId: {capture.get('sessionId')}\n")

except SdpConnectionError as exc:
    print(f"[ERROR] Cannot reach SDPCLI Server: {exc}")
    sys.exit(1)
except SdpStateError as exc:
    print(f"[ERROR] Device state error: {exc}")
    sys.exit(1)
except SdpJobError as exc:
    print(f"[ERROR] Job failed: {exc.error}")
    sys.exit(1)
except SdpTimeoutError as exc:
    print(f"[ERROR] Timed out: {exc}")
    sys.exit(1)
finally:
    print("=== Disconnect ===")
    try:
        client.disconnect()
        print("Disconnected.")
    except Exception as exc:
        print(f"Disconnect error (ignored): {exc}")

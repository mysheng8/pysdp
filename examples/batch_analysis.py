"""
batch_analysis.py — Analyse multiple snapshots from a single .sdp file.

Usage:
    python examples/batch_analysis.py <sdp_path> [snapshot_ids] [targets]

Example:
    python examples/batch_analysis.py D:/captures/session.sdp 2,3,4 label,metrics,status
"""

import sys

from pysdp import SdpClient, SdpConnectionError, SdpJobError, SdpValidationError

SDP_PATH     = sys.argv[1] if len(sys.argv) > 1 else "D:/captures/session.sdp"
SNAPSHOT_IDS = [int(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2 else [2]
TARGETS      = sys.argv[3] if len(sys.argv) > 3 else "dc,label,metrics,status"

client = SdpClient("http://localhost:5000", timeout=900, verbose=True)

print(f"SDP:      {SDP_PATH}")
print(f"Snapshots: {SNAPSHOT_IDS}")
print(f"Targets:  {TARGETS}\n")

results = []
errors  = []

for snap_id in SNAPSHOT_IDS:
    print(f"=== Snapshot {snap_id} ===")
    try:
        result = client.analyze(
            sdp_path=SDP_PATH,
            snapshot_id=snap_id,
            targets=TARGETS,
        )
        print(f"  captureDir: {result.get('captureDir')}")
        print(f"  targets:    {result.get('targets')}\n")
        results.append((snap_id, result))
    except SdpValidationError as exc:
        print(f"  [SKIP] Validation error: {exc}\n")
        errors.append((snap_id, str(exc)))
    except SdpJobError as exc:
        print(f"  [FAIL] Job error: {exc.error}\n")
        errors.append((snap_id, exc.error))
    except SdpConnectionError as exc:
        print(f"[ERROR] Cannot reach SDPCLI Server: {exc}")
        sys.exit(1)

print("=== Summary ===")
print(f"Completed: {len(results)}/{len(SNAPSHOT_IDS)}")
for snap_id, result in results:
    print(f"  snapshot {snap_id} → {result.get('captureDir')}")
if errors:
    print(f"Failed: {len(errors)}")
    for snap_id, msg in errors:
        print(f"  snapshot {snap_id}: {msg}")

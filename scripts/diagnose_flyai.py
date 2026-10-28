"""One-shot, read-only FlyAI API diagnostic. Does not print credentials or ticket URLs."""
import json
import os
import subprocess
import sys

if not os.environ.get("FLYAI_API_KEY"):
    print("ERROR: FLYAI_API_KEY secret not configured")
    sys.exit(2)

cmd = ["./node_modules/.bin/flyai", "search-flight",
       "--origin", "CDG", "--destination", "TPE", "--dep-date", "2026-12-20"]
print("Diagnostic: CDG -> TPE, 2026-12-20; no max-price, no sort")
try:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=40,
                            env=os.environ.copy(), check=False)
except subprocess.TimeoutExpired:
    print("RESULT: CLI timeout (40 seconds)")
    sys.exit(1)
except OSError as exc:
    print("RESULT: Could not start flyai CLI:", type(exc).__name__)
    sys.exit(1)

print("CLI exit code:", result.returncode)
# Do not print full output, which may contain booking links or other sensitive fields.
try:
    obj = json.loads(result.stdout)
except json.JSONDecodeError:
    print("RESULT: Non-JSON response; output omitted for safety")
    sys.exit(1)
print("API status:", str(obj.get("status", ""))[:40])
print("API message:", str(obj.get("message", ""))[:250])
print("API systemMessage:", str(obj.get("systemMessage", ""))[:250])
data = obj.get("data")
if isinstance(data, dict):
    items = data.get("itemList")
    print("Flight item count:", len(items) if isinstance(items, list) else "unknown")
sys.exit(0 if result.returncode == 0 and str(obj.get("status")) == "0" else 1)

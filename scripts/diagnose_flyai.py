"""Read-only two-route FlyAI diagnostic; never prints credentials or booking URLs."""
import json
import os
import subprocess
import sys
import time

ROUTES = [
    ("domestic_control", "PVG", "PEK", "2026-12-20"),
    ("international_target", "CDG", "TPE", "2026-12-20"),
]

def diagnose(label, origin, destination, date):
    cmd = ["./node_modules/.bin/flyai", "search-flight",
           "--origin", origin, "--destination", destination, "--dep-date", date]
    print(f"\n=== {label}: {origin} -> {destination}, {date} ===", flush=True)
    print("No max-price / sort; no booking URL or credentials printed.", flush=True)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=40, env=os.environ.copy(), check=False)
    except subprocess.TimeoutExpired:
        print("RESULT: CLI timeout (40 seconds)", flush=True)
        return False
    except OSError as exc:
        print("RESULT: CLI launch error:", type(exc).__name__, flush=True)
        return False
    print("CLI exit code:", result.returncode, flush=True)
    try:
        obj = json.loads(result.stdout)
    except (ValueError, TypeError):
        print("RESULT: Non-JSON response; raw output omitted", flush=True)
        return False
    print("API status:", str(obj.get("status", ""))[:40], flush=True)
    print("API message:", str(obj.get("message") or "")[:250], flush=True)
    print("API systemMessage:", str(obj.get("systemMessage") or "")[:250], flush=True)
    data = obj.get("data")
    if isinstance(data, dict):
        items = data.get("itemList")
        print("Flight item count:", len(items) if isinstance(items, list) else "unknown", flush=True)
    return result.returncode == 0 and str(obj.get("status")) == "0"

def main():
    if not os.environ.get("FLYAI_API_KEY"):
        print("ERROR: FLYAI_API_KEY secret is missing")
        return 2
    outcomes = []
    for i, route in enumerate(ROUTES):
        if i:
            time.sleep(12)  # maintain provider-friendly request spacing
        outcomes.append(diagnose(*route))
    print("\n=== Summary ===")
    for route, ok in zip(ROUTES, outcomes):
        print(route[0], "SUCCESS" if ok else "FAILED")
    return 0 if all(outcomes) else 1

if __name__ == "__main__":
    sys.exit(main())

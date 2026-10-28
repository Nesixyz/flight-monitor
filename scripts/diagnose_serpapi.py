"""Safe, single-request round-trip Google Flights diagnostic via SerpApi.

No price-monitor state changes, no notifications and no booking URLs output.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

URL = "https://serpapi.com/search.json"
PARAMS = {
    "engine": "google_flights",
    "departure_id": "CDG",
    "arrival_id": "TPE",
    "outbound_date": "2026-12-20",
    "return_date": "2026-12-28",
    "type": "1",
    "travel_class": "1",
    "stops": "2",
    "currency": "CNY",
    "hl": "en",
    "gl": "fr",
    "adults": "1",
}


def diagnose():
    key = os.environ.get("SERPAPI_API_KEY", "").strip()
    if not key:
        print("ERROR: Missing SERPAPI_API_KEY GitHub Actions secret")
        return 2
    params = dict(PARAMS, api_key=key)
    # POST form fields to avoid embedding secrets in URLs and CI logs.
    request = urllib.request.Request(
        URL,
        data=urllib.parse.urlencode(params).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    print("Testing Google Flights: CDG -> TPE, Dec 20 to Dec 28, economy, <=1 stop.")
    print("One SerpApi request. No API key, raw body, links, or tokens printed.", flush=True)
    try:
        with urllib.request.urlopen(request, timeout=55) as response:
            body = response.read(2_000_000)
            status = response.status
    except urllib.error.HTTPError as exc:
        print("HTTP error:", exc.code)
        return 1
    except (urllib.error.URLError, TimeoutError) as exc:
        print("Network failure:", type(exc).__name__)
        return 1
    try:
        result = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        print("Error: Non-JSON response")
        return 1
    metadata = result.get("search_metadata") or {}
    flights = (result.get("best_flights") or []) + (result.get("other_flights") or [])
    print("HTTP status:", status)
    print("Search status:", str(metadata.get("status", "unknown"))[:50])
    print("API error:", str(result.get("error") or "")[:240])
    print("Returned itineraries:", len(flights))
    print("Departure tokens:", sum(bool(f.get("departure_token")) for f in flights))
    for i, item in enumerate(flights[:3], 1):
        print("Example", i, "price:", item.get("price"), "CNY",
              "duration:", item.get("total_duration"), "minutes",
              "stops:", max(0, len(item.get("flights") or []) - 1))
    if result.get("error"):
        return 1
    if metadata.get("status") not in (None, "Success"):
        return 1
    return 0 if flights else 1


if __name__ == "__main__":
    sys.exit(diagnose())

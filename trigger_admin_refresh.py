#!/usr/bin/env python3
import os
import sys

import requests


def main() -> int:
    base_url = os.environ.get("EUROMILLIONS_DASHBOARD_URL", "").strip().rstrip("/")
    token = os.environ.get("ADMIN_REFRESH_TOKEN", "").strip()
    if not base_url or not token:
        print("Missing EUROMILLIONS_DASHBOARD_URL or ADMIN_REFRESH_TOKEN.", file=sys.stderr)
        return 2

    url = f"{base_url}/admin/refresh"
    response = requests.get(url, headers={"X-Admin-Token": token}, timeout=120)
    print(f"admin refresh status={response.status_code}")
    print(response.text[:2000])
    response.raise_for_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

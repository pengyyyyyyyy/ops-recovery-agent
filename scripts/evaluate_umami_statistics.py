"""Verify a unique synthetic pageview through the Umami statistics API."""

from __future__ import annotations

import argparse
import http.client
import json
import time
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

SITE = "d77f6116-74b8-435f-959d-03577bc1a5cb"
AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"


def request(method, path, *, token=None, share_token=None, payload=None):
    headers = {"User-Agent": AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if share_token:
        headers["x-umami-share-token"] = share_token
        headers["x-umami-share-context"] = "true"
    body = None if payload is None else json.dumps(payload).encode()
    if body is not None:
        headers["Content-Type"] = "application/json"
    peer = http.client.HTTPConnection("127.0.0.1", 3000, timeout=6)
    try:
        peer.request(method, path, body=body, headers=headers)
        response = peer.getresponse()
        return response.status, response.read(32769)
    finally:
        peer.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    auth = parser.add_mutually_exclusive_group(required=True)
    auth.add_argument("--token-file", type=Path)
    auth.add_argument("--share-slug-file", type=Path)
    args = parser.parse_args()
    token = None
    share_token = None
    if args.token_file and args.token_file.is_file():
        token = args.token_file.read_text(encoding="utf-8").strip()
    if token is not None and (not token or len(token) > 4096 or any(c.isspace() for c in token)):
        raise SystemExit("The token file must contain one nonempty credential.")
    if args.share_slug_file:
        slug = args.share_slug_file.read_text(encoding="utf-8").strip()
        if not 16 <= len(slug) <= 100 or not slug.isascii() or not slug.isalnum():
            raise SystemExit("The share file must contain one valid Umami share slug.")
        status, raw = request("GET", "/api/share/" + slug)
        if status != 200 or len(raw) > 32768:
            raise SystemExit(f"Umami did not issue the temporary share token (HTTP {status}).")
        share = json.loads(raw)
        if share.get("websiteId") != SITE or share.get("shareType") != 1 or not share.get("token"):
            raise SystemExit("The share must be restricted to the synthetic pilot site.")
        share_token = share["token"]
    name = "/ops-pilot/statistics/" + uuid4().hex
    since = int(time.time() * 1000) - 10_000

    def stats():
        query = urlencode({"startAt": since, "endAt": int(time.time() * 1000) + 1000, "path": name})
        status, raw = request(
            "GET", f"/api/websites/{SITE}/stats?{query}", token=token, share_token=share_token
        )
        if status != 200:
            return status, None
        if len(raw) > 32768:
            raise ValueError("Unexpected Umami stats response size")
        return status, json.loads(raw)["pageviews"]

    status, before = stats()
    if status in {401, 403}:
        print(
            json.dumps(
                {
                    "check": "umami_stats_api",
                    "status": "blocked",
                    "http_status": status,
                    "reason": "a valid credential for the synthetic site is required",
                }
            )
        )
        return
    if status != 200 or before != 0:
        raise SystemExit(f"Unexpected stats baseline HTTP {status}; expected zero pageviews.")
    code, raw = request(
        "POST",
        "/api/send",
        payload={
            "type": "event",
            "payload": {
                "website": SITE,
                "hostname": "ops-pilot.invalid",
                "url": name,
                "language": "en-US",
                "screen": "1920x1080",
            },
        },
    )
    if code != 200 or not json.loads(raw).get("sessionId"):
        raise SystemExit(f"Umami rejected the synthetic pageview with HTTP {code}.")
    started = time.monotonic()
    while time.monotonic() - started < 30:
        status, views = stats()
        if status == 200 and views == 1:
            print(
                json.dumps(
                    {
                        "check": "umami_stats_api",
                        "status": "passed",
                        "pageviews_before": before,
                        "pageviews_after": views,
                        "visibility_delay_ms": round((time.monotonic() - started) * 1000),
                    }
                )
            )
            return
        if status != 200 or views not in {0, 1}:
            raise SystemExit(f"Unexpected statistics response HTTP {status} during verification.")
        time.sleep(1)
    raise SystemExit("The synthetic pageview was not visible in the statistics API within 30 seconds.")


if __name__ == "__main__":
    main()

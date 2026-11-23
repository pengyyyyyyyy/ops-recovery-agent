"""Bounded load generator for the dedicated local Umami synthetic site."""

import argparse
import http.client
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

SITE = "d77f6116-74b8-435f-959d-03577bc1a5cb"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"


def send(batch, number):
    started = time.monotonic()
    request = {
        "type": "event",
        "payload": {
            "website": SITE,
            "hostname": "ops-pilot.invalid",
            "url": f"/ops-pilot/load/{batch}/{number}",
            "name": "pilot.load",
        },
    }
    connection = http.client.HTTPConnection("127.0.0.1", 3000, timeout=5)
    status = None
    accepted = False
    try:
        connection.request(
            "POST",
            "/api/send",
            body=json.dumps(request),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        response = connection.getresponse()
        status = response.status
        body = response.read(8192)
        accepted = status == 200 and bool(json.loads(body).get("sessionId"))
    except (OSError, http.client.HTTPException, ValueError, AttributeError):
        pass
    finally:
        connection.close()
    return {"ok": accepted, "status": status, "seconds": time.monotonic() - started}


def load(rate, seconds, concurrency):
    batch = uuid4().hex
    sample, outstanding = [], set()
    started = time.monotonic()
    attempted = rate * seconds
    dropped = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for number in range(attempted):
            time.sleep(max(0, started + number / rate - time.monotonic()))
            for future in list(outstanding):
                if future.done():
                    sample.append(future.result())
                    outstanding.remove(future)
            if len(outstanding) == concurrency:
                dropped += 1
            else:
                outstanding.add(pool.submit(send, batch, number))
        sample.extend(future.result() for future in outstanding)
    latencies = sorted(item["seconds"] * 1000 for item in sample)
    quantiles = (
        {f"p{pct}_ms": round(latencies[math.ceil(len(latencies) * pct / 100) - 1], 2) for pct in (50, 95, 99)}
        if latencies
        else {}
    )
    return {
        "environment": "local synthetic Umami site",
        "batch": batch,
        "target_requests_per_second": rate,
        "schedule_seconds": seconds,
        "concurrency": concurrency,
        "offered": attempted,
        "sent": len(sample),
        "accepted": sum(item["ok"] for item in sample),
        "dropped_before_send": dropped,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        **quantiles,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rate", type=int, default=1, choices=range(1, 21))
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--concurrency", type=int, default=4, choices=range(1, 17))
    args = parser.parse_args()
    if not 1 <= args.seconds <= 300:
        parser.error("--seconds must be between 1 and 300")
    print(json.dumps(load(args.rate, args.seconds, args.concurrency), indent=2))


if __name__ == "__main__":
    main()

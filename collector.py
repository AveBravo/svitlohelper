#!/usr/bin/env python3
"""One-shot Marstek cloud telemetry collector for GitHub Actions.

No writes to the physical station. Credentials, request URLs,
account identifiers and raw responses are never logged.
"""

import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime


ORIGIN = "https://eu.hamedata.com"
DESTINATION = (
    "https://svitlohelper-cloud.svitlohelper.workers.dev/ingest"
)
USER_AGENT = "SvitlohelperCollector/1.1"

MAX_RESPONSE_BYTES = 500000
RETRY_DELAYS = (5, 15)

RETRYABLE_ERRORS = {
    "marstek: readings_missing",
    "marstek: power_invalid",
    "marstek: login_token_missing",
    "marstek: connection_failed",
    "marstek: invalid_json",
    "marstek: http_429",
    "marstek: http_500",
    "marstek: http_502",
    "marstek: http_503",
    "marstek: http_504",
    "marstek: http_525",
}


def log(message):
    print(message, flush=True)


def fetch_json(method, path, args):
    url = ORIGIN + path + "?" + urllib.parse.urlencode(args)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            "marstek: http_" + str(exc.code)
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError(
            "marstek: connection_failed"
        ) from None

    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("marstek: response_too_large")

    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise RuntimeError("marstek: invalid_json") from None


def search_token(value, depth=0):
    if depth > 8:
        return None

    if isinstance(value, dict):
        for key, child in value.items():
            if (
                str(key).lower() == "token"
                and isinstance(child, str)
                and len(child) >= 8
            ):
                return child

        for child in value.values():
            token = search_token(child, depth + 1)
            if token:
                return token

    elif isinstance(value, list):
        for child in value:
            token = search_token(child, depth + 1)
            if token:
                return token

    return None


def valid_number(raw):
    if raw is None or isinstance(raw, bool) or raw == "":
        return None

    try:
        number = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None

    return number if math.isfinite(number) else None


def valid_power(raw):
    number = valid_number(raw)
    if number is not None and 0 <= number <= 12000:
        return number
    return None


def find_readings(value, depth=0):
    if depth > 10:
        return []

    found = []

    if isinstance(value, dict):
        fields = {
            str(key).lower(): child
            for key, child in value.items()
        }
        soc = valid_number(fields.get("soc"))

        if (
            soc is not None
            and 0 <= soc <= 100
            and ("charge" in fields or "discharge" in fields)
        ):
            found.append(fields)

        for child in value.values():
            found.extend(find_readings(child, depth + 1))

    elif isinstance(value, list):
        for child in value:
            found.extend(find_readings(child, depth + 1))

    return found


def print_payload_summary(payload):
    """Log structure and counts only, never raw response values."""
    counts = {
        "objects": 0,
        "soc_fields": 0,
        "valid_soc": 0,
        "charge_fields": 0,
        "discharge_fields": 0,
        "complete_readings": 0,
        "hme3_readings": 0,
    }

    def walk(value, depth=0):
        if depth > 10:
            return

        if isinstance(value, dict):
            counts["objects"] += 1
            fields = {
                str(key).lower(): child
                for key, child in value.items()
            }

            for field in ("soc", "charge", "discharge"):
                if field in fields:
                    counts[field + "_fields"] += 1

            soc = valid_number(fields.get("soc"))
            if soc is not None and 0 <= soc <= 100:
                counts["valid_soc"] += 1

                if "charge" in fields or "discharge" in fields:
                    counts["complete_readings"] += 1

                    if (
                        str(fields.get("model", "")).upper()
                        == "HME-3"
                    ):
                        counts["hme3_readings"] += 1

            for child in value.values():
                walk(child, depth + 1)

        elif isinstance(value, list):
            for child in value:
                walk(child, depth + 1)

    walk(payload)

    summary = "; ".join(
        f"{name}={count}" for name, count in counts.items()
    )
    log(
        f"Marstek response: type={type(payload).__name__}; "
        + summary
    )


def parse_report_time(raw):
    """Trust only positive epochs or dates with explicit timezone."""
    if isinstance(raw, bool):
        return None

    number = valid_number(raw)
    if isinstance(raw, (int, float)) and number is not None:
        seconds = number / 1000 if number > 1e12 else number
        return int(seconds) if seconds > 0 else None

    if not isinstance(raw, str):
        return None

    raw = raw.strip()

    if raw.isdigit() and len(raw) in (10, 13):
        number = int(raw)
        seconds = number // 1000 if len(raw) == 13 else number
        return seconds if seconds > 0 else None

    if "T" not in raw:
        return None

    try:
        parsed = datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        )
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None

        seconds = int(parsed.timestamp())
        return seconds if seconds > 0 else None
    except (ValueError, OverflowError, OSError):
        return None


def collect_once(email, pwd_md5):
    login = fetch_json(
        "POST",
        "/app/Solar/v2_get_device.php",
        {
            "mailbox": email,
            "pwd": pwd_md5,
        },
    )

    token = search_token(login)
    if not token:
        raise RuntimeError("marstek: login_token_missing")

    devices = fetch_json(
        "GET",
        "/ems/api/v1/getDeviceList",
        {"token": token},
    )

    print_payload_summary(devices)

    reading_list = [
        reading
        for reading in find_readings(devices)
        if str(reading.get("model", "")).upper() != "HME-3"
    ]

    if not reading_list:
        raise RuntimeError("marstek: readings_missing")

    log(f"Marstek: matching records={len(reading_list)}")

    # Preserve the existing choice of the first matching station.
    reading = reading_list[0]

    soc = valid_number(reading.get("soc"))
    charge = valid_power(reading.get("charge"))
    discharge = valid_power(reading.get("discharge"))
    load = valid_power(reading.get("load"))

    if charge is None and discharge is None:
        raise RuntimeError("marstek: power_invalid")

    timestamp = parse_report_time(
        reading.get("report_time")
    )

    # Do not publish a timestamp ahead of the collector's clock.
    now = int(time.time())
    if timestamp is not None and timestamp > now:
        timestamp = None

    return {
        "soc": soc,
        "charge_power_w": charge,
        "discharge_power_w": discharge,
        "load_power_w": load,
        "device_updated_at_epoch": timestamp,
        "fetched_at_epoch": now,
    }


def collect(email, pwd_md5):
    total_attempts = len(RETRY_DELAYS) + 1

    for attempt in range(total_attempts):
        log(
            f"Marstek: attempt {attempt + 1}/{total_attempts}"
        )

        try:
            return collect_once(email, pwd_md5)
        except RuntimeError as exc:
            if (
                str(exc) not in RETRYABLE_ERRORS
                or attempt == total_attempts - 1
            ):
                raise

            delay = RETRY_DELAYS[attempt]
            log(f"Marstek: {exc}; retry in {delay}s")
            time.sleep(delay)

    raise RuntimeError("marstek: readings_missing")


def send_status(status, ingest_key):
    body = json.dumps(
        status,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")

    request = urllib.request.Request(
        DESTINATION,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Ingest-Key": ingest_key,
            "User-Agent": USER_AGENT,
        },
    )

    try:
        with urllib.request.urlopen(
            request, timeout=20
        ) as response:
            if response.status != 200:
                raise RuntimeError(
                    "cloudflare: ingest_failed"
                )
            raw = response.read(4097)

    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            "cloudflare: http_" + str(exc.code)
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError(
            "cloudflare: connection_failed"
        ) from None

    if len(raw) > 4096:
        raise RuntimeError("cloudflare: response_too_large")

    try:
        result = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise RuntimeError(
            "cloudflare: invalid_json"
        ) from None

    if not (
        isinstance(result, dict)
        and result.get("ok")
        and result.get("stored")
    ):
        raise RuntimeError("cloudflare: ingest_failed")


def main():
    email = os.environ.get("MARSTEK_EMAIL", "").strip()
    md5 = os.environ.get("MARSTEK_PWD_MD5", "").strip()
    key = os.environ.get("INGEST_KEY", "").strip()

    if not email or len(key) < 32 or not md5:
        raise RuntimeError("config: missing_secrets")

    if (
        len(md5) != 32
        or any(
            char not in "0123456789abcdefABCDEF"
            for char in md5
        )
    ):
        raise RuntimeError("config: invalid_password_hash")

    status = collect(email, md5)

    log(
        f"Cloud: SOC={status['soc']}%, "
        f"charge={status['charge_power_w']} W, "
        f"discharge={status['discharge_power_w']} W, "
        f"load={status['load_power_w']} W"
    )

    reported = status["device_updated_at_epoch"]
    age = (
        round((time.time() - reported) / 60)
        if reported is not None
        else "unknown"
    )
    log(f"Marstek report age: {age} min")

    send_status(status, key)
    log("Marstek: snapshot accepted by Cloudflare.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        # Only self-authored error codes, never raw exceptions.
        print(
            "Collector failed: " + str(exc),
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)

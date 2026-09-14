from __future__ import annotations

import hashlib
import io
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from flash_employment.productivity_probe import (
    ProbeAttempt,
    ProbeFailure,
    ProbeStore,
    ProductivityHttpClient,
    first_valid_attempt,
    validates_previous_productivity_release,
    validates_target_productivity_release,
)


def previous_payload() -> bytes:
    return b"""<html><body>
    Transmission of material in this release is embargoed until 8:30 a.m. (ET)
    Thursday, August 6, 2026
    PRODUCTIVITY AND COSTS, SECOND QUARTER 2026, PRELIMINARY
    </body></html>"""


def target_payload() -> bytes:
    return b"""<html><body>
    Transmission of material in this release is embargoed until 8:30 a.m. (ET)
    Thursday, September 3, 2026
    PRODUCTIVITY AND COSTS, SECOND QUARTER 2026, REVISED
    </body></html>"""


def probe_attempt(
    *,
    payload: bytes = b"evidence",
    validation: str = "VALID_TARGET",
    mono_ns: int = 2_000_000,
    wall_ns: int = 3_000_000,
) -> ProbeAttempt:
    return ProbeAttempt(
        "prod2_current",
        "https://www.bls.gov/news.release/prod2.nr0.htm",
        "RELEASE_OBSERVATION",
        2,
        wall_ns - 2_000,
        mono_ns - 2_000,
        wall_ns - 1_000,
        mono_ns - 1_000,
        wall_ns,
        mono_ns,
        200,
        "Thu, 03 Sep 2026 12:30:00 GMT",
        '"etag"',
        "Thu, 03 Sep 2026 12:30:00 GMT",
        "max-age=0",
        payload,
        hashlib.sha256(payload).hexdigest(),
        validation,
        None,
    )


class ProductivityValidationTests(unittest.TestCase):
    def test_previous_payload_is_not_new_release(self) -> None:
        payload = previous_payload()
        self.assertTrue(validates_previous_productivity_release(payload))
        self.assertFalse(validates_target_productivity_release(payload))

    def test_changed_but_invalid_payload_is_rejected(self) -> None:
        payload = b"Productivity and Costs changed, but this is not a release."
        self.assertFalse(validates_previous_productivity_release(payload))
        self.assertFalse(validates_target_productivity_release(payload))

    def test_q2_2026_revised_release_is_accepted(self) -> None:
        self.assertTrue(validates_target_productivity_release(target_payload()))

    def test_http_error_and_timeout_are_classified(self) -> None:
        client = ProductivityHttpClient(
            "prod2_current", contact="operator@example.com"
        )
        http_error = urllib.error.HTTPError(
            client.url, 503, "unavailable", {}, io.BytesIO(b"")
        )
        with patch.object(client.opener, "open", side_effect=http_error):
            attempt = client.fetch(
                phase="RELEASE_OBSERVATION",
                request_number=1,
                baseline_sha256="baseline",
            )
        self.assertEqual(attempt.validation_result, "HTTP_ERROR")
        with patch.object(client.opener, "open", side_effect=TimeoutError("slow")):
            attempt = client.fetch(
                phase="RELEASE_OBSERVATION",
                request_number=2,
                baseline_sha256="baseline",
            )
        self.assertEqual(attempt.validation_result, "TIMEOUT")

    def test_no_release_is_a_clean_bounded_failure(self) -> None:
        with self.assertRaisesRegex(ProbeFailure, "bounded window"):
            first_valid_attempt({})


class ProductivityPersistenceTests(unittest.TestCase):
    def test_timing_evidence_and_payload_hash_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "probe.sqlite"
            store = ProbeStore(path)
            run_id = store.start(
                __import__("datetime").date(2026, 9, 3), scheduled_wall_ns=1_000_000
            )
            attempt = probe_attempt(payload=target_payload())
            store.attempt(run_id, attempt)
            store.finish(
                run_id,
                status="COMPLETE",
                error=None,
                first=attempt,
                scheduled_wall_ns=1_000_000,
                valid_by_source={attempt.source_name: attempt},
            )
            store.close()
            conn = sqlite3.connect(path)
            row = conn.execute(
                """SELECT request_started_wall_ns,request_started_monotonic_ns,
                body_complete_wall_ns,body_complete_monotonic_ns,
                parse_complete_wall_ns,parse_complete_monotonic_ns,payload_sha256
                FROM probe_attempts WHERE run_id=?""",
                (run_id,),
            ).fetchone()
            summary = conn.execute(
                "SELECT scheduled_to_first_valid_ms FROM probe_summary WHERE run_id=?",
                (run_id,),
            ).fetchone()
            conn.close()
        self.assertTrue(all(value is not None for value in row[:6]))
        self.assertEqual(row[6], hashlib.sha256(target_payload()).hexdigest())
        self.assertEqual(summary[0], 2.0)

"""Parse-cache lifetime (hours, default one day) and the /api/parse lookup."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.cache import ParseCache, content_key
from app.config import (
    DEFAULT_PARSE_CACHE_TTL_HOURS,
    display_ttl_hours,
    parse_cache_ttl_hours,
)
from app.ocr import OcrResult, TextSegment


class TtlHoursTests(unittest.TestCase):
    def test_default_is_one_day_when_unset_or_blank(self) -> None:
        self.assertEqual(DEFAULT_PARSE_CACHE_TTL_HOURS, 24.0)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PARSE_CACHE_TTL_HOURS", None)
            self.assertEqual(parse_cache_ttl_hours(), 24.0)
            self.assertEqual(parse_cache_ttl_hours(""), 24.0)
            self.assertEqual(parse_cache_ttl_hours("   "), 24.0)

    def test_hours_accepts_fractions_with_dot_or_comma(self) -> None:
        self.assertEqual(parse_cache_ttl_hours("6"), 6.0)
        self.assertEqual(parse_cache_ttl_hours("  2.5 "), 2.5)
        self.assertEqual(parse_cache_ttl_hours("1,5"), 1.5)
        self.assertEqual(parse_cache_ttl_hours("0"), 0.0)
        self.assertEqual(display_ttl_hours(24.0), 24)
        self.assertIsInstance(display_ttl_hours(24.0), int)
        self.assertEqual(display_ttl_hours(1.5), 1.5)

    def test_rejects_values_that_are_not_a_number_of_hours(self) -> None:
        for raw in ("nope", "24h", "-1", "-0.5", "inf", "nan"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as caught:
                    parse_cache_ttl_hours(raw)
                self.assertIn("PARSE_CACHE_TTL_HOURS", str(caught.exception))


class ParseCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [1_000_000.0]
        self.cache = ParseCache(ttl_hours=24, clock=lambda: self.now[0])

    def test_put_get_stays_in_this_process_only(self) -> None:
        key = content_key("счёт".encode())
        invoice = {"vendor_name": "ООО Ромашка", "amount": "10.00"}
        stored = self.cache.put(key, invoice=invoice, sources={"amount": "Итого 10"}, ocr={"lines": ["Итого 10"]})
        self.assertTrue(stored)
        got = self.cache.get(key)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got.invoice, invoice)
        self.assertEqual(got.stored_at, 1_000_000.0)
        self.assertNotIn("total_ms", got.ocr)

        revived = ParseCache(ttl_hours=24, clock=lambda: self.now[0])
        self.assertIsNone(revived.get(key))

    def test_lifetime_is_absolute_and_expires_after_the_ttl(self) -> None:
        key = content_key(b"invoice")
        self.cache.put(key, invoice={"amount": "1.00"}, sources={}, ocr={"ocr_ms": 5})
        self.now[0] += 10
        got = self.cache.get(key)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(self.cache.age_seconds(got.stored_at), 10)
        self.assertEqual(self.cache._entries[key].stored_at, 1_000_000.0)

        self.now[0] = 1_000_000.0 + 24 * 3600
        self.assertIsNone(self.cache.get(key))
        self.assertNotIn(key, self.cache._entries)

    def test_get_does_not_sweep_other_expired_entries(self) -> None:
        asked = content_key(b"asked")
        other = content_key(b"other")
        self.cache.put(asked, invoice={"n": "asked"}, sources={}, ocr={})
        self.cache.put(other, invoice={"n": "other"}, sources={}, ocr={})
        self.now[0] += 24 * 3600
        self.cache.put(other, invoice={"n": "fresh"}, sources={}, ocr={})
        self.assertIsNone(self.cache.get(asked))
        self.assertNotIn(asked, self.cache._entries)
        self.assertEqual(self.cache.get(other).invoice, {"n": "fresh"})

    def test_claim_purge_is_at_most_once_a_minute(self) -> None:
        self.assertTrue(self.cache.claim_purge())
        self.assertFalse(self.cache.claim_purge())
        self.now[0] += 59
        self.assertFalse(self.cache.claim_purge())
        self.now[0] += 1
        self.assertTrue(self.cache.claim_purge())
        disabled = ParseCache(ttl_hours=0)
        self.assertFalse(disabled.claim_purge())

    def test_zero_ttl_disables_the_cache(self) -> None:
        cache = ParseCache(ttl_hours=0)
        key = content_key(b"invoice")
        self.assertFalse(cache.enabled)
        self.assertFalse(cache.put(key, invoice={}, sources={}, ocr={}))
        self.assertIsNone(cache.get(key))
        self.assertEqual(cache._entries, {})

    def test_one_hour_boundary(self) -> None:
        cache = ParseCache(ttl_hours=1, clock=lambda: self.now[0])
        key = content_key(b"hour")
        cache.put(key, invoice={"n": "1"}, sources={}, ocr={})
        self.now[0] += 3599
        self.assertIsNotNone(cache.get(key))
        self.now[0] += 1
        self.assertIsNone(cache.get(key))

    def test_purge_keeps_a_fresh_entry_and_drops_an_expired_one(self) -> None:
        fresh = content_key(b"fresh")
        expired = content_key(b"expired")
        self.cache.put(expired, invoice={"n": "old"}, sources={}, ocr={})
        self.now[0] += 10
        self.cache.put(fresh, invoice={"n": "new"}, sources={}, ocr={})
        self.now[0] = 1_000_000.0 + 24 * 3600
        self.cache.purge_expired()
        self.assertIsNone(self.cache.get(expired))
        got = self.cache.get(fresh)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got.invoice, {"n": "new"})

    def test_key_must_be_a_sha256(self) -> None:
        with self.assertRaises(ValueError):
            self.cache.get("../etc/passwd")
        with self.assertRaises(ValueError):
            self.cache.get("abc")

    def test_negative_ttl_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ParseCache(ttl_hours=-1)

    def test_concurrent_writes(self) -> None:
        errors: list[BaseException] = []
        key = content_key(b"same")

        def worker() -> None:
            try:
                self.cache.put(key, invoice={"a": "1"}, sources={}, ocr={"lines": []})
                if self.cache.get(key) is None:
                    errors.append(AssertionError("missing entry"))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])


class ProcessEnvironmentTests(unittest.TestCase):
    def test_fresh_process_defaults_to_one_day(self) -> None:
        env = os.environ.copy()
        env.pop("PARSE_CACHE_TTL_HOURS", None)
        out = subprocess.check_output(
            [sys.executable, "-c", "from app.config import parse_cache_ttl_hours; print(parse_cache_ttl_hours())"],
            cwd=ROOT,
            env=env,
            text=True,
        )
        self.assertEqual(out.strip(), "24.0")

    def test_server_reads_ttl_from_the_environment(self) -> None:
        env = os.environ.copy()
        env["PARSE_CACHE_TTL_HOURS"] = "6"
        out = subprocess.check_output(
            [sys.executable, "-c", "from app.main import cache; print(cache.ttl_hours)"],
            cwd=ROOT,
            env=env,
            text=True,
        )
        self.assertEqual(out.strip(), "6.0")

    def test_server_refuses_a_bad_ttl(self) -> None:
        env = os.environ.copy()
        env["PARSE_CACHE_TTL_HOURS"] = "nope"
        result = subprocess.run(
            [sys.executable, "-c", "import app.main"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PARSE_CACHE_TTL_HOURS", result.stderr)


class FakeEngine:
    def __init__(self) -> None:
        self.calls = 0
        self.loads = 0
        self.is_ready = False
        self.last_pdf = False

    def load(self) -> None:
        self.loads += 1
        self.is_ready = True

    def recognize(self, data: bytes, *, pdf: bool = False) -> OcrResult:
        self.calls += 1
        self.last_pdf = pdf
        if data.startswith(b"bad"):
            from app.ocr import DocumentDecodeError

            raise DocumentDecodeError("The file is not a supported image.")
        return OcrResult(
            segments=[
                TextSegment("Blue Ridge Supply LLC", 0.99, 0, 0, 200, 30),
                TextSegment("Invoice No: 2026-1142", 0.99, 0, 40, 220, 60),
                TextSegment("Amount Due: $10.00", 0.99, 0, 80, 200, 100),
            ],
            image_width=220,
            image_height=120,
            duration_ms=1234,
        )


class ParseApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from app import main

        self.main = main
        self.TestClient = TestClient
        self.now = [1_700_000_000.0]
        self.engine = FakeEngine()
        self.original = (main.engine, main.cache, main.PRELOAD_MODEL)
        main.engine = self.engine
        main.PRELOAD_MODEL = False
        main.cache = ParseCache(ttl_hours=24, clock=lambda: self.now[0])
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        self.main.engine, self.main.cache, self.main.PRELOAD_MODEL = self.original

    def _post(self, client, name: str, data: bytes, content_type: str = "image/png"):
        return client.post("/api/parse", files={"file": (name, data, content_type)})

    def test_second_upload_of_the_same_bytes_skips_ocr(self) -> None:
        data = b"invoice-bytes"
        with self.TestClient(self.main.app) as client:
            health = client.get("/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["cache_ttl_hours"], 24)
            self.assertIsInstance(health.json()["cache_ttl_hours"], int)

            first = self._post(client, "first.png", data)
            self.now[0] += 10
            second = self._post(client, "second.png", data)
            other = self._post(client, "other.png", b"other-bytes")

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(other.status_code, 200, other.text)
        self.assertEqual(self.engine.calls, 2)
        self.assertEqual(self.engine.loads, 0)

        body = first.json()
        again = second.json()
        self.assertEqual(body["cache"], {"hit": False, "ttl_hours": 24})
        self.assertEqual(again["cache"], {"hit": True, "ttl_hours": 24, "age_seconds": 10})
        self.assertEqual(again["invoice"], body["invoice"])
        self.assertEqual(again["sources"], body["sources"])
        self.assertNotEqual(body["invoice"]["invoice_number"], "")
        self.assertEqual(again["ocr"]["lines"], body["ocr"]["lines"])
        self.assertEqual(again["ocr"]["ocr_ms"], 1234)
        self.assertEqual(again["file"]["name"], "second.png")
        self.assertEqual(body["file"]["name"], "first.png")
        self.assertGreaterEqual(again["ocr"]["total_ms"], 0)
        self.assertEqual(other.json()["cache"]["hit"], False)

        stored = self.main.cache.get(content_key(data))
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertNotIn("total_ms", stored.ocr)
        self.assertNotIn("name", stored.invoice)

    def test_expired_entry_is_parsed_again(self) -> None:
        self.main.cache = ParseCache(ttl_hours=1, clock=lambda: self.now[0])
        data = b"invoice-bytes"
        with self.TestClient(self.main.app) as client:
            first = self._post(client, "a.png", data)
            self.now[0] += 3600
            second = self._post(client, "a.png", data)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertFalse(first.json()["cache"]["hit"])
        self.assertFalse(second.json()["cache"]["hit"])
        self.assertEqual(self.engine.calls, 2)

    def test_disabled_cache_parses_every_upload(self) -> None:
        self.main.cache = ParseCache(ttl_hours=0)
        with self.TestClient(self.main.app) as client:
            self.assertEqual(client.get("/health").json()["cache_ttl_hours"], 0)
            first = self._post(client, "a.png", b"invoice-bytes")
            second = self._post(client, "a.png", b"invoice-bytes")
        self.assertEqual(first.json()["cache"], {"hit": False, "ttl_hours": 0})
        self.assertEqual(second.json()["cache"], {"hit": False, "ttl_hours": 0})
        self.assertEqual(self.engine.calls, 2)
        self.assertEqual(self.main.cache._entries, {})

    def test_failures_are_not_cached(self) -> None:
        with self.TestClient(self.main.app) as client:
            empty = self._post(client, "a.png", b"")
            unsupported = self._post(client, "a.txt", b"hello", "text/plain")
            broken = self._post(client, "a.png", b"bad-image")
            broken_again = self._post(client, "a.png", b"bad-image")
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(unsupported.status_code, 415)
        self.assertEqual(broken.status_code, 400)
        self.assertEqual(broken_again.status_code, 400)
        self.assertEqual(self.engine.calls, 2)
        self.assertEqual(self.main.cache._entries, {})

    def test_background_purge_removes_other_expired_entries_after_the_response(self) -> None:
        stale_key = content_key(b"old-file")
        with self.TestClient(self.main.app) as client:
            self.now[0] -= 24 * 3600
            self.main.cache.put(stale_key, invoice={"n": "old"}, sources={}, ocr={})
            self.now[0] += 24 * 3600
            held = self._post(client, "a.png", b"invoice-bytes")
            self.assertEqual(held.status_code, 200, held.text)
            self.assertIn(stale_key, self.main.cache._entries)

            self.now[0] += 60
            swept = self._post(client, "b.png", b"other-bytes")
            self.assertEqual(swept.status_code, 200, swept.text)
            self.assertNotIn(stale_key, self.main.cache._entries)
            self.assertIsNotNone(self.main.cache.get(content_key(b"other-bytes")))

    def test_fractional_ttl_is_reported_as_given(self) -> None:
        self.main.cache = ParseCache(ttl_hours=1.5, clock=lambda: self.now[0])
        with self.TestClient(self.main.app) as client:
            response = self._post(client, "a.png", b"invoice-bytes")
        self.assertEqual(response.json()["cache"]["ttl_hours"], 1.5)
        self.assertIsInstance(response.json()["cache"]["ttl_hours"], float)


if __name__ == "__main__":
    unittest.main()

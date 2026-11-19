"""Parse-cache lifetime (hours, default one day) and the /api/parse lookup."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.cache import ParseCache, content_key, settings_fingerprint
from app.config import (
    DEFAULT_PARSE_CACHE_TTL_HOURS,
    display_ttl_hours,
    parse_cache_directory,
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

    def test_directory_default_and_override(self) -> None:
        with mock.patch.dict(os.environ, {"PARSE_CACHE_DIR": ""}, clear=False):
            with mock.patch("app.config.Path.home", return_value=Path("/tmp/cache-home")):
                self.assertEqual(parse_cache_directory(None), Path("/tmp/cache-home/.cache/invoice-ocr"))
                self.assertEqual(parse_cache_directory(""), Path("/tmp/cache-home/.cache/invoice-ocr"))
        self.assertEqual(parse_cache_directory("  /var/cache/ocr  "), Path("/var/cache/ocr"))
        self.assertEqual(parse_cache_directory("  ~/invoices  "), Path("~/invoices").expanduser())


class ParseCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = [1_000_000.0]
        self.cache = ParseCache(
            self.tmp.name,
            ttl_hours=24,
            fingerprint="test-fingerprint",
            clock=lambda: self.now[0],
        )

    def test_round_trip_does_not_create_the_directory_until_a_store(self) -> None:
        missing = Path(self.tmp.name) / "absent"
        cache = ParseCache(missing, ttl_hours=24, fingerprint="test-fingerprint", clock=lambda: self.now[0])
        self.assertFalse(missing.exists())
        self.assertIsNone(cache.get(content_key(b"missing")))
        self.assertFalse(missing.exists())

    def test_put_get_and_a_new_instance_see_the_same_entry(self) -> None:
        key = content_key("счёт".encode())
        invoice = {"vendor_name": "ООО Ромашка", "amount": "10.00"}
        directory = Path(self.tmp.name) / "nested"
        cache = ParseCache(directory, ttl_hours=24, fingerprint="test-fingerprint", clock=lambda: self.now[0])
        stored = cache.put(key, invoice=invoice, sources={"amount": "Итого 10"}, ocr={"lines": ["Итого 10"]})
        self.assertTrue(stored)
        path = directory / f"{key}.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["fingerprint"], "test-fingerprint")
        self.assertNotIn("total_ms", payload["ocr"])
        self.assertIn("Ромашка", path.read_text(encoding="utf-8"))

        got = cache.get(key)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got.invoice, invoice)
        self.assertEqual(got.stored_at, 1_000_000.0)

        revived = ParseCache(directory, ttl_hours=24, fingerprint="test-fingerprint", clock=lambda: self.now[0])
        again = revived.get(key)
        self.assertIsNotNone(again)
        assert again is not None
        self.assertEqual(again.invoice, invoice)

    def test_lifetime_is_absolute_and_expires_after_the_ttl(self) -> None:
        key = content_key(b"invoice")
        self.cache.put(key, invoice={"amount": "1.00"}, sources={}, ocr={"ocr_ms": 5})
        self.now[0] += 10
        got = self.cache.get(key)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(self.cache.age_seconds(got.stored_at), 10)
        stored_at = json.loads((Path(self.tmp.name) / f"{key}.json").read_text())["stored_at"]
        self.assertEqual(stored_at, 1_000_000.0)

        self.now[0] = 1_000_000.0 + 24 * 3600
        self.assertIsNone(self.cache.get(key))
        self.assertFalse((Path(self.tmp.name) / f"{key}.json").exists())

    def test_zero_ttl_disables_the_cache(self) -> None:
        cache = ParseCache(Path(self.tmp.name) / "off", ttl_hours=0, fingerprint="test-fingerprint")
        key = content_key(b"invoice")
        self.assertFalse(cache.enabled)
        self.assertFalse(cache.put(key, invoice={}, sources={}, ocr={}))
        self.assertIsNone(cache.get(key))
        self.assertFalse((Path(self.tmp.name) / "off").exists())

    def test_one_hour_boundary(self) -> None:
        cache = ParseCache(self.tmp.name, ttl_hours=1, fingerprint="test-fingerprint", clock=lambda: self.now[0])
        key = content_key(b"hour")
        cache.put(key, invoice={"n": "1"}, sources={}, ocr={})
        self.now[0] += 3599
        self.assertIsNotNone(cache.get(key))
        self.now[0] += 1
        self.assertIsNone(cache.get(key))

    def test_other_fingerprint_is_a_miss_and_drops_the_file(self) -> None:
        key = content_key(b"invoice")
        self.cache.put(key, invoice={"n": "1"}, sources={}, ocr={})
        other = ParseCache(self.tmp.name, ttl_hours=24, fingerprint="other", clock=lambda: self.now[0])
        self.assertIsNone(other.get(key))
        self.assertFalse((Path(self.tmp.name) / f"{key}.json").exists())

    def test_corrupt_entry_is_a_miss_and_removed(self) -> None:
        key = content_key(b"invoice")
        path = Path(self.tmp.name) / f"{key}.json"
        path.write_text("{", encoding="utf-8")
        self.assertIsNone(self.cache.get(key))
        self.assertFalse(path.exists())

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
            ParseCache(self.tmp.name, ttl_hours=-1, fingerprint="x")

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

    def test_settings_fingerprint_is_stable(self) -> None:
        self.assertEqual(settings_fingerprint(), settings_fingerprint())
        self.assertEqual(len(settings_fingerprint()), 64)


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

    def test_server_reads_ttl_and_directory_without_creating_it(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "not-created"
        env = os.environ.copy()
        env["PARSE_CACHE_TTL_HOURS"] = "6"
        env["PARSE_CACHE_DIR"] = str(directory)
        out = subprocess.check_output(
            [
                sys.executable,
                "-c",
                "from app.main import cache\n"
                "print(cache.ttl_hours)\n"
                "print(cache.directory)\n"
                "print(int(cache.directory.exists()))",
            ],
            cwd=ROOT,
            env=env,
            text=True,
        )
        self.assertEqual(out.splitlines(), ["6.0", str(directory), "0"])

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
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = [1_700_000_000.0]
        self.engine = FakeEngine()
        self.original = (main.engine, main.cache, main.PRELOAD_MODEL)
        main.engine = self.engine
        main.PRELOAD_MODEL = False
        main.cache = ParseCache(
            self.tmp.name,
            ttl_hours=24,
            fingerprint="api",
            clock=lambda: self.now[0],
        )
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

        stored = json.loads((Path(self.tmp.name) / f"{content_key(data)}.json").read_text(encoding="utf-8"))
        self.assertNotIn("total_ms", stored["ocr"])
        self.assertNotIn("name", stored)

    def test_expired_entry_is_parsed_again(self) -> None:
        self.main.cache = ParseCache(self.tmp.name, ttl_hours=1, fingerprint="api", clock=lambda: self.now[0])
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
        self.main.cache = ParseCache(Path(self.tmp.name) / "off", ttl_hours=0, fingerprint="api")
        with self.TestClient(self.main.app) as client:
            self.assertEqual(client.get("/health").json()["cache_ttl_hours"], 0)
            first = self._post(client, "a.png", b"invoice-bytes")
            second = self._post(client, "a.png", b"invoice-bytes")
        self.assertEqual(first.json()["cache"], {"hit": False, "ttl_hours": 0})
        self.assertEqual(second.json()["cache"], {"hit": False, "ttl_hours": 0})
        self.assertEqual(self.engine.calls, 2)
        self.assertFalse((Path(self.tmp.name) / "off").exists())

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
        self.assertEqual(list(Path(self.tmp.name).glob("*.json")), [])

    def test_fractional_ttl_is_reported_as_given(self) -> None:
        self.main.cache = ParseCache(self.tmp.name, ttl_hours=1.5, fingerprint="api", clock=lambda: self.now[0])
        with self.TestClient(self.main.app) as client:
            response = self._post(client, "a.png", b"invoice-bytes")
        self.assertEqual(response.json()["cache"]["ttl_hours"], 1.5)
        self.assertIsInstance(response.json()["cache"]["ttl_hours"], float)


if __name__ == "__main__":
    unittest.main()

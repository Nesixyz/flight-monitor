"""Regression tests for alert accuracy, disappearing flights, and URL safety."""
import unittest
from unittest.mock import patch
import monitor


def flight(key, date="2026-12-20"):
    return {
        "dedupe_key": key,
        "direction": "outbound",
        "dep_date": date,
        "price": 5200,
        "seat_category": "economy",
    }


class PricingTests(unittest.TestCase):
    def test_unavailable_tax_price_is_not_accepted(self):
        f = {"flight_numbers": "TEST1", "base_price": 4800, "jump_url": "https://m.tb.cn/abc"}
        with patch.object(monitor, "_fetch_total_price", return_value=None):
            self.assertFalse(monitor.apply_total_price(f))
        self.assertFalse(f["price_verified"])

    def test_unreasonable_tax_price_is_not_accepted(self):
        f = {"flight_numbers": "TEST2", "base_price": 4800, "jump_url": "https://m.tb.cn/abc"}
        with patch.object(monitor, "_fetch_total_price", return_value=30000):
            self.assertFalse(monitor.apply_total_price(f))

    def test_verified_price_is_used(self):
        f = {"flight_numbers": "TEST3", "base_price": 4800, "jump_url": "https://m.tb.cn/abc", "direction": "outbound"}
        with patch.object(monitor, "_fetch_total_price", return_value=6000):
            self.assertTrue(monitor.apply_total_price(f))
        self.assertEqual(f["price"], 6000)
        self.assertTrue(f["price_verified"])

    def test_untrusted_url_never_reaches_network(self):
        with patch("http.client.HTTPSConnection") as conn:
            for url in ("http://m.tb.cn/test", "https://evil.example/test",
                        "https://m.tb.cn.evil.example/test", "https://127.0.0.1/test",
                        "https://m.tb.cn:444/test", "https://user@m.tb.cn/test"):
                with self.subTest(url=url):
                    self.assertIsNone(monitor._fetch_total_price(url))
            conn.assert_not_called()


class DiffTests(unittest.TestCase):
    def setUp(self):
        self.prev = {
            "outbound|economy|TEST|2026-12-20 10:00": {
                "price": 5500, "dep_date": "2026-12-20",
                "direction": "outbound", "seat_category": "economy", "miss_count": 1
            }
        }

    def test_unqueried_old_date_does_not_disappear(self):
        _, _, gone = monitor.diff_flights([], self.prev, successful_queries=set())
        self.assertEqual(gone, [])
        self.assertEqual(next(iter(self.prev.values()))["miss_count"], 1)

    def test_api_failed_date_does_not_disappear(self):
        _, _, gone = monitor.diff_flights([], self.prev,
            unqueried={"outbound|2026-12-20"},
            successful_queries={"outbound|2026-12-20"})
        self.assertEqual(gone, [])

    def test_successful_empty_query_marks_disappearance(self):
        _, _, gone = monitor.diff_flights([], self.prev,
            successful_queries={"outbound|2026-12-20"})
        self.assertEqual(len(gone), 1)


if __name__ == "__main__":
    unittest.main()

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("export_static", ROOT / "build" / "export_static.py")
EXPORT_STATIC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPORT_STATIC)


class CollectSymbolsTests(unittest.TestCase):
    def test_watchlist_charts_are_canonical_and_deduplicated(self):
        positions, charts = EXPORT_STATIC.collect_symbols(
            {
                "positions": [{"symbol": "sh600519"}],
                "markets": {"indices": [{"symbol": "sh000001"}]},
                "watchlist": [
                    {"symbol": "sh600519"},
                    {"symbol": "sz000001"},
                    {"thscode": "601899.SH"},
                    {"symbol": "sh601899"},
                ],
            }
        )

        self.assertEqual(positions, ["600519.SH"])
        self.assertEqual(charts, ["sh000001", "000001.SZ", "601899.SH"])

    def test_malformed_watchlist_rows_never_create_export_keys(self):
        positions, charts = EXPORT_STATIC.collect_symbols(
            {
                "watchlist": [
                    None,
                    {},
                    {"symbol": "bad"},
                    {"symbol": "sh123"},
                    {"thscode": "601899.SH.extra"},
                    {"symbol": "bj920001"},
                ]
            }
        )

        self.assertEqual(positions, [])
        self.assertEqual(charts, ["920001.BJ"])

    def test_non_watchlist_chart_contract_is_preserved(self):
        positions, charts = EXPORT_STATIC.collect_symbols(
            {
                "positions": [{"symbol": "sz000001"}],
                "markets": {
                    "indices": [{"symbol": "sh000001"}],
                    "global": [{"symbol": "usNVDA"}],
                    "overnight": [{"symbol": "b_SAMSUNG"}],
                },
            }
        )

        self.assertEqual(positions, ["000001.SZ"])
        self.assertEqual(charts, ["sh000001", "usNVDA", "b_SAMSUNG"])


if __name__ == "__main__":
    unittest.main()

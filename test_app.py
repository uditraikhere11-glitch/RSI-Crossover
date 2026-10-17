import tempfile
import unittest
from pathlib import Path

import app


class ChartinkAppTests(unittest.TestCase):
    def test_chart_url_keeps_snapshot_and_encodes_symbol(self):
        self.assertIn("relative-strength-index-chart-snapshot-2139", app.build_chartink_url("gail"))
        self.assertTrue(app.build_chartink_url("gail").endswith("&symbol=GAIL"))
        self.assertTrue(app.build_chartink_url("M&M").endswith("&symbol=M%26M"))

    def test_original_strategy_fragments_remain_present(self):
        for fragment in (
            "daily rsi( 50 ) >",
            "weekly rsi( 50 ) >",
            "market cap > 2500",
            "daily close > 30",
            "daily max( 250 , daily high ) * 0.75",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, app.SCAN_CLAUSE)

    def test_state_round_trip(self):
        original_path = app.STATE_FILE
        try:
            with tempfile.TemporaryDirectory() as directory:
                app.STATE_FILE = Path(directory) / "state.json"
                app.save_state({"GAIL", "INFY"})
                stocks, initialized = app.load_state()
                self.assertEqual(stocks, {"GAIL", "INFY"})
                self.assertTrue(initialized)
        finally:
            app.STATE_FILE = original_path


if __name__ == "__main__":
    unittest.main()

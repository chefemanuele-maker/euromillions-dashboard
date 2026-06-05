import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import pandas as pd
    import app as app_module
    import euromillions_live_dashboard as euro
except ModuleNotFoundError as exc:  # Allows stdlib discovery on machines before pip install -r requirements.txt.
    raise unittest.SkipTest(f"Missing runtime dependency: {exc.name}")


class EuroMillionsCoreTests(unittest.TestCase):
    def setUp(self):
        self._original_paths = (
            euro.LOCAL_HISTORY,
            euro.BASELINE_HISTORY,
            euro.USER_ORIGINAL,
            euro.REFRESH_STATE_FILE,
            euro.DASHBOARD_CACHE,
            app_module.euro.LOCAL_HISTORY,
            app_module.euro.BASELINE_HISTORY,
            app_module.euro.USER_ORIGINAL,
            app_module.euro.REFRESH_STATE_FILE,
            app_module.euro.DASHBOARD_CACHE,
        )

    def tearDown(self):
        (
            euro.LOCAL_HISTORY,
            euro.BASELINE_HISTORY,
            euro.USER_ORIGINAL,
            euro.REFRESH_STATE_FILE,
            euro.DASHBOARD_CACHE,
            app_module.euro.LOCAL_HISTORY,
            app_module.euro.BASELINE_HISTORY,
            app_module.euro.USER_ORIGINAL,
            app_module.euro.REFRESH_STATE_FILE,
            app_module.euro.DASHBOARD_CACHE,
        ) = self._original_paths

    def configure_temp_runtime(self, temp_dir: Path) -> pd.DataFrame:
        fixture = Path(__file__).with_name("euromillions_export_2026-03-16.csv")
        history = euro.standardize_columns(pd.read_csv(fixture)).head(30)
        official = euro.standardize_columns(pd.read_csv(fixture)).tail(1)

        local_history = temp_dir / "euromillions_history_live.csv"
        baseline_history = temp_dir / "euromillions_export_2026-06-02.csv"
        user_original = temp_dir / "euromillions_export_2026-03-16.csv"
        state_file = temp_dir / "euromillions_refresh_state.json"
        cache_file = temp_dir / "euromillions_dashboard_payload.json"
        history.to_csv(local_history, index=False)
        history.to_csv(baseline_history, index=False)
        history.to_csv(user_original, index=False)

        euro.LOCAL_HISTORY = app_module.euro.LOCAL_HISTORY = local_history
        euro.BASELINE_HISTORY = app_module.euro.BASELINE_HISTORY = baseline_history
        euro.USER_ORIGINAL = app_module.euro.USER_ORIGINAL = user_original
        euro.REFRESH_STATE_FILE = app_module.euro.REFRESH_STATE_FILE = state_file
        euro.DASHBOARD_CACHE = app_module.euro.DASHBOARD_CACHE = cache_file
        return official

    def test_total_combinations_is_exact(self):
        self.assertEqual(euro.TOTAL_COMBINATIONS, math.comb(50, 5) * math.comb(12, 2))
        self.assertEqual(euro.TOTAL_COMBINATIONS, 139_838_160)

    def test_prize_tier_math_and_any_prize_odds(self):
        self.assertEqual(euro.prize_tier_ways(5, 2), 1)
        self.assertEqual(round(euro.TOTAL_COMBINATIONS / euro.prize_tier_ways(5, 1)), 6_991_908)
        any_prize_odds = 1 / euro.exact_any_prize_probability()
        self.assertGreater(any_prize_odds, 12.9)
        self.assertLess(any_prize_odds, 13.1)

    def test_pack_odds_scale_without_changing_single_line_truth(self):
        one = euro.pack_jackpot_probability(1)
        five = euro.pack_jackpot_probability(5)
        self.assertEqual(one["jackpot_odds_text"], "1 in 139,838,160")
        self.assertEqual(one["any_prize_single_line_odds_text"], five["any_prize_single_line_odds_text"])
        self.assertGreater(five["jackpot_probability_pct"], one["jackpot_probability_pct"])
        self.assertIn("Every valid EuroMillions line", one["truth"])

    def test_standardize_normalizes_unsorted_draw_numbers(self):
        df = pd.DataFrame([{
            "draw_date": "2026-01-02",
            "ball_1": 50, "ball_2": 1, "ball_3": 25, "ball_4": 7, "ball_5": 3,
            "lucky_star_1": 12, "lucky_star_2": 2,
        }])
        out = euro.standardize_columns(df)
        self.assertEqual([int(out.iloc[0][f"ball_{i}"]) for i in range(1, 6)], [1, 3, 7, 25, 50])
        self.assertEqual([int(out.iloc[0]["lucky_star_1"]), int(out.iloc[0]["lucky_star_2"])], [2, 12])

    def test_generated_pack_lines_are_valid(self):
        path = Path(__file__).with_name("euromillions_export_2026-03-16.csv")
        hist = euro.enrich_history(euro.standardize_columns(pd.read_csv(path)))
        pack = euro.generate_premium_line_pack(hist, total_lines=5)
        self.assertEqual(len(pack), 5)
        for _, row in pack.iterrows():
            balls = euro.parse_line_numbers(row["balls"])
            stars = euro.parse_line_numbers(row["stars"])
            self.assertEqual(len(balls), 5)
            self.assertEqual(len(set(balls)), 5)
            self.assertTrue(all(1 <= n <= 50 for n in balls))
            self.assertEqual(len(stars), 2)
            self.assertEqual(len(set(stars)), 2)
            self.assertTrue(all(1 <= n <= 12 for n in stars))
        self.assertLessEqual(euro.line_pack_diversity_report(pack)["max_pair_overlap"], 3)

    def test_public_dashboard_payload_skips_backfill_when_cache_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            official = self.configure_temp_runtime(Path(tmp))
            with (
                mock.patch.object(euro, "fetch_official_xml", return_value=official),
                mock.patch.object(euro, "fetch_missing_backfill") as backfill,
            ):
                payload = euro.build_dashboard_payload(premium_line_count=5)

            backfill.assert_not_called()
            self.assertFalse(payload["cache_used"])
            self.assertEqual(payload["refresh"]["source"], "official_xml_quick")

    def test_cold_deploy_baseline_has_complete_recent_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            baseline = Path(__file__).with_name("euromillions_export_2026-06-02.csv")
            temp_baseline = temp_dir / baseline.name
            temp_baseline.write_bytes(baseline.read_bytes())

            euro.LOCAL_HISTORY = app_module.euro.LOCAL_HISTORY = temp_dir / "euromillions_history_live.csv"
            euro.BASELINE_HISTORY = app_module.euro.BASELINE_HISTORY = temp_baseline
            euro.USER_ORIGINAL = app_module.euro.USER_ORIGINAL = temp_dir / "euromillions_export_2026-03-16.csv"
            euro.REFRESH_STATE_FILE = app_module.euro.REFRESH_STATE_FILE = temp_dir / "euromillions_refresh_state.json"
            euro.DASHBOARD_CACHE = app_module.euro.DASHBOARD_CACHE = temp_dir / "euromillions_dashboard_payload.json"

            df = euro.load_local_history()
            quality = euro.history_quality_report(df)

            self.assertEqual(len(df), 1951)
            self.assertEqual(str(pd.to_datetime(df["draw_date"]).dt.date.max()), "2026-06-02")
            self.assertTrue(quality["ok"])
            self.assertEqual(quality["missing_recent_count"], 0)

    def test_public_routes_skip_backfill_when_cache_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            official = self.configure_temp_runtime(Path(tmp))
            with (
                mock.patch.object(euro, "fetch_official_xml", return_value=official),
                mock.patch.object(euro, "fetch_missing_backfill") as backfill,
            ):
                client = app_module.app.test_client()
                dashboard = client.get("/euromillions")
                suggested = client.get("/api/suggested?lines=5")

            backfill.assert_not_called()
            self.assertEqual(dashboard.status_code, 200)
            self.assertEqual(suggested.status_code, 200)
            self.assertEqual(suggested.get_json()["refresh"]["source"], "official_xml_quick")

    def test_admin_refresh_supports_get_post_and_can_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            official = self.configure_temp_runtime(Path(tmp))
            app_module.app.config["TESTING"] = True
            client = app_module.app.test_client()

            self.assertEqual(client.get("/admin/refresh").status_code, 403)
            self.assertEqual(client.post("/admin/refresh").status_code, 403)

            with (
                mock.patch.dict("os.environ", {"ADMIN_REFRESH_TOKEN": "test-secret"}),
                mock.patch.object(euro, "fetch_official_xml", return_value=official),
                mock.patch.object(euro, "fetch_missing_backfill", return_value=(pd.DataFrame(), 0, [])) as backfill,
            ):
                get_response = client.get("/admin/refresh", headers={"X-Admin-Token": "test-secret"})
                post_response = client.post("/admin/refresh", headers={"X-Admin-Token": "test-secret"})

            self.assertEqual(get_response.status_code, 200)
            self.assertEqual(post_response.status_code, 200)
            self.assertGreaterEqual(backfill.call_count, 2)


if __name__ == "__main__":
    unittest.main()

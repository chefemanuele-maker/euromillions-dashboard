import math
import unittest
from pathlib import Path

try:
    import pandas as pd
    import euromillions_live_dashboard as euro
except ModuleNotFoundError as exc:  # Allows stdlib discovery on machines before pip install -r requirements.txt.
    raise unittest.SkipTest(f"Missing runtime dependency: {exc.name}")


class EuroMillionsCoreTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

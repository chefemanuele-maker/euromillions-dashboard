#!/usr/bin/env python3
"""Lightweight checks for the EuroMillions probability/value engine."""

import math
import unittest

try:
    import euromillions_live_dashboard as euro
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(f"Missing runtime dependency: {exc.name}")


def test_total_combinations():
    assert euro.TOTAL_COMBINATIONS == math.comb(50, 5) * math.comb(12, 2)
    assert euro.TOTAL_COMBINATIONS == 139_838_160


def test_popularity_risk_penalises_common_patterns():
    obvious = euro.popularity_risk_score([1, 2, 3, 4, 5], [1, 2])
    better = euro.popularity_risk_score([8, 19, 32, 41, 50], [7, 12])
    assert obvious > better
    assert obvious >= 80


def test_pack_odds_and_budget():
    odds = euro.pack_jackpot_probability(5)
    strategy = euro.budget_strategy(5)
    assert odds["lines"] == 5
    assert odds["estimated_cost_gbp"] == 12.5
    assert strategy["cost_per_draw_gbp"] == 12.5
    assert "negative" in odds["expected_loss_warning"]


if __name__ == "__main__":
    test_total_combinations()
    test_popularity_risk_penalises_common_patterns()
    test_pack_odds_and_budget()
    print("engine core checks OK")

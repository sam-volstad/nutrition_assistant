"""Focused deterministic checks for prototype recommendation priority."""

from math import isclose
from types import SimpleNamespace

import pandas as pd

from nutrition_assistant.planner.optimizer import (
    FOUNDATIONAL_BENEFIT_MULTIPLIER,
    PREFERENCE_BONUSES,
    UPPER_LIMIT_PENALTY,
    rank_meals,
    recommendation_explanation,
    score_candidate_meal,
)


def current_score(foundational_progress: float) -> pd.DataFrame:
    rows = [
        (1008, "Energy", "kcal", 2000.0, None, 2000.0, None),
        (1003, "Protein", "g", 50.0, 50.0, 50.0, None),
        (1079, "Fiber", "g", 30.0, 30.0, 30.0, None),
        (1162, "Vitamin C", "mg", 100.0, 100.0, 100.0, 2000.0),
        (1093, "Sodium", "mg", 2300.0, None, 1500.0, 2300.0),
        (9999, "Test upper limit", "mg", 50.0, None, None, 100.0),
    ]
    return pd.DataFrame([
        {
            "nutrient_id": nutrient_id,
            "name": name,
            "unit_name": unit,
            "amount_consumed": reference * foundational_progress
            if nutrient_id in {1008, 1003, 1079}
            else (90.0 if nutrient_id == 9999 else 0.0),
            "minimum_amount": minimum,
            "target_amount": target,
            "maximum_amount": maximum,
            "reported": True,
            "coverage_ratio": 1.0,
            "coverage_state": "complete",
        }
        for nutrient_id, name, unit, reference, minimum, target, maximum in rows
    ])


def candidate(**amounts: float) -> pd.DataFrame:
    nutrient_ids = {
        "energy": 1008,
        "protein": 1003,
        "fiber": 1079,
        "vitamin_c": 1162,
        "sodium": 1093,
        "upper_limit": 9999,
    }
    return pd.DataFrame([
        {
            "nutrient_id": nutrient_ids[name],
            "amount_consumed": amount,
            "coverage_ratio": 1.0,
            "coverage_state": "complete",
        }
        for name, amount in amounts.items()
    ])


def main() -> None:
    low = current_score(0.25)
    substantial = score_candidate_meal(low, candidate(energy=500.0))
    micronutrient = score_candidate_meal(low, candidate(vitamin_c=35.0))
    assert substantial["base_benefit_score"] == 0.25
    assert substantial["benefit_score"] == 0.50
    assert substantial["score"] > micronutrient["score"]

    fiber = score_candidate_meal(low, candidate(fiber=6.0))
    fiber_detail = fiber["details"].loc[
        fiber["details"]["nutrient_id"] == 1079
    ].iloc[0]
    assert fiber_detail["foundational_multiplier"] == FOUNDATIONAL_BENEFIT_MULTIPLIER
    assert fiber_detail["weighted_benefit"] == 0.4

    explanation = score_candidate_meal(
        low,
        candidate(fiber=9.0, energy=400.0, vitamin_c=50.0),
    )
    assert explanation["major_nutrients_helped"] == [
        "Fiber", "Energy", "Vitamin C"
    ]
    assert [
        detail["name"] for detail in explanation["explanation_nutrient_details"][:3]
    ] == explanation["major_nutrients_helped"]
    assert recommendation_explanation(explanation)[0] == (
        "Helps: Fiber, Energy, Vitamin C"
    )
    protein_explanation = score_candidate_meal(
        low,
        candidate(protein=10.0, energy=100.0, vitamin_c=50.0),
    )
    assert protein_explanation["major_nutrients_helped"] == [
        "Protein", "Energy", "Vitamin C"
    ]

    covered = current_score(0.90)
    assert score_candidate_meal(
        covered, candidate(vitamin_c=35.0)
    )["score"] > score_candidate_meal(covered, candidate(energy=500.0))["score"]
    assert micronutrient["benefit_score"] == 0.35
    assert score_candidate_meal(low, candidate(sodium=500.0))["benefit_score"] == 0

    over_limit = score_candidate_meal(low, candidate(upper_limit=20.0))
    assert over_limit["penalty_score"] >= UPPER_LIMIT_PENALTY
    assert over_limit["score"] + PREFERENCE_BONUSES["preferred"] < 0

    class Engine:
        def calculate_meal(self, meal):
            return candidate(energy=500.0 if meal.name == "food" else 0.0)

    meals = [SimpleNamespace(name="food")]
    neutral = rank_meals(low, meals, Engine(), meal_ids=[1])[0]
    preferred = rank_meals(
        low, meals, Engine(), meal_ids=[1], preferences={1: "preferred"}
    )[0]
    assert isclose(
        preferred["score"] - neutral["score"],
        PREFERENCE_BONUSES["preferred"],
    )
    print("Recommendation priority checks passed")


if __name__ == "__main__":
    main()

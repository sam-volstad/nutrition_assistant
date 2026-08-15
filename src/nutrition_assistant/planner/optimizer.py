import pandas as pd


def score_candidate_meal(current_score, candidate_nutrients):
    """
    Score a candidate meal by how much it closes known nutrient gaps.

    Only nutrients with a defined minimum are rewarded.
    Missing nutrient data is never treated as zero.
    """

    gaps = current_score[
        current_score["reported"]
        & current_score["minimum_amount"].notna()
        & (current_score["remaining_to_target"] > 0)
    ][
        [
            "nutrient_id",
            "name",
            "target_amount",
            "remaining_to_target",
        ]
    ]

    candidate = candidate_nutrients[
        ["nutrient_id", "amount_consumed"]
    ].rename(
        columns={"amount_consumed": "candidate_amount"}
    )

    comparison = gaps.merge(
        candidate,
        on="nutrient_id",
        how="inner"
    )

    if comparison.empty:
        return {
            "score": 0.0,
            "nutrients_helped": 0,
            "details": comparison,
        }

    # Don't give credit for more than the amount actually needed.
    comparison["gap_filled"] = comparison[
        ["candidate_amount", "remaining_to_target"]
    ].min(axis=1)

    # Normalize nutrients so 100 mg calcium doesn't automatically
    # outweigh 1 mg of another nutrient just because the units are larger.
    comparison["benefit"] = (
        comparison["gap_filled"]
        / comparison["target_amount"]
    )

    return {
        "score": comparison["benefit"].sum(),
        "nutrients_helped": (comparison["gap_filled"] > 0).sum(),
        "details": comparison,
    }

def rank_meals(current_score, meals, nutrition_engine):
    results = []

    for meal in meals:
        nutrients = nutrition_engine.calculate_meal(meal)

        candidate_score = score_candidate_meal(
            current_score,
            nutrients
        )

        results.append({
            "meal": meal,
            "meal_name": meal.name,
            "score": candidate_score["score"],
            "nutrients_helped": candidate_score["nutrients_helped"],
            "details": candidate_score["details"],
        })

    return sorted(
        results,
        key=lambda result: result["score"],
        reverse=True
    )

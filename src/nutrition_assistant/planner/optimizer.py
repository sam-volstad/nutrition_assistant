import pandas as pd


ACCEPTABLE_PROGRESS = 0.90
UPPER_LIMIT_PENALTY = 100.0
NON_BENEFICIAL_TARGET_IDS = {1093}  # Sodium should not be rewarded as a gap.
PREFERENCE_BONUSES = {
    "neutral": 0.0,
    "acceptable": 0.05,
    "preferred": 0.10,
}
EXCLUDED_PREFERENCES = {"avoid", "never"}


def score_candidate_meal(current_score, candidate_nutrients):
    """Score known benefits and upper-limit conflicts for one candidate meal."""
    current_source = current_score.copy()
    if "coverage_state" not in current_source:
        current_source["coverage_state"] = current_source["reported"].map(
            {True: "complete", False: "unknown"}
        )
        current_source["coverage_ratio"] = current_source["reported"].astype(float)
    current = current_source[
        [
            "nutrient_id", "name", "unit_name", "amount_consumed",
            "minimum_amount", "target_amount", "maximum_amount", "reported",
            "coverage_ratio", "coverage_state",
        ]
    ].rename(columns={
        "amount_consumed": "current_amount",
        "coverage_ratio": "current_coverage_ratio",
        "coverage_state": "current_coverage_state",
    })
    candidate_columns = ["nutrient_id", "amount_consumed"]
    for column in ("coverage_ratio", "coverage_state"):
        if column in candidate_nutrients:
            candidate_columns.append(column)
    candidate = candidate_nutrients[candidate_columns].rename(columns={
        "amount_consumed": "candidate_amount",
        "coverage_ratio": "candidate_coverage_ratio",
        "coverage_state": "candidate_coverage_state",
    })
    details = current.merge(candidate, on="nutrient_id", how="left")

    if "candidate_coverage_state" not in details:
        details["candidate_coverage_state"] = details["candidate_amount"].notna().map(
            {True: "complete", False: "unknown"}
        )
        details["candidate_coverage_ratio"] = (
            details["candidate_amount"].notna().astype(float)
        )

    numeric_columns = [
        "current_amount", "minimum_amount", "target_amount",
        "maximum_amount", "candidate_amount",
    ]
    details[numeric_columns] = details[numeric_columns].apply(
        pd.to_numeric, errors="coerce"
    )
    details["reported"] = details["reported"].fillna(False).astype(bool)

    details["candidate_reported"] = details["candidate_amount"].notna()
    details["reference_amount"] = details["target_amount"].combine_first(
        details["minimum_amount"]
    )
    details["acceptable_amount"] = details["reference_amount"] * ACCEPTABLE_PROGRESS
    details["gap_before"] = (
        details["acceptable_amount"] - details["current_amount"]
    ).clip(lower=0)
    details["gap_filled"] = 0.0
    details["benefit"] = 0.0

    benefit_mask = (
        details["reported"]
        & details["candidate_reported"]
        & details["reference_amount"].notna()
        & (details["reference_amount"] > 0)
        & (details["gap_before"] > 0)
        & ~details["nutrient_id"].isin(NON_BENEFICIAL_TARGET_IDS)
    )
    details.loc[benefit_mask, "gap_filled"] = details.loc[
        benefit_mask, ["candidate_amount", "gap_before"]
    ].min(axis=1)
    details.loc[benefit_mask, "benefit"] = (
        details.loc[benefit_mask, "gap_filled"]
        / details.loc[benefit_mask, "reference_amount"]
        * details.loc[benefit_mask, "candidate_coverage_ratio"]
    )
    details["benefit_based_on_partial_data"] = (
        (details["benefit"] > 0)
        & (details["candidate_coverage_state"] == "partial")
    )

    complete_projection = (
        (details["current_coverage_state"] == "complete")
        & (details["candidate_coverage_state"] == "complete")
    )
    details["projected_amount"] = float("nan")
    details.loc[complete_projection, "projected_amount"] = (
        details.loc[complete_projection, "current_amount"]
        + details.loc[complete_projection, "candidate_amount"]
    )
    # This is a lower bound assembled only from reported values, not an
    # assumption that unreported values are zero.
    details["known_subtotal_after"] = (
        details["current_amount"].fillna(0)
        + details["candidate_amount"].fillna(0)
    )
    details["maximum_progress_after"] = (
        details["projected_amount"] / details["maximum_amount"]
    )
    details["upper_limit_excess"] = (
        details["known_subtotal_after"] - details["maximum_amount"]
    ).clip(lower=0)
    details["penalty"] = 0.0

    over_max_mask = (
        details["maximum_amount"].notna()
        & (details["maximum_amount"] > 0)
        & (details["known_subtotal_after"] > details["maximum_amount"])
    )
    if over_max_mask.any():
        details.loc[over_max_mask, "penalty"] = UPPER_LIMIT_PENALTY * (
            1
            + details.loc[over_max_mask, "upper_limit_excess"]
            / details.loc[over_max_mask, "maximum_amount"]
        )

    helped = details[details["benefit"] > 0].sort_values(
        "benefit", ascending=False
    )
    known_limit_warnings = details[
        details["maximum_amount"].notna()
        & (
            (details["known_subtotal_after"] > details["maximum_amount"])
            | (details["maximum_progress_after"] >= ACCEPTABLE_PROGRESS)
            | ~complete_projection
        )
    ].copy()
    known_limit_warnings["limit_status"] = "uncertain_limit"
    known_limit_warnings.loc[
        known_limit_warnings["known_subtotal_after"]
        > known_limit_warnings["maximum_amount"],
        "limit_status",
    ] = "over_max"
    known_limit_warnings.loc[
        (known_limit_warnings["limit_status"] != "over_max")
        & known_limit_warnings["maximum_progress_after"].ge(ACCEPTABLE_PROGRESS),
        "limit_status",
    ] = "approaching_max"

    benefit_score = float(details["benefit"].sum())
    penalty_score = float(details["penalty"].sum())
    return {
        "score": benefit_score - penalty_score,
        "benefit_score": benefit_score,
        "penalty_score": penalty_score,
        "nutrients_helped": int((details["benefit"] > 0).sum()),
        "major_nutrients_helped": helped["name"].head(3).tolist(),
        "partial_benefit_nutrients": helped.loc[
            helped["benefit_based_on_partial_data"], "name"
        ].tolist(),
        "upper_limit_warnings": known_limit_warnings[
            ["name", "unit_name", "projected_amount", "known_subtotal_after",
             "maximum_amount", "limit_status", "penalty"]
        ].to_dict("records"),
        "details": details,
    }


def recommendation_explanation(recommendation):
    helped = recommendation["major_nutrients_helped"]
    benefit_text = (
        "Helps: " + ", ".join(helped)
        if helped
        else "No currently known nutrient gaps helped"
    )
    partial_help = recommendation.get("partial_benefit_nutrients", [])
    if partial_help:
        benefit_text += " (partial data: " + ", ".join(partial_help) + ")"
    if recommendation.get("preference") == "preferred":
        benefit_text = "Preferred meal • " + benefit_text
    elif recommendation.get("preference") == "acceptable":
        benefit_text = "Acceptable meal • " + benefit_text

    warnings = recommendation["upper_limit_warnings"]
    if not warnings:
        warning_text = "No reported upper-limit conflicts"
    else:
        warning_parts = []
        for warning in warnings:
            if warning["limit_status"] == "over_max":
                warning_parts.append(f'{warning["name"]} would exceed its maximum')
            elif warning["limit_status"] == "uncertain_limit":
                warning_parts.append(
                    f'{warning["name"]} upper-limit safety is uncertain '
                    "because coverage is incomplete"
                )
            else:
                warning_parts.append(f'{warning["name"]} would approach its maximum')
        warning_text = "Caution: " + "; ".join(warning_parts)
    return benefit_text, warning_text


def rank_meals(
    current_score,
    meals,
    nutrition_engine,
    meal_ids=None,
    preferences=None,
):
    if meal_ids is None:
        meal_ids = [None] * len(meals)
    if len(meal_ids) != len(meals):
        raise ValueError("meal_ids must match the number of meals")

    preferences = preferences or {}
    results = []
    for meal_id, meal in zip(meal_ids, meals):
        preference = preferences.get(meal_id, "neutral")
        if preference in EXCLUDED_PREFERENCES:
            continue
        if preference not in PREFERENCE_BONUSES:
            raise ValueError(f"Unknown meal preference: {preference}")

        nutrients = nutrition_engine.calculate_meal(meal)
        candidate_score = score_candidate_meal(current_score, nutrients)
        nutrition_score = candidate_score["score"]
        preference_bonus = PREFERENCE_BONUSES[preference]
        results.append({
            "meal_id": meal_id,
            "meal": meal,
            "meal_name": meal.name,
            **candidate_score,
            "nutrition_score": nutrition_score,
            "preference": preference,
            "preference_bonus": preference_bonus,
            "score": nutrition_score + preference_bonus,
        })

    return sorted(
        results,
        key=lambda result: (-result["score"], result["meal_name"].lower()),
    )

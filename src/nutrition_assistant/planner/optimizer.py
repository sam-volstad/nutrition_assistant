import re

import pandas as pd


ACCEPTABLE_PROGRESS = 0.90
UPPER_LIMIT_PENALTY = 100.0
NON_BENEFICIAL_TARGET_IDS = {1093}  # Sodium should not be rewarded as a gap.
FOUNDATIONAL_NUTRIENT_IDS = {1008, 1003, 1079}  # Energy, protein, and fiber.
FOUNDATIONAL_BENEFIT_MULTIPLIER = 2.0
PREFERENCE_BONUSES = {
    "neutral": 0.0,
    "acceptable": 0.05,
    "preferred": 0.10,
}
EXCLUDED_PREFERENCES = {"avoid", "never"}
MAX_FOOD_GAP_NUTRIENTS = 5
FOOD_CANDIDATES_PER_NUTRIENT = 12
MAX_FOOD_CANDIDATE_POOL = 40
VISIBLE_FOOD_RECOMMENDATIONS = 5


def foundational_gaps_remain(current_score) -> bool:
    """Whether a reported foundational nutrient remains below 90% of target."""
    foundational = current_score[
        current_score["nutrient_id"].isin(FOUNDATIONAL_NUTRIENT_IDS)
    ].copy()
    foundational["reference_amount"] = foundational["target_amount"].combine_first(
        foundational["minimum_amount"]
    )
    progress = foundational["amount_consumed"] / foundational["reference_amount"]
    return bool((
        foundational["reported"]
        & foundational["reference_amount"].gt(0)
        & progress.lt(ACCEPTABLE_PROGRESS)
    ).any())


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
    details["base_normalized_benefit"] = 0.0

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
    details.loc[benefit_mask, "base_normalized_benefit"] = (
        details.loc[benefit_mask, "gap_filled"]
        / details.loc[benefit_mask, "reference_amount"]
        * details.loc[benefit_mask, "candidate_coverage_ratio"]
    )
    details["foundational_multiplier"] = 1.0
    foundational_gap = (
        details["nutrient_id"].isin(FOUNDATIONAL_NUTRIENT_IDS)
        & details["reported"]
        & details["reference_amount"].notna()
        & (details["reference_amount"] > 0)
        & (
            details["current_amount"] / details["reference_amount"]
            < ACCEPTABLE_PROGRESS
        )
    )
    details.loc[
        foundational_gap, "foundational_multiplier"
    ] = FOUNDATIONAL_BENEFIT_MULTIPLIER
    details["weighted_benefit"] = (
        details["base_normalized_benefit"]
        * details["foundational_multiplier"]
    )
    # Keep the established detail name as the effective benefit consumed by
    # ranking and explanations.
    details["benefit"] = details["weighted_benefit"]
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
    details["known_maximum_progress_after"] = (
        details["known_subtotal_after"] / details["maximum_amount"]
    )
    details["upper_limit_data_complete"] = complete_projection
    details["upper_limit_uncertain"] = (
        details["maximum_amount"].notna() & ~complete_projection
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

    helped = details[details["benefit"] > 0].copy()
    helped["foundational"] = helped["nutrient_id"].isin(
        FOUNDATIONAL_NUTRIENT_IDS
    )
    foundational_helped = helped[helped["foundational"]].sort_values(
        ["weighted_benefit", "nutrient_id"], ascending=[False, True]
    )
    other_helped = helped[~helped["foundational"]].sort_values(
        ["weighted_benefit", "nutrient_id"], ascending=[False, True]
    )
    explanation_helped = pd.concat(
        [foundational_helped, other_helped], ignore_index=True
    )
    known_limit_warnings = details[
        details["maximum_amount"].notna()
        & (
            (details["known_subtotal_after"] > details["maximum_amount"])
            | (
                complete_projection
                & (details["maximum_progress_after"] >= ACCEPTABLE_PROGRESS)
            )
            | (
                ~complete_projection
                & (details["known_maximum_progress_after"] >= 0.80)
            )
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

    base_benefit_score = float(details["base_normalized_benefit"].sum())
    benefit_score = float(details["weighted_benefit"].sum())
    penalty_score = float(details["penalty"].sum())
    return {
        "score": benefit_score - penalty_score,
        "base_benefit_score": base_benefit_score,
        "benefit_score": benefit_score,
        "penalty_score": penalty_score,
        "nutrients_helped": int((details["benefit"] > 0).sum()),
        "major_nutrients_helped": explanation_helped["name"].head(3).tolist(),
        "explanation_nutrient_details": explanation_helped[
            ["nutrient_id", "name", "base_normalized_benefit",
             "foundational_multiplier", "weighted_benefit", "foundational"]
        ].to_dict("records"),
        "partial_benefit_nutrients": explanation_helped.loc[
            explanation_helped["benefit_based_on_partial_data"], "name"
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


def select_food_recommendation_gaps(
    current_score, limit=MAX_FOOD_GAP_NUTRIENTS
):
    """Select the largest known, positively useful gaps for SQL prefiltering."""
    gaps = current_score.copy()
    gaps["reference_amount"] = gaps["target_amount"].combine_first(
        gaps["minimum_amount"]
    )
    gaps["gap_fraction"] = (
        ACCEPTABLE_PROGRESS
        - gaps["amount_consumed"] / gaps["reference_amount"]
    )
    gaps = gaps[
        gaps["reported"]
        & gaps["reference_amount"].notna()
        & (gaps["reference_amount"] > 0)
        & (gaps["gap_fraction"] > 0)
        & ~gaps["nutrient_id"].isin(NON_BENEFICIAL_TARGET_IDS)
    ].sort_values(["gap_fraction", "nutrient_id"], ascending=[False, True])
    return gaps.head(limit)["nutrient_id"].astype(int).tolist()


def rank_food_candidates(
    current_score,
    candidates,
    nutrition_engine,
    preferences=None,
):
    """Score an already bounded set of foods using one deterministic portion."""
    preferences = preferences or {}
    results = []
    for candidate in candidates.itertuples(index=False):
        fdc_id = int(candidate.fdc_id)
        preference = preferences.get(fdc_id, "neutral")
        if preference in EXCLUDED_PREFERENCES:
            continue
        if preference not in PREFERENCE_BONUSES:
            raise ValueError(f"Unknown food preference: {preference}")

        nutrients = nutrition_engine.calculate_food_nutrients(
            fdc_id, float(candidate.gram_weight)
        )
        candidate_score = score_candidate_meal(current_score, nutrients)
        nutrition_score = candidate_score["score"]
        preference_bonus = PREFERENCE_BONUSES[preference]
        results.append({
            "fdc_id": fdc_id,
            "description": candidate.description,
            "data_type": candidate.data_type,
            "brand_name": candidate.brand_name,
            "brand_owner": candidate.brand_owner,
            "candidate_class": getattr(candidate, "candidate_class", "ordinary_food"),
            "classification_reason": getattr(
                candidate, "classification_reason", "not supplied"
            ),
            "reported_nutrient_count": int(candidate.reported_nutrient_count),
            "portion_id": candidate.portion_id,
            "portion_amount": candidate.portion_amount,
            "modifier": candidate.modifier,
            "portion_unit": candidate.portion_unit,
            "assumed_grams": float(candidate.gram_weight),
            **candidate_score,
            "nutrition_score": nutrition_score,
            "preference": preference,
            "preference_bonus": preference_bonus,
            "score": nutrition_score + preference_bonus,
        })

    def normalize_identity(value):
        if value is None:
            return ""
        text = str(value).strip().lower()
        if text in {"", "nan", "<na>"}:
            return ""
        return re.sub(r"[^a-z0-9]+", " ", text).strip()

    def identity_key(result):
        description = normalize_identity(result["description"])
        modifier = normalize_identity(result["modifier"])
        grams = round(result["assumed_grams"], 3)
        if result["data_type"] == "branded_food":
            # Owner is the stable fallback when USDA records omit brand_name.
            owner = normalize_identity(result["brand_owner"])
            brand = normalize_identity(result["brand_name"])
            return ("branded", description, owner or brand, grams, modifier)
        return ("generic", description, grams, modifier)

    # Equivalent USDA releases compete without merging any nutrient records.
    # Score represents nutritional fit; coverage quality and FDC ID break ties.
    deduplicated = {}
    for result in results:
        key = identity_key(result)
        incumbent = deduplicated.get(key)
        priority = (
            result["score"],
            result["reported_nutrient_count"],
            -result["fdc_id"],
        )
        if incumbent is None or priority > (
            incumbent["score"],
            incumbent["reported_nutrient_count"],
            -incumbent["fdc_id"],
        ):
            deduplicated[key] = result

    return sorted(
        deduplicated.values(),
        key=lambda result: (
            -result["score"],
            result["description"].lower(),
            result["fdc_id"],
        ),
    )

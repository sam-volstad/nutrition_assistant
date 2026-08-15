import math

import pandas as pd


class NutritionEngine:
    def __init__(self, food_repository):
        self.food_repository = food_repository

    def calculate_food_nutrients(self, fdc_id, grams):
        if not math.isfinite(grams) or grams <= 0:
            raise ValueError("grams must be a positive finite number")

        nutrients = self.food_repository.get_nutrients(fdc_id).copy()

        if nutrients.empty:
            raise ValueError(f"No nutrient data found for fdc_id {fdc_id}")

        # Missing USDA nutrient rows mean not reported/available, not zero.
        nutrients["amount_consumed"] = (
            nutrients["amount"] * grams / 100.0
        )

        return nutrients

    def calculate_portion_nutrients(
        self,
        fdc_id,
        quantity=1,
        portion_id=None
    ):
        grams = self.resolve_portion_grams(
            fdc_id=fdc_id,
            quantity=quantity,
            portion_id=portion_id,
        )

        return self.calculate_food_nutrients(
            fdc_id=fdc_id,
            grams=grams
        )

    def get_usable_portions(self, fdc_id):
        portions = self.food_repository.get_portions(fdc_id)
        if portions.empty:
            return portions

        return portions[
            portions.apply(self._has_usable_gram_weight, axis=1)
        ].copy()

    def resolve_portion_grams(
        self,
        fdc_id,
        quantity=1,
        portion_id=None,
    ):
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("quantity must be a positive finite number")

        portions = self.food_repository.get_portions(fdc_id)

        if portions.empty:
            raise ValueError(
                f"No portion information found for fdc_id {fdc_id}"
            )

        if portion_id is not None:
            matching = portions[
                portions["portion_id"] == portion_id
            ]

            if matching.empty:
                raise ValueError(
                    f"Portion {portion_id} not found for fdc_id {fdc_id}"
                )

            portion = matching.iloc[0]

            if not self._has_usable_gram_weight(portion):
                raise ValueError(
                    f"Portion {portion_id} has an invalid gram weight"
                )

        else:
            usable = portions[
                portions.apply(self._has_usable_gram_weight, axis=1)
            ]

            if usable.empty:
                raise ValueError(
                    f"No portion with a valid gram weight found for fdc_id {fdc_id}"
                )

            if len(usable) > 1:
                raise ValueError(
                    f"Multiple portions found for fdc_id {fdc_id}; "
                    "provide a portion_id"
                )

            portion = usable.iloc[0]

        return float(portion["gram_weight"] * quantity)

    def calculate_meal(self, meal):
        if not meal.ingredients:
            raise ValueError("Meal contains no ingredients")

        results = []

        for ingredient in meal.ingredients:
            nutrients = self.calculate_food_nutrients(
                ingredient.fdc_id,
                ingredient.grams
            )

            results.append(nutrients)

        meal_totals = (
            pd.concat(results)
            .groupby(
                ["nutrient_id", "name", "unit_name"],
                as_index=False
            )["amount_consumed"]
            .sum(min_count=1)
        )

        return meal_totals

    @staticmethod
    def _has_usable_gram_weight(portion):
        gram_weight = portion["gram_weight"]
        return (
            pd.notna(gram_weight)
            and math.isfinite(gram_weight)
            and gram_weight > 0
        )

    def score_against_targets(self, nutrients, targets):
        scored = targets.merge(
            nutrients[
                ["nutrient_id", "amount_consumed"]
            ],
            on="nutrient_id",
            how="left"
        )

        scored["reported"] = scored["amount_consumed"].notna()

        scored["target_progress"] = (
            scored["amount_consumed"]
            / scored["target_amount"]
        )

        scored["minimum_progress"] = (
            scored["amount_consumed"]
            / scored["minimum_amount"]
        )

        scored["maximum_progress"] = (
            scored["amount_consumed"]
            / scored["maximum_amount"]
        )

        scored["remaining_to_target"] = (
            scored["target_amount"]
            - scored["amount_consumed"]
        ).clip(lower=0)

        def get_status(row):
            if not row["reported"]:
                return "unknown"

            if pd.notna(row["maximum_amount"]) and row["amount_consumed"] > row["maximum_amount"]:
                return "over_max"

            if pd.notna(row["minimum_amount"]) and row["amount_consumed"] < row["minimum_amount"]:
                return "below_minimum"

            if pd.notna(row["target_amount"]):
                if row["amount_consumed"] < row["target_amount"]:
                    return "below_target"
                return "met"

            return "within_limit"

        scored["status"] = scored.apply(get_status, axis=1)

        return scored

    def calculate_day(self, meals):
        if not meals:
            raise ValueError("Day contains no meals")

        results = []

        for meal in meals:
            meal_nutrients = self.calculate_meal(meal)
            results.append(meal_nutrients)

        day_totals = (
            pd.concat(results)
            .groupby(
                ["nutrient_id", "name", "unit_name"],
                as_index=False
            )["amount_consumed"]
            .sum(min_count=1)
        )

        return day_totals


    def summarize_score(self, score):
        columns = [
            "name",
            "unit_name",
            "amount_consumed",
            "minimum_amount",
            "target_amount",
            "maximum_amount",
            "minimum_progress",
            "target_progress",
            "maximum_progress",
            "remaining_to_target",
            "status",
            "reported"
        ]

        return score[columns]

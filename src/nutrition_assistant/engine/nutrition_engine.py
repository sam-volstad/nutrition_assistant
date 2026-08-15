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

    def get_usable_portions(self, fdc_id, portions=None):
        if portions is None:
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

        for item_index, ingredient in enumerate(meal.ingredients):
            nutrients = self.calculate_food_nutrients(
                ingredient.fdc_id,
                ingredient.grams
            )
            nutrients["_item_index"] = item_index
            results.append(nutrients)

        reported = pd.concat(results, ignore_index=True)
        amounts = reported.groupby("nutrient_id", as_index=False).agg(
            amount_consumed=("amount_consumed", lambda values: values.sum(min_count=1)),
            contributing_items=("_item_index", "nunique"),
        )
        meal_totals = self.food_repository.get_nutrient_catalog().merge(
            amounts, on="nutrient_id", how="left"
        )
        meal_totals["contributing_items"] = (
            meal_totals["contributing_items"].fillna(0).astype(int)
        )
        meal_totals["total_relevant_items"] = len(meal.ingredients)
        meal_totals = self._add_coverage(meal_totals)

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
        coverage_columns = [
            "contributing_items", "total_relevant_items",
            "coverage_ratio", "coverage_state",
        ]
        available_coverage = [
            column for column in coverage_columns if column in nutrients.columns
        ]
        scored = targets.merge(
            nutrients[
                ["nutrient_id", "amount_consumed", *available_coverage]
            ],
            on="nutrient_id",
            how="left"
        )

        scored["reported"] = scored["amount_consumed"].notna()
        if "coverage_state" not in scored:
            scored["coverage_state"] = scored["reported"].map(
                {True: "complete", False: "unknown"}
            )
            scored["coverage_ratio"] = scored["reported"].astype(float)
            scored["contributing_items"] = scored["reported"].astype(int)
            scored["total_relevant_items"] = 1

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

            if row["coverage_state"] == "partial":
                return "partial"

            if pd.notna(row["target_amount"]):
                progress = row["target_progress"]
            elif pd.notna(row["minimum_amount"]):
                progress = row["minimum_progress"]
            elif pd.notna(row["maximum_amount"]):
                return "within_limit"
            else:
                return "within_limit"

            if progress < 0.80:
                return "low"
            if progress < 0.90:
                return "approaching"
            return "acceptable"

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
            pd.concat(results, ignore_index=True)
            .groupby(["nutrient_id", "name", "unit_name"], as_index=False)
            .agg(
                amount_consumed=(
                    "amount_consumed", lambda values: values.sum(min_count=1)
                ),
                contributing_items=("contributing_items", "sum"),
                total_relevant_items=("total_relevant_items", "sum"),
            )
        )
        day_totals = self._add_coverage(day_totals)

        return day_totals

    @staticmethod
    def _add_coverage(nutrients):
        nutrients["coverage_ratio"] = (
            nutrients["contributing_items"]
            / nutrients["total_relevant_items"]
        )
        nutrients["coverage_state"] = "partial"
        nutrients.loc[
            nutrients["contributing_items"] == 0, "coverage_state"
        ] = "unknown"
        nutrients.loc[
            nutrients["contributing_items"]
            == nutrients["total_relevant_items"],
            "coverage_state",
        ] = "complete"
        return nutrients


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
            "reported",
            "contributing_items",
            "total_relevant_items",
            "coverage_ratio",
            "coverage_state",
        ]

        return score[columns]

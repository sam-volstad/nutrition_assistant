"""Focused checks for proactive ordinary-food candidate eligibility."""

from nutrition_assistant.config import DATABASE_PATH
from nutrition_assistant.repositories.food_repository import (
    FOUNDATIONAL_SERVING_THRESHOLDS,
    FoodRepository,
)


def metadata(repository, pattern):
    row = repository.con.execute(
        """
        SELECT
            f.description,
            f.data_type,
            f.food_category_id AS food_category,
            b.branded_food_category,
            b.household_serving_fulltext AS modifier,
            b.brand_name,
            b.brand_owner
        FROM food f
        LEFT JOIN branded_food b USING (fdc_id)
        WHERE lower(f.description) LIKE ?
        ORDER BY f.fdc_id
        LIMIT 1
        """,
        [pattern],
    ).fetchdf()
    assert not row.empty, pattern
    return row.iloc[0]


def main() -> None:
    repository = FoodRepository(DATABASE_PATH)
    try:
        classify = repository.classify_recommendation_candidate
        beet = metadata(repository, "%beetelite%nitric oxide%superfood%")
        vitamin_packet = metadata(
            repository, "%dietary supplement powder packets%"
        )
        cacao = metadata(repository, "%truvibe%cacao powder courage%")
        assert classify(beet)[0] == "supplement_like"
        assert classify(vitamin_packet)[0] == "supplement_like"
        assert classify(cacao)[0] == "ingredient_like"

        ordinary_examples = (
            metadata(repository, "%beans, black, mature seeds, cooked%"),
            metadata(repository, "apple, raw%"),
            metadata(repository, "%yogurt, plain, low fat%"),
            metadata(repository, "%weetabix whole grain cereal%"),
            metadata(repository, "%potato chips, original%"),
            metadata(repository, "%kala chana bengal gram%"),
            metadata(repository, "%matcha%green tea powder%"),
        )
        assert all(classify(item)[0] == "ordinary_food" for item in ordinary_examples)

        gap_ids = [1008, 1003, 1079, 1162, 1089]
        unrestricted = repository.get_recommendation_candidates(
            gap_ids, per_nutrient_limit=30, pool_limit=40
        )
        gated = repository.get_recommendation_candidates(
            gap_ids,
            per_nutrient_limit=30,
            pool_limit=40,
            require_foundational_contribution=True,
        )
        assert len(unrestricted) <= 40 and len(gated) <= 40
        assert set(gated["candidate_class"]) == {"ordinary_food"}
        assert all(
            row.energy_per_serving >= FOUNDATIONAL_SERVING_THRESHOLDS["energy_per_serving"]
            or row.protein_per_serving >= FOUNDATIONAL_SERVING_THRESHOLDS["protein_per_serving"]
            or row.fiber_per_serving >= FOUNDATIONAL_SERVING_THRESHOLDS["fiber_per_serving"]
            for row in gated.itertuples(index=False)
        )
        assert not unrestricted.description.str.contains(
            "TRUVIBE|BEETELITE|dietary supplement", case=False, na=False
        ).any()
        print("Ordinary-food recommendation checks passed")
    finally:
        repository.close()


if __name__ == "__main__":
    main()

"""Checks for diagnosed food recommendation correctness regressions."""

from nutrition_assistant.config import DATABASE_PATH
from nutrition_assistant.repositories.food_repository import FoodRepository


def metadata(repository, pattern):
    result = repository.con.execute(
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
    assert not result.empty, pattern
    return result.iloc[0]


def main() -> None:
    repository = FoodRepository(DATABASE_PATH)
    try:
        hemp = repository.con.execute(
            """
            SELECT f.fdc_id, fn.amount, b.serving_size
            FROM food f
            JOIN food_nutrient fn USING (fdc_id)
            JOIN branded_food b USING (fdc_id)
            WHERE lower(f.description) = 'raw hemp seeds'
              AND fn.nutrient_id = 1095
            ORDER BY f.fdc_id
            """
        ).fetchdf()
        assert (hemp["amount"] == 10000.0).all()
        assert (hemp.groupby("fdc_id").size() == 1).all()
        hemp_consumed = hemp.iloc[0].amount * hemp.iloc[0].serving_size / 100
        hemp_penalty = 100 * (1 + (hemp_consumed - 40) / 40)
        assert hemp_consumed == 3000.0 and hemp_penalty == 7500.0

        latte = repository.con.execute(
            """
            SELECT f.fdc_id, fn.amount, b.serving_size
            FROM food f
            JOIN food_nutrient fn USING (fdc_id)
            JOIN branded_food b USING (fdc_id)
            WHERE lower(f.description) LIKE 'cafe latte protein powder%'
              AND fn.nutrient_id = 1087
            ORDER BY f.fdc_id
            """
        ).fetchdf()
        assert (latte["amount"] == 1703704.0).all()
        assert (latte.groupby("fdc_id").size() == 1).all()
        latte_consumed = latte.iloc[0].amount * latte.iloc[0].serving_size / 100
        latte_penalty = 100 * (1 + (latte_consumed - 2500) / 2500)
        assert round(latte_consumed, 2) == 460000.08
        assert round(latte_penalty, 4) == 18400.0032

        classify = repository.classify_recommendation_candidate
        assert classify(metadata(repository, "%cafe latte protein powder%"))[0] == (
            "supplement_like"
        )
        assert classify(metadata(repository, "%agar-agar powder%"))[0] == (
            "ingredient_like"
        )
        assert classify(metadata(repository, "%capers capotes%"))[0] == "ordinary_food"
        assert classify(metadata(repository, "%lollipops%"))[0] == "ordinary_food"
        assert classify(metadata(repository, "%matcha%green tea powder%"))[0] == (
            "ordinary_food"
        )

        zinc = repository.get_recommendation_candidates(
            [1095], per_nutrient_limit=100, pool_limit=100
        )
        calcium = repository.get_recommendation_candidates(
            [1087], per_nutrient_limit=100, pool_limit=100
        )
        assert not zinc.description.str.contains(
            "raw hemp seeds", case=False, na=False
        ).any()
        assert not calcium.description.str.contains(
            "cafe latte protein powder", case=False, na=False
        ).any()

        equivalent = repository.get_recommendation_candidates(
            [1008, 1003, 1079], per_nutrient_limit=12, pool_limit=10
        )
        duplicate_counts = equivalent["description"].value_counts()
        assert duplicate_counts.max() > 1
        duplicate_description = duplicate_counts[duplicate_counts > 1].index[0]
        duplicate_rows = equivalent[
            equivalent["description"] == duplicate_description
        ]
        after_veto = repository.get_recommendation_candidates(
            [1008, 1003, 1079],
            excluded_fdc_ids=[int(duplicate_rows.iloc[0].fdc_id)],
            per_nutrient_limit=12,
            pool_limit=10,
        )
        assert duplicate_description not in set(after_veto["description"])

        # Exclusions must be applied before contributor ranking so each batch
        # refills from lower-ranked eligible foods rather than going stale.
        excluded = set()
        for _ in range(5):
            batch = repository.get_recommendation_candidates(
                [1008, 1003, 1079],
                excluded_fdc_ids=excluded,
                per_nutrient_limit=12,
                pool_limit=3,
            )
            ids = set(batch["fdc_id"].astype(int))
            assert ids.isdisjoint(excluded)
            if not ids:
                break
            excluded.update(ids)
        assert len(excluded) > 3
        exhausted = repository.get_recommendation_candidates(
            [999999999], per_nutrient_limit=3, pool_limit=3
        )
        assert exhausted.empty
        print("Food recommendation correctness checks passed")
    finally:
        repository.close()


if __name__ == "__main__":
    main()

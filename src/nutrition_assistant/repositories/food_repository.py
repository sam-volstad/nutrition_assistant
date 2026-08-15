from pathlib import Path
import re

import duckdb
import pandas as pd


FOUNDATIONAL_SERVING_THRESHOLDS = {
    "energy_per_serving": 100.0,
    "protein_per_serving": 5.0,
    "fiber_per_serving": 3.0,
}


class FoodRepository:
    def __init__(self, db_path):
        db_path = Path(db_path)
        if not db_path.is_file():
            raise FileNotFoundError(
                f"Nutrition database not found at {db_path}. "
                "Run initialize_database() first."
            )

        self.con = duckdb.connect(str(db_path))

    @staticmethod
    def classify_recommendation_candidate(metadata):
        """Classify proactive candidates without changing search eligibility."""
        def normalized(field):
            value = metadata.get(field)
            if value is None or pd.isna(value):
                return ""
            return re.sub(r"\s+", " ", str(value).strip().lower())

        description = normalized("description")
        category_parts = {
            normalized("food_category"),
            normalized("branded_food_category"),
        } - {""}
        category = " ".join(sorted(category_parts))
        serving = normalized("modifier")

        supplement_phrase = re.search(
            r"\b(dietary supplement|supplement powder|vitamin supplement|"
            r"mineral supplement|multivitamin|vitamin[ /-]*mineral supplement)\b",
            description,
        )
        performance_concentrate = (
            "nitric oxide" in description
            and re.search(
                r"\b(powder(?:ed)?|superfood|activator|concentrate(?:d)?|"
                r"crystals?|packets?)\b",
                f"{description} {serving}",
            )
        )
        if (
            "supplement" in category
            or bool(category_parts & {"vitamins", "minerals"})
            or supplement_phrase
            or performance_concentrate
        ):
            reason = (
                "performance-compound concentrate wording"
                if performance_concentrate
                else "explicit supplement metadata"
            )
            return "supplement_like", reason

        ingredient_category = re.search(
            r"\b(spices and herbs|spices|seasonings|flour(?:s)? and cornmeal|"
            r"baking ingredients|baking supplies)\b",
            category,
        )
        ingredient_description = re.search(
            r"\b(cocoa|cacao)\b.*\bpowder\b|"
            r"\b(powdered (?:milk|peanut butter)|baking powder|cornstarch)\b|"
            r"\b(curry|mustard|garlic|onion|chili) powder\b|"
            r"\b(flour|meal concentrate)\b",
            description,
        )
        if ingredient_category or ingredient_description:
            reason = (
                "ingredient-oriented food category"
                if ingredient_category
                else "explicit culinary ingredient wording"
            )
            return "ingredient_like", reason

        return "ordinary_food", "no supplement or ingredient-only signal"

    def search(self, search_term, limit=20, include_incomplete=False):
        return self.con.execute("""
            WITH matches AS (
                SELECT
                    f.fdc_id,
                    f.description,
                    f.data_type,
                    f.food_category_id,
                    q.reported_nutrient_count,
                    q.has_nutrients,
                    q.has_energy,
                    q.has_protein,
                    q.has_carbohydrate,
                    q.has_fat,
                    q.has_fiber,
                    q.has_serving_or_portion,
                    CASE f.data_type
                        WHEN 'foundation_food' THEN 1
                        WHEN 'sr_legacy_food' THEN 2
                        WHEN 'survey_fndds_food' THEN 3
                        WHEN 'branded_food' THEN 4
                        ELSE 5
                    END AS data_type_rank
                FROM food f
                JOIN food_quality q USING (fdc_id)
                WHERE lower(f.description) LIKE ?
                  AND (? OR q.has_nutrients)
                ORDER BY
                    data_type_rank,
                    length(f.description),
                    (
                        CAST(q.has_energy AS INTEGER)
                        + CAST(q.has_protein AS INTEGER)
                        + CAST(q.has_carbohydrate AS INTEGER)
                        + CAST(q.has_fat AS INTEGER)
                        + CAST(q.has_fiber AS INTEGER)
                    ) DESC,
                    q.reported_nutrient_count DESC,
                    q.has_serving_or_portion DESC,
                    f.description,
                    f.fdc_id
                LIMIT ?
            ),
            branded_matches AS (
                SELECT
                    fdc_id,
                    any_value(brand_name) AS brand_name,
                    any_value(brand_owner) AS brand_owner,
                    any_value(household_serving_fulltext) AS household_serving_fulltext,
                    any_value(serving_size) AS serving_size,
                    any_value(serving_size_unit) AS serving_size_unit
                FROM branded_food
                WHERE fdc_id IN (SELECT fdc_id FROM matches)
                GROUP BY fdc_id
            )
            SELECT
                f.fdc_id,
                f.description,
                f.data_type,
                f.food_category_id,
                fc.food_category,
                f.reported_nutrient_count,
                f.has_nutrients,
                f.has_energy,
                f.has_protein,
                f.has_carbohydrate,
                f.has_fat,
                f.has_fiber,
                f.has_serving_or_portion,
                b.brand_name,
                b.brand_owner,
                b.household_serving_fulltext,
                b.serving_size,
                b.serving_size_unit
            FROM matches f
            LEFT JOIN (
                SELECT
                    CAST(id AS VARCHAR) AS food_category_id,
                    any_value(description) AS food_category
                FROM food_category
                GROUP BY id
            ) fc
                ON f.food_category_id = fc.food_category_id
            LEFT JOIN branded_matches b
                ON f.fdc_id = b.fdc_id
            ORDER BY
                f.data_type_rank,
                length(f.description),
                (
                    CAST(f.has_energy AS INTEGER)
                    + CAST(f.has_protein AS INTEGER)
                    + CAST(f.has_carbohydrate AS INTEGER)
                    + CAST(f.has_fat AS INTEGER)
                    + CAST(f.has_fiber AS INTEGER)
                ) DESC,
                f.reported_nutrient_count DESC,
                f.has_serving_or_portion DESC,
                f.description,
                f.fdc_id
        """, [f"%{search_term.lower()}%", include_incomplete, limit]).fetchdf()

    def get_food_quality(self, fdc_id):
        quality = self.con.execute(
            "SELECT * FROM food_quality WHERE fdc_id = ?", [fdc_id]
        ).fetchdf()
        if quality.empty:
            raise ValueError(f"Unknown fdc_id: {fdc_id}")
        return quality.iloc[0]

    def get_recommendation_candidates(
        self,
        nutrient_ids,
        excluded_fdc_ids=None,
        per_nutrient_limit=12,
        pool_limit=40,
        require_foundational_contribution=False,
    ):
        """Return a bounded pool of strong contributors with one clear portion."""
        nutrient_ids = list(dict.fromkeys(int(value) for value in nutrient_ids))
        if not nutrient_ids:
            return self.con.execute(
                "SELECT NULL::BIGINT AS fdc_id WHERE FALSE"
            ).fetchdf()
        excluded_fdc_ids = list(
            dict.fromkeys(int(value) for value in (excluded_fdc_ids or []))
        )
        nutrient_placeholders = ", ".join("?" for _ in nutrient_ids)
        exclusion_sql = ""
        parameters = [*nutrient_ids, per_nutrient_limit]
        if excluded_fdc_ids:
            exclusion_placeholders = ", ".join("?" for _ in excluded_fdc_ids)
            exclusion_sql = f"AND ci.fdc_id NOT IN ({exclusion_placeholders})"
            parameters.extend(excluded_fdc_ids)
        # At most one bounded contributor per gap/rank enters classification.
        # Filtering occurs before detailed nutrition scoring, then the public
        # candidate pool is capped at pool_limit.
        prefilter_limit = len(nutrient_ids) * per_nutrient_limit
        parameters.append(prefilter_limit)

        candidates = self.con.execute(
            f"""
            WITH branded_metadata AS (
                SELECT
                    fdc_id,
                    ANY_VALUE(brand_name) AS brand_name,
                    ANY_VALUE(brand_owner) AS brand_owner,
                    ANY_VALUE(branded_food_category) AS branded_food_category
                FROM branded_food
                GROUP BY fdc_id
            ),
            repository_portions AS (
                SELECT
                    fp.fdc_id,
                    fp.id AS portion_id,
                    fp.amount AS portion_amount,
                    fp.modifier,
                    mu.name AS portion_unit,
                    fp.gram_weight,
                    'food_portion' AS portion_source
                FROM food_portion fp
                LEFT JOIN measure_unit mu ON fp.measure_unit_id = mu.id
                WHERE TRY_CAST(fp.gram_weight AS DOUBLE) > 0
                  AND isfinite(TRY_CAST(fp.gram_weight AS DOUBLE))
                UNION ALL
                SELECT
                    b.fdc_id,
                    NULL AS portion_id,
                    1.0 AS portion_amount,
                    b.household_serving_fulltext AS modifier,
                    b.serving_size_unit AS portion_unit,
                    b.serving_size AS gram_weight,
                    'branded_food' AS portion_source
                FROM branded_food b
                WHERE TRY_CAST(b.serving_size AS DOUBLE) > 0
                  AND isfinite(TRY_CAST(b.serving_size AS DOUBLE))
                  AND lower(trim(b.serving_size_unit)) IN ('g', 'gram', 'grams')
                  AND (
                      NULLIF(
                          regexp_extract(
                              lower(trim(b.household_serving_fulltext)),
                              '^([0-9]+(?:[.][0-9]+)?)[ ]*(?:g|gram|grams)$',
                              1
                          ),
                          ''
                      ) IS NULL
                      OR abs(
                          TRY_CAST(regexp_extract(
                              lower(trim(b.household_serving_fulltext)),
                              '^([0-9]+(?:[.][0-9]+)?)[ ]*(?:g|gram|grams)$',
                              1
                          ) AS DOUBLE)
                          - TRY_CAST(b.serving_size AS DOUBLE)
                      ) <= greatest(
                          2.0,
                          TRY_CAST(b.serving_size AS DOUBLE) * 0.25
                      )
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM food_portion fp WHERE fp.fdc_id = b.fdc_id
                  )
            ),
            deterministic_portions AS (
                SELECT
                    fdc_id,
                    ANY_VALUE(portion_id) AS portion_id,
                    ANY_VALUE(portion_amount) AS portion_amount,
                    ANY_VALUE(modifier) AS modifier,
                    ANY_VALUE(portion_unit) AS portion_unit,
                    ANY_VALUE(gram_weight) AS gram_weight,
                    ANY_VALUE(portion_source) AS portion_source
                FROM repository_portions
                GROUP BY fdc_id
                HAVING COUNT(*) = 1
            ),
            contributions AS (
                SELECT
                    fn.fdc_id,
                    fn.nutrient_id,
                    SUM(TRY_CAST(fn.amount AS DOUBLE)) AS amount_per_100g
                FROM food_nutrient fn
                WHERE fn.nutrient_id IN ({nutrient_placeholders})
                  AND TRY_CAST(fn.amount AS DOUBLE) > 0
                  AND isfinite(TRY_CAST(fn.amount AS DOUBLE))
                GROUP BY fn.fdc_id, fn.nutrient_id
            ),
            foundational_contributions AS (
                SELECT
                    fn.fdc_id,
                    SUM(CASE WHEN fn.nutrient_id = 1008
                        THEN TRY_CAST(fn.amount AS DOUBLE) END) AS energy_per_100g,
                    SUM(CASE WHEN fn.nutrient_id = 1003
                        THEN TRY_CAST(fn.amount AS DOUBLE) END) AS protein_per_100g,
                    SUM(CASE WHEN fn.nutrient_id = 1079
                        THEN TRY_CAST(fn.amount AS DOUBLE) END) AS fiber_per_100g
                FROM food_nutrient fn
                WHERE fn.nutrient_id IN (1008, 1003, 1079)
                GROUP BY fn.fdc_id
            ),
            implausible_nutrient_density AS (
                SELECT DISTINCT fdc_id
                FROM food_nutrient
                WHERE (
                    nutrient_id = 1008
                    AND TRY_CAST(amount AS DOUBLE) > 1000
                ) OR (
                    nutrient_id IN (1003, 1004, 1005, 1079)
                    AND TRY_CAST(amount AS DOUBLE) > 105
                )
            ),
            ranked_contributors AS (
                SELECT
                    c.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY c.nutrient_id
                        ORDER BY c.amount_per_100g DESC, c.fdc_id
                    ) AS contributor_rank
                FROM contributions c
                JOIN food_quality q USING (fdc_id)
                JOIN deterministic_portions dp USING (fdc_id)
                JOIN food f USING (fdc_id)
                LEFT JOIN branded_metadata bm USING (fdc_id)
                LEFT JOIN food_preferences pref USING (fdc_id)
                LEFT JOIN implausible_nutrient_density bad USING (fdc_id)
                WHERE q.has_nutrients
                  AND q.has_serving_or_portion
                  AND bad.fdc_id IS NULL
                  AND NOT (
                      lower(COALESCE(bm.branded_food_category, ''))
                          LIKE '%supplement%'
                      OR lower(trim(COALESCE(bm.branded_food_category, '')))
                          IN ('vitamins', 'minerals')
                      OR (
                          f.data_type = 'branded_food'
                          AND lower(f.description) LIKE '%vitamin%'
                          AND lower(f.description) LIKE '%mineral%'
                          AND (
                              lower(f.description) LIKE '%mix%'
                              OR lower(f.description) LIKE '%powder%'
                          )
                      )
                      OR (
                          f.data_type = 'branded_food'
                          AND regexp_matches(
                              lower(COALESCE(f.description, '')),
                              'dietary[ ]+supplement|supplement[ ]+powder|vitamin[ ]+supplement|mineral[ ]+supplement|multivitamin|vitamin[ /-]*mineral[ ]+supplement'
                          )
                      )
                  )
                  AND COALESCE(pref.preference, 'neutral') NOT IN ('avoid', 'never')
            ),
            bounded AS (
                SELECT *
                FROM ranked_contributors
                WHERE contributor_rank <= ?
            ),
            candidate_ids AS (
                SELECT
                    fdc_id,
                    MIN(contributor_rank) AS best_contributor_rank,
                    COUNT(DISTINCT nutrient_id) AS gaps_matched
                FROM bounded
                GROUP BY fdc_id
            )
            SELECT
                ci.fdc_id,
                f.description,
                f.data_type,
                COALESCE(cat.description, f.food_category_id) AS food_category,
                bm.brand_name,
                bm.brand_owner,
                bm.branded_food_category,
                dp.portion_id,
                dp.portion_amount,
                dp.modifier,
                dp.portion_unit,
                dp.gram_weight,
                dp.portion_source,
                q.reported_nutrient_count,
                fc.energy_per_100g * dp.gram_weight / 100.0 AS energy_per_serving,
                fc.protein_per_100g * dp.gram_weight / 100.0 AS protein_per_serving,
                fc.fiber_per_100g * dp.gram_weight / 100.0 AS fiber_per_serving,
                ci.gaps_matched,
                ci.best_contributor_rank
            FROM candidate_ids ci
            JOIN food f USING (fdc_id)
            JOIN food_quality q USING (fdc_id)
            JOIN deterministic_portions dp USING (fdc_id)
            LEFT JOIN foundational_contributions fc USING (fdc_id)
            LEFT JOIN food_category cat
                ON TRY_CAST(f.food_category_id AS BIGINT) = cat.id
            LEFT JOIN branded_metadata bm USING (fdc_id)
            LEFT JOIN food_preferences pref USING (fdc_id)
            WHERE COALESCE(pref.preference, 'neutral') NOT IN ('avoid', 'never')
              {exclusion_sql}
            ORDER BY
                ci.gaps_matched DESC,
                ci.best_contributor_rank,
                CASE f.data_type
                    WHEN 'survey_fndds_food' THEN 0
                    WHEN 'sr_legacy_food' THEN 1
                    WHEN 'foundation_food' THEN 2
                    WHEN 'branded_food' THEN 3
                    ELSE 4
                END,
                q.reported_nutrient_count DESC,
                ci.fdc_id
            LIMIT ?
            """,
            parameters,
        ).fetchdf()

        if candidates.empty:
            candidates["candidate_class"] = pd.Series(dtype="object")
            candidates["classification_reason"] = pd.Series(dtype="object")
            return candidates
        classifications = candidates.apply(
            self.classify_recommendation_candidate,
            axis=1,
            result_type="expand",
        )
        classifications.columns = ["candidate_class", "classification_reason"]
        candidates = pd.concat([candidates, classifications], axis=1)
        candidates = candidates[candidates["candidate_class"] == "ordinary_food"]
        if require_foundational_contribution:
            substantial = pd.Series(False, index=candidates.index)
            for column, threshold in FOUNDATIONAL_SERVING_THRESHOLDS.items():
                substantial |= pd.to_numeric(
                    candidates[column], errors="coerce"
                ).ge(threshold)
            candidates = candidates[substantial]
        return candidates.head(pool_limit).reset_index(drop=True)

    def get_nutrients(self, fdc_id):
        return self.con.execute("""
            SELECT
                n.id AS nutrient_id,
                n.name,
                n.unit_name,
                fn.amount
            FROM food_nutrient fn
            JOIN nutrient n
                ON fn.nutrient_id = n.id
            WHERE fn.fdc_id = ?
            ORDER BY n.rank
        """, [fdc_id]).fetchdf()

    def get_nutrient_catalog(self):
        """Return the nutrient universe used to represent unreported rows."""
        return self.con.execute("""
            SELECT DISTINCT
                id AS nutrient_id,
                name,
                unit_name
            FROM nutrient
            ORDER BY nutrient_id
        """).fetchdf()

    def get_portions(self, fdc_id):
        portions = self.con.execute("""
            SELECT
                fp.id AS portion_id,
                fp.amount,
                fp.modifier,
                fp.gram_weight,
                mu.name AS unit,
                'food_portion' AS source
            FROM food_portion fp
            LEFT JOIN measure_unit mu
                ON fp.measure_unit_id = mu.id
            WHERE fp.fdc_id = ?
            ORDER BY fp.gram_weight
        """, [fdc_id]).fetchdf()

        if not portions.empty:
            return portions

        return self.con.execute("""
            SELECT
                NULL AS portion_id,
                1.0 AS amount,
                household_serving_fulltext AS modifier,
                serving_size AS gram_weight,
                serving_size_unit AS unit,
                'branded_food' AS source
            FROM branded_food
            WHERE fdc_id = ?
        """, [fdc_id]).fetchdf()

    def close(self):
        self.con.close()

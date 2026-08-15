from pathlib import Path

import duckdb


class FoodRepository:
    def __init__(self, db_path):
        db_path = Path(db_path)
        if not db_path.is_file():
            raise FileNotFoundError(
                f"Nutrition database not found at {db_path}. "
                "Run initialize_database() first."
            )

        self.con = duckdb.connect(str(db_path))

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

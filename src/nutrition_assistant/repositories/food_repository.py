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

    def search(self, search_term, limit=20):
        return self.con.execute("""
            WITH matches AS (
                SELECT
                    fdc_id,
                    description,
                    data_type,
                    food_category_id,
                    CASE data_type
                        WHEN 'foundation_food' THEN 1
                        WHEN 'sr_legacy_food' THEN 2
                        WHEN 'survey_fndds_food' THEN 3
                        WHEN 'branded_food' THEN 4
                        ELSE 5
                    END AS data_type_rank
                FROM food
                WHERE lower(description) LIKE ?
                ORDER BY
                    data_type_rank,
                    length(description),
                    description,
                    fdc_id
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
                f.description,
                f.fdc_id
        """, [f"%{search_term.lower()}%", limit]).fetchdf()

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

import duckdb


class FoodRepository:
    def __init__(self, db_path):
        self.con = duckdb.connect(str(db_path))

    def search(self, search_term, limit=20):
        return self.con.execute("""
            SELECT
                fdc_id,
                data_type,
                description,
                food_category_id
            FROM food
            WHERE lower(description) LIKE ?
            ORDER BY
                CASE data_type
                    WHEN 'foundation_food' THEN 1
                    WHEN 'sr_legacy_food' THEN 2
                    WHEN 'survey_fndds_food' THEN 3
                    WHEN 'branded_food' THEN 4
                    ELSE 5
                END,
                length(description),
                description
            LIMIT ?
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

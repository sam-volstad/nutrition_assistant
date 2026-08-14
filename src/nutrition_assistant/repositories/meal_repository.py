from nutrition_assistant.models.ingredient import Ingredient
from nutrition_assistant.models.meal import Meal


class MealRepository:
    def __init__(self, connection):
        self.con = connection

    def create_meal(self, meal: Meal) -> int:
        if not meal.ingredients:
            raise ValueError("Meal contains no ingredients")

        result = self.con.execute(
            """
            INSERT INTO meals (meal_name)
            VALUES (?)
            RETURNING meal_id
            """,
            [meal.name],
        ).fetchone()

        meal_id = result[0]

        for ingredient in meal.ingredients:
            self.con.execute(
                """
                INSERT INTO meal_ingredients (
                    meal_id,
                    fdc_id,
                    grams
                )
                VALUES (?, ?, ?)
                """,
                [
                    meal_id,
                    ingredient.fdc_id,
                    ingredient.grams,
                ],
            )

        return meal_id

    def get_meal(self, meal_id: int) -> Meal:
        meal_row = self.con.execute(
            """
            SELECT meal_name
            FROM meals
            WHERE meal_id = ?
            """,
            [meal_id],
        ).fetchone()

        if meal_row is None:
            raise ValueError(f"Unknown meal_id: {meal_id}")

        ingredient_rows = self.con.execute(
            """
            SELECT
                mi.fdc_id,
                mi.grams,
                f.description
            FROM meal_ingredients mi
            JOIN food f
                ON mi.fdc_id = f.fdc_id
            WHERE mi.meal_id = ?
            ORDER BY mi.fdc_id
            """,
            [meal_id],
        ).fetchall()

        ingredients = [
            Ingredient(
                fdc_id=fdc_id,
                grams=grams,
                name=description,
            )
            for fdc_id, grams, description in ingredient_rows
        ]

        return Meal(
            name=meal_row[0],
            ingredients=ingredients,
        )

    def list_meals(self):
        return self.con.execute(
            """
            SELECT
                m.meal_id,
                m.meal_name,
                COUNT(mi.fdc_id) AS ingredient_count
            FROM meals m
            LEFT JOIN meal_ingredients mi
                ON m.meal_id = mi.meal_id
            GROUP BY
                m.meal_id,
                m.meal_name
            ORDER BY
                m.meal_name
            """
        ).fetchdf()

    def delete_meal(self, meal_id: int):
        exists = self.con.execute(
            """
            SELECT 1
            FROM meals
            WHERE meal_id = ?
            """,
            [meal_id],
        ).fetchone()

        if exists is None:
            raise ValueError(f"Unknown meal_id: {meal_id}")

        self.con.execute(
            """
            DELETE FROM meal_ingredients
            WHERE meal_id = ?
            """,
            [meal_id],
        )

        self.con.execute(
            """
            DELETE FROM meals
            WHERE meal_id = ?
            """,
            [meal_id],
        )

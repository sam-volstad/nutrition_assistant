"""Run a lightweight end-to-end check against the configured database."""

from nutrition_assistant.config import DATABASE_PATH
from nutrition_assistant.engine.nutrition_engine import NutritionEngine
from nutrition_assistant.repositories.food_repository import FoodRepository
from nutrition_assistant.repositories.daily_log_repository import DailyLogRepository
from nutrition_assistant.repositories.meal_repository import MealRepository
from nutrition_assistant.repositories.target_repository import TargetRepository


def main() -> None:
    food_repository = FoodRepository(DATABASE_PATH)
    meal_repository = MealRepository(food_repository.con)
    target_repository = TargetRepository(food_repository.con)
    daily_log_repository = DailyLogRepository(food_repository.con)
    engine = NutritionEngine(food_repository)

    try:
        meal_row = food_repository.con.execute(
            """
            SELECT m.meal_id
            FROM meals m
            WHERE EXISTS (
                SELECT 1
                FROM meal_ingredients mi
                WHERE mi.meal_id = m.meal_id
            )
            AND NOT EXISTS (
                SELECT 1
                FROM meal_ingredients mi
                WHERE mi.meal_id = m.meal_id
                AND NOT EXISTS (
                    SELECT 1
                    FROM food_nutrient fn
                    WHERE fn.fdc_id = mi.fdc_id
                )
            )
            ORDER BY m.meal_id
            LIMIT 1
            """
        ).fetchone()
        if meal_row is None:
            raise RuntimeError(
                "No saved meal with reported nutrient data is available "
                "for the smoke check"
            )

        meal = meal_repository.get_meal(meal_row[0])
        meal_nutrients = engine.calculate_meal(meal)
        day_nutrients = engine.calculate_day([meal])

        targets = target_repository.get_profile("default")
        history_dates = daily_log_repository.get_available_dates()

        meal_score = engine.score_against_targets(meal_nutrients, targets)
        day_score = engine.score_against_targets(day_nutrients, targets)

        print(f"Loaded meal: {meal.name}")
        print(f"Meal nutrient rows: {len(meal_nutrients)}")
        print(f"Meal score rows: {len(meal_score)}")
        print(f"Day score rows: {len(day_score)}")
        print(f"Logged history dates: {len(history_dates)}")
    finally:
        food_repository.close()


if __name__ == "__main__":
    main()

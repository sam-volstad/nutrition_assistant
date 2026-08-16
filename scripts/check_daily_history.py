"""Focused persistence checks for prototype daily history."""

from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile

import duckdb

from nutrition_assistant.database.bootstrap import _create_application_schema
from nutrition_assistant.engine.nutrition_engine import NutritionEngine
from nutrition_assistant.models.ingredient import Ingredient
from nutrition_assistant.models.meal import Meal
from nutrition_assistant.repositories.daily_log_repository import DailyLogRepository
from nutrition_assistant.repositories.food_repository import FoodRepository
from nutrition_assistant.repositories.meal_repository import MealRepository


def main() -> None:
    with NamedTemporaryFile(suffix=".duckdb", delete=False) as temporary:
        database_path = Path(temporary.name)
    database_path.unlink()

    connection = duckdb.connect(str(database_path))
    connection.execute(
        "CREATE TABLE food (fdc_id BIGINT PRIMARY KEY, description VARCHAR)"
    )
    connection.execute(
        "CREATE TABLE nutrient (id BIGINT, name VARCHAR, unit_name VARCHAR, rank DOUBLE)"
    )
    connection.execute(
        "CREATE TABLE food_nutrient (fdc_id BIGINT, nutrient_id BIGINT, amount DOUBLE)"
    )
    connection.executemany(
        "INSERT INTO food VALUES (?, ?)",
        [(1, "Fixture oats"), (2, "Fixture milk")],
    )
    connection.executemany(
        "INSERT INTO nutrient VALUES (?, ?, ?, ?)",
        [
            (1008, "Energy", "kcal", 1),
            (1003, "Protein", "g", 2),
            (1162, "Vitamin C", "mg", 3),
        ],
    )
    connection.executemany(
        "INSERT INTO food_nutrient VALUES (?, ?, ?)",
        [(1, 1008, 100), (2, 1008, 50), (1, 1003, 8)],
    )
    _create_application_schema(connection)

    meal_repository = MealRepository(connection)
    log_repository = DailyLogRepository(connection)
    original = Meal("Breakfast", [Ingredient(1, 80, "Fixture oats")])
    meal_id = meal_repository.create_meal(original)
    first_id = log_repository.add_entry(
        date(2026, 8, 14), original, source_meal_id=meal_id
    )
    second_id = log_repository.add_entry(
        date(2026, 8, 14),
        Meal("Milk", [Ingredient(2, 237.5, "Fixture milk")]),
        source_type="recommended_food",
    )
    log_repository.add_entry(
        date(2026, 8, 13),
        Meal("Custom oats", [Ingredient(1, 42.25, "Fixture oats")]),
    )
    connection.close()

    food_repository = FoodRepository(database_path)
    try:
        log_repository = DailyLogRepository(food_repository.con)
        meal_repository = MealRepository(food_repository.con)
        entries = log_repository.get_entries(date(2026, 8, 14))
        assert [entry.entry_id for entry in entries] == [first_id, second_id]
        assert entries[1].meal.ingredients[0].grams == 237.5
        before = NutritionEngine(food_repository).calculate_day(
            [entry.meal for entry in entries]
        )
        coverage = dict(zip(before["name"], before["coverage_state"]))
        assert coverage == {
            "Energy": "complete",
            "Protein": "partial",
            "Vitamin C": "unknown",
        }

        meal_repository.update_meal(
            meal_id, Meal("Changed", [Ingredient(1, 10, "Fixture oats")])
        )
        meal_repository.delete_meal(meal_id)
        snapshots = log_repository.get_entries(date(2026, 8, 14))
        assert snapshots[0].meal.ingredients[0].grams == 80
        after = NutritionEngine(food_repository).calculate_day(
            [entry.meal for entry in snapshots]
        )
        assert before.equals(after)

        assert log_repository.get_available_dates() == [
            date(2026, 8, 14), date(2026, 8, 13)
        ]
        log_repository.delete_entry(first_id)
        remaining = log_repository.get_entries(date(2026, 8, 14))
        assert [entry.entry_id for entry in remaining] == [second_id]
    finally:
        food_repository.close()
        database_path.unlink(missing_ok=True)

    print("Daily history checks passed")


if __name__ == "__main__":
    main()

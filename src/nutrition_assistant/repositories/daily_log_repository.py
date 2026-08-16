from datetime import date, datetime

from nutrition_assistant.models.daily_entry import DailyEntry
from nutrition_assistant.models.ingredient import Ingredient
from nutrition_assistant.models.meal import Meal


VALID_SOURCE_TYPES = {"saved_meal", "one_off", "recommended_food"}


def _normalize_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError("eaten_date must be a valid calendar date") from error


class DailyLogRepository:
    """Persist consumed ingredient snapshots for calendar-day reconstruction.

    FDC IDs and consumed grams are snapshotted, but USDA nutrient values are not;
    replacing the source database could therefore change historical calculations.
    """

    def __init__(self, connection):
        self.con = connection

    def add_entry(
        self,
        eaten_date,
        meal: Meal,
        source_meal_id: int | None = None,
        source_type: str | None = None,
    ) -> int:
        eaten_date = _normalize_date(eaten_date)
        if not meal.ingredients:
            raise ValueError("Daily entry contains no ingredients")
        display_name = meal.name.strip()
        if not display_name:
            raise ValueError("Daily entry name is required")
        source_type = source_type or (
            "saved_meal" if source_meal_id is not None else "one_off"
        )
        if source_type not in VALID_SOURCE_TYPES:
            raise ValueError(f"Unknown daily entry source_type: {source_type}")

        self.con.execute("BEGIN TRANSACTION")
        try:
            fdc_ids = {ingredient.fdc_id for ingredient in meal.ingredients}
            placeholders = ", ".join("?" for _ in fdc_ids)
            existing_ids = {
                int(row[0])
                for row in self.con.execute(
                    f"SELECT fdc_id FROM food WHERE fdc_id IN ({placeholders})",
                    list(fdc_ids),
                ).fetchall()
            }
            missing_ids = sorted(fdc_ids - existing_ids)
            if missing_ids:
                raise ValueError(f"Unknown fdc_id values: {missing_ids}")
            if source_meal_id is not None and self.con.execute(
                "SELECT 1 FROM meals WHERE meal_id = ?", [source_meal_id]
            ).fetchone() is None:
                raise ValueError(f"Unknown meal_id: {source_meal_id}")
            entry_id = self.con.execute(
                """
                INSERT INTO daily_entries (
                    eaten_date, display_name, source_type, source_meal_id
                ) VALUES (?, ?, ?, ?)
                RETURNING entry_id
                """,
                [eaten_date, display_name, source_type, source_meal_id],
            ).fetchone()[0]
            self.con.executemany(
                """
                INSERT INTO daily_entry_ingredients (
                    entry_id, ingredient_order, fdc_id, grams, display_name
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        entry_id,
                        index,
                        ingredient.fdc_id,
                        ingredient.grams,
                        ingredient.name,
                    )
                    for index, ingredient in enumerate(meal.ingredients)
                ],
            )
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        return int(entry_id)

    def get_entries(self, eaten_date) -> list[DailyEntry]:
        eaten_date = _normalize_date(eaten_date)
        rows = self.con.execute(
            """
            SELECT
                e.entry_id,
                e.eaten_date,
                e.display_name,
                e.source_type,
                e.source_meal_id,
                e.created_at,
                i.ingredient_order,
                i.fdc_id,
                i.grams,
                i.display_name
            FROM daily_entries e
            JOIN daily_entry_ingredients i USING (entry_id)
            WHERE e.eaten_date = ?
            ORDER BY e.created_at, e.entry_id, i.ingredient_order
            """,
            [eaten_date],
        ).fetchall()
        grouped = {}
        for row in rows:
            entry_id = int(row[0])
            if entry_id not in grouped:
                grouped[entry_id] = {
                    "metadata": row[:6],
                    "ingredients": [],
                }
            grouped[entry_id]["ingredients"].append(
                Ingredient(fdc_id=int(row[7]), grams=float(row[8]), name=row[9])
            )
        return [
            DailyEntry(
                entry_id=entry_id,
                eaten_date=values["metadata"][1],
                display_name=values["metadata"][2],
                source_type=values["metadata"][3],
                source_meal_id=(
                    int(values["metadata"][4])
                    if values["metadata"][4] is not None
                    else None
                ),
                created_at=values["metadata"][5],
                meal=Meal(
                    name=values["metadata"][2],
                    ingredients=values["ingredients"],
                ),
            )
            for entry_id, values in grouped.items()
        ]

    def delete_entry(self, entry_id: int) -> None:
        self.con.execute("BEGIN TRANSACTION")
        try:
            if self.con.execute(
                "SELECT 1 FROM daily_entries WHERE entry_id = ?", [entry_id]
            ).fetchone() is None:
                raise ValueError(f"Unknown daily entry_id: {entry_id}")
            self.con.execute(
                "DELETE FROM daily_entry_ingredients WHERE entry_id = ?",
                [entry_id],
            )
            self.con.execute(
                "DELETE FROM daily_entries WHERE entry_id = ?", [entry_id]
            )
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise

    def get_available_dates(self) -> list[date]:
        return [
            row[0]
            for row in self.con.execute(
                """
                SELECT DISTINCT eaten_date
                FROM daily_entries
                ORDER BY eaten_date DESC
                """
            ).fetchall()
        ]

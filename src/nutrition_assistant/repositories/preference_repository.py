VALID_PREFERENCES = frozenset(
    {"preferred", "acceptable", "neutral", "avoid", "never"}
)
class PreferenceRepository:
    """Persist preferences; food methods can later share this repository."""

    def __init__(self, connection):
        self.con = connection

    def get_meal_preference(self, meal_id: int) -> str:
        row = self.con.execute(
            """
            SELECT preference
            FROM meal_preferences
            WHERE meal_id = ?
            """,
            [meal_id],
        ).fetchone()
        return row[0] if row is not None else "neutral"

    def set_meal_preference(self, meal_id: int, preference: str) -> None:
        self._validate_preference(preference)
        if self.con.execute(
            "SELECT 1 FROM meals WHERE meal_id = ?", [meal_id]
        ).fetchone() is None:
            raise ValueError(f"Unknown meal_id: {meal_id}")

        if preference == "neutral":
            self.remove_meal_preference(meal_id)
            return

        self.con.execute(
            """
            INSERT INTO meal_preferences (meal_id, preference)
            VALUES (?, ?)
            ON CONFLICT (meal_id)
            DO UPDATE SET preference = EXCLUDED.preference
            """,
            [meal_id, preference],
        )

    def remove_meal_preference(self, meal_id: int) -> None:
        self.con.execute(
            """
            DELETE FROM meal_preferences
            WHERE meal_id = ?
            """,
            [meal_id],
        )

    def get_meal_preferences(self, meal_ids=None) -> dict[int, str]:
        if meal_ids is not None:
            meal_ids = list(meal_ids)
            if not meal_ids:
                return {}
            placeholders = ", ".join("?" for _ in meal_ids)
            rows = self.con.execute(
                f"""
                SELECT meal_id, preference
                FROM meal_preferences
                WHERE meal_id IN ({placeholders})
                """,
                meal_ids,
            ).fetchall()
        else:
            rows = self.con.execute(
                """
                SELECT meal_id, preference
                FROM meal_preferences
                """
            ).fetchall()
        return {int(meal_id): preference for meal_id, preference in rows}

    @staticmethod
    def _validate_preference(preference: str) -> None:
        if preference not in VALID_PREFERENCES:
            choices = ", ".join(sorted(VALID_PREFERENCES))
            raise ValueError(f"Unknown preference '{preference}'; choose from {choices}")

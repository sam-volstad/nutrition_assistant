from dataclasses import dataclass
from datetime import date, datetime

from .meal import Meal


@dataclass
class DailyEntry:
    entry_id: int
    eaten_date: date
    display_name: str
    source_type: str
    source_meal_id: int | None
    created_at: datetime
    meal: Meal

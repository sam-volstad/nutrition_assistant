from dataclasses import dataclass
import math


@dataclass
class Ingredient:
    fdc_id: int
    grams: float
    name: str | None = None

    def __post_init__(self):
        if not math.isfinite(self.grams) or self.grams <= 0:
            raise ValueError("grams must be a positive finite number")

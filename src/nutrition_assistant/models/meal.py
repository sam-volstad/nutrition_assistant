from dataclasses import dataclass, field

from .ingredient import Ingredient


@dataclass
class Meal:
    name: str
    ingredients: list[Ingredient] = field(default_factory=list)

    def add_ingredient(self, ingredient: Ingredient):
        self.ingredients.append(ingredient)

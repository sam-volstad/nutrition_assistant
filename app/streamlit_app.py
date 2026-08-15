from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st

from nutrition_assistant.config import DATABASE_PATH
from nutrition_assistant.engine.nutrition_engine import NutritionEngine
from nutrition_assistant.models.ingredient import Ingredient
from nutrition_assistant.models.meal import Meal
from nutrition_assistant.repositories.food_repository import FoodRepository
from nutrition_assistant.repositories.meal_repository import MealRepository
from nutrition_assistant.repositories.target_repository import TargetRepository


@st.cache_resource(show_spinner=False)
def get_services(database_path: str):
    food_repository = FoodRepository(Path(database_path))
    return (
        food_repository,
        MealRepository(food_repository.con),
        TargetRepository(food_repository.con),
        NutritionEngine(food_repository),
    )


def initialize_session_state() -> None:
    st.session_state.setdefault("meal_ingredients", [])
    st.session_state.setdefault("today_meal_ids", [])


def format_portion(portion) -> str:
    modifier = portion.get("modifier")
    amount = portion.get("amount")
    portion_unit = portion.get("unit")
    gram_weight = portion.get("gram_weight")

    description = modifier if pd.notna(modifier) and modifier else "Serving"
    unit = portion_unit if pd.notna(portion_unit) else ""
    amount_text = f"{amount:g} " if pd.notna(amount) else ""
    gram_text = f"{gram_weight:g} g" if pd.notna(gram_weight) else "unknown grams"
    return f"{description} — {amount_text}{unit} ({gram_text})"


def score_display(engine, nutrients, targets):
    score = engine.score_against_targets(nutrients, targets)
    summary = engine.summarize_score(score).copy()
    summary["target_progress"] = summary["target_progress"] * 100
    return summary.rename(
        columns={
            "name": "Nutrient",
            "unit_name": "Unit",
            "amount_consumed": "Amount consumed",
            "target_amount": "Target",
            "maximum_amount": "Maximum",
            "target_progress": "Progress (%)",
            "remaining_to_target": "Remaining",
            "status": "Status",
            "reported": "Reported",
        }
    )[
        [
            "Nutrient",
            "Amount consumed",
            "Unit",
            "Target",
            "Maximum",
            "Progress (%)",
            "Remaining",
            "Status",
            "Reported",
        ]
    ]


def render_build_meal(food_repository, meal_repository, engine) -> None:
    st.header("Build Meal")
    meal_name = st.text_input("Meal name", key="builder_meal_name")
    search_term = st.text_input("Search food", key="food_search").strip()

    if not search_term:
        st.info("Enter a food name to search.")
        search_results = None
    else:
        search_results = food_repository.search(search_term, limit=25)

    if search_results is not None and search_results.empty:
        st.warning("No foods matched that search.")

    if search_results is not None and not search_results.empty:
        results_by_fdc_id = {
            int(row.fdc_id): row
            for row in search_results.itertuples(index=False)
        }

        def food_label(fdc_id):
            row = results_by_fdc_id[fdc_id]
            category = row.food_category_id
            category_text = f"category {category}" if pd.notna(category) else "no category"
            return (
                f"{row.description} — {row.data_type}, "
                f"{category_text}, FDC {row.fdc_id}"
            )

        selected_fdc_id = st.selectbox(
            "Search results",
            list(results_by_fdc_id),
            format_func=food_label,
            index=None,
            placeholder="Select a food",
            key="selected_food_fdc_id",
        )

        if selected_fdc_id is not None:
            selected_food = results_by_fdc_id[selected_fdc_id]
            fdc_id = int(selected_food.fdc_id)
            portions = food_repository.get_portions(fdc_id)
            usable_portions = engine.get_usable_portions(fdc_id, portions=portions)

            st.subheader(selected_food.description)
            if portions.empty:
                st.info("No portion information is available. Enter grams directly.")
            else:
                st.dataframe(
                    portions[
                        ["amount", "modifier", "unit", "gram_weight", "source"]
                    ],
                    hide_index=True,
                    use_container_width=True,
                )

            methods = ["Enter grams directly"]
            selectable_portions = (
                not usable_portions.empty
                and (
                    len(usable_portions) == 1
                    or usable_portions["portion_id"].notna().all()
                )
            )
            if selectable_portions:
                methods.insert(0, "Use a portion")

            method = st.radio(
                "Amount method",
                methods,
                horizontal=True,
                key=f"amount_method_{fdc_id}",
            )

            grams = None
            portion_id = None
            quantity = 1.0
            if method == "Use a portion":
                portion_indexes = usable_portions.index.tolist()
                selected_portion_index = st.selectbox(
                    "Portion",
                    portion_indexes,
                    format_func=lambda index: format_portion(
                        usable_portions.loc[index]
                    ),
                    index=0 if len(portion_indexes) == 1 else None,
                    placeholder="Choose a portion",
                    key=f"portion_{fdc_id}",
                )
                quantity = st.number_input(
                    "Quantity",
                    min_value=0.01,
                    value=1.0,
                    step=0.25,
                    key=f"quantity_{fdc_id}",
                )
                if selected_portion_index is not None:
                    selected_portion = usable_portions.loc[selected_portion_index]
                    if pd.notna(selected_portion["portion_id"]):
                        portion_id = int(selected_portion["portion_id"])
            else:
                grams = st.number_input(
                    "Grams",
                    min_value=0.01,
                    value=100.0,
                    step=5.0,
                    key=f"grams_{fdc_id}",
                )

            if st.button("Add to meal", type="primary"):
                existing_ids = {
                    item["fdc_id"] for item in st.session_state.meal_ingredients
                }
                if fdc_id in existing_ids:
                    st.error("That food is already in the current meal.")
                elif method == "Use a portion" and selected_portion_index is None:
                    st.error("Choose a portion before adding the food.")
                else:
                    try:
                        resolved_grams = (
                            float(grams)
                            if method == "Enter grams directly"
                            else engine.resolve_portion_grams(
                                fdc_id,
                                quantity=quantity,
                                portion_id=portion_id,
                            )
                        )
                        ingredient = Ingredient(
                            fdc_id=fdc_id,
                            grams=resolved_grams,
                            name=selected_food.description,
                        )
                    except ValueError as error:
                        st.error(str(error))
                    else:
                        st.session_state.meal_ingredients.append(
                            {
                                "fdc_id": ingredient.fdc_id,
                                "name": ingredient.name,
                                "grams": ingredient.grams,
                            }
                        )
                        st.success(f"Added {ingredient.name}.")

    st.subheader("Current meal")
    ingredients = st.session_state.meal_ingredients
    if not ingredients:
        st.info("No ingredients added yet.")
    else:
        for index, item in enumerate(ingredients):
            description, remove = st.columns([5, 1])
            description.write(f'{item["name"]} — {item["grams"]:.1f} g')
            if remove.button("Remove", key=f"remove_ingredient_{index}"):
                ingredients.pop(index)
                st.rerun()

        if st.button("Save meal"):
            if not meal_name.strip():
                st.error("Enter a meal name before saving.")
            else:
                meal = Meal(
                    name=meal_name.strip(),
                    ingredients=[
                        Ingredient(
                            fdc_id=item["fdc_id"],
                            grams=item["grams"],
                            name=item["name"],
                        )
                        for item in ingredients
                    ],
                )
                try:
                    meal_id = meal_repository.create_meal(meal)
                except ValueError as error:
                    st.error(str(error))
                else:
                    st.session_state.meal_ingredients = []
                    st.success(f"Saved {meal.name} as meal {meal_id}.")


def render_saved_meals(meal_repository, target_repository, engine) -> None:
    st.header("Saved Meals")
    meals = meal_repository.list_meals()
    if meals.empty:
        st.info("No saved meals yet.")
        return

    meal_ids = [int(value) for value in meals["meal_id"]]
    labels = {
        int(row.meal_id): f"{row.meal_name} ({row.ingredient_count} ingredients)"
        for row in meals.itertuples()
    }
    selected_meal_id = st.selectbox(
        "Saved meal",
        meal_ids,
        format_func=labels.get,
        key="selected_saved_meal",
    )

    try:
        meal = meal_repository.get_meal(selected_meal_id)
        nutrients = engine.calculate_meal(meal)
        targets = target_repository.get_profile("default")
    except ValueError as error:
        st.error(str(error))
        return

    st.subheader(meal.name)
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Food": ingredient.name,
                    "FDC ID": ingredient.fdc_id,
                    "Grams": ingredient.grams,
                }
                for ingredient in meal.ingredients
            ]
        ),
        hide_index=True,
        use_container_width=True,
    )

    st.subheader("Nutrient totals")
    st.dataframe(nutrients, hide_index=True, use_container_width=True)
    st.subheader("Default target score")
    st.caption("Blank values mean the nutrient or limit is not reported/available, not zero.")
    st.dataframe(
        score_display(engine, nutrients, targets),
        hide_index=True,
        use_container_width=True,
    )

    if st.button("Add meal to Today"):
        if selected_meal_id in st.session_state.today_meal_ids:
            st.warning("That meal is already in Today.")
        else:
            st.session_state.today_meal_ids.append(selected_meal_id)
            st.success(f"Added {meal.name} to Today.")


def render_today(meal_repository, target_repository, engine) -> None:
    st.header("Today")
    meal_ids = st.session_state.today_meal_ids
    if not meal_ids:
        st.info("Add saved meals from the Saved Meals tab.")
        return

    meals = []
    for index, meal_id in enumerate(list(meal_ids)):
        try:
            meal = meal_repository.get_meal(meal_id)
        except ValueError as error:
            st.error(str(error))
            continue

        meals.append(meal)
        description, remove = st.columns([5, 1])
        description.write(meal.name)
        if remove.button("Remove", key=f"remove_today_{index}_{meal_id}"):
            meal_ids.pop(index)
            st.rerun()

    if not meals:
        return

    try:
        day_nutrients = engine.calculate_day(meals)
        targets = target_repository.get_profile("default")
    except ValueError as error:
        st.error(str(error))
        return

    st.subheader("Day nutrient totals")
    st.dataframe(day_nutrients, hide_index=True, use_container_width=True)
    st.subheader("Default target score")
    st.caption("Blank values mean the nutrient or limit is not reported/available, not zero.")
    st.dataframe(
        score_display(engine, day_nutrients, targets),
        hide_index=True,
        use_container_width=True,
    )


def main() -> None:
    st.set_page_config(page_title="Nutrition Assistant", layout="wide")
    st.title("Nutrition Assistant")
    initialize_session_state()

    if not DATABASE_PATH.is_file():
        st.error(
            f"Nutrition database not found at {DATABASE_PATH}. "
            "Run initialize_database() before starting the app."
        )
        st.stop()

    try:
        services = get_services(str(DATABASE_PATH))
    except FileNotFoundError as error:
        st.error(str(error))
        st.stop()
    except duckdb.IOException as error:
        st.error(f"Could not open the nutrition database: {error}")
        st.stop()

    food_repository, meal_repository, target_repository, engine = services
    build_tab, saved_tab, today_tab = st.tabs(
        ["Build Meal", "Saved Meals", "Today"]
    )
    with build_tab:
        render_build_meal(food_repository, meal_repository, engine)
    with saved_tab:
        render_saved_meals(meal_repository, target_repository, engine)
    with today_tab:
        render_today(meal_repository, target_repository, engine)

if __name__ == "__main__":
    main()

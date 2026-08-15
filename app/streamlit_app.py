from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st

from nutrition_assistant.config import DATABASE_PATH
from nutrition_assistant.engine.nutrition_engine import NutritionEngine
from nutrition_assistant.models.ingredient import Ingredient
from nutrition_assistant.models.meal import Meal
from nutrition_assistant.planner.optimizer import (
    rank_meals,
    recommendation_explanation,
)
from nutrition_assistant.repositories.food_repository import FoodRepository
from nutrition_assistant.repositories.meal_repository import MealRepository
from nutrition_assistant.repositories.preference_repository import PreferenceRepository
from nutrition_assistant.repositories.target_repository import TargetRepository


@st.cache_resource(show_spinner=False)
def get_services(database_path: str):
    food_repository = FoodRepository(Path(database_path))
    return (
        food_repository,
        MealRepository(food_repository.con),
        PreferenceRepository(food_repository.con),
        TargetRepository(food_repository.con),
        NutritionEngine(food_repository),
    )


def initialize_session_state() -> None:
    st.session_state.setdefault("meal_ingredients", [])
    st.session_state.setdefault("today_entries", [])
    st.session_state.setdefault("today_recommendation_vetoes", set())
    st.session_state.setdefault("editing_meal_id", None)
    st.session_state.setdefault("meal_library_selection_version", 0)
    deleted_meal_id = st.session_state.pop("pending_deleted_meal_id", None)
    if deleted_meal_id is not None:
        clear_deleted_meal_state(st.session_state, deleted_meal_id)
    pending = st.session_state.pop("pending_builder_meal", None)
    if pending is not None:
        st.session_state.meal_ingredients = pending["ingredients"]
        st.session_state.editing_meal_id = pending["meal_id"]
        st.session_state.builder_meal_name = pending["name"]


def clear_deleted_meal_state(state, deleted_meal_id: int) -> None:
    """Remove only session references tied to a successfully deleted meal."""
    state["today_entries"] = [
        entry
        for entry in state.get("today_entries", [])
        if not (
            entry["kind"] == "saved"
            and entry["meal_id"] == deleted_meal_id
        )
    ]
    state.get("today_recommendation_vetoes", set()).discard(deleted_meal_id)

    selection_version = state.get("meal_library_selection_version", 0)
    state.pop(f"selected_saved_meal_{selection_version}", None)
    state["meal_library_selection_version"] = selection_version + 1
    if state.get("confirm_delete_meal_id") == deleted_meal_id:
        state.pop("confirm_delete_meal_id", None)
    state.pop(f"meal_preference_{deleted_meal_id}", None)

    pending_builder = state.get("pending_builder_meal")
    editing_deleted_meal = state.get("editing_meal_id") == deleted_meal_id
    pending_deleted_meal = (
        pending_builder is not None
        and pending_builder.get("meal_id") == deleted_meal_id
    )
    if editing_deleted_meal or pending_deleted_meal:
        state["editing_meal_id"] = None
        state["meal_ingredients"] = []
        state.pop("pending_builder_meal", None)
        state.pop("builder_meal_name", None)
        state.pop("builder_loaded_notice", None)
        for key in list(state):
            if str(key).startswith(f"builder_grams_{deleted_meal_id}_"):
                state.pop(key, None)


def meal_from_builder(name: str = "Custom meal") -> Meal:
    return Meal(
        name=name,
        ingredients=[
            Ingredient(
                fdc_id=item["fdc_id"],
                grams=item["grams"],
                name=item["name"],
            )
            for item in st.session_state.meal_ingredients
        ],
    )


def add_saved_meal_to_today(meal_id: int) -> bool:
    if any(
        entry["kind"] == "saved" and entry["meal_id"] == meal_id
        for entry in st.session_state.today_entries
    ):
        return False
    st.session_state.today_entries.append({"kind": "saved", "meal_id": meal_id})
    return True


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


def available_text(value):
    if pd.isna(value) or value == "":
        return None
    return str(value)


def format_serving(food) -> str | None:
    household_text = available_text(food.household_serving_fulltext)
    serving_size = food.serving_size
    serving_unit = available_text(food.serving_size_unit)

    size_text = None
    if pd.notna(serving_size):
        size_text = f"{serving_size:g}"
        if serving_unit:
            size_text = f"{size_text} {serving_unit}"

    if household_text and size_text:
        return f"{household_text} ({size_text})"
    return household_text or size_text


def format_food_identity(food) -> str:
    brand = available_text(food.brand_name) or available_text(food.brand_owner)
    if brand:
        return brand

    type_labels = {
        "foundation_food": "Generic",
        "sr_legacy_food": "Generic",
        "survey_fndds_food": "Survey food",
        "branded_food": "Branded food",
    }
    return type_labels.get(food.data_type, food.data_type.replace("_", " ").title())


def format_food_result(food) -> str:
    parts = [food.description, format_food_identity(food)]
    serving = format_serving(food)
    if serving:
        parts.append(serving)
    return " | ".join(parts) + f" · FDC {food.fdc_id}"


def food_details(food) -> dict:
    details = {
        "FDC ID": int(food.fdc_id),
        "Description": food.description,
        "Data type": food.data_type,
    }
    optional = {
        "Food category": food.food_category,
        "Brand name": food.brand_name,
        "Brand owner": food.brand_owner,
        "Household serving": food.household_serving_fulltext,
        "Serving size": food.serving_size,
        "Serving size unit": food.serving_size_unit,
    }
    details.update(
        {
            label: value
            for label, value in optional.items()
            if pd.notna(value) and value != ""
        }
    )
    return details


def score_display(engine, nutrients, targets):
    score = engine.score_against_targets(nutrients, targets)
    summary = engine.summarize_score(score).copy()
    summary["display_progress"] = (
        summary["target_progress"]
        .combine_first(summary["minimum_progress"])
        .combine_first(summary["maximum_progress"])
        * 100
    )
    status_labels = {
        "unknown": "? Unknown",
        "low": "↓ Low",
        "approaching": "△ Approaching",
        "acceptable": "✓ Good",
        "within_limit": "✓ Within limit",
        "over_max": "! Over maximum",
    }
    summary["status"] = summary["status"].map(status_labels)
    return summary.rename(
        columns={
            "name": "Nutrient",
            "unit_name": "Unit",
            "amount_consumed": "Amount consumed",
            "minimum_amount": "Minimum",
            "target_amount": "Target",
            "maximum_amount": "Maximum",
            "display_progress": "Progress (%)",
            "remaining_to_target": "Remaining",
            "status": "Status",
            "reported": "Reported",
        }
    )[
        [
            "Nutrient",
            "Amount consumed",
            "Unit",
            "Minimum",
            "Target",
            "Maximum",
            "Progress (%)",
            "Remaining",
            "Status",
            "Reported",
        ]
    ]


def style_score_display(score_table):
    status_colors = {
        "✓ Good": "background-color: #dbeafe; color: #1e3a5f",
        "✓ Within limit": "background-color: #dbeafe; color: #1e3a5f",
        "△ Approaching": "background-color: #fff3bf; color: #5f4800",
        "↓ Low": "background-color: #ffe0b2; color: #5d3500",
        "! Over maximum": "background-color: #f4cccc; color: #6b1d16",
        "? Unknown": "background-color: #e9ecef; color: #343a40",
    }
    return score_table.style.map(
        lambda status: status_colors.get(status, ""),
        subset=["Status"],
    ).format(na_rep="—")


def render_build_meal(food_repository, meal_repository, engine) -> None:
    st.header("Build Meal")
    if st.session_state.pop("builder_loaded_notice", False):
        st.info("Library meal loaded for editing.")
    meal_name = st.text_input("Meal name", key="builder_meal_name")
    search_term = st.text_input("Search food", key="food_search").strip()
    if st.session_state.get("last_food_search") != search_term:
        st.session_state.last_food_search = search_term
        st.session_state.selected_food_fdc_id = None

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
        result_table = pd.DataFrame(
            [
                {
                    "Food": row.description,
                    "Brand / identity": format_food_identity(row),
                    "Serving": format_serving(row),
                    "Food type": row.data_type.replace("_", " ").title(),
                    "FDC ID": int(row.fdc_id),
                }
                for row in results_by_fdc_id.values()
            ]
        )
        selection = st.dataframe(
            result_table,
            hide_index=True,
            width="stretch",
            on_select="rerun",
            selection_mode="single-row",
            key=f"food_results_{search_term}",
        )
        selected_rows = selection.selection.rows
        if selected_rows:
            st.session_state.selected_food_fdc_id = int(
                result_table.iloc[selected_rows[0]]["FDC ID"]
            )
        selected_fdc_id = st.session_state.get("selected_food_fdc_id")
        if selected_fdc_id not in results_by_fdc_id:
            selected_fdc_id = None

        if selected_fdc_id is not None:
            selected_food = results_by_fdc_id[selected_fdc_id]
            fdc_id = int(selected_food.fdc_id)
            portions = food_repository.get_portions(fdc_id)
            usable_portions = engine.get_usable_portions(fdc_id, portions=portions)

            st.subheader(selected_food.description)
            with st.expander("Food details"):
                st.dataframe(
                    pd.DataFrame([food_details(selected_food)]),
                    hide_index=True,
                    width="stretch",
                )
            if portions.empty:
                st.info("No portion information is available. Enter grams directly.")
            else:
                with st.expander("Available portion data"):
                    st.dataframe(
                        portions[
                            ["amount", "modifier", "unit", "gram_weight", "source"]
                        ],
                        hide_index=True,
                        width="stretch",
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
                if len(portion_indexes) == 1:
                    selected_portion_index = portion_indexes[0]
                    st.write(
                        "Portion: "
                        + format_portion(usable_portions.loc[selected_portion_index])
                    )
                else:
                    selected_portion_index = st.selectbox(
                        "Portion",
                        portion_indexes,
                        format_func=lambda index: format_portion(
                            usable_portions.loc[index]
                        ),
                        index=None,
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
            description, amount, remove = st.columns([4, 2, 1])
            description.write(item["name"])
            item["grams"] = amount.number_input(
                "Grams",
                min_value=0.01,
                value=float(item["grams"]),
                step=5.0,
                key=(
                    f'builder_grams_{st.session_state.editing_meal_id or "new"}_'
                    f'{item["fdc_id"]}'
                ),
                label_visibility="collapsed",
            )
            if remove.button("Remove", key=f"remove_ingredient_{index}"):
                ingredients.pop(index)
                st.rerun()

        editing_meal_id = st.session_state.editing_meal_id
        if editing_meal_id is not None:
            update, cancel = st.columns(2)
            if update.button("Update Meal Library", type="primary"):
                if not meal_name.strip():
                    st.error("Enter a meal name before updating.")
                else:
                    try:
                        meal_repository.update_meal(
                            editing_meal_id,
                            meal_from_builder(meal_name.strip()),
                        )
                    except ValueError as error:
                        st.error(str(error))
                    else:
                        st.session_state.meal_ingredients = []
                        st.session_state.editing_meal_id = None
                        st.success("Meal Library entry updated.")
            if cancel.button("Cancel editing"):
                st.session_state.meal_ingredients = []
                st.session_state.editing_meal_id = None
                st.rerun()
        else:
            add_today, save_library, save_and_add = st.columns(3)
            if add_today.button("Add to Today"):
                st.session_state.today_entries.append(
                    {"kind": "temporary", "meal": meal_from_builder()}
                )
                st.session_state.meal_ingredients = []
                st.toast("Added Custom meal to Today without saving it.")
                st.rerun()

            if save_library.button("Save to Meal Library"):
                if not meal_name.strip():
                    st.error("Enter a meal name before saving to the Meal Library.")
                else:
                    try:
                        meal_repository.create_meal(
                            meal_from_builder(meal_name.strip())
                        )
                    except ValueError as error:
                        st.error(str(error))
                    else:
                        st.session_state.meal_ingredients = []
                        st.success(f"Saved {meal_name.strip()} to the Meal Library.")

            if save_and_add.button("Save to Library & Add to Today"):
                if not meal_name.strip():
                    st.error("Enter a meal name before saving to the Meal Library.")
                else:
                    meal = meal_from_builder(meal_name.strip())
                    try:
                        meal_id = meal_repository.create_meal(meal)
                    except ValueError as error:
                        st.error(str(error))
                    else:
                        add_saved_meal_to_today(meal_id)
                        st.session_state.meal_ingredients = []
                        st.toast(f"Saved {meal.name} and added it to Today.")
                        st.rerun()


def render_saved_meals(
    meal_repository, preference_repository, target_repository, engine
) -> None:
    st.header("Meal Library")
    meals = meal_repository.list_meals()
    if meals.empty:
        st.info("No meals in the library yet.")
        return

    meal_ids = [int(value) for value in meals["meal_id"]]
    labels = {
        int(row.meal_id): f"{row.meal_name} ({row.ingredient_count} ingredients)"
        for row in meals.itertuples()
    }
    selected_meal_id = st.selectbox(
        "Library meal",
        meal_ids,
        format_func=labels.get,
        key=(
            "selected_saved_meal_"
            f"{st.session_state.meal_library_selection_version}"
        ),
    )

    try:
        meal = meal_repository.get_meal(selected_meal_id)
    except ValueError as error:
        st.error(str(error))
        return

    st.subheader(meal.name)
    st.dataframe(
        pd.DataFrame([
            {"Food": ingredient.name, "FDC ID": ingredient.fdc_id,
             "Grams": ingredient.grams}
            for ingredient in meal.ingredients
        ]),
        hide_index=True,
        width="stretch",
    )

    preference_labels = {
        "preferred": "Preferred",
        "acceptable": "Acceptable",
        "neutral": "Neutral",
        "avoid": "Avoid",
        "never": "Never suggest",
    }
    current_preference = preference_repository.get_meal_preference(
        selected_meal_id
    )
    selected_preference = st.selectbox(
        "Recommendation preference",
        list(preference_labels),
        index=list(preference_labels).index(current_preference),
        format_func=preference_labels.get,
        key=f"meal_preference_{selected_meal_id}",
    )
    if selected_preference != current_preference:
        try:
            preference_repository.set_meal_preference(
                selected_meal_id, selected_preference
            )
        except ValueError as error:
            st.error(str(error))
        else:
            st.toast(
                f"Preference set to {preference_labels[selected_preference]}."
            )
            st.rerun()

    add, edit, delete = st.columns(3)
    if add.button("Add to Today", key="library_add_today"):
        if add_saved_meal_to_today(selected_meal_id):
            st.toast(f"Added {meal.name} to Today.")
            st.rerun()
        else:
            st.warning("That saved meal is already in Today.")
    if edit.button("Edit meal", key="library_edit"):
        st.session_state.pending_builder_meal = {
            "meal_id": selected_meal_id,
            "name": meal.name,
            "ingredients": [
                {"fdc_id": ingredient.fdc_id, "name": ingredient.name,
                 "grams": ingredient.grams}
                for ingredient in meal.ingredients
            ],
        }
        st.session_state.builder_loaded_notice = True
        st.rerun()
    if delete.button("Delete meal", key="library_delete"):
        st.session_state.confirm_delete_meal_id = selected_meal_id

    if st.session_state.get("confirm_delete_meal_id") == selected_meal_id:
        st.warning(f'Permanently delete "{meal.name}" from the Meal Library?')
        confirm, cancel = st.columns(2)
        if confirm.button("Yes, delete", type="primary"):
            try:
                meal_repository.delete_meal(selected_meal_id)
            except ValueError as error:
                st.error(str(error))
            else:
                st.session_state.pending_deleted_meal_id = selected_meal_id
                st.rerun()
        if cancel.button("Cancel deletion"):
            st.session_state.confirm_delete_meal_id = None
            st.rerun()

    try:
        nutrients = engine.calculate_meal(meal)
        targets = target_repository.get_profile("default")
    except ValueError as error:
        st.warning(f"Nutrient analysis is unavailable for this meal: {error}")
        return

    st.subheader("Nutrient totals")
    with st.expander("Raw nutrient totals"):
        st.dataframe(nutrients, hide_index=True, width="stretch")
    st.subheader("Default target score")
    st.caption("Blank values mean the nutrient or limit is not reported/available, not zero.")
    st.dataframe(
        style_score_display(score_display(engine, nutrients, targets)),
        hide_index=True,
        width="stretch",
    )

def render_today(
    meal_repository, preference_repository, target_repository, engine
) -> None:
    st.header("Today")
    entries = st.session_state.today_entries
    vetoes = st.session_state.today_recommendation_vetoes
    if vetoes and st.button("Clear today's Not today choices"):
        vetoes.clear()
        st.rerun()
    if not entries:
        st.info("Add a one-off meal from Meal Builder or a saved Meal Library entry.")
        return

    meals = []
    saved_meal_ids = []
    for index, entry in enumerate(list(entries)):
        if entry["kind"] == "saved":
            meal_id = entry["meal_id"]
            try:
                meal = meal_repository.get_meal(meal_id)
            except ValueError as error:
                st.error(str(error))
                continue
            saved_meal_ids.append(meal_id)
            label = f"{meal.name} · Meal Library"
        else:
            meal = entry["meal"]
            label = f"{meal.name} · One-off"

        meals.append(meal)
        description, remove = st.columns([5, 1])
        description.write(label)
        if remove.button("Remove", key=f"remove_today_{index}"):
            entries.pop(index)
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
    with st.expander("Raw day nutrient totals"):
        st.dataframe(day_nutrients, hide_index=True, width="stretch")
    st.subheader("Default target score")
    st.caption("Blank values mean the nutrient or limit is not reported/available, not zero.")
    st.dataframe(
        style_score_display(score_display(engine, day_nutrients, targets)),
        hide_index=True,
        width="stretch",
    )

    st.subheader("What should I eat next?")
    st.caption(
        "Prototype recommendations use reported nutrient data and are not "
        "medical advice or a claim of an objectively optimal diet."
    )
    saved_meals = meal_repository.list_meals()
    preferences = preference_repository.get_meal_preferences(
        saved_meals["meal_id"].astype(int).tolist()
    )
    excluded_ids = set(saved_meal_ids) | set(vetoes)
    eligible_rows = saved_meals[
        ~saved_meals["meal_id"].isin(excluded_ids)
        & ~saved_meals["meal_id"].map(
            lambda meal_id: preferences.get(int(meal_id), "neutral")
            in {"avoid", "never"}
        )
    ]
    if eligible_rows.empty:
        st.info("No other saved meals are available to recommend.")
        return

    eligible_ids = []
    eligible_meals = []
    for row in eligible_rows.itertuples(index=False):
        try:
            candidate = meal_repository.get_meal(int(row.meal_id))
            engine.calculate_meal(candidate)
        except ValueError as error:
            st.warning(f'Skipped "{row.meal_name}": {error}')
            continue
        eligible_ids.append(int(row.meal_id))
        eligible_meals.append(candidate)

    if not eligible_meals:
        st.info("No eligible saved meals could be loaded.")
        return

    recommendations = rank_meals(
        current_score=engine.score_against_targets(day_nutrients, targets),
        meals=eligible_meals,
        nutrition_engine=engine,
        meal_ids=eligible_ids,
        preferences=preferences,
    )
    for rank, recommendation in enumerate(recommendations[:3], start=1):
        benefit_text, warning_text = recommendation_explanation(recommendation)
        with st.container(border=True):
            st.markdown(f"**{rank}. {recommendation['meal_name']}**")
            st.write(benefit_text)
            if recommendation["upper_limit_warnings"]:
                st.warning(f"⚠️ {warning_text}")
            else:
                st.write(f"✓ {warning_text}")
            st.caption(
                f"Score: {recommendation['score']:.3f} "
                f"(nutrition {recommendation['nutrition_score']:.3f} + "
                f"preference {recommendation['preference_bonus']:.2f}) · "
                f"Gaps helped: {recommendation['nutrients_helped']}"
            )
            add, not_today = st.columns(2)
            if add.button(
                "Add to Today", key=f"add_recommendation_{recommendation['meal_id']}"
            ):
                add_saved_meal_to_today(recommendation["meal_id"])
                st.rerun()
            if not_today.button(
                "Not today", key=f"veto_recommendation_{recommendation['meal_id']}"
            ):
                vetoes.add(recommendation["meal_id"])
                st.rerun()


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

    (
        food_repository,
        meal_repository,
        preference_repository,
        target_repository,
        engine,
    ) = services
    today_tab, build_tab, saved_tab = st.tabs(
        ["Today", "Meal Builder", "Meal Library"]
    )
    with today_tab:
        render_today(
            meal_repository, preference_repository, target_repository, engine
        )
    with build_tab:
        render_build_meal(food_repository, meal_repository, engine)
    with saved_tab:
        render_saved_meals(
            meal_repository, preference_repository, target_repository, engine
        )

if __name__ == "__main__":
    main()

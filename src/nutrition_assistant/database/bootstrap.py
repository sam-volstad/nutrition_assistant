from pathlib import Path

import duckdb

from nutrition_assistant.config import DATABASE_PATH, PROJECT_ROOT


USDA_TABLES = (
    "food",
    "nutrient",
    "food_nutrient",
    "food_portion",
    "measure_unit",
    "food_category",
    "branded_food",
)

# Current prototype baseline: adult DRI-based daily targets.
# Tuple fields: nutrient_id, minimum, target, maximum, reference_type, notes.
DEFAULT_NUTRIENT_TARGETS = (
    (1008, None, 2576.0, None, "EER", None),
    (1003, 49.0, 49.0, None, "RDA", None),
    (1079, 36.0, 36.0, None, "AI", None),
    (1087, 1000.0, 1000.0, 2500.0, "RDA/UL", None),
    (1089, 8.0, 8.0, 45.0, "RDA/UL", None),
    (
        1090,
        420.0,
        420.0,
        None,
        "RDA",
        "The supplemental magnesium UL does not apply to magnesium from food.",
    ),
    (1092, 3400.0, 3400.0, None, "AI", None),
    (1093, None, 1500.0, 2300.0, "AI/UL", None),
    (1095, 11.0, 11.0, 40.0, "RDA/UL", None),
    (1106, 900.0, 900.0, 3000.0, "RDA/UL", None),
    (1162, 90.0, 90.0, 2000.0, "RDA/UL", None),
    (1114, 15.0, 15.0, 100.0, "RDA/UL", None),
    (1109, 15.0, 15.0, 1000.0, "RDA/UL", None),
    (1185, 120.0, 120.0, None, "AI", None),
    (1177, 400.0, 400.0, 1000.0, "RDA/UL", None),
    (1178, 2.4, 2.4, None, "RDA", None),
)


def initialize_database(
    database_path: str | Path = DATABASE_PATH,
    raw_data_path: str | Path | None = None,
) -> Path:
    """Create the prototype database from USDA CSVs and application tables."""
    database_path = Path(database_path)
    raw_data_path = (
        Path(raw_data_path)
        if raw_data_path is not None
        else PROJECT_ROOT / "data" / "Raw"
    )

    if not database_path.is_file():
        missing_csvs = [
            raw_data_path / f"{table_name}.csv"
            for table_name in USDA_TABLES
            if not (raw_data_path / f"{table_name}.csv").is_file()
        ]
        if missing_csvs:
            missing_list = ", ".join(str(path) for path in missing_csvs)
            raise FileNotFoundError(f"Missing required USDA CSV files: {missing_list}")

    database_path.parent.mkdir(parents=True, exist_ok=True)

    connection = duckdb.connect(str(database_path))
    try:
        connection.execute("BEGIN TRANSACTION")
        _load_usda_tables(connection, raw_data_path)
        _create_views(connection)
        _create_application_schema(connection)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()

    return database_path


def _table_exists(connection, table_name: str) -> bool:
    return connection.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_name = ?
        """,
        [table_name],
    ).fetchone()[0] > 0


def _load_usda_tables(connection, raw_data_path: Path) -> None:
    for table_name in USDA_TABLES:
        if _table_exists(connection, table_name):
            continue

        csv_path = raw_data_path / f"{table_name}.csv"
        if not csv_path.is_file():
            raise FileNotFoundError(
                f"Missing USDA CSV for table '{table_name}': {csv_path}"
            )

        connection.execute(
            f"""
            CREATE TABLE {table_name} AS
            SELECT *
            FROM read_csv_auto(?, sample_size=-1)
            """,
            [str(csv_path)],
        )


def _create_views(connection) -> None:
    connection.execute(
        """
        CREATE OR REPLACE VIEW generic_foods AS
        SELECT *
        FROM food
        WHERE data_type IN (
            'foundation_food',
            'sr_legacy_food',
            'survey_fndds_food'
        )
        """
    )
    connection.execute(
        """
        CREATE OR REPLACE VIEW branded_foods AS
        SELECT *
        FROM food
        WHERE data_type = 'branded_food'
        """
    )


def _create_application_schema(connection) -> None:
    statements = (
        "CREATE SEQUENCE IF NOT EXISTS meal_id_seq START 1",
        "CREATE SEQUENCE IF NOT EXISTS log_id_seq START 1",
        "CREATE SEQUENCE IF NOT EXISTS target_profile_id_seq START 1",
        """
        CREATE TABLE IF NOT EXISTS food_preferences (
            fdc_id BIGINT PRIMARY KEY,
            preference VARCHAR NOT NULL,
            notes VARCHAR
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS meals (
            meal_id BIGINT PRIMARY KEY DEFAULT nextval('meal_id_seq'),
            meal_name VARCHAR NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS meal_ingredients (
            meal_id BIGINT NOT NULL,
            fdc_id BIGINT NOT NULL,
            grams DOUBLE NOT NULL,
            PRIMARY KEY (meal_id, fdc_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS weekly_plan (
            day_of_week INTEGER NOT NULL,
            meal_slot VARCHAR NOT NULL,
            meal_id BIGINT NOT NULL,
            PRIMARY KEY (day_of_week, meal_slot)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS food_log (
            log_id BIGINT PRIMARY KEY DEFAULT nextval('log_id_seq'),
            eaten_at TIMESTAMP,
            input_text VARCHAR,
            fdc_id BIGINT,
            grams DOUBLE,
            match_confidence DOUBLE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS food_aliases (
            alias VARCHAR PRIMARY KEY,
            fdc_id BIGINT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS target_profiles (
            profile_id BIGINT PRIMARY KEY DEFAULT nextval('target_profile_id_seq'),
            profile_name VARCHAR NOT NULL UNIQUE,
            notes VARCHAR
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS nutrient_targets_v2 (
            profile_id BIGINT NOT NULL,
            nutrient_id BIGINT NOT NULL,
            minimum_amount DOUBLE,
            target_amount DOUBLE,
            maximum_amount DOUBLE,
            reference_type VARCHAR,
            period VARCHAR NOT NULL DEFAULT 'daily',
            notes VARCHAR,
            PRIMARY KEY (profile_id, nutrient_id)
        )
        """,
    )

    for statement in statements:
        connection.execute(statement)

    connection.execute(
        """
        INSERT INTO target_profiles (profile_name, notes)
        SELECT 'default', 'Baseline DRI-based nutrition targets'
        WHERE NOT EXISTS (
            SELECT 1
            FROM target_profiles
            WHERE profile_name = 'default'
        )
        """
    )
    _seed_default_targets(connection)


def _seed_default_targets(connection) -> None:
    connection.executemany(
        """
        INSERT OR IGNORE INTO nutrient_targets_v2 (
            profile_id,
            nutrient_id,
            minimum_amount,
            target_amount,
            maximum_amount,
            reference_type,
            period,
            notes
        )
        SELECT
            profile_id,
            ?,
            ?,
            ?,
            ?,
            ?,
            'daily',
            ?
        FROM target_profiles
        WHERE profile_name = 'default'
        """,
        DEFAULT_NUTRIENT_TARGETS,
    )

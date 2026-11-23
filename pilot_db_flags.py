"""Database-only survey flags; never included in an application response."""
from pathlib import Path


def create_survey_flags_view(engine):
    sql = (Path(__file__).resolve().parent / "sql" / "pilot_user_survey_flags.sql").read_text()
    with engine.begin() as connection:
        if engine.dialect.name == "sqlite":
            connection.exec_driver_sql("DROP VIEW IF EXISTS pilot_user_survey_flags")
            sql = sql.replace("CREATE OR REPLACE VIEW", "CREATE VIEW", 1)
        connection.exec_driver_sql(sql)

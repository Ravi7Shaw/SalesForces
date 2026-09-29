import clickhouse_connect

from app.config import settings
from app.services.schema import OBJECTS, SCHEMAS, ddl, table_name  # noqa: F401  (re-exported)


class ClickHouseStore:
    def __init__(self):
        self.client = clickhouse_connect.get_client(
            host=settings.clickhouse_host,
            port=settings.clickhouse_port,
            username=settings.clickhouse_user,
            password=settings.clickhouse_password,
            database="analytics",
        )

    def ensure_table(self, obj: str):
        self.client.command(ddl(obj))

    def ensure_tables(self, objects: list[str]):
        for obj in objects:
            self.ensure_table(obj)

    def insert_rows(self, obj: str, rows: list[list]):
        self.ensure_table(obj)
        if rows:
            self.client.insert(table_name(obj), rows, column_names=SCHEMAS[obj].column_names)

    def create_views(self):
        for obj in OBJECTS:
            t = table_name(obj)
            self.client.command(f"CREATE VIEW IF NOT EXISTS v_{t} AS SELECT * FROM {t} FINAL")

    def count(self, obj: str) -> int:
        return int(self.client.query(f"SELECT count() FROM {table_name(obj)} FINAL").result_rows[0][0])

    def describe(self, obj: str, sample_rows: int = 5) -> dict | None:
        """Schema, engine/partition/primary-key metadata, row count and a few sample rows. None if the table doesn't exist."""
        t = table_name(obj)
        meta = self.client.query(
            "SELECT engine, partition_key, sorting_key, primary_key FROM system.tables "
            "WHERE database = 'analytics' AND name = {t:String}",
            parameters={"t": t},
        ).result_rows
        if not meta:
            return None
        engine, partition_key, sorting_key, primary_key = meta[0]
        columns = self.client.query(
            "SELECT name, type FROM system.columns WHERE database = 'analytics' AND table = {t:String} ORDER BY position",
            parameters={"t": t},
        ).result_rows
        sample = self.client.query(f"SELECT * FROM {t} FINAL ORDER BY organisation_id, id LIMIT {int(sample_rows)}")
        return {
            "object": obj,
            "table": t,
            "engine": engine,
            "partition_key": partition_key,
            "sorting_key": sorting_key,
            "primary_key": primary_key,
            "columns": [{"name": n, "type": ty} for n, ty in columns],
            "rows": self.count(obj),
            "sample": [dict(zip(sample.column_names, (None if v is None else str(v) for v in r))) for r in sample.result_rows],
        }

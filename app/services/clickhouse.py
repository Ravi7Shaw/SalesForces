import re

import clickhouse_connect

from app.config import settings

OBJECT_COLUMNS = {
    "Accounts": ["id String", "name String", "industry Nullable(String)", "website Nullable(String)", "organisation_id String", "ingested_at DateTime"],
    "Contacts": ["id String", "first_name String", "last_name String", "email Nullable(String)", "account_id Nullable(String)", "organisation_id String", "ingested_at DateTime"],
    "Opportunities": ["id String", "name String", "amount Float64", "stage String", "account_id Nullable(String)", "organisation_id String", "ingested_at DateTime"],
    "Leads": ["id String", "first_name String", "last_name String", "company Nullable(String)", "email Nullable(String)", "status String", "organisation_id String", "ingested_at DateTime"],
    "Tasks": ["id String", "subject String", "status String", "activity_date String", "owner_id Nullable(String)", "organisation_id String", "ingested_at DateTime"],
    "Cases": ["id String", "subject String", "status String", "priority String", "account_id Nullable(String)", "organisation_id String", "ingested_at DateTime"],
    "Products": ["id String", "name String", "code String", "family Nullable(String)", "is_active Bool", "organisation_id String", "ingested_at DateTime"],
    "PricebookEntries": ["id String", "product_id String", "price Float64", "is_active Bool", "organisation_id String", "ingested_at DateTime"],
    "Contracts": ["id String", "account_id Nullable(String)", "status String", "start_date String", "end_date String", "organisation_id String", "ingested_at DateTime"],
    "Assets": ["id String", "name String", "account_id Nullable(String)", "product_id Nullable(String)", "status String", "organisation_id String", "ingested_at DateTime"],
}


def table_name(obj: str) -> str:
    return "salesforce_" + re.sub(r"[^a-zA-Z0-9_]", "_", obj).lower()


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
        cols = ", ".join(OBJECT_COLUMNS[obj])
        # ReplacingMergeTree makes re-processing an already landed batch idempotent at query time.
        ddl = (
            f"CREATE TABLE IF NOT EXISTS {table_name(obj)} ({cols}) "
            "ENGINE = ReplacingMergeTree(ingested_at) "
            "PARTITION BY organisation_id "
            "ORDER BY (organisation_id, id)"
        )
        self.client.command(ddl)

    def insert_rows(self, obj: str, rows: list[list]):
        if not rows:
            self.ensure_table(obj)
            return
        self.ensure_table(obj)
        columns = [x.split()[0] for x in OBJECT_COLUMNS[obj]]
        self.client.insert(table_name(obj), rows, column_names=columns)

    def create_views(self):
        self.client.command("CREATE VIEW IF NOT EXISTS v_salesforce_accounts AS SELECT * FROM salesforce_accounts FINAL")
        self.client.command("CREATE VIEW IF NOT EXISTS v_salesforce_contacts AS SELECT * FROM salesforce_contacts FINAL")
        self.client.command("CREATE VIEW IF NOT EXISTS v_salesforce_opportunities AS SELECT * FROM salesforce_opportunities FINAL")

    def count(self, obj: str) -> int:
        self.ensure_table(obj)
        return int(self.client.query(f"SELECT count() FROM {table_name(obj)} FINAL").result_rows[0][0])

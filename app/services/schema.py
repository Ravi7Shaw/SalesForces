"""Single source of truth for every Salesforce object we ingest.

The same definition drives (1) the SOQL sent to Bulk API v2, (2) CSV -> typed
row conversion and (3) the ClickHouse DDL, so the three can never drift apart.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone


@dataclass(frozen=True)
class Field:
    sf: str  # Salesforce API field name (CSV header)
    col: str  # ClickHouse column name
    type: str  # ClickHouse type, e.g. "String", "Nullable(Date)"


@dataclass(frozen=True)
class SObject:
    name: str  # name used by this service / API / UI (plural)
    api_name: str  # real Salesforce sObject API name
    fields: tuple[Field, ...]

    @property
    def soql_fields(self) -> list[str]:
        return [f.sf for f in self.fields]

    @property
    def columns(self) -> list[tuple[str, str]]:
        return (
            [(f.col, f.type) for f in self.fields]
            + [("organisation_id", "String"), ("ingested_at", "DateTime")]
        )

    @property
    def column_names(self) -> list[str]:
        return [c for c, _ in self.columns]


def _o(name: str, api_name: str, *fields: tuple[str, str, str]) -> SObject:
    return SObject(name, api_name, tuple(Field(*f) for f in fields))


_S, _NS = "String", "Nullable(String)"

SCHEMAS: dict[str, SObject] = {
    o.name: o
    for o in [
        _o("Accounts", "Account", ("Id", "id", _S), ("Name", "name", _S),
           ("Industry", "industry", _NS), ("Website", "website", _NS)),
        _o("Contacts", "Contact", ("Id", "id", _S), ("FirstName", "first_name", _NS),
           ("LastName", "last_name", _S), ("Email", "email", _NS), ("AccountId", "account_id", _NS)),
        _o("Opportunities", "Opportunity", ("Id", "id", _S), ("Name", "name", _S),
           ("Amount", "amount", "Nullable(Float64)"), ("StageName", "stage", _S),
           ("CloseDate", "close_date", "Nullable(Date)"), ("AccountId", "account_id", _NS)),
        _o("Leads", "Lead", ("Id", "id", _S), ("FirstName", "first_name", _NS),
           ("LastName", "last_name", _S), ("Company", "company", _S),
           ("Email", "email", _NS), ("Status", "status", _S)),
        _o("Tasks", "Task", ("Id", "id", _S), ("Subject", "subject", _NS), ("Status", "status", _S),
           ("ActivityDate", "activity_date", "Nullable(Date)"), ("OwnerId", "owner_id", _S)),
        _o("Cases", "Case", ("Id", "id", _S), ("Subject", "subject", _NS), ("Status", "status", _S),
           ("Priority", "priority", _NS), ("AccountId", "account_id", _NS)),
        _o("Products", "Product2", ("Id", "id", _S), ("Name", "name", _S),
           ("ProductCode", "code", _NS), ("Family", "family", _NS), ("IsActive", "is_active", "Bool")),
        _o("PricebookEntries", "PricebookEntry", ("Id", "id", _S), ("Product2Id", "product_id", _S),
           ("UnitPrice", "price", "Float64"), ("IsActive", "is_active", "Bool")),
        _o("Contracts", "Contract", ("Id", "id", _S), ("AccountId", "account_id", _S), ("Status", "status", _S),
           ("StartDate", "start_date", "Nullable(Date)"), ("EndDate", "end_date", "Nullable(Date)")),
        _o("Assets", "Asset", ("Id", "id", _S), ("Name", "name", _S), ("AccountId", "account_id", _NS),
           ("Product2Id", "product_id", _NS), ("Status", "status", _NS)),
    ]
}

OBJECTS = list(SCHEMAS)


def table_name(obj: str) -> str:
    return "salesforce_" + re.sub(r"[^a-zA-Z0-9_]", "_", obj).lower()


def ddl(obj: str) -> str:
    cols = ", ".join(f"{c} {t}" for c, t in SCHEMAS[obj].columns)
    # ReplacingMergeTree keyed on (organisation_id, id): re-landing a batch is idempotent (query with FINAL).
    return (
        f"CREATE TABLE IF NOT EXISTS {table_name(obj)} ({cols}) "
        "ENGINE = ReplacingMergeTree(ingested_at) "
        "ORDER BY (organisation_id, id) "
        "PARTITION BY organisation_id "
        "PRIMARY KEY (organisation_id, id)"
    )


def soql(obj: str, limit: int) -> str:
    s = SCHEMAS[obj]
    return f"SELECT {', '.join(s.soql_fields)} FROM {s.api_name} LIMIT {int(limit)}"


_DEFAULTS = {"String": "", "Float64": 0.0, "Bool": False, "Date": date(1970, 1, 1)}


def _convert(value: str | None, ch_type: str):
    nullable = ch_type.startswith("Nullable(")
    base = ch_type[len("Nullable("):-1] if nullable else ch_type
    if value is None or value == "":
        return None if nullable else _DEFAULTS[base]
    if base == "String":
        return value
    if base == "Float64":
        return float(value)
    if base == "Bool":
        return value.strip().lower() in {"true", "1", "yes"}
    if base == "Date":
        return date.fromisoformat(value.strip()[:10])
    raise ValueError(f"Unsupported ClickHouse type {ch_type}")


def csv_rows(obj: str, text: str, organisation_id: str) -> list[list]:
    """Parse a Bulk API CSV into typed rows matching ``SCHEMAS[obj].column_names``."""
    import csv
    import io

    schema = SCHEMAS[obj]
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return []  # Salesforce returns an empty body when the query matches nothing
    missing = [f.sf for f in schema.fields if f.sf not in reader.fieldnames]
    if missing:
        raise ValueError(f"{obj}: CSV is missing expected columns {missing}")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = []
    for line_no, raw in enumerate(reader, start=2):
        row = []
        for f in schema.fields:
            try:
                row.append(_convert(raw.get(f.sf), f.type))
            except ValueError as exc:
                raise ValueError(f"{obj} line {line_no}, field {f.sf}: bad value {raw.get(f.sf)!r} ({exc})") from exc
        row.extend([organisation_id, now])
        rows.append(row)
    return rows

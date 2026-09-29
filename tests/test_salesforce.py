import pytest

from app.services.salesforce import OBJECTS, csv_rows


def test_has_ten_objects():
    assert len(OBJECTS) >= 10


def test_account_mapping():
    csv = "Id,Name,Industry,Website\nA1,Acme,Technology,https://acme.test\n"
    rows = csv_rows("Accounts", csv, "org1")
    assert rows[0][0] == "A1"
    assert rows[0][4] == "org1"


def test_all_object_csv_shapes():
    samples = {
        "Accounts": "Id,Name,Industry,Website\nA1,A,Tech,http://a\n",
        "Contacts": "Id,FirstName,LastName,Email,AccountId\nC1,F,L,e@x,A1\n",
        "Opportunities": "Id,Name,Amount,StageName,CloseDate,AccountId\nO1,O,10.5,Prospecting,2026-12-31,A1\n",
        "Leads": "Id,FirstName,LastName,Company,Email,Status\nL1,F,L,C,e@x,Open\n",
        "Tasks": "Id,Subject,Status,ActivityDate,OwnerId\nT1,S,Open,2026-09-22,U1\n",
        "Cases": "Id,Subject,Status,Priority,AccountId\nCA1,S,Open,Normal,A1\n",
        "Products": "Id,Name,ProductCode,Family,IsActive\nP1,P,SKU,F,true\n",
        "PricebookEntries": "Id,Product2Id,UnitPrice,IsActive\nPB1,P1,10,true\n",
        "Contracts": "Id,AccountId,Status,StartDate,EndDate\nCO1,A1,Active,2026-01-01,2027-01-01\n",
        "Assets": "Id,Name,AccountId,Product2Id,Status\nAS1,A,A1,P1,Active\n",
    }
    for obj, text in samples.items():
        assert len(csv_rows(obj, text, "org1")) == 1


def test_blank_optional_field_becomes_null_not_empty_string():
    csv = "Id,FirstName,LastName,Email,AccountId\nC1,,Doe,,\n"
    rows = csv_rows("Contacts", csv, "org1")
    first_name, account_id = rows[0][1], rows[0][4]
    assert first_name is None
    assert account_id is None


def test_missing_expected_column_raises():
    csv = "Id,Name\nA1,Acme\n"  # missing Industry, Website
    with pytest.raises(ValueError, match="missing expected columns"):
        csv_rows("Accounts", csv, "org1")


def test_real_salesforce_api_names_used_in_soql():
    from app.services.schema import soql

    assert " FROM Account " in soql("Accounts", 10) + " "
    assert " FROM PricebookEntry " in soql("PricebookEntries", 10) + " "

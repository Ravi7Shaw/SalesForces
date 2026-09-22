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
        "Opportunities": "Id,Name,Amount,StageName,AccountId\nO1,O,10.5,Prospecting,A1\n",
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

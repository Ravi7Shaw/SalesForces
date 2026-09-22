from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api.routes import router
from app.db import Base, engine
from app.models import IngestionJob

Base.metadata.create_all(engine)

app = FastAPI(title="BE-1 Salesforce Bulk Ingestion Platform", version="2.0.0")
app.include_router(router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "be1-api"}


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    return FileResponse("frontend/index.html")

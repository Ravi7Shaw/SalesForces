from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api.routes import router
from app.auth import validate_auth_config
from app.config import settings
from app.db import Base, engine
from app.models import IngestionJob, JobObject  # noqa: F401  (registers the tables)
from app.services.worker import manager


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Schema creation + auth validation + crash recovery happen at startup, not import,
    # so importing the app (e.g. in tests) never needs a live database.
    validate_auth_config()
    Base.metadata.create_all(engine)
    recovered = manager.startup_recovery()
    if recovered:
        import logging

        logging.getLogger("ingest").warning("recovered %d orphaned job(s) as interrupted: %s", len(recovered), recovered)
    yield


app = FastAPI(title="BE-1 Salesforce Bulk Ingestion Platform", version="2.2.0", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "be1-api", "auth_enabled": settings.auth_enabled}


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    # Static HTML only, no data. Browsers can't attach a custom header when navigating here,
    # so auth is enforced on the /api/v1/* calls the page itself makes (see app/auth.py and
    # frontend/index.html, which prompts for and stores the API key in-memory).
    return FileResponse("frontend/index.html")

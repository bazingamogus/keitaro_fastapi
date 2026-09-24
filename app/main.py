from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from . import crud, models, schemas
from .database import Base, engine, get_db
from .deps import get_client
from .keitaro_client import KeitaroAPIError, KeitaroClient
from .logging_config import setup_logging
from .stream_editor import router as stream_editor_router

BASE_DIR = Path(__file__).resolve().parent

logger = setup_logging()

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Keitaro Campaign Creator")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.include_router(stream_editor_router)


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.post("/api/offers/sync")
def sync_offers(db: Session = Depends(get_db), client: KeitaroClient = Depends(get_client)):
    logger.info("Синхронизация офферов: старт")
    try:
        count = crud.sync_offers(db, client)
    except KeitaroAPIError as e:
        logger.error("Ошибка синхронизации офферов: %s", e)
        raise HTTPException(status_code=502, detail=str(e))
    logger.info("Синхронизировано офферов: %s", count)
    return {"synced": count}


@app.get("/api/offers/search", response_model=list[schemas.OfferOut])
def search_offers(q: str = "", db: Session = Depends(get_db)):
    return crud.search_offers(db, q)


@app.get("/api/campaigns", response_model=list[schemas.CampaignLogOut])
def list_campaigns(db: Session = Depends(get_db)):
    return crud.list_logs(db)


@app.post("/api/campaigns", response_model=schemas.CampaignCreateResponse)
def create_campaign(
    payload: schemas.CampaignCreateRequest,
    db: Session = Depends(get_db),
    client: KeitaroClient = Depends(get_client),
):
    logger.info(
        "Создаю кампанию: name=%r geo=%s offer_id=%s",
        payload.name, ",".join(payload.geo), payload.offer_id,
    )
    log = crud.create_log(db, name=payload.name, geo=",".join(payload.geo), offer_id=payload.offer_id)

    try:
        group_id = client.find_campaign_group_id()
        domain_id = client.find_first_id_optional("domains")
        traffic_source_id = client.find_first_id_optional("traffic_sources")
        logger.info(
            "group_id=%s domain_id=%s traffic_source_id=%s",
            group_id, domain_id, traffic_source_id,
        )

        geo_filter_name = client.find_filter_name("country")
        action = client.get_default_action()
        logger.info("geo_filter=%s action=%s", geo_filter_name, action.get("key"))

        campaign_id = client.create_campaign(payload.name, group_id, traffic_source_id, domain_id)
        logger.info("campaign_id=%s", campaign_id)
        stream1_id = client.create_geo_redirect_stream(
            campaign_id, payload.geo, geo_filter_name, action
        )
        logger.info("geo_redirect_stream_id=%s", stream1_id)
        stream2_id = client.create_offer_stream(campaign_id, payload.offer_id, action)
        logger.info("offer_stream_id=%s", stream2_id)

        crud.update_log(
            db, log,
            status="success",
            keitaro_campaign_id=campaign_id,
            geo_redirect_stream_id=stream1_id,
            offer_stream_id=stream2_id,
        )
        logger.info("Кампания %r создана успешно (id=%s)", payload.name, campaign_id)
        return schemas.CampaignCreateResponse(
            campaign_id=campaign_id,
            geo_redirect_stream_id=stream1_id,
            offer_stream_id=stream2_id,
        )
    except KeitaroAPIError as e:
        logger.error("Ошибка создания кампании %r: %s", payload.name, e)
        crud.update_log(db, log, status="error", error=str(e))
        raise HTTPException(status_code=502, detail=str(e))

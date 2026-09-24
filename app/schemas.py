from typing import List

from pydantic import BaseModel, Field


class OfferOut(BaseModel):
    id: int
    name: str

    model_config = {"from_attributes": True}


class CampaignCreateRequest(BaseModel):
    name: str = Field(..., min_length=1)
    geo: List[str] = Field(..., min_length=1, description="Список ISO-кодов стран")
    offer_id: int


class CampaignCreateResponse(BaseModel):
    campaign_id: int
    geo_redirect_stream_id: int
    offer_stream_id: int


class CampaignLogOut(BaseModel):
    id: int
    name: str
    geo: str
    offer_id: int
    keitaro_campaign_id: int | None
    geo_redirect_stream_id: int | None
    offer_stream_id: int | None
    status: str
    error: str | None

    model_config = {"from_attributes": True}


class StreamOfferAdd(BaseModel):
    offer_id: int
    campaign_id: int


class StreamOfferUpdate(BaseModel):
    share: int | None = Field(None, ge=0, le=100)
    pinned: bool | None = None

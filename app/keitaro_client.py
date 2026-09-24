"""
Тонкий клиент над Keitaro Admin API.

"""
import json
import logging
import re
import time

import requests

logger = logging.getLogger("keitaro.client")

TIMEOUT = (5, 30)             # сек.: (подключение, ожидание ответа)
MAX_RETRIES = 3               # повторов после первой попытки
BACKOFF = 1.0                 # пауза 1, 2, 4 с; при 429 — Retry-After (не больше MAX_WAIT)
MAX_WAIT = 30
RETRY_STATUSES = {500, 502, 503, 504}


class KeitaroAPIError(RuntimeError):
    pass


class KeitaroClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Api-Key": api_key,
            "Content-Type": "application/json",
        })

    # --- низкоуровневые вызовы -------------------------------------------------
    def get(self, path: str, params: dict | None = None):
        logger.debug("GET %s params=%s", path, params)
        r = self._request("GET", path, params=params, idempotent=True)
        logger.info("GET %s -> %s", path, r.status_code)
        self._check(r)
        return r.json()

    def post(self, path: str, payload: dict, idempotent: bool = False):
        """idempotent=True — только для POST без побочных эффектов (report/build)."""
        logger.info("POST %s payload=%s", path, json.dumps(payload, ensure_ascii=False))
        r = self._request("POST", path, payload=payload, idempotent=idempotent)
        logger.info("POST %s -> %s", path, r.status_code)
        self._check(r)
        return r.json()

    def put(self, path: str, payload: dict):
        logger.info("PUT %s payload=%s", path, json.dumps(payload, ensure_ascii=False))
        r = self._request("PUT", path, payload=payload, idempotent=True)
        logger.info("PUT %s -> %s", path, r.status_code)
        self._check(r)
        return r.json()

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 payload: dict | None = None, idempotent: bool) -> requests.Response:
        """Запрос с таймаутом и повторами.
        429 и таймаут подключения повторяются всегда (запрос не был обработан).
        5xx, таймаут ответа и обрыв соединения — только для idempotent-запросов:
        повтор POST /campaigns или POST /streams мог бы создать дубликат."""
        url = f"{self.base_url}{path}"
        for attempt in range(MAX_RETRIES + 1):
            last = attempt == MAX_RETRIES
            wait = BACKOFF * 2 ** attempt
            try:
                r = self.session.request(method, url, params=params, json=payload, timeout=TIMEOUT)
            except requests.ConnectTimeout as e:
                reason = e
            except (requests.Timeout, requests.ConnectionError) as e:
                if not idempotent:
                    raise KeitaroAPIError(f"{method} {url}: {e} (не повторяю — запрос мог быть обработан)")
                reason = e
            else:
                if r.status_code == 429:
                    wait = self._retry_after(r, wait)
                elif not (idempotent and r.status_code in RETRY_STATUSES):
                    return r
                if last:
                    return r
                reason = f"HTTP {r.status_code}"
            if last:
                raise KeitaroAPIError(f"{method} {url}: {reason} (после {MAX_RETRIES} повторов)")
            logger.warning("%s %s: %s — повтор %s/%s через %.0f с",
                           method, path, reason, attempt + 1, MAX_RETRIES, wait)
            time.sleep(wait)
        raise AssertionError("unreachable")

    @staticmethod
    def _retry_after(r: requests.Response, default: float) -> float:
        try:
            return min(float(r.headers.get("Retry-After", default)), MAX_WAIT)
        except ValueError:            # Retry-After в формате даты — используем свою паузу
            return default

    @staticmethod
    def _check(r: requests.Response):
        if not r.ok:
            raise KeitaroAPIError(f"{r.request.method} {r.url} -> {r.status_code}: {r.text}")

    # --- обнаружение id/enum-значений по документированным discovery-эндпоинтам -
    def find_first_id(self, endpoint: str, label: str, params: dict | None = None):
        data = self.get(f"/{endpoint}", params=params)
        if not data:
            raise KeitaroAPIError(f"В аккаунте нет ни одного объекта '{label}' ({endpoint}).")
        return data[0]["id"]

    def find_first_id_optional(self, endpoint: str, params: dict | None = None):
        """Как find_first_id, но не падает, если объектов нет — возвращает None.
        Подходит для полей, необязательных в CampaignRequest (domain_id,
        traffic_source_id): если в аккаунте ещё не заведено ни одного домена/
        источника, кампанию всё равно можно создать без них."""
        data = self.get(f"/{endpoint}", params=params)
        return data[0]["id"] if data else None

    # /groups требует обязательный query-параметр 'type'
    # campaigns | offers | landings | domains 
    def find_campaign_group_id(self, label: str = "group"):
        return self.find_first_id_optional("groups", params={"type": "campaigns"})

    def find_filter_name(self, hint: str) -> str:
        filters = self.get("/stream_filters")
        for f in filters:
            candidate = (f.get("value") or f.get("name") or "").lower()
            if hint in candidate:
                return f.get("value") or f.get("name")
        available = [f.get("value") or f.get("name") for f in filters]
        raise KeitaroAPIError(
            f"Не нашёл фильтр, содержащий '{hint}', среди /stream_filters. "
            f"Доступно: {available}"
        )

    def get_default_action(self) -> dict:
        # take the first one by default
        actions = self.get("/streams_actions")
        if not actions:
            raise KeitaroAPIError("GET /streams_actions вернул пустой список.")
        return actions[0]

    # --- офферы (для автокомплита) ---------------------------------------------
    def list_offers(self) -> list[dict]:
        return self.get("/offers")

    # --- создание кампании -------------------------------------------------------
    def create_campaign(self, name: str, group_id: int | None,
                         traffic_source_id: int | None, domain_id: int | None) -> int:
        payload = {
            "name": name,
            "alias": re.sub(r'[^a-z0-9\-\_]', '', name.lower()), # making alias url-safe
            "type": "position",        # enum: position | weight
            "state": "active",         # enum: active | disabled | archived
            "cost_type": "CPC",        # enum: CPM | CPC | CPAR | RevShare | CPA | KPC | CPAG | CPP
            "cost_value": 0,
        }
        # group_id / traffic_source_id / domain_id необязательны в CampaignRequest —
        # передаём их, только если нашли существующий объект.
        if group_id is not None:
            payload["group_id"] = group_id
        if traffic_source_id is not None:
            payload["traffic_source_id"] = traffic_source_id
        if domain_id is not None:
            payload["domain_id"] = domain_id
        return self.post("/campaigns", payload)["id"]

    def create_geo_redirect_stream(self, campaign_id: int, geo_list: list[str],
                                    geo_filter_name: str, action: dict,
                                    position: int = 1) -> int:
        # geo redirect stream
        option_field = action.get("field") or "url"
        payload = {
            "campaign_id": campaign_id,
            "type": "regular",         # Flow type enum: format | regular | default
            "schema": "redirect",      # enum: landings | redirect | action
            "name": f"GEO {','.join(geo_list)} -> Google",
            "position": position,
            "state": "active",         # enum: active | disabled | deleted
            "action_type": action["key"],
            "action_options": {option_field: "https://www.google.com"},
            "filter_or": False,
            "filters": [
                {"name": geo_filter_name, "mode": "accept", "payload": geo_list}
            ],
        }
        return self.post("/streams", payload)["id"]

    def create_offer_stream(self, campaign_id: int, offer_id: int,
                             action: dict, position: int = 2) -> int:
        # stream for the offer itself
        payload = {
            "campaign_id": campaign_id,
            "type": "regular",
            "schema": "landings",
            "name": "Offer",
            "position": position,
            "state": "active",
            "action_type": action["key"],
            "offers": [{"offer_id": offer_id, "share": 100, "state": "active"}],
        }
        return self.post("/streams", payload)["id"]

    # --- редактор потоков -----------------------------------------------------------
    def list_campaigns(self) -> list[dict]:
        return self.get("/campaigns")

    def get_campaign(self, campaign_id: int) -> dict:
        return self.get(f"/campaigns/{campaign_id}")

    def list_campaign_streams(self, campaign_id: int) -> list[dict]:
        return self.get(f"/campaigns/{campaign_id}/streams")

    def get_stream(self, stream_id: int) -> dict:
        return self.get(f"/streams/{stream_id}")

    def update_stream_offers(self, stream_id: int, offers: list[dict]) -> dict:
        # offers — полный список: всё, чего в нём нет, из потока удаляется
        return self.put(f"/streams/{stream_id}", {"offers": offers})

    def build_report(self, campaign_id: int, interval: str) -> list[dict]:
        """Клики/конверсии/доход по (stream_id, offer_id) за interval (today, yesterday, ...)."""
        payload = {
            "range": {"interval": interval},
            "dimensions": ["stream_id", "offer_id"],
            "measures": ["clicks", "conversions", "revenue"],
            "filters": [{"name": "campaign_id", "operator": "EQUALS", "expression": campaign_id}],
        }
        return self.post("/report/build", payload, idempotent=True).get("rows", [])

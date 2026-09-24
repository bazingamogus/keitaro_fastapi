## Структура

```
app/
├── config.py              — настройки из .env (pydantic-settings)
├── database.py            — SQLAlchemy engine/session (по умолчанию SQLite)
├── models.py              — таблицы БД (кэш офферов, журнал, черновик и кэш редактора потоков)
├── schemas.py             — Pydantic-схемы запросов/ответов
├── keitaro_client.py      — весь HTTP-код похода в Keitaro Admin API
├── crud.py                — операции с локальной БД
├── deps.py                — общие FastAPI-зависимости (get_client)
├── logging_config.py      — логирование в logs/app.log
├── main.py                — FastAPI-приложение, роуты создания кампании и офферов
├── stream_editor.py       — FastAPI-роуты редактора потоков
├── streams_service.py     — логика редактора: загрузка, правки, ребаланс, submit
└── static/
    ├── index.html         — фронтенд (форма Name/Geo/Offer/Create)
    └── streams.html       — фронтенд редактора потоков
```

## Запуск

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# в .env вставьте свой KEITARO_API_KEY

uvicorn app.main:app --reload
```

## Создание кампании http://127.0.0.1:8000

Веб-инструмент для создания кампании в Keitaro с двумя потоками:
GEO-редирект на google.com + дефолтный поток на выбранный оффер.

Перед первым использованием автокомплита офферов нажмите
«Обновить кэш офферов» (дергает `POST /api/offers/sync`, который тянет
`GET /offers` из Keitaro и складывает в локальную таблицу `offers`,
чтобы автокомплит работал по SQL `ILIKE`, а не ходил в Keitaro на
каждое нажатие клавиши).

## Редактор потоков http://127.0.0.1:8000/streams

Список кампаний из Keitaro → клик по кампании (`/streams/{id}`) → потоки
со схемой `landings` и их офферы:

- **Add / Remove / Restore** оффера (поиск по кэшу офферов) и правка
  **Share** кликом по проценту. Правки сохраняются только локально
  (таблица `stream_offers`) и отправляются в Keitaro (`PUT /streams/{id}`)
  только кнопкой **Submit changes to KT**; **Undo changes** откатывает
  черновик без вызовов API. Несохранённые правки переживают перезагрузку.
- Удалённый оффер не стирается из БД, а помечается `removed = true` и
  показывается серым с кнопкой **Restore**.
- Share незакреплённых активных офферов автоматически делится поровну
  (34/33/33). Ручная правка share закрепляет оффер (иконка булавки);
  закрепление — локальная настройка, в Keitaro не отправляется.
- **Stats / Trends** — клики, конверсии, CR, доход за сегодня и вчера
  (`POST /report/build`; если отчёт недоступен — колонки пустые).
- Кампании, потоки, preview и статистика кэшируются в БД (`editor_*`
  таблицы). Keitaro читается только при первом открытии списка/кампании и
  по кнопкам **Fetch campaigns from KT** / **Fetch streams from KT**
  (несохранённые правки кампании при этом теряются). Submit делает
  `PUT` + `GET /streams/{id}` на каждый изменённый поток. **View in KT** —
  открыть кампанию в админке Keitaro.

## Логи

Все действия (синхронизация офферов, создание кампании по шагам,
каждый запрос к Keitaro API с payload и статусом ответа, ошибки)
пишутся в `logs/app.log` (ротация: 5 файлов по 5 МБ).
Путь и уровень настраиваются в `.env`:
`LOG_FILE=logs/app.log`, `LOG_LEVEL=INFO` (или `DEBUG`).
На странице показывается только итоговый статус операции.

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    keitaro_base_url: str = "https://tlgk.host/admin_api/v1"
    keitaro_api_key: str = ""
    database_url: str = "sqlite:///./keitaro.db"
    log_file: str = "logs/app.log"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()

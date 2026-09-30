from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read from environment variables, or from backend/.env in local development."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    environment: str = "development"

    # The API connects as denti_app, which cannot bypass row level security.
    database_url: str
    # Migrations and the seed script run as denti_owner, which owns the tables.
    migration_database_url: str

    jwt_secret: str
    # Password hashing cost. 12 in real use; tests lower it so they run quickly.
    bcrypt_rounds: int = 12
    cors_origins: str = "http://localhost:5180"
    # Where the app's pages are, for links sent to patients (a shared prescription opens at
    # <app_base_url>/<clinic>/d/<token>).
    app_base_url: str = "http://localhost:5180"

    # The browser reaches the API under this path (the Vite dev server and the hosting
    # rewrite forward /api to this app). Cookies are scoped to it.
    api_path_prefix: str = "/api"
    cookie_secure: bool = False

    access_token_minutes: int = 15
    refresh_token_days: int = 7
    trusted_device_days: int = 30
    login_code_minutes: int = 10
    login_code_max_attempts: int = 5
    login_code_resend_seconds: int = 30
    login_codes_per_15_minutes: int = 5

    # "local" keeps files in backend/.storage (development and tests). "s3" is used live.
    storage_backend: str = "local"
    storage_dir: str = ".storage"
    s3_bucket: str | None = None

    # How a move to a higher plan is paid: "test" (no money taken) or, once connected,
    # "razorpay". Left empty, it is "test" everywhere except production.
    payment_backend: str | None = None

    # "console" prints emails in the backend window (local development and tests).
    email_backend: str = "console"
    email_from: str = "Denti <no-reply@denti.in>"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()

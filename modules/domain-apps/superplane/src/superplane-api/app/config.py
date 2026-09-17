"""Application settings loaded from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Control plane API server configuration.

    All values can be overridden via environment variables.
    For Aurora IAM auth in production, set DATABASE_URL to the Aurora endpoint
    and configure IAM authentication at the driver level.
    """

    # Application
    app_name: str = "superplane-api"
    app_version: str = "0.1.0"
    debug: bool = False

    # Database
    database_url: str = (
        "postgresql+asyncpg://superplane:superplane@localhost:5432/superplane"
    )

    # AWS
    aws_region: str = "us-east-1"
    aws_account_id: str = ""

    # Cognito
    cognito_user_pool_id: str = ""
    cognito_app_client_id: str = ""
    cognito_app_client_secret: str = ""

    # JWT Auth
    jwt_secret_key: str = "CHANGE-ME-IN-PRODUCTION"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # Internal API (machine-to-machine auth for bootstrap workflows)
    internal_api_token: str = ""

    # GitHub Actions (for workspace bootstrap/teardown)
    github_token: str = ""
    github_repo: str = "aws-innovate/AISuperPlane"

    # CORS
    cors_origins: list[str] = ["*"]

    # Rate limiting
    rate_limit_per_minute: int = 60

    model_config = {"env_prefix": "", "case_sensitive": False}


settings = Settings()

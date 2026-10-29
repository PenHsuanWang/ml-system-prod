from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config.constants import DEFAULT_APP_NAME, DEFAULT_QUEUE_NAME
from src.config.environment import Environment


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_name: str = DEFAULT_APP_NAME
    environment: Environment = "development"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    workers: int = Field(default=1, ge=1)
    reload: bool = False
    log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"
    task_backend: Literal["disabled", "memory", "distributed"] = "memory"
    expose_job_api: bool = False
    task_concurrency: int = Field(default=4, ge=1, le=1024)
    task_queue_size: int = Field(default=100, ge=1)
    job_retention_seconds: float = Field(default=3600, gt=0)
    job_max_records: int = Field(default=1000, ge=1)
    job_timeout_seconds: float = Field(default=60, gt=0)
    shutdown_timeout_seconds: float = Field(default=10, gt=0)
    worker_processes: int = Field(default=1, ge=1)
    rabbitmq_url: SecretStr = SecretStr("amqp://guest:guest@localhost:5672/")
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    queue_name: str = Field(default=DEFAULT_QUEUE_NAME, min_length=1, max_length=100)
    http_timeout_seconds: float = Field(default=10, gt=0)
    mongo_enabled: bool = False
    mongo_uri: SecretStr = SecretStr("mongodb://localhost:27017")
    mongo_database: str = Field(default="backend", pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,62}$")
    mongo_pool_size: int = Field(default=20, ge=1, le=1000)
    identity_enabled: bool = False
    jwt_secret: SecretStr | None = None
    jwt_issuer: str = "backend"
    jwt_audience: str = "backend-api"
    access_token_seconds: int = Field(default=900, ge=60, le=3600)
    password_hash_concurrency: int = Field(default=2, ge=1, le=32)
    thread_workers: int = Field(default=4, ge=1, le=128)
    thread_queue_size: int = Field(default=16, ge=0, le=10000)
    kernel_api_enabled: bool = False
    exports_api_enabled: bool = False
    kernel_timeout_seconds: float = Field(default=30, gt=0)
    resource_backend: Literal["memory", "redis"] = "memory"
    resource_limits: dict[str, int] = Field(default_factory=lambda: {"compute": 1})
    resource_lease_seconds: float = Field(default=30, ge=3, le=3600)

    @model_validator(mode="after")
    def validate_runtime(self) -> Self:
        if self.reload and self.workers != 1:
            raise ValueError("reload requires exactly one Uvicorn worker")
        if self.task_backend == "memory" and self.workers != 1:
            raise ValueError(
                "memory task backend requires one worker; use disabled or distributed for multiple"
            )
        if self.environment == "production" and self.task_backend == "memory":
            raise ValueError("production requires disabled or distributed task backend")
        if self.task_backend == "disabled" and self.expose_job_api:
            raise ValueError("job queries require an enabled task backend")
        if self.identity_enabled:
            if not self.mongo_enabled:
                raise ValueError("identity requires MongoDB")
            if self.jwt_secret is None or len(self.jwt_secret.get_secret_value().encode()) < 32:
                raise ValueError("identity requires a JWT secret of at least 32 bytes")
        if (self.kernel_api_enabled or self.exports_api_enabled) and not self.identity_enabled:
            raise ValueError("kernel/export APIs require identity and data authorization")
        if self.environment == "production" and self.expose_job_api and not self.identity_enabled:
            raise ValueError("production job queries require identity")
        if self.kernel_api_enabled and self.workers > 1 and self.resource_backend != "redis":
            raise ValueError("multi-worker kernel execution requires Redis resource coordination")
        if (
            self.environment == "production"
            and self.kernel_api_enabled
            and self.resource_backend != "redis"
        ):
            raise ValueError("production kernel execution requires Redis resource coordination")
        if any(
            not key or not isinstance(value, int) or isinstance(value, bool) or value < 1
            for key, value in self.resource_limits.items()
        ):
            raise ValueError("resource_limits must contain nonempty keys and positive capacities")
        return self

"""读取与 Java 同名的环境变量，并把 JDBC URL 转成 SQLAlchemy URL。"""

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def database_url_from_jdbc(jdbc_url: str, username: str, password: str) -> str:
    """``jdbc:mysql://host:port/db?...`` → ``mysql+asyncmy://...``。"""
    parsed = urlsplit(jdbc_url.removeprefix("jdbc:"))
    return f"mysql+asyncmy://{username}:{password}@{parsed.hostname}:{parsed.port}{parsed.path}"


class Settings(BaseSettings):
    """服务配置。Python 栈默认端口与 Java 栈错开。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    jwt_secret: str = Field(default="", validation_alias="AUTOWONDER_JWT_SECRET")
    jwt_access_ttl_seconds: int = 7200
    jwt_refresh_ttl_seconds: int = 604800
    secret_master_key: str = Field(default="", validation_alias="AUTOWONDER_SECRET_MASTER_KEY")
    database_url: str = Field(
        default="mysql+asyncmy://root:autowonder@127.0.0.1:33061/autowonder",
        validation_alias="AUTOWONDER_DATABASE_URL",
    )
    spring_datasource_url: str = Field(default="", validation_alias="SPRING_DATASOURCE_URL")
    spring_datasource_username: str = Field(
        default="autowonder",
        validation_alias="SPRING_DATASOURCE_USERNAME",
    )
    spring_datasource_password: str = Field(
        default="",
        validation_alias="SPRING_DATASOURCE_PASSWORD",
    )
    redis_host: str = Field(default="127.0.0.1", validation_alias="REDIS_HOST")
    redis_port: int = Field(default=63791, validation_alias="REDIS_PORT")
    redis_password: str = Field(default="", validation_alias="REDIS_PASSWORD")
    redis_database: int = Field(default=0, validation_alias="REDIS_DATABASE")
    http_port: int = Field(default=7002, validation_alias="AUTOWONDER_HTTP_PORT")
    repo_test_git_binary: str = Field(
        default="git",
        validation_alias="AUTOWONDER_REPO_TEST_GIT_BINARY",
    )
    repo_test_timeout_sec: int = Field(
        default=30,
        validation_alias="AUTOWONDER_REPO_TEST_TIMEOUT_SEC",
    )
    public_base_url: str = Field(
        default="http://localhost:7002",
        validation_alias="AUTOWONDER_PUBLIC_BASE_URL",
    )
    deployment_version: str = Field(default="x.x.x", validation_alias="AUTOWONDER_VERSION")
    recommended_runtime_version: str = Field(
        default="0.2.163",
        validation_alias="AUTOWONDER_RUNTIME_RECOMMENDED_VERSION",
    )
    community_edition: bool = Field(default=True, validation_alias="AUTOWONDER_COMMUNITY_EDITION")
    aone_enabled: bool = Field(default=False, validation_alias="AUTOWONDER_AONE_ENABLED")
    s3_enabled: bool = Field(default=False, validation_alias="S3_ENABLED")
    s3_endpoint: str = Field(default="", validation_alias="S3_ENDPOINT")
    s3_public_endpoint: str = Field(default="", validation_alias="S3_PUBLIC_ENDPOINT")
    s3_region: str = Field(default="us-east-1", validation_alias="S3_REGION")
    s3_access_key_id: str = Field(default="", validation_alias="S3_ACCESS_KEY_ID")
    s3_access_key_secret: str = Field(default="", validation_alias="S3_ACCESS_KEY_SECRET")
    s3_force_path_style: bool = Field(default=True, validation_alias="S3_FORCE_PATH_STYLE")
    oss_enabled: bool = Field(default=True, validation_alias="OSS_ENABLED")
    oss_endpoint: str = Field(default="", validation_alias="OSS_ENDPOINT")
    oss_public_endpoint: str = Field(default="", validation_alias="OSS_PUBLIC_ENDPOINT")
    oss_bucket: str = Field(default="", validation_alias="OSS_BUCKET")
    oss_access_key_id: str = Field(default="", validation_alias="OSS_ACCESS_KEY_ID")
    oss_access_key_secret: str = Field(default="", validation_alias="OSS_ACCESS_KEY_SECRET")
    oss_task_pkg_bucket: str = Field(default="", validation_alias="OSS_TASK_PKG_BUCKET")
    oss_artifact_bucket: str = Field(default="", validation_alias="OSS_ARTIFACT_BUCKET")
    oss_skill_bucket: str = Field(default="", validation_alias="OSS_SKILL_BUCKET")
    oss_backup_bucket: str = Field(default="", validation_alias="OSS_BACKUP_BUCKET")
    scheduled_task_enabled: bool = Field(
        default=True,
        validation_alias="AUTOWONDER_SCHEDULED_TASK_ENABLED",
    )
    scheduled_task_scanner_enabled: bool = Field(
        default=True,
        validation_alias="AUTOWONDER_SCHEDULED_TASK_SCANNER_ENABLED",
    )
    scheduled_task_cluster_ready: bool = Field(
        default=True,
        validation_alias="AUTOWONDER_SCHEDULED_TASK_CLUSTER_READY",
    )
    workitem_stuck_threshold_ms: int = Field(
        default=3_600_000,
        validation_alias="AUTOWONDER_WORKITEM_STUCK_THRESHOLD_MS",
    )

    @property
    def resolved_database_url(self) -> str:
        """显式 JDBC URL 优先，否则用 ``AUTOWONDER_DATABASE_URL``。"""
        if self.spring_datasource_url:
            return database_url_from_jdbc(
                self.spring_datasource_url,
                self.spring_datasource_username,
                self.spring_datasource_password,
            )
        return self.database_url

    @property
    def redis_url(self) -> str:
        """组装 redis.asyncio 使用的 URL。"""
        if self.redis_password:
            auth = f":{self.redis_password}@"
        else:
            auth = ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_database}"


@lru_cache
def get_settings() -> Settings:
    """进程内一份配置。"""
    return Settings()

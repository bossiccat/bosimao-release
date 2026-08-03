"""配置系统：统一加载 config/*.yaml + .env（pydantic-settings）"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


class MonitorTarget(BaseModel):
    """单个监控目标（对应 config/monitors.yaml 一项）"""

    app_id: str
    app_name: str
    process_name: str
    window_title_regex: str = ".*"
    poll_interval_seconds: int = 6
    enabled: bool = True


class CaptureConfig(BaseModel):
    max_width: int = 1280
    jpeg_quality: int = 85
    tmp_dir: str = "./tmp/captures"


class DetectionConfig(BaseModel):
    """检测参数（对应 config/detection.yaml）"""

    stuck_frame_threshold: int = 3
    stuck_timeout_seconds: int = 120
    off_track_frame_threshold: int = 2
    prompt_template: str = "./config/prompts/vision_analyze.md"
    min_alert_interval_seconds: int = 60
    max_alerts_per_hour: int = 30


class ReminderConfig(BaseModel):
    level_1_status_dot: bool = True
    level_2_pet_move: bool = True
    level_3_pet_alert: bool = True
    level_4_voice_push: bool = True
    voice_alert_enabled: bool = True
    push_alert_enabled: bool = True


class WecomConfig(BaseModel):
    enabled: bool = True
    rate_limit_per_minute: int = 15


class NtfyConfig(BaseModel):
    enabled: bool = True
    server: str = "https://ntfy.sh"
    priority: str = "default"
    with_screenshot: bool = True


class CircuitBreakerConfig(BaseModel):
    fail_threshold: int = 3
    cooldown_seconds: int = 300


class PushConfig(BaseModel):
    providers: list[str] = ["wecom", "ntfy"]
    wecom: WecomConfig = Field(default_factory=WecomConfig)
    ntfy: NtfyConfig = Field(default_factory=NtfyConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)


class MonitorsConfig(BaseModel):
    monitors: list[MonitorTarget] = Field(default_factory=list)
    voice_active_poll_interval_seconds: int = 12
    capture: CaptureConfig = Field(default_factory=CaptureConfig)


class Settings(BaseSettings):
    """环境变量（.env）+ 全局配置"""

    model_config = SettingsConfigDict(env_file=str(PROJECT_ROOT / ".env"), extra="ignore")

    model_server_host: str = "127.0.0.1"
    model_server_port: int = 19080
    model_dir: str = "D:\\models\\MiniCPM-o-4_5-gguf"
    model_file: str = "MiniCPM-o-4_5-Q4_K_M.gguf"
    model_ctx_size: int = 8192
    model_ngl: int = 99

    backend_port: int = 8000

    wecom_webhook_url: str = ""
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""

    log_level: str = "INFO"


def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"配置文件缺失: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_monitors() -> MonitorsConfig:
    return MonitorsConfig.model_validate(_load_yaml("monitors.yaml"))


def load_detection() -> DetectionConfig:
    doc = _load_yaml("detection.yaml")
    return DetectionConfig.model_validate(doc["detection"])


def load_push() -> PushConfig:
    doc = _load_yaml("push.yaml")
    return PushConfig.model_validate(doc["push"])


def load_reminder() -> ReminderConfig:
    return ReminderConfig.model_validate(_load_yaml("detection.yaml")["reminder"])


def load_settings() -> Settings:
    return Settings()


class AppConfig:
    """聚合配置对象（热重载时重建）"""

    def __init__(self) -> None:
        self.settings = load_settings()
        self.monitors = load_monitors()
        self.detection = load_detection()
        self.push = load_push()
        self.reminder = load_reminder()

    def reload(self) -> list[str]:
        """热重载 config/*.yaml，返回错误清单（空 = 成功）"""
        errors: list[str] = []
        try:
            self.monitors = load_monitors()
        except Exception as e:  # noqa: BLE001
            errors.append(f"monitors.yaml: {e}")
        try:
            self.detection = load_detection()
            self.reminder = load_reminder()
        except Exception as e:  # noqa: BLE001
            errors.append(f"detection.yaml: {e}")
        try:
            self.push = load_push()
        except Exception as e:  # noqa: BLE001
            errors.append(f"push.yaml: {e}")
        return errors


# 全局唯一配置实例
config = AppConfig()

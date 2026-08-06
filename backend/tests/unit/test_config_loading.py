"""config 加载回归测试（P0 门禁）：detection/push/monitors YAML 真实值必须生效

背景缺陷（M-1 审计实证）：
    load_detection()/load_push() 曾对整个 YAML 文档 model_validate，
    忽略顶层 `detection:` / `push:` 键 → 注入 5/180/3 输出恒默认 3/120/2。
本测试锁定"配置值必须生效"，修复前红、修复后绿（TDD 门禁）。

用法：
    monkeypatch app.config.CONFIG_DIR 指向临时目录，写入测试 YAML 后调用加载函数。
"""
from __future__ import annotations

import textwrap

import pytest
import yaml

import app.config as config_module


def write_yaml(tmp_path, name: str, content: str) -> None:
    (tmp_path / name).write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


class TestDetectionLoading:
    def test_detection_values_applied(self, tmp_path, monkeypatch):
        """注入 5/180/3/90/20 → 必须原样加载（曾恒默认 3/120/2）"""
        write_yaml(
            tmp_path,
            "detection.yaml",
            """
            detection:
              stuck_frame_threshold: 5
              stuck_timeout_seconds: 180
              off_track_frame_threshold: 3
              prompt_template: ./config/prompts/x.md
              min_alert_interval_seconds: 90
              max_alerts_per_hour: 20
            reminder:
              level_4_voice_push: true
            """,
        )
        monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
        cfg = config_module.load_detection()
        assert cfg.stuck_frame_threshold == 5, "注入 5 却得默认 3 → config 层级 bug 复现"
        assert cfg.stuck_timeout_seconds == 180, "注入 180 却得默认 120 → config 层级 bug 复现"
        assert cfg.off_track_frame_threshold == 3, "注入 3 却得默认 2 → config 层级 bug 复现"
        assert cfg.min_alert_interval_seconds == 90
        assert cfg.max_alerts_per_hour == 20

    def test_missing_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
        with pytest.raises(FileNotFoundError):
            config_module.load_detection()


class TestPushLoading:
    def test_push_values_applied(self, tmp_path, monkeypatch):
        """providers=[ntfy]、wecom/feishu/ntfy 全关、熔断 2/60 → 必须原样加载（曾恒默认）"""
        write_yaml(
            tmp_path,
            "push.yaml",
            """
            push:
              providers:
                - ntfy
              wecom:
                enabled: false
                rate_limit_per_minute: 7
              feishu:
                enabled: false
                rate_limit_per_minute: 50
                webhook_url: https://open.feishu.cn/open-apis/bot/v2/hook/t
                verification_token: verif-abc
              ntfy:
                enabled: false
                server: https://example.com
                priority: high
                with_screenshot: false
              circuit_breaker:
                fail_threshold: 2
                cooldown_seconds: 60
            """,
        )
        monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
        cfg = config_module.load_push()
        assert cfg.providers == ["ntfy"], "providers 必须来自 YAML"
        assert cfg.wecom.enabled is False, "wecom.enabled=false 必须生效"
        assert cfg.wecom.rate_limit_per_minute == 7
        assert cfg.feishu.enabled is False, "feishu.enabled=false 必须生效"
        assert cfg.feishu.rate_limit_per_minute == 50
        assert cfg.feishu.webhook_url.endswith("/hook/t")
        assert cfg.feishu.verification_token == "verif-abc"
        assert cfg.ntfy.enabled is False, "ntfy.enabled=false 必须生效"
        assert cfg.ntfy.server == "https://example.com"
        assert cfg.ntfy.priority == "high"
        assert cfg.ntfy.with_screenshot is False
        assert cfg.circuit_breaker.fail_threshold == 2
        assert cfg.circuit_breaker.cooldown_seconds == 60

    def test_missing_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
        with pytest.raises(FileNotFoundError):
            config_module.load_push()


class TestMonitorsLoading:
    def test_monitors_values_applied(self, tmp_path, monkeypatch):
        write_yaml(
            tmp_path,
            "monitors.yaml",
            """
            monitors:
              - app_id: codex
                app_name: OpenAI Codex
                process_name: codex.exe
                window_title_regex: "(?i)codex"
                poll_interval_seconds: 6
                enabled: true
              - app_id: trae
                app_name: Trae
                process_name: trae.exe
            voice_active_poll_interval_seconds: 30
            capture:
              max_width: 640
              jpeg_quality: 70
            """,
        )
        monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
        cfg = config_module.load_monitors()
        assert [m.app_id for m in cfg.monitors] == ["codex", "trae"]
        assert cfg.monitors[0].process_name == "codex.exe"
        assert cfg.monitors[0].poll_interval_seconds == 6
        assert cfg.voice_active_poll_interval_seconds == 30
        assert cfg.capture.max_width == 640
        assert cfg.capture.jpeg_quality == 70


class TestReminderLoading:
    def test_reminder_values_applied(self, tmp_path, monkeypatch):
        write_yaml(
            tmp_path,
            "detection.yaml",
            """
            detection:
              stuck_frame_threshold: 5
            reminder:
              level_4_voice_push: false
              push_alert_enabled: false
            """,
        )
        monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
        cfg = config_module.load_reminder()
        assert cfg.level_4_voice_push is False
        assert cfg.push_alert_enabled is False


class TestInvalidYaml:
    def test_malformed_yaml_raises(self, tmp_path, monkeypatch):
        """非法 YAML（语法错误）→ 必须抛异常，不得静默回退默认值"""
        (tmp_path / "detection.yaml").write_text(
            "detection: [unclosed", encoding="utf-8"
        )
        monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
        with pytest.raises(yaml.YAMLError):
            config_module.load_detection()

    def test_wrong_shape_yaml_raises(self, tmp_path, monkeypatch):
        """YAML 合法但形状错误（顶层是 list）→ 必须抛异常"""
        (tmp_path / "push.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
        monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
        with pytest.raises(Exception):
            config_module.load_push()

"""API 全流程冒烟（mock DeepSeek + 本地 9B）：4 端点 + 注入未确认拒绝 + 审计预览"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "backend")

from fastapi.testclient import TestClient  # noqa: E402

from app.api import routes_brain  # noqa: E402
from app.brain.injector import InjectFocusResult, InjectResult, Injector  # noqa: E402
from app.brain.intent_service import IntentService  # noqa: E402
from app.brain.pipeline import BrainPipeline  # noqa: E402
from app.brain.store import TaskStore  # noqa: E402
from app.brain.task_service import TaskService  # noqa: E402
from app.config import BrainConfig, MonitorsConfig  # noqa: E402
from app.main import app  # noqa: E402

SECRET = "D:\\data\\secret\\app"


class FakeLocal:
    async def chat(self, prompt, max_tokens=512):
        if "会话摘要器" in prompt:
            return f"用户需要重构 {SECRET} 目录的数据层，改成接口+实现。"
        return '{"intent_type":"refactor","target_app":"codex","confidence":0.9,"clarifying_questions":[]}'


class FakeDeep:
    def key_configured(self):
        return True

    def circuit_open(self):
        return False

    async def chat(self, messages, *, max_tokens, temperature=0.2):
        return "请重构数据层：\n1. 拆分接口实现\n2. 迁移调用\n3. 验证一致，失败回滚"

    async def chat_json(self, messages, *, max_tokens, json_schema=None, temperature=0.2):
        return {
            "subtasks": [
                {
                    "id": f"T{i}",
                    "goal": f"步骤{i}",
                    "acceptance": [f"验收{i}"],
                    "rollback_hint": "",
                    "depends_on": [],
                }
                for i in range(1, 4)
            ]
        }


class FakeInjector(Injector):
    async def validate_focus(self, target_app="codex"):
        return InjectFocusResult(ok=True, window_title="ChatGPT", reason="")

    async def inject(self, task):
        print("  [injector.inject 被调用] instruction 前20字:", task.instruction.instruction_text[:20])
        return InjectResult(ok=True, channel="sendinput")


def main() -> None:
    cfg = BrainConfig()
    tmp = Path(tempfile.mkdtemp())
    fake_deep = FakeDeep()
    fake_local = FakeLocal()
    store = TaskStore(path=tmp / "bt.json")
    injector = FakeInjector(cfg, MonitorsConfig(), audit_path=tmp / "inject_audit.jsonl", instructions_dir=tmp / "instructions")
    pipeline = BrainPipeline(cfg, fake_deep, IntentService(fake_local, cfg), TaskService(fake_deep, cfg), store, injector)
    routes_brain.pipeline = pipeline

    c = TestClient(app)
    r = c.post("/api/v1/brain/intent", json={"text": "帮我重构项目数据层"})
    data = r.json()["data"]
    has_secret = SECRET in data["intent"]["sanitized_summary"]
    print("1.intent:", r.status_code, data["status"], "summary含路径?", has_secret)
    assert r.status_code == 202, "意图受理应返回 202 + task_id"
    assert has_secret is False, "脱敏摘要不应含路径"
    tid = data["task_id"]

    r = c.post("/api/v1/brain/task", json={"task_id": tid})
    d = r.json()["data"]
    print("2.task:", r.status_code, d["status"], "subtasks=", len(d["subtasks"]), "token?", bool(d["confirm_token"]), "degraded?", d["degraded"])
    token = d["confirm_token"]

    r = c.post("/api/v1/brain/inject", json={"task_id": tid, "decision": "confirm"})
    print("3a.inject无token:", r.status_code, r.json())
    assert r.status_code == 403, "未确认必须拒绝"

    r = c.post("/api/v1/brain/inject", json={"task_id": tid, "decision": "confirm", "confirm_token": token})
    print("3b.inject确认:", r.status_code, r.json()["data"])
    assert r.json()["data"]["status"] == "injected"

    r = c.get("/api/v1/brain/tasks?status=injected")
    print("4.list:", r.status_code, "total=", r.json()["data"]["total"])
    assert r.json()["data"]["total"] == 1

    lines = (tmp / "inject_audit.jsonl").read_text(encoding="utf-8").strip().split("\n")
    entry = json.loads(lines[0])
    print("5.audit preview len:", len(entry["instruction_preview"]), "action:", entry["action"], "result:", entry["result"])
    assert len(entry["instruction_preview"]) <= 60
    print("SMOKE OK: 全流程 4 端点 + 注入未确认拒绝 + 脱敏 + 审计60字预览")


if __name__ == "__main__":
    main()

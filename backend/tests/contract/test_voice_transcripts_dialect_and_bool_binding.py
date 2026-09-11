"""transcripts 时间列方言归一 + control_plane boolean 绑定回归守卫。

背景（持久化从 SQLite 迁往 CloudBase PostgreSQL）：

A. `TranscriptService.list()` 把 `started_at` / `created_at` 原样丢回调用方。
   SQLite 下这两列表里存的是 Unix float，PG 下是 `timestamptz`，驱动读回来是
   `datetime` —— 同一个 API 两种类型，调用方必然分叉。契约要求：读出时间列统一
   过 `dialect.timestamp_from_storage(...)` 归一成 Unix float，**且与迁移前
   SQLite 返回值保持数值一致**。`delete()` 也必须走方言事务
   （`dialect.transaction(conn)`），不能再裸用 `with conn:`。

B. 真 bug（已在 commit dc74f16 修复）：`control_plane_acknowledgements.inherited`
   是云端 boolean 列，历史上写整数字面量 0/1，PG 报
   `column inherited is of type boolean but expression is of type integer`。
   本契约用可注入 fake 连接捕获**真实 SQL + 真实参数元组**，给这两处（
   `control_plane_ack.py` 的 INSERT、`control_plane_retry.py` 的 carry-forward）
   上锁，防止以后有人改回整数绑定。

fake 连接不是 MagicMock：只记录真正被执行的 SQL 串与绑定参数，断言落在
"SQL 是否方言化 / 绑定值类型是否正确"上，而不是 mock 调用次数。
"""
from __future__ import annotations

import re
import sys
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# backend/tests/contract 下没有 __init__.py，显式把 backend/ 放进 sys.path
# （与 test_voice_control_plane_dialect.py / test_voice_store_dialect_parity.py 一致）。
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.voice.control_plane import SessionLedger  # noqa: E402
from app.voice.pg_storage import VoiceStoreProtocol  # noqa: E402
from app.voice.sql_dialect import POSTGRES_DIALECT, SQLITE_DIALECT  # noqa: E402
from app.voice.transcripts import MemoryKeyCipher, TranscriptService  # noqa: E402

COMMIT = "-- commit --"
ROLLBACK = "-- rollback --"

# 固定时刻：PG 侧绑 aware datetime，SQLite 侧绑同一时刻的 Unix float。
_T_EARLY = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
_T_LATE = datetime(2024, 1, 3, 3, 4, 5, tzinfo=timezone.utc)
_EARLY = _T_EARLY.timestamp()
_LATE = _T_LATE.timestamp()


# ---------------------------------------------------------------------------
# 可注入的假连接：记录真实 SQL + 真实参数元组
# ---------------------------------------------------------------------------


class _Cursor:
    """`conn.execute()` 的返回形状（游标）。"""

    def __init__(self, conn: "FakeConn") -> None:
        self._conn = conn
        # DELETE 依赖 rowcount；命中单行即为 1。
        self.rowcount = 1

    def fetchone(self):
        return self._conn.pop_row()

    def fetchall(self):
        return self._conn.pop_rowset()


class FakeConn:
    """同时具备 sqlite3 与 psycopg3 两种连接形状的假连接。

    - sqlite3 形状：`with conn:`（`__enter__/__exit__` 成功即 commit）；方言层的
      `SqliteDialect.transaction()` 走 `conn.execute("BEGIN IMMEDIATE")` + commit。
    - psycopg3 形状：`conn.transaction()`（协议层事务）；`PostgresDialect.transaction()`
      走 `with conn.transaction():`，**不会**发出 `BEGIN IMMEDIATE`。

    行 / 行集按调用顺序预置，用尽后返回 None / []（与真实空结果一致）。
    """

    def __init__(self, rows=(), rowsets=()) -> None:
        self.log: list[tuple[str, tuple]] = []
        self._rows = deque(rows)
        self._rowsets = deque(rowsets)
        # PG 路径是否真的进入了协议层事务（区分 `with conn:` 与 `dialect.transaction`）。
        self.transaction_entries = 0

    # ---- 行数据 ----

    def pop_row(self):
        return self._rows.popleft() if self._rows else None

    def pop_rowset(self):
        return self._rowsets.popleft() if self._rowsets else []

    # ---- DBAPI 形状 ----

    def execute(self, sql, params=()):
        self.log.append((sql, tuple(params)))
        return _Cursor(self)

    def commit(self) -> None:
        self.log.append((COMMIT, ()))

    def rollback(self) -> None:
        self.log.append((ROLLBACK, ()))

    @contextmanager
    def transaction(self):
        """psycopg3 `Connection.transaction()` 的形状：退出即提交。"""
        self.transaction_entries += 1
        yield self
        self.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.rollback() if exc_type is not None else self.commit()
        return False

    # ---- 断言辅助 ----

    @property
    def sql(self) -> list[str]:
        return [sql for sql, _ in self.log]

    def params_for(self, needle: str) -> tuple:
        for sql, params in self.log:
            if needle in sql:
                return params
        raise AssertionError(f"未捕获到含 {needle!r} 的语句，实际：{self.sql}")


def _assert_no_integer_literal(sql: str, label: str) -> None:
    """SQL 里不得出现整数字面量（boolean 列禁止写 0/1 字面量）。"""
    match = re.search(r"(?<![\w.])[0-9]+(?![\w.])", sql)
    assert match is None, (
        f"{label} 的 SQL 里出现整数字面量 {match.group(0)!r}，"
        f"boolean 列必须参数绑定 Python bool：{sql}"
    )


# ---------------------------------------------------------------------------
# transcripts：假 store + 服务构造
# ---------------------------------------------------------------------------


class FakeTranscriptStore:
    """`TranscriptService` 只需要 `dialect` / `connect()` / `write_audit()`。"""

    def __init__(self, dialect, conn: FakeConn) -> None:
        self.dialect = dialect
        self._conn = conn
        self.audits: list[tuple] = []

    @contextmanager
    def connect(self):
        yield self._conn

    def write_audit(self, *args, **kwargs) -> None:
        self.audits.append((args, kwargs))

    def get_setting(self, key):
        return None


def _transcript_service(dialect, *, rows=(), rowsets=()):
    conn = FakeConn(rows=rows, rowsets=rowsets)
    store = FakeTranscriptStore(dialect, conn)
    return TranscriptService(store, MemoryKeyCipher()), conn


def _transcript_row(dialect) -> dict:
    """同一时刻：SQLite 侧是 Unix float，PG 侧是 timestamptz（读回 datetime）。"""
    return {
        "id": 1,
        "session_id": "sess-1",
        "encryption_version": "mem-v1",
        "started_at": _T_EARLY if dialect is POSTGRES_DIALECT else _EARLY,
        "created_at": _T_LATE if dialect is POSTGRES_DIALECT else _LATE,
    }


# ---------------------------------------------------------------------------
# A1. 方言契约本体：timestamp_from_storage 两种方言都归一成 float
# ---------------------------------------------------------------------------


def test_timestamp_from_storage_contract_is_unix_float_on_both_dialects() -> None:
    """钉死关键契约：SQLite 收 float、PG 收 datetime，出参都必须是 Unix float。"""
    sqlite_out = SQLITE_DIALECT.timestamp_from_storage(_EARLY)
    pg_out = POSTGRES_DIALECT.timestamp_from_storage(_T_EARLY)

    assert type(sqlite_out) is float, f"SQLite 归一后应为 float，拿到 {type(sqlite_out).__name__}"
    assert type(pg_out) is float, f"PG 归一后应为 float，拿到 {type(pg_out).__name__}"
    assert sqlite_out == _EARLY
    assert pg_out == _EARLY


def test_list_normalises_pg_timestamptz_to_unix_float() -> None:
    service, _conn = _transcript_service(
        POSTGRES_DIALECT, rowsets=[[_transcript_row(POSTGRES_DIALECT)]]
    )

    row = service.list()[0]

    assert type(row["started_at"]) is float, (
        f"PG 下 started_at 应归一成 float，拿到 {type(row['started_at']).__name__}"
    )
    assert row["started_at"] == _EARLY
    assert type(row["created_at"]) is float, (
        f"PG 下 created_at 应归一成 float，拿到 {type(row['created_at']).__name__}"
    )
    assert row["created_at"] == _LATE


def test_list_returns_identical_types_and_values_across_dialects() -> None:
    """同一 API 在两种方言下必须同类型同数值——否则调用方必然分叉。"""
    pg_service, _ = _transcript_service(
        POSTGRES_DIALECT, rowsets=[[_transcript_row(POSTGRES_DIALECT)]]
    )
    lite_service, _ = _transcript_service(
        SQLITE_DIALECT, rowsets=[[_transcript_row(SQLITE_DIALECT)]]
    )

    pg_row = pg_service.list()[0]
    lite_row = lite_service.list()[0]

    for key in ("started_at", "created_at"):
        assert type(pg_row[key]) is type(lite_row[key]), (
            f"{key} 类型分叉：PG={type(pg_row[key]).__name__} "
            f"SQLite={type(lite_row[key]).__name__}"
        )
        assert type(pg_row[key]) is float
        assert pg_row[key] == lite_row[key]


# ---------------------------------------------------------------------------
# A2. delete() 必须走方言事务路径，且返回值（删除行数）不变
# ---------------------------------------------------------------------------


def test_sqlite_delete_uses_dialect_transaction_and_returns_rowcount() -> None:
    service, conn = _transcript_service(SQLITE_DIALECT)

    deleted = service.delete(7)

    assert deleted == 1, f"delete() 应返回删除行数 1，拿到 {deleted!r}"
    assert "BEGIN IMMEDIATE" in conn.sql, (
        f"SQLite 的 delete() 未走方言事务（缺 BEGIN IMMEDIATE）：{conn.sql}"
    )
    assert COMMIT in conn.sql, f"delete() 没有提交：{conn.sql}"
    joined = " | ".join(conn.sql)
    assert "%s" not in joined, f"SQLite 路径混进了 PG 占位符：{conn.sql}"
    assert "?" in joined, f"SQLite 路径丢了 ? 占位符：{conn.sql}"


def test_postgres_delete_uses_protocol_transaction_and_returns_rowcount() -> None:
    service, conn = _transcript_service(POSTGRES_DIALECT)

    deleted = service.delete(7)

    assert deleted == 1, f"delete() 应返回删除行数 1，拿到 {deleted!r}"
    assert conn.transaction_entries == 1, (
        "PG 的 delete() 必须走协议层 conn.transaction()，实际未进入方言事务路径"
    )
    joined = " | ".join(conn.sql)
    assert "BEGIN IMMEDIATE" not in joined, f"PG 路径出现 SQLite 专有事务：{conn.sql}"
    assert "?" not in joined, f"PG 路径仍用 ? 占位符：{conn.sql}"
    assert "%s" in joined, f"PG 路径没有用 %s 占位符：{conn.sql}"
    assert COMMIT in conn.sql, f"delete() 没有提交：{conn.sql}"


def test_delete_all_also_uses_dialect_transaction_and_returns_rowcount() -> None:
    """删除全部（transcript_id=None）同样走方言事务，返回值仍是 rowcount。"""
    service, conn = _transcript_service(POSTGRES_DIALECT)

    deleted = service.delete()

    assert deleted == 1
    assert conn.transaction_entries == 1, "删除全部分支也必须走协议层事务"
    # 无参 DELETE 仍然不得出现 SQLite 占位符或 BEGIN IMMEDIATE。
    joined = " | ".join(conn.sql)
    assert "?" not in joined, f"PG 路径仍用 ? 占位符：{conn.sql}"
    assert "BEGIN IMMEDIATE" not in joined


# ---------------------------------------------------------------------------
# B. boolean 绑定的回归守卫（守住已修复的真 bug，不再改动生产文件）
# ---------------------------------------------------------------------------

_PROTOCOL_REPOS = (
    "pairing_codes", "pending_sessions", "hello_proofs", "device_credentials",
    "nonces", "rate_limit", "audit", "settings",
)
_PROTOCOL_METHODS = (
    "save_device", "get_device", "verify_device_secret", "revoke_device", "list_devices",
    "create_pairing_code", "consume_pairing_code", "register_device_from_pairing",
    "record_revoke_confirmation", "consume_nonce", "purge_expired_nonces", "write_audit",
    "get_setting", "set_setting", "write_session_event", "list_session_events",
    "enqueue_pending_session", "claim_pending_session", "consume_pending_sign_claim",
)


class FakeLedgerStore:
    """满足 `VoiceStoreProtocol` 结构校验的最小门面；只有 `connect()` 有真实行为。"""

    def __init__(self, dialect, conn: FakeConn) -> None:
        self.dialect = dialect
        self._conn = conn
        for name in _PROTOCOL_REPOS:
            setattr(self, name, object())

    @contextmanager
    def connect(self):
        yield self._conn

    def initialize(self) -> None:
        return None


def _unsupported(name: str):
    def _stub(self, *args, **kwargs):
        raise AssertionError(f"{name}() 不应在 boolean 绑定契约测试中被调用")

    return _stub


for _name in _PROTOCOL_METHODS:
    setattr(FakeLedgerStore, _name, _unsupported(_name))


TERMINATION_ROW = {
    "termination_id": "term-1",
    "session_id": "sess-1",
    "generation": 7,
    "operation": "terminate",
    "request_id": "req-1",
    "payload_hash": "hash",
    "parent_termination_id": None,
    "result": "pending",
    "state": "TERMINATING",
    "terminal_at": None,
    "device_id": "dev-1",
    "room_id": "room-1",
    "session_state": "TERMINATING",
}

PARENT_TERMINATION_ROW = {
    **TERMINATION_ROW,
    "termination_id": "term-parent",
    "request_id": "req-parent",
    "result": "partial",
    "state": "TERMINATION_PARTIAL",
    "session_state": "TERMINATION_PARTIAL",
}


def test_fake_ledger_store_satisfies_voice_store_protocol() -> None:
    """元测试：假门面必须真的过得了结构校验，否则下面的断言全是假绿。"""
    assert isinstance(FakeLedgerStore(POSTGRES_DIALECT, FakeConn()), VoiceStoreProtocol)


def test_record_ack_binds_inherited_as_python_bool() -> None:
    conn = FakeConn(
        rows=[dict(TERMINATION_ROW), None, (0,), dict(TERMINATION_ROW)],
        rowsets=[
            [{"reporter": "android", "result": "confirmed"}],
            [{"acknowledgement": "android_trtc_left", "result": "confirmed"}],
        ],
    )
    ledger = SessionLedger(FakeLedgerStore(POSTGRES_DIALECT, conn))

    ledger.record_ack(
        termination_id="term-1", session_id="sess-1", device_id="dev-1",
        room_id="room-1", generation=7, acknowledgement="android_trtc_left",
        reporter="android", result="confirmed",
    )

    sql = next(
        s for s in conn.sql if "INSERT INTO control_plane_acknowledgements" in s
    )
    params = conn.params_for("INSERT INTO control_plane_acknowledgements")
    # 列序：termination_id, acknowledgement, result, inherited, created_at, updated_at
    inherited = params[3]
    assert type(inherited) is bool, (
        f"inherited 必须绑定 Python bool，拿到 {type(inherited).__name__}={inherited!r}"
    )
    assert inherited is False, f"record_ack 的 inherited 应为 False，拿到 {inherited!r}"
    _assert_no_integer_literal(sql, "record_ack INSERT acknowledgements")


def test_retry_carry_forward_binds_inherited_as_python_bool() -> None:
    conn = FakeConn(
        rows=[dict(PARENT_TERMINATION_ROW), None, None, dict(TERMINATION_ROW)],
        rowsets=[[]],
    )
    ledger = SessionLedger(FakeLedgerStore(POSTGRES_DIALECT, conn))

    ledger.retry_termination(
        session_id="sess-1", parent_termination_id="term-parent",
        request_id="req-child", reason="retry_failed_acknowledgements",
    )

    sql = next(
        s for s in conn.sql if "INSERT INTO control_plane_acknowledgements" in s
    )
    params = conn.params_for("INSERT INTO control_plane_acknowledgements")
    # INSERT ... SELECT 列序：termination_id, <inherited=True>, created_at, updated_at,
    # parent_termination_id（carry-forward 的 inherited 位于第 2 个绑定参数）。
    inherited = params[1]
    assert type(inherited) is bool, (
        f"carry-forward 的 inherited 必须绑定 Python bool，"
        f"拿到 {type(inherited).__name__}={inherited!r}"
    )
    assert inherited is True, f"carry-forward 的 inherited 应为 True，拿到 {inherited!r}"
    _assert_no_integer_literal(sql, "retry carry-forward INSERT acknowledgements")

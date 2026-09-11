"""控制面账本（control_plane_base / control_plane_sessions）的 SQL 方言契约。

背景：持久化正从 SQLite 迁往 CloudBase PostgreSQL。云端 schema 里
`created_at` / `updated_at` / `expires_at` / `consumed_at` 全是 `timestamptz`，
而账本历史代码有三处硬编码 SQLite 语义，在 PG 上会直接报错：

1. 事务发裸 `BEGIN IMMEDIATE`（PG 无此语法，BEGIN 由协议层 `Connection.transaction()` 发出）；
2. 占位符硬编码 `?`（PG 用 `%s`）；
3. 时间列写 Unix float（`timestamptz` 只接受 aware datetime）。

本契约**用可注入的 fake 连接捕获真实 SQL 串来断言**，不是数 mock 调用次数：
只有真正把 SQL 拼成方言占位符、真正把时间过一遍 `timestamp_to_storage`，
断言才会通过。

本契约现已覆盖整个控制面账本：`control_plane_base.py` / `control_plane_sessions.py`
（第一批）以及 `control_plane_ack.py` / `control_plane_kws.py` /
`control_plane_retry.py` / `control_plane_wake.py`（第二批）。四个次级账本
（ack 上报 / kws readiness / retry get_termination / wake 消费）接入同一方言层：
`self.ph` 占位符、`self._txn()` 方言化事务、`timestamp_to_storage` 时间归一、
`json_to_storage`/`json_from_storage` 处理 jsonb。`txn_sql()` 切到首个提交哨兵，
只断言事务内语句。
"""
import hashlib
import sys
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone

sys.path.insert(0, "backend")

from app.voice.control_plane import SessionLedger  # noqa: E402
from app.voice.pg_storage import VoiceStoreProtocol  # noqa: E402
from app.voice.sql_dialect import POSTGRES_DIALECT, SQLITE_DIALECT  # noqa: E402
from app.voice.user_sig_cipher import UserSigCipher  # noqa: E402

COMMIT = "-- commit --"
ROLLBACK = "-- rollback --"

SESSION_ROW = {
    "session_id": "sess-1",
    "device_id": "dev-1",
    "room_id": "room-1",
    "generation": 7,
    "state": "ACTIVE",
}
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
PAYLOAD = {
    "session_id": "sess-1",
    "device_id": "dev-1",
    "room_id": "room-1",
    "generation": 7,
}


# ---------------------------------------------------------------------------
# 可注入的假连接 / 假门面
# ---------------------------------------------------------------------------


class _Result:
    """`conn.execute()` 的返回形状（游标）。"""

    def __init__(self, conn: "FakeConn") -> None:
        self._conn = conn
        # `mark_kws_ready` 依赖 UPDATE 的 rowcount（命中单行即为 1）。
        self.rowcount = 1

    def fetchone(self):
        return self._conn.pop_row()

    def fetchall(self):
        return self._conn.pop_rowset()


class FakeConn:
    """记录每条真实 SQL 串的假连接。

    提交 / 回滚都往日志里落一个哨兵，`txn_sql()` 因此能切出"事务内"的语句。
    行数据由测试按调用顺序预置；用尽后返回 None / []（与真实空结果一致）。
    """

    def __init__(self, rows=(), rowsets=()) -> None:
        self.log: list[tuple[str, tuple]] = []
        self._rows = deque(rows)
        self._rowsets = deque(rowsets)

    # ---- 行数据 ----

    def pop_row(self):
        return self._rows.popleft() if self._rows else None

    def pop_rowset(self):
        return self._rowsets.popleft() if self._rowsets else []

    # ---- DBAPI 形状 ----

    def execute(self, sql, params=()):
        self.log.append((sql, tuple(params)))
        return _Result(self)

    def commit(self) -> None:
        self.log.append((COMMIT, ()))

    def rollback(self) -> None:
        self.log.append((ROLLBACK, ()))

    @contextmanager
    def transaction(self):
        """psycopg3 `Connection.transaction()` 的形状：退出即提交。"""
        yield self
        self.commit()

    # ---- 断言辅助 ----

    @property
    def sql(self) -> list[str]:
        return [sql for sql, _ in self.log]

    def txn_sql(self) -> list[str]:
        """首个提交哨兵之前的语句（尚未迁移的读回路径不计入）。"""
        out: list[str] = []
        for sql, _ in self.log:
            if sql == COMMIT:
                break
            out.append(sql)
        return out

    def params_for(self, needle: str) -> tuple:
        for sql, params in self.log:
            if needle in sql:
                return params
        raise AssertionError(f"未捕获到含 {needle!r} 的语句，实际：{self.sql}")


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


class FakeStore:
    """满足 `VoiceStoreProtocol` 结构校验的最小门面；只有 `connect()` 有真实行为。

    `LedgerBase.__init__` 用 `isinstance(store, VoiceStoreProtocol)` 做结构校验
    （ea7ab20 特意加的），因此假门面必须补齐 Protocol 的全部成员。业务方法一律
    补成显式抛错的桩：本契约不该走到它们，走到了就该炸而不是静默通过。
    """

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
        raise AssertionError(f"{name}() 不应在方言契约测试中被调用")

    return _stub


for _name in _PROTOCOL_METHODS:
    setattr(FakeStore, _name, _unsupported(_name))


def _ledger(dialect, rows=(), rowsets=()):
    conn = FakeConn(rows=rows, rowsets=rowsets)
    return SessionLedger(FakeStore(dialect, conn)), conn


# ---------------------------------------------------------------------------
# 元测试：假门面必须真的过得了结构校验，否则下面全是假绿
# ---------------------------------------------------------------------------


def test_fake_store_satisfies_voice_store_protocol() -> None:
    assert isinstance(FakeStore(SQLITE_DIALECT, FakeConn()), VoiceStoreProtocol)


def test_ledger_adopts_dialect_from_store() -> None:
    """账本的方言必须来自 store，而不是各处 if/else 或硬编码。"""
    pg_ledger, _ = _ledger(POSTGRES_DIALECT)
    sqlite_ledger, _ = _ledger(SQLITE_DIALECT)
    assert pg_ledger.dialect is POSTGRES_DIALECT
    assert sqlite_ledger.dialect is SQLITE_DIALECT
    assert (pg_ledger.ph, sqlite_ledger.ph) == ("%s", "?")


# ---------------------------------------------------------------------------
# PG 路径
# ---------------------------------------------------------------------------


def test_pg_create_session_emits_no_begin_immediate_and_no_qmark() -> None:
    ledger, conn = _ledger(POSTGRES_DIALECT, rows=[None, dict(SESSION_ROW)])

    ledger.create_session(
        session_id="sess-1", device_id="dev-1", room_id="room-1", generation=7
    )

    joined = " | ".join(conn.sql)
    assert "BEGIN IMMEDIATE" not in joined, f"PG 路径仍发裸 BEGIN IMMEDIATE：{conn.sql}"
    assert "?" not in joined, f"PG 路径仍用 ? 占位符：{conn.sql}"
    assert "%s" in joined, f"PG 路径没有用 %s 占位符：{conn.sql}"


def test_pg_create_session_writes_aware_datetime_for_timestamptz() -> None:
    ledger, conn = _ledger(POSTGRES_DIALECT, rows=[None, dict(SESSION_ROW)])

    ledger.create_session(
        session_id="sess-1", device_id="dev-1", room_id="room-1", generation=7
    )

    created_at, updated_at = conn.params_for("INSERT INTO control_plane_sessions")[-2:]
    for column, value in (("created_at", created_at), ("updated_at", updated_at)):
        assert isinstance(value, datetime), (
            f"{column} 写入 {type(value).__name__}={value!r}，timestamptz 只接受 datetime"
        )
        assert value.tzinfo is not None, f"{column} 必须是 aware datetime，拿到 {value!r}"


def test_pg_begin_termination_transaction_sql_is_dialect_clean() -> None:
    ledger, conn = _ledger(
        POSTGRES_DIALECT,
        rows=[dict(SESSION_ROW), None, dict(TERMINATION_ROW)],
        rowsets=[[]],
    )

    ledger.begin_termination(
        session_id="sess-1", generation=7, request_id="req-1", payload=dict(PAYLOAD)
    )

    txn = " | ".join(conn.txn_sql())
    assert "BEGIN IMMEDIATE" not in txn, f"PG 路径仍发裸 BEGIN IMMEDIATE：{conn.txn_sql()}"
    assert "?" not in txn, f"PG 路径仍用 ? 占位符：{conn.txn_sql()}"
    assert txn.count("%s") >= 7, f"占位符数量不对，事务内 SQL：{conn.txn_sql()}"


def test_pg_begin_termination_writes_aware_datetime_for_timestamptz() -> None:
    ledger, conn = _ledger(
        POSTGRES_DIALECT,
        rows=[dict(SESSION_ROW), None, dict(TERMINATION_ROW)],
        rowsets=[[]],
    )

    ledger.begin_termination(
        session_id="sess-1", generation=7, request_id="req-1", payload=dict(PAYLOAD)
    )

    insert_ts = conn.params_for("INSERT INTO control_plane_terminations")[-2:]
    update_ts = conn.params_for("UPDATE control_plane_sessions")[0]
    for value in (*insert_ts, update_ts):
        assert isinstance(value, datetime), (
            f"时间列写入 {type(value).__name__}={value!r}，timestamptz 只接受 datetime"
        )
        assert value.tzinfo is not None, f"时间列必须是 aware datetime，拿到 {value!r}"


def test_pg_termination_context_select_uses_dialect_placeholder() -> None:
    """`_termination_context` 在 base.py 内，同样不得硬编码 `?`。"""
    ledger, conn = _ledger(POSTGRES_DIALECT, rows=[dict(TERMINATION_ROW)])

    ledger._termination_context(conn, "term-1")

    sql = conn.sql[0]
    assert "?" not in sql, f"_termination_context 仍用 ? 占位符：{sql}"
    assert "%s" in sql, f"_termination_context 没有用 %s 占位符：{sql}"


# ---------------------------------------------------------------------------
# SQLite 路径：不回归
# ---------------------------------------------------------------------------


def test_sqlite_create_session_still_uses_qmark_and_begin_immediate() -> None:
    ledger, conn = _ledger(SQLITE_DIALECT, rows=[None, dict(SESSION_ROW)])

    ledger.create_session(
        session_id="sess-1", device_id="dev-1", room_id="room-1", generation=7
    )

    assert "BEGIN IMMEDIATE" in conn.sql, f"SQLite 写事务丢了 BEGIN IMMEDIATE：{conn.sql}"
    joined = " | ".join(conn.sql)
    assert "%s" not in joined, f"SQLite 路径混进了 PG 占位符：{conn.sql}"
    assert "?" in joined, f"SQLite 路径丢了 ? 占位符：{conn.sql}"
    assert COMMIT in conn.sql, f"SQLite 写事务没有提交：{conn.sql}"


def test_sqlite_create_session_still_writes_unix_float() -> None:
    ledger, conn = _ledger(SQLITE_DIALECT, rows=[None, dict(SESSION_ROW)])

    ledger.create_session(
        session_id="sess-1", device_id="dev-1", room_id="room-1", generation=7
    )

    for value in conn.params_for("INSERT INTO control_plane_sessions")[-2:]:
        assert isinstance(value, float), (
            f"SQLite 侧时间列应为 Unix float，拿到 {type(value).__name__}={value!r}"
        )


def test_sqlite_begin_termination_rolls_back_on_error() -> None:
    """异常路径必须回滚——方言化事务不能把 rollback 语义弄丢。"""
    ledger, conn = _ledger(SQLITE_DIALECT, rows=[None])

    try:
        ledger.begin_termination(
            session_id="missing", generation=7, request_id="req-1", payload=dict(PAYLOAD)
        )
    except Exception:
        pass
    else:  # pragma: no cover - 会话不存在必须抛错
        raise AssertionError("session 不存在时 begin_termination 应当抛错")

    assert ROLLBACK in conn.sql, f"异常路径没有回滚：{conn.sql}"
    assert COMMIT not in conn.sql, f"异常路径不该提交：{conn.sql}"


# ---------------------------------------------------------------------------
# 第二批：ack / kws / retry / wake 四个次级账本接入方言层
# ---------------------------------------------------------------------------

TEST_KEY = hashlib.sha256(b"control-plane-dialect-contract-key").digest()

PRIOR_ROW = {
    "session_id": "prior-1",
    "device_id": "dev-1",
    "room_id": "room-0",
    "generation": 7,
    "state": "KWS_READY",
}

WAKE_EXPIRES_AT = 4102444800.0


@contextmanager
def _fake_psycopg_json(monkeypatch):
    """离线环境没有 psycopg：注入最小 `psycopg.types.json.Json`。

    被测生产代码仍是"惰性导入 psycopg、不做静默降级"，这里只替契约测试提供
    驱动适配器，断言的是绑定值的形态（jsonb 包装 vs TEXT 字符串）。
    """
    import types as _types

    class Json:
        def __init__(self, obj) -> None:
            self.obj = obj

    json_mod = _types.ModuleType("psycopg.types.json")
    json_mod.Json = Json
    types_mod = _types.ModuleType("psycopg.types")
    types_mod.json = json_mod
    pkg = _types.ModuleType("psycopg")
    pkg.types = types_mod
    monkeypatch.setitem(sys.modules, "psycopg", pkg)
    monkeypatch.setitem(sys.modules, "psycopg.types", types_mod)
    monkeypatch.setitem(sys.modules, "psycopg.types.json", json_mod)
    yield Json


def _cipher_ledger(dialect, rows=(), rowsets=()):
    conn = FakeConn(rows=rows, rowsets=rowsets)
    return SessionLedger(
        FakeStore(dialect, conn), user_sig_cipher=UserSigCipher(TEST_KEY)
    ), conn


def _wake_call(ledger) -> dict:
    return ledger.consume_wake(
        session_id="sess-new",
        device_id="dev-1",
        prior_session_id="prior-1",
        prior_generation=7,
        wake_event_id="wake-1",
        user_sig="sig-plaintext",
        expires_at=WAKE_EXPIRES_AT,
    )


def _assert_aware_datetimes(values, label: str) -> None:
    for value in values:
        assert isinstance(value, datetime), (
            f"{label} 写入 {type(value).__name__}={value!r}，timestamptz 只接受 datetime"
        )
        assert value.tzinfo is not None, f"{label} 必须是 aware datetime，拿到 {value!r}"


def test_pg_record_ack_transaction_sql_is_dialect_clean() -> None:
    ledger, conn = _ledger(
        POSTGRES_DIALECT,
        rows=[dict(TERMINATION_ROW), None, (1,), dict(TERMINATION_ROW)],
        rowsets=[
            [{"reporter": "android", "result": "confirmed"}],
            [{"acknowledgement": "android_trtc_left", "result": "confirmed"}],
        ],
    )

    ledger.record_ack(
        termination_id="term-1", session_id="sess-1", device_id="dev-1",
        room_id="room-1", generation=7, acknowledgement="android_trtc_left",
        reporter="android", result="confirmed",
    )

    txn = " | ".join(conn.txn_sql())
    assert "BEGIN IMMEDIATE" not in txn, f"PG 路径仍发裸 BEGIN IMMEDIATE：{conn.txn_sql()}"
    assert "?" not in txn, f"PG 路径仍用 ? 占位符：{conn.txn_sql()}"
    assert "%s" in txn, f"PG 路径没有用 %s 占位符：{conn.txn_sql()}"


def test_pg_record_ack_writes_aware_datetime_for_timestamptz() -> None:
    ledger, conn = _ledger(
        POSTGRES_DIALECT,
        rows=[dict(TERMINATION_ROW), None, (1,), dict(TERMINATION_ROW)],
        rowsets=[
            [{"reporter": "android", "result": "confirmed"}],
            [{"acknowledgement": "android_trtc_left", "result": "confirmed"}],
        ],
    )

    ledger.record_ack(
        termination_id="term-1", session_id="sess-1", device_id="dev-1",
        room_id="room-1", generation=7, acknowledgement="android_trtc_left",
        reporter="android", result="confirmed",
    )

    _assert_aware_datetimes(
        conn.params_for("INSERT INTO control_plane_ack_reports")[-3:],
        "ack_reports 时间列",
    )


def test_pg_mark_kws_ready_transaction_sql_is_dialect_clean(monkeypatch) -> None:
    ledger, conn = _ledger(POSTGRES_DIALECT, rows=[(1,)])

    with _fake_psycopg_json(monkeypatch):
        assert ledger.mark_kws_ready("sess-1", 7) is True

    txn = " | ".join(conn.txn_sql())
    assert "BEGIN IMMEDIATE" not in txn, f"PG 路径仍发裸 BEGIN IMMEDIATE：{conn.txn_sql()}"
    assert "?" not in txn, f"PG 路径仍用 ? 占位符：{conn.txn_sql()}"
    assert "%s" in txn, f"PG 路径没有用 %s 占位符：{conn.txn_sql()}"


def test_pg_mark_kws_ready_writes_datetime_and_jsonb(monkeypatch) -> None:
    ledger, conn = _ledger(POSTGRES_DIALECT, rows=[(1,)])

    with _fake_psycopg_json(monkeypatch) as json_adapter:
        ledger.mark_kws_ready("sess-1", 7, evidence={"rms": 0.5})

    params = conn.params_for("INSERT INTO control_plane_kws_readiness")
    assert isinstance(params[3], json_adapter), (
        f"evidence_json 在 PG 侧必须是 jsonb 包装，拿到 {type(params[3]).__name__}"
    )
    _assert_aware_datetimes(params[-3:], "kws_readiness 时间列")


def test_pg_get_termination_uses_dialect_placeholder_and_normalises_time() -> None:
    row = dict(TERMINATION_ROW)
    row["terminal_at"] = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    ledger, conn = _ledger(POSTGRES_DIALECT, rows=[row], rowsets=[[]])

    result = ledger.get_termination("term-1")

    sql = conn.sql[0]
    assert "?" not in sql, f"get_termination 仍用 ? 占位符：{sql}"
    assert "%s" in sql, f"get_termination 没有用 %s 占位符：{sql}"
    assert isinstance(result["terminal_at"], float), (
        f"terminal_at 应归一成 float，拿到 {type(result['terminal_at']).__name__}"
    )


def test_pg_consume_wake_transaction_sql_is_dialect_clean(monkeypatch) -> None:
    ledger, conn = _cipher_ledger(
        POSTGRES_DIALECT, rows=[None, dict(PRIOR_ROW), (0,)]
    )

    with _fake_psycopg_json(monkeypatch):
        record = _wake_call(ledger)

    txn = " | ".join(conn.txn_sql())
    assert "BEGIN IMMEDIATE" not in txn, f"PG 路径仍发裸 BEGIN IMMEDIATE：{conn.txn_sql()}"
    assert "?" not in txn, f"PG 路径仍用 ? 占位符：{conn.txn_sql()}"
    assert "%s" in txn, f"PG 路径没有用 %s 占位符：{conn.txn_sql()}"
    assert record["user_sig"] == "sig-plaintext"


def test_pg_consume_wake_writes_datetime_and_jsonb(monkeypatch) -> None:
    ledger, conn = _cipher_ledger(
        POSTGRES_DIALECT, rows=[None, dict(PRIOR_ROW), (0,)]
    )

    with _fake_psycopg_json(monkeypatch) as json_adapter:
        _wake_call(ledger)

    claim = conn.params_for("INSERT INTO pending_session_claims")
    _assert_aware_datetimes(claim[-3:], "pending_session_claims 时间列")

    wake = conn.params_for("INSERT INTO control_plane_wake_events")
    assert isinstance(wake[7], json_adapter), (
        f"record_json 在 PG 侧必须是 jsonb 包装，拿到 {type(wake[7]).__name__}"
    )
    _assert_aware_datetimes(wake[-3:], "wake_events 时间列")
    assert isinstance(wake[8], (bytes, bytearray)) and bytes(wake[8]), (
        "user_sig_ciphertext 必须是非空 bytes"
    )
    assert isinstance(wake[9], str) and wake[9].strip(), (
        "user_sig_encryption_version 必须非空"
    )


def test_sqlite_mark_kws_ready_still_uses_qmark_and_float() -> None:
    ledger, conn = _ledger(SQLITE_DIALECT, rows=[(1,)])

    assert ledger.mark_kws_ready("sess-1", 7) is True

    assert "BEGIN IMMEDIATE" in conn.sql, f"SQLite 写事务丢了 BEGIN IMMEDIATE：{conn.sql}"
    params = conn.params_for("INSERT INTO control_plane_kws_readiness")
    assert isinstance(params[3], str), "SQLite 侧 evidence_json 仍是 TEXT 字符串"
    for value in params[-3:]:
        assert isinstance(value, float), (
            f"SQLite 侧时间列应为 Unix float，拿到 {type(value).__name__}"
        )
    joined = " | ".join(conn.sql)
    assert "%s" not in joined, f"SQLite 路径混进了 PG 占位符：{conn.sql}"
    assert "?" in joined, f"SQLite 路径丢了 ? 占位符：{conn.sql}"


def test_sqlite_consume_wake_still_uses_qmark_and_float() -> None:
    ledger, conn = _cipher_ledger(SQLITE_DIALECT, rows=[None, dict(PRIOR_ROW), (0,)])

    record = _wake_call(ledger)

    assert "BEGIN IMMEDIATE" in conn.sql, f"SQLite 写事务丢了 BEGIN IMMEDIATE：{conn.sql}"
    claim = conn.params_for("INSERT INTO pending_session_claims")
    for value in claim[-3:]:
        assert isinstance(value, float), (
            f"SQLite 侧时间列应为 Unix float，拿到 {type(value).__name__}"
        )
    wake = conn.params_for("INSERT INTO control_plane_wake_events")
    assert isinstance(wake[7], str), "SQLite 侧 record_json 仍是 TEXT 字符串"
    assert isinstance(wake[8], (bytes, bytearray))
    joined = " | ".join(conn.sql)
    assert "%s" not in joined, f"SQLite 路径混进了 PG 占位符：{conn.sql}"
    assert "?" in joined, f"SQLite 路径丢了 ? 占位符：{conn.sql}"
    assert record["user_sig"] == "sig-plaintext"

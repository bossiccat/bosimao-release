"""pending session 原子领取 + transcripts 自增主键取回的方言契约（TDD）。

背景：持久化从 SQLite 迁往 CloudBase PostgreSQL。两处硬伤在多实例部署下会造成
**真实数据错误**：

1. ``PendingSessionRepository.claim_one`` 是「先 SELECT 再 UPDATE」，仅靠 SQLite
   ``BEGIN IMMEDIATE`` 互斥。PG 多实例下两个实例会领到同一条 pending session →
   重复签发 userSig。PG 必须用 ``FOR UPDATE SKIP LOCKED`` 让并发实例跳过已被
   他人锁定的行；SQLite 无此语法，继续靠 ``BEGIN IMMEDIATE`` 写锁。
2. ``transcripts.py`` 用 ``cursor.lastrowid`` 取自增主键。psycopg3 无此属性，
   PG 上必炸。必须走方言的 ``insert_returning`` / ``last_insert_id``。

本文件不连真实数据库、不依赖 psycopg：PG 路径用可注入 fake 连接捕获**真实被执行
的 SQL 串与绑定参数**，并**建模行锁**证明并发行为，而不是数 mock 调用次数。
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

# backend/tests/contract 下没有 __init__.py，显式把 backend/ 放进 sys.path
# （与同目录 test_voice_store_dialect_parity.py 第 22-26 行一致）。
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.voice.repositories.pending_sessions import PendingSessionRepository  # noqa: E402
from app.voice.sql_dialect import POSTGRES_DIALECT  # noqa: E402
from app.voice.storage import VoiceStore  # noqa: E402
from app.voice.transcripts import MemoryKeyCipher, TranscriptService  # noqa: E402

NOW = 1_700_000_000.0
FAR_FUTURE = 4_102_444_800.0  # 2100-01-01

CLAIM_ROW = {
    "id": 1,
    "session_id": "sess-1",
    "device_id": "dev-1",
    "room_id": "room-1",
    "generation": 1,
    "expires_at": FAR_FUTURE,
    "claimed_at": None,
}


class _Result:
    """游标形状：``fetchone`` + ``rowcount``。"""

    def __init__(self, row, rowcount: int = 1) -> None:
        self._row = row
        self.rowcount = rowcount

    def fetchone(self):
        return self._row


def _connect_factory(conn):
    """把单个 fake conn 包成 ``RepositoryBase`` 需要的上下文管理器工厂。"""

    @contextmanager
    def _cm():
        yield conn

    return _cm


# ---------------------------------------------------------------------------
# 1. PG 领取 SQL：必须含 FOR UPDATE SKIP LOCKED，且是合法 PG 占位符
# ---------------------------------------------------------------------------


class _SingleClaimConn:
    """极简 fake：SELECT 返回一行可领取记录，UPDATE 命中 1 行。"""

    def __init__(self, row) -> None:
        self.sql: list[str] = []
        self.params: list[tuple] = []
        self._row = row
        self._claimed = False

    def execute(self, sql, params=()):
        self.sql.append(sql)
        self.params.append(tuple(params))
        if sql.strip().upper().startswith("SELECT"):
            return _Result(self._row if not self._claimed else None,
                           rowcount=0 if self._claimed else 1)
        if self._claimed:
            return _Result(None, rowcount=0)
        self._claimed = True
        return _Result(None, rowcount=1)

    @contextmanager
    def transaction(self):
        yield self

    def commit(self) -> None:  # pragma: no cover - PG 走协议层
        pass

    def rollback(self) -> None:  # pragma: no cover
        pass


def test_pg_claim_select_uses_skip_locked_and_percent_s() -> None:
    conn = _SingleClaimConn(dict(CLAIM_ROW))
    repo = PendingSessionRepository(_connect_factory(conn), POSTGRES_DIALECT)

    claimed = repo.claim_one(now=NOW)

    assert claimed is not None and claimed["session_id"] == "sess-1", claimed
    selects = [sql for sql in conn.sql if sql.strip().upper().startswith("SELECT")]
    assert selects, f"未捕获到领取 SELECT，实际：{conn.sql}"
    claim_sql = selects[0]
    assert "FOR UPDATE SKIP LOCKED" in claim_sql.upper(), (
        f"PG 领取 SELECT 缺 SKIP LOCKED：{claim_sql}"
    )
    assert "?" not in claim_sql, f"PG 领取 SELECT 仍用 ? 占位符：{claim_sql}"
    assert "%s" in claim_sql, f"PG 领取 SELECT 没有用 %s 占位符：{claim_sql}"


# ---------------------------------------------------------------------------
# 2. 并发语义：两个实例共享受控 fake 表，证明同一时刻只有一方领到同一行
# ---------------------------------------------------------------------------


class _ClaimTable:
    """受控的共享表 + 行锁状态。"""

    def __init__(self, rows) -> None:
        self.rows = rows          # list[dict]
        self.locked: set = set()  # 被某个开启事务锁定的行 id
        self.after_lock_hook = None
        self.hook_fired = False


class _ClaimConn:
    """建模 ``FOR UPDATE SKIP LOCKED`` 行锁的 fake 连接。

    - 领取 SELECT：挑第一条 ``claimed_at is None`` 且未被锁定的行；若 SQL 含
      ``FOR UPDATE SKIP LOCKED`` 就把该行加进 ``locked``（持锁到事务结束）。
    - 领取 UPDATE：命中未 claim 的选中行则置 ``claimed_at``、rowcount=1。
    - ``transaction()`` 退出（提交）释放本连接持有的锁。
    - 首个领取 SELECT 之后触发一次 ``after_lock_hook``：让「实例 2」在实例 1
      仍处于事务中（已锁行、尚未 UPDATE）时发起领取，模拟真实并发重叠。
    """

    def __init__(self, table: _ClaimTable) -> None:
        self.table = table
        self.own_locks: set = set()
        self.selected = None
        self.sql: list[str] = []

    def _eligible(self):
        for row in self.table.rows:
            if row["claimed_at"] is None and row["id"] not in self.table.locked:
                return row
        return None

    def execute(self, sql, params=()):
        self.sql.append(sql)
        upper = sql.upper()
        if upper.strip().startswith("SELECT") and "PENDING_SESSION_CLAIMS" in upper:
            row = self._eligible()
            if row is not None and "FOR UPDATE SKIP LOCKED" in upper:
                self.table.locked.add(row["id"])
                self.own_locks.add(row["id"])
            self.selected = row
            hook = self.table.after_lock_hook
            if hook is not None and not self.table.hook_fired:
                self.table.hook_fired = True
                hook()
            return _Result(row, rowcount=0 if row is None else 1)
        if upper.strip().startswith("UPDATE") and "PENDING_SESSION_CLAIMS" in upper:
            row = self.selected
            if row is None or row["claimed_at"] is not None:
                return _Result(None, rowcount=0)
            row["claimed_at"] = params[1] if len(params) > 1 else True
            return _Result(None, rowcount=1)
        return _Result(None, rowcount=0)

    @contextmanager
    def transaction(self):
        try:
            yield self
        finally:
            for row_id in self.own_locks:
                self.table.locked.discard(row_id)
            self.own_locks.clear()

    def commit(self) -> None:  # pragma: no cover
        pass

    def rollback(self) -> None:  # pragma: no cover
        pass


def _two_instances(rows):
    table = _ClaimTable(rows)
    repo1 = PendingSessionRepository(_connect_factory(_ClaimConn(table)), POSTGRES_DIALECT)
    repo2 = PendingSessionRepository(_connect_factory(_ClaimConn(table)), POSTGRES_DIALECT)
    return table, repo1, repo2


def test_pg_concurrent_claim_single_row_second_instance_gets_none() -> None:
    table, repo1, repo2 = _two_instances([dict(CLAIM_ROW)])
    captured: dict = {}

    def _interleave() -> None:
        # 实例 1 已锁行、尚未 UPDATE 时，实例 2 尝试领取。
        captured["second"] = repo2.claim_one(now=NOW)

    table.after_lock_hook = _interleave

    first = repo1.claim_one(now=NOW)

    assert first is not None and first["session_id"] == "sess-1", (
        f"实例 1 应领到 sess-1，实际：{first}"
    )
    assert captured.get("second") is None, (
        f"同一行被并发实例 2 重复领到：{captured.get('second')}"
    )


def test_pg_concurrent_claim_two_rows_second_instance_gets_different_row() -> None:
    rows = [dict(CLAIM_ROW), {**CLAIM_ROW, "id": 2, "session_id": "sess-2"}]
    table, repo1, repo2 = _two_instances(rows)
    captured: dict = {}
    table.after_lock_hook = lambda: captured.__setitem__("second", repo2.claim_one(now=NOW))

    first = repo1.claim_one(now=NOW)
    second = captured.get("second")

    assert first is not None and second is not None, (first, second)
    assert first["session_id"] != second["session_id"], (
        f"两个并发实例领到了同一条：{first} / {second}"
    )
    assert {first["session_id"], second["session_id"]} == {"sess-1", "sess-2"}


# ---------------------------------------------------------------------------
# 3. PG transcripts：INSERT ... RETURNING，返回真实新 id，不碰 lastrowid
# ---------------------------------------------------------------------------


class _InsertReturningConn:
    def __init__(self, new_id: int) -> None:
        self.sql: list[str] = []
        self.params: list[tuple] = []
        self._new_id = new_id

    def execute(self, sql, params=()):
        self.sql.append(sql)
        self.params.append(tuple(params))
        return _Result({"id": self._new_id}, rowcount=1)

    @contextmanager
    def transaction(self):
        yield self

    def commit(self) -> None:  # pragma: no cover
        pass

    def rollback(self) -> None:  # pragma: no cover
        pass

    def __enter__(self):  # 兼容未修复前 ``with conn:`` 写法，让 RED 落在 lastrowid
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _PgTranscriptStore:
    def __init__(self, conn) -> None:
        self.dialect = POSTGRES_DIALECT
        self._conn = conn
        self.audits: list = []

    @contextmanager
    def connect(self):
        yield self._conn

    def write_audit(self, *args, **kwargs) -> None:
        self.audits.append((args, kwargs))


def test_pg_transcripts_insert_uses_returning_and_returns_real_id() -> None:
    conn = _InsertReturningConn(42)
    store = _PgTranscriptStore(conn)
    svc = TranscriptService(store, MemoryKeyCipher(), persistence_checker=lambda: True)

    new_id = svc.save("sess-1", "hello", now=NOW)

    assert new_id == 42, f"应返回 RETURNING 的真实新 id，实际：{new_id!r}"
    inserts = [sql for sql in conn.sql if "INSERT INTO transcripts" in sql]
    assert inserts, f"未捕获到 transcripts INSERT，实际：{conn.sql}"
    insert_sql = inserts[0]
    assert "RETURNING" in insert_sql.upper(), f"PG INSERT 未走 RETURNING：{insert_sql}"
    assert "?" not in insert_sql, f"PG INSERT 仍用 ? 占位符：{insert_sql}"


# ---------------------------------------------------------------------------
# 4. SQLite 路径不回归
# ---------------------------------------------------------------------------


def test_sqlite_pending_claim_still_works(tmp_path: Path) -> None:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    store.enqueue_pending_session("sess-1", "dev-1", "room-1", 1, FAR_FUTURE, now=NOW)

    claimed = store.claim_pending_session(now=NOW)
    assert claimed is not None and claimed["session_id"] == "sess-1", claimed
    assert store.claim_pending_session(now=NOW) is None


def test_sqlite_transcripts_still_returns_lastrowid(tmp_path: Path) -> None:
    store = VoiceStore(tmp_path / "voice.db")
    store.initialize()
    svc = TranscriptService(store, MemoryKeyCipher(), persistence_checker=lambda: True)

    transcript_id = svc.save("sess-1", "hello", now=NOW)

    assert isinstance(transcript_id, int) and transcript_id > 0, transcript_id
    assert svc.get(transcript_id) == "hello"

"""契约：**声明了却没人消费的字段，必须是一次有意识的决定**。

缺陷形状（本仓今天反复踩到）
------------------------------
`release-policy.json` / `command-lock.json` / `claims/*.json` 是控制面：写在里面的
每个键都该要么被门禁读取并据以判红，要么被显式认定为"给人读、不参与判定"。
但历史上多次出现"**声明与实际执行脱节**"：

* `allowed_evidence_kinds` 曾只在写入侧生效，校验侧对 `kind` 零引用 ⇒ 门禁放行伪造 kind；
* `--expect-gui` 曾是死参数（`store_true` + `default=True` 且无人读取）；
* `required_checks` 只被 `release` 分支执行，`verify` 分支连 `--command-lock` 都不读；
* `target.artifact` 那句描述没有任何校验器读取。

这类缺陷**不会自己暴露**：JSON 里有键、读码时"看起来有这项控制"，只有人工逐键核对
才发现它其实是装饰。所以需要一条守卫，让"新增一个没人消费的字段"必然红。

守卫的形状（刻意不靠人工清单的自觉）
------------------------------------
* **消费集合是机械算出来的**：AST 解析门禁的全部 Python 面（见 `_enforcement_modules`），
  收集 `obj.get("key")` / `obj["key"]`（仅 Load 上下文，写入不算读取）作为"读取点"。
* **声明集合是从盘上的 JSON 现读的**：policy 顶层键、command-lock 顶层键 + 每个 check 的
  每个键、每份 claim 的顶层键 + `target` 子键 + `evidence[]` 条目子键。
* 断言 `实际零消费集合 == 冻结清单`（增键要红、删键要红），
  并且**对每个冻结项断言它今天确实零读取点**（防止冻结项过期变成"永久豁免"）。
* 判据不区分"读了但不产生后果"（B 类）与"从未被读"（C 类）——**对门禁而言两者等价**：
  都没能让这条控制咬人。B/C 的细分只出现在审计报告里。

已知近似（写下来，避免后人误以为它比实际更强）
----------------------------------------------
读取点只按**键名字面量**匹配，不追踪数据流。理论上，门禁面里某个**别的** JSON
恰好用了同名键，会把本应"零消费"的声明键误判为已消费（漏报）。当前 28 个声明键中
不存在这种同名碰撞（7 个零消费键的裸名在门禁面里都没有任何字典键读取）。
正向/反向对照见 `test_detector_reads_a_known_consumed_key_and_ignores_a_sentinel`。
"""

from __future__ import annotations

import ast
import glob
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

GOVERNANCE = REPO_ROOT / "governance"
POLICY_PATH = GOVERNANCE / "release-policy.json"
COMMAND_LOCK_PATH = GOVERNANCE / "command-lock.json"
CLAIMS_DIR = GOVERNANCE / "claims"

# ── 冻结清单：今天"声明了但门禁不消费"的字段 ────────────────────────────────
# 键 = 命名空间化字段名；值 = **为什么它可以不参与判定**。
# 新增一项必须在这里写明理由；删掉任何一项都会让守卫变红（因为实际集合没变）。
# 反过来，如果某个冻结项开始被门禁读取，守卫也会红 —— 那时应当把它从本表移除，
# 而不是让本表变成一份永不失效的豁免名册。
ALLOWED_UNCONSUMED = {
    # 2026-09-21 移除三项（原审计报告 C-1 / C-2）——它们已从"声明"变成"被执行"：
    #   policy.schema_version        -> release-preflight.py:_load_policy 判 UNSUPPORTED_POLICY_SCHEMA
    #   policy.release_channel       -> release-preflight.py:_load_policy 判 UNSUPPORTED_RELEASE_CHANNEL
    #   command_lock.schema_version  -> release-preflight.py:_load_command_lock 判 UNSUPPORTED_COMMAND_LOCK_SCHEMA
    # 本文件的那条"冻结项不得变成永久豁免"断言当时**红着提醒了**，并给出读取点行号 ——
    # 这正是它存在的意义：修好之后必须回来把豁免删掉，而不是让它悄悄留成永久豁免。
    "claim.risk": (
        "人读的语义元数据（'这条声明在断言什么'）。写入侧只做整字典合并保留"
        "（scripts/record-release-evidence.py:153-159），没有任何校验器读取或断言它。"
    ),
    "claim.required_scenarios": (
        "人读的场景清单（'必须演示哪些场景'）。同 claim.risk，只被写入侧合并保留，"
        "无校验器读取 —— 门禁不会因为场景没被演示而拦。"
    ),
    "claim.legacy_tasks": (
        "溯源元数据（旧任务名）。同 claim.risk，只被写入侧合并保留，无校验器读取。"
    ),
    "claim.target.artifact": (
        "人读的产物描述（'绑定的哪条产物链'）。门禁机械比对的是 target.artifact_commit "
        "与 target.artifact_sha256，**这句描述没有任何校验器读取**。"
        "其'不得被写入侧覆盖'由 backend/tests/contract/test_record_release_evidence_contract.py:169 "
        "守护，但那条属于 deploy-cloudrun 流水线，不在本 workflow 的 test job 里。"
    ),
}


def _enforcement_modules():
    """门禁的 Python 面：governance 包全部模块 + 三个顶层入口脚本。

    刻意用 glob 而不是硬编码模块清单：新增 `scripts/release_governance/xxx.py`
    会被自动纳入扫描，否则新模块里的消费点会被漏算、把已消费的键误判成零消费。
    """
    modules = sorted(glob.glob(str(REPO_ROOT / "scripts" / "release_governance" / "*.py")))
    modules += [
        str(REPO_ROOT / "scripts" / "release-preflight.py"),
        str(REPO_ROOT / "scripts" / "check-release-blockers.py"),
        str(REPO_ROOT / "scripts" / "record-release-evidence.py"),
    ]
    return modules


def _dict_key_reads(path):
    """返回该文件里全部"字典键读取"的键名 → [(行号, 形式)]。

    只算 Load 上下文：`claim["claim_id"] = x` 是**写入**，不是消费，不计入。
    """
    reads = {}
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return reads
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            reads.setdefault(node.slice.value, []).append((node.lineno, "[]"))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            reads.setdefault(node.args[0].value, []).append((node.lineno, ".get()"))
    return reads


def _read_index():
    """{裸键名: [读取点...]}，跨全部门禁模块汇总。"""
    index = {}
    for module in _enforcement_modules():
        for key, sites in _dict_key_reads(module).items():
            index.setdefault(key, []).extend(sites)
    return index


def _declared_keys():
    """命名空间化字段名 → 裸键名，覆盖三个声明位置的全部声明键。"""
    declared = {}

    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    for key in policy:
        declared["policy.%s" % key] = key

    lock = json.loads(COMMAND_LOCK_PATH.read_text(encoding="utf-8"))
    for key in lock:
        declared["command_lock.%s" % key] = key
    for check in lock.get("checks") or []:
        for key in check:
            declared["command_lock.checks[].%s" % key] = key

    claim_top, claim_target, claim_evidence = set(), set(), set()
    for claim_path in sorted(CLAIMS_DIR.glob("*.json")):
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        claim_top |= set(claim)
        claim_target |= set(claim.get("target") or {})
        for evidence in claim.get("evidence") or []:
            claim_evidence |= set(evidence)
    for key in sorted(claim_top):
        declared["claim.%s" % key] = key
    for key in sorted(claim_target):
        declared["claim.target.%s" % key] = key
    for key in sorted(claim_evidence):
        declared["claim.evidence[].%s" % key] = key

    return declared


def _unconsumed(declared, read_index):
    """零消费字段集合。裸键名只要在门禁面里出现过一次字典键读取，即视为已消费。"""
    return {name for name, bare in declared.items() if bare not in read_index}


def _differences(actual, frozen):
    """(多出来的, 少掉的) —— 两个方向都要能报出来，否则守卫只挡一半。"""
    return sorted(actual - frozen), sorted(frozen - actual)


def _stale(frozen, declared, read_index):
    """冻结清单里已经站不住的条目：字段没了，或字段其实已被读取。"""
    offenders = []
    for name in sorted(frozen):
        bare = declared.get(name)
        if bare is None:
            offenders.append((name, "字段已不在任何声明文件里"))
        elif bare in read_index:
            offenders.append((name, "已被门禁读取：%s" % (read_index[bare][:3],)))
    return offenders


# ── 守卫本体 ────────────────────────────────────────────────────────────────


def test_enforcement_surface_is_present_and_readable():
    """守卫自己的底座：门禁面存在、可解析、确实读到了键。

    没有这条，`_read_index()` 返回空字典时「一切都零消费」会被静默吞掉，
    整条守卫退化成"永远红"或"永远绿"。
    """
    modules = _enforcement_modules()
    assert len(modules) >= 7, "门禁模块面疑似被削小：%s" % modules
    for module in modules:
        assert Path(module).is_file(), "门禁模块缺失（重命名了？守卫的扫描面要同步）: %s" % module
    index = _read_index()
    assert len(index) >= 20, "门禁面里读到的字典键太少（%d 个），检测器可能坏了" % len(index)


def test_detector_reads_a_known_consumed_key_and_ignores_a_sentinel():
    """检测器的正/反向对照 —— 证明它既不瞎、也不乱报。

    正向：`required_claim_ids` 是**确定**被门禁读取的（model.py:122、verify.py:201/236）。
    反向：一个绝不存在的键名必须零命中。
    """
    index = _read_index()

    assert "required_claim_ids" in index, "检测器漏掉了确定被读的键 —— 它瞎了"
    assert index["required_claim_ids"], "命中集为空"
    assert "__no_such_key_in_this_repository__" not in index, "检测器把不存在的键报了命中"


def test_declared_unconsumed_keys_equal_the_frozen_inventory():
    """核心判据：实际零消费集合必须**逐项等于**冻结清单。

    新增一个没人消费的字段 ⇒ 实际集合变大 ⇒ 红。
    删掉一个声明字段 ⇒ 实际集合变小 ⇒ 红。
    两条路都必须让人回到这张表前，写下"它为什么可以不参与判定"。
    """
    actual = _unconsumed(_declared_keys(), _read_index())
    frozen = set(ALLOWED_UNCONSUMED)
    added, missing = _differences(actual, frozen)

    assert not added, (
        "发现**新声明但无人消费**的字段：%s\n"
        "门禁里没有任何地方读取它们，因此它们只是装饰。请二选一：\n"
        "  (a) 让门禁真的消费它（读它并据以判红）；或\n"
        "  (b) 把它连同理由写进本文件顶部的 ALLOWED_UNCONSUMED。" % added
    )
    assert not missing, (
        "冻结清单里有 %s 已不再是「零消费」（被消费了，或字段被删了）。\n"
        "冻结项不得变成永久豁免：请把它们从 ALLOWED_UNCONSUMED 移除。" % missing
    )


def test_every_frozen_entry_still_has_zero_readers():
    """防止冻结项过期：每个冻结项今天必须**确实**找不到读取点。"""
    offenders = _stale(set(ALLOWED_UNCONSUMED), _declared_keys(), _read_index())
    assert not offenders, (
        "冻结清单里的条目已经不成立了（它们是过期豁免）：\n  %s"
        % "\n  ".join("%s -> %s" % pair for pair in offenders)
    )


def test_frozen_inventory_entries_all_carry_a_reason():
    """每一项冻结都必须写明理由 —— 空理由等于变相豁免。"""
    for name, reason in ALLOWED_UNCONSUMED.items():
        assert isinstance(reason, str) and len(reason.strip()) >= 20, (
            "冻结项 %s 缺少实质理由" % name
        )


def test_guard_judgements_fire_on_synthetic_mutations():
    """守卫的自证：把三组**合成**变异喂给同一套判据，三组都必须被抓住。

    刻意用合成输入而不是盘上的真实清单：否则本自证会与真实清单耦合 ——
    某个冻结项将来被合法消费掉时，自证会跟着误红，那时人们只会去改自证，
    而不是去改真正该改的地方。
    """
    read_index = {"consumed_key": [("some_module.py", 1, ".get()")]}
    declared = {"ns.consumed_key": "consumed_key", "ns.novel_key": "novel_key"}
    frozen = {"ns.novel_key"}

    # 底色：一个已消费键 + 一个已冻结的零消费键，判据自洽
    assert _unconsumed(declared, read_index) == {"ns.novel_key"}
    assert _differences(_unconsumed(declared, read_index), frozen) == ([], [])

    # 变异①：多一个谁都不读的声明键
    assert _differences(
        _unconsumed({**declared, "ns.injected": "injected"}, read_index), frozen
    ) == (["ns.injected"], [])

    # 变异②：从冻结清单里删一项（实际集合没变）
    assert _differences(_unconsumed(declared, read_index), set()) == (["ns.novel_key"], [])

    # 变异③：把一个真被读的键塞进冻结清单 —— 两条判据都要抓住它
    assert _differences(
        _unconsumed(declared, read_index), frozen | {"ns.consumed_key"}
    ) == ([], ["ns.consumed_key"])
    assert [name for name, _ in _stale(frozen | {"ns.consumed_key"}, declared, read_index)] == [
        "ns.consumed_key"
    ]


# mutation_verify — status_detector 变异验证

## 目的

证明 `tests/unit/test_status_detector.py` 能真正杀掉 4 个阈值/超时变异，
杜绝"假绿"：边界用例若测不出变异，说明测试复述实现、零信息量。

## 4 个变异靶

| # | 变异名 | 语义 | 对应杀灭用例 |
|---|--------|------|--------------|
| 1 | frame_threshold_3_to_2 | 有效帧阈值 3→2（比较改为 `>= threshold - 1`） | `test_2_frames_elapsed_120_not_trigger` |
| 2 | frame_threshold_3_to_4 | 有效帧阈值 3→4（比较改为 `>= threshold + 1`） | `test_3_frames_exact_120_triggers` |
| 3 | timeout_120_to_121 | 有效超时 120→121（比较改为 `>= timeout + 1`） | `test_3_frames_exact_120_triggers` |
| 4 | timeout_120_to_119 | 有效超时 120→119（比较改为 `>= timeout - 1`） | `test_timeout_119s_not_trigger`（既有用例） |

> 注：变异注入在 `app/engine/status_detector.py` 的比较表达式上做 ±1，
> 语义等价于把阈值/超时配置默认值改掉（测试显式传 cfg，故不能改 config 默认值）。

## 用法

```bash
cd backend
C:/Users/Administrator/.workbuddy/binaries/python/envs/monitor-app/Scripts/python.exe \
    tests/unit/mutation_verify/run_mutation_verify.py
```

输出：`mutation_report.txt`（本目录）。退出码 0 = 4 变异全杀，1 = 有存活变异。

## 原理与安全

- 脚本对 `app/engine/status_detector.py` 逐个注入变异 → 跑 `test_status_detector.py` → 恢复原实现。
- 变异副本保存在 `mutants/`（含 `_original_backup.py`）。
- 用 `try/finally` 保证每次注入后必然恢复；恢复后校验 sha256 与原文件一致。
- 除 `app/engine/status_detector.py` 的瞬时替换（即刻恢复）外，**不修改任何实现代码**。

## 结论判定

- KILLED：变异实现下测试失败（pytest exit != 0）→ 该变异被杀。
- SURVIVED：变异实现下测试全绿 → 测试有洞，必须补用例。
- 目标：4/4 KILLED。

## config 层级 bug 回归验证（先红后绿）

`config_regression_verify.py`：临时把 `app/config.py` 还原为历史 buggy 版本
（`load_detection/load_push` 整包 `model_validate`，忽略顶层 `detection:`/`push:` 键），
跑 `test_config_loading.py` → 必须 RED（注入 5/180/3 得默认 3/120/2 被断言捕获）；
恢复修复版 → 必须 GREEN。

```bash
python tests/unit/mutation_verify/config_regression_verify.py
```

输出：`config_regression_report.txt`。退出码 0 = 红绿门满足。

> 说明：后端在测试落地前已完成 config 修复，故本脚本通过"临时还原 buggy 版"
> 给出测试能捕获该 bug 的实证（红），再验证修复版通过（绿）。

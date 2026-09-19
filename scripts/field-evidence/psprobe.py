"""可信的进程探针（可复用）。

为什么要重写而不是修一修：
  installed_e2e.py 里的 procs() 报「侧车 0 个」，而**同一时刻**侧车日志正在以 500ms 打点。
  于是做了两组对照：
    · proc_probe_live.py：同一个 probe() 里，v3 看到 5 个进程，v1/v4/v5/v6 看到 0 个；
    · field_probe.py    ：把"带不带 ExecutablePath""-Command 还是 -EncodedCommand"
                          拆成 7 个对照 —— **全都正常看到 5 个**，包括 v1 的等价写法 E。
  同一个命令串在两次运行里给出不同结果 ⇒ 结论不是"某字段不能用"，而是
  **这套取法不可复现，任何单次调用的结果都不可作为证据**。

  最可能的载体是「把脚本塞进 -Command 的参数里」这件事本身：
  Python 的 list2cmdline 会把 `"` 转义成 `\\"`，再由 powershell.exe 的
  CommandLineToArgvW 解回来，最后 PowerShell 再解析一遍 —— 三层引号语义叠在一起。
  这条路径上任何非确定性都会表现为"命令跑了、rc=0、输出却是空的"，
  正是我们最该消灭的那种"看起来跑了，其实什么都没发生"。

修法（两条，缺一不可）：
  1. **把脚本落到 .ps1 用 -File 执行** —— 命令行里不再出现任何引号，消除整类不确定性；
     输出一律走 **base64**，stdout 保证是纯 ASCII，不受代码页影响（路径里有"贾克斯·星核"）。
  2. **探针自带阳性对照 + 双源交叉**，并把"探针本身可信吗"一起返回（trusted 字段）。
     取不到阳性对照时返回 trusted=False，而不是返回 0 —— 0 和"看不见"必须能区分。

对外接口：
    probe_jax() -> dict
        {
          "trusted": bool,          # 探针本次是否可信（阳性对照 + 双源一致）
          "why_untrusted": str,
          "control_powershell": int,# 阳性对照：本机 powershell.exe 个数（必须 >= 1）
          "total_processes": int,
          "jax": [{"pid","name","path","source"}...],
          "cim_names": [...], "gp_names": [...],
          "ps_stdout_bytes": int, "ps_stderr": str,
        }
"""
import base64
import json
import pathlib
import subprocess
import tempfile

PS = r"""
$ErrorActionPreference = 'Continue'
function B64([string]$s) {
  if ([string]::IsNullOrEmpty($s)) { return '' }
  return [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($s))
}

$cim = @(Get-CimInstance Win32_Process)
$cimJax = @()
foreach ($p in $cim) {
  if ($p.Name -eq 'jax-pet.exe' -or $p.Name -eq 'jax-rtc-sidecar.exe') {
    $cimJax += ('{0}|{1}|{2}' -f $p.ProcessId, $p.Name, (B64 $p.ExecutablePath))
  }
}
$cimAll = @()
foreach ($p in $cim) {
  if ($p.Name -like 'jax*') { $cimAll += ('{0}|{1}|{2}' -f $p.ProcessId, $p.Name, (B64 $p.ExecutablePath)) }
}

$gp = @(Get-Process -ErrorAction SilentlyContinue)
$gpJax = @()
foreach ($p in $gp) {
  if ($p.ProcessName -eq 'jax-pet' -or $p.ProcessName -eq 'jax-rtc-sidecar') {
    $pp = ''
    try { $pp = [string]$p.Path } catch { $pp = '' }
    $gpJax += ('{0}|{1}|{2}' -f $p.Id, ($p.ProcessName + '.exe'), (B64 $pp))
  }
}

$ctrl = 0
foreach ($p in $cim) { if ($p.Name -eq 'powershell.exe') { $ctrl++ } }

$obj = [ordered]@{
  control_powershell = $ctrl
  total_processes    = $cim.Count
  cim_jax            = $cimJax
  cim_jax_like       = $cimAll
  gp_jax             = $gpJax
}
$obj | ConvertTo-Json -Compress -Depth 4
"""

_PS1 = None


def _ps1_path():
    global _PS1
    if _PS1 is None:
        d = pathlib.Path(tempfile.gettempdir()) / "jax-pe"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "probe_processes.ps1"
        # UTF-8 BOM：PowerShell 5.1 靠 BOM 判 UTF-8，否则按 ANSI 读会把中文注释读坏
        p.write_text(PS, encoding="utf-8-sig")
        _PS1 = p
    return _PS1


def _decode(row):
    pid, name, b64 = (row.split("|", 2) + ["", ""])[:3]
    try:
        path = base64.b64decode(b64).decode("utf-8", "replace") if b64 else ""
    except Exception:  # noqa: BLE001
        path = "<b64 decode fail>"
    return pid.strip(), name.strip(), path


def probe_jax(timeout=90):
    """跑一次探针，返回带可信度自评的字典。"""
    res = {
        "trusted": False,
        "why_untrusted": "",
        "control_powershell": 0,
        "total_processes": 0,
        "jax": [],
        "cim_names": [],
        "gp_names": [],
        "ps_stdout_bytes": 0,
        "ps_stderr": "",
    }
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(_ps1_path())],
            capture_output=True, timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        res["why_untrusted"] = f"powershell 起不来/超时: {e!r}"
        return res

    raw = r.stdout or b""
    res["ps_stdout_bytes"] = len(raw)
    res["ps_stderr"] = (r.stderr or b"").decode("utf-8", "replace")[:400]
    try:
        data = json.loads(raw.decode("utf-8-sig", "replace"))
    except Exception as e:  # noqa: BLE001
        res["why_untrusted"] = f"stdout 不是 JSON（rc={r.returncode}）: {e!r}"
        return res

    def norm(x):
        return x if isinstance(x, list) else ([x] if x else [])

    res["control_powershell"] = int(data.get("control_powershell") or 0)
    res["total_processes"] = int(data.get("total_processes") or 0)
    cim = [c for c in (_decode(x) for x in norm(data.get("cim_jax"))) if c[1]]
    gps = [c for c in (_decode(x) for x in norm(data.get("gp_jax"))) if c[1]]
    res["cim_names"] = sorted(f"{n}:{p}" for p, n, _ in cim)
    res["gp_names"] = sorted(f"{n}:{p}" for p, n, _ in gps)
    res["jax"] = [{"pid": p, "name": n, "path": path, "source": "cim"} for p, n, path in cim]

    # --- 可信度自评：这是本模块存在的理由 ---
    reasons = []
    if res["control_powershell"] < 1:
        reasons.append("阳性对照失败：连 powershell.exe 自己都看不见")
    if res["total_processes"] < 20:
        reasons.append(f"进程总数异常少（{res['total_processes']}），枚举疑似被截断")
    if len(res["ps_stderr"]) > 200:
        reasons.append(f"stderr 偏长（{len(res['ps_stderr'])} B），脚本可能部分失败")
    if res["cim_names"] != res["gp_names"]:
        reasons.append(f"双源不一致 CIM={res['cim_names']} GP={res['gp_names']}")
    if len(cim) != len(gps):
        reasons.append(f"双源条数不一致 CIM={len(cim)} GP={len(gps)}")
    res["why_untrusted"] = "; ".join(reasons)
    res["trusted"] = not reasons
    return res


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(probe_jax(), ensure_ascii=False, indent=2))

"""harness 自检：**先证明这把尺子量得到东西**，再用它去量产品。

理由和进程探针那次一模一样：如果"可见控制台窗口 = 0"是因为
`EnumWindows` 那条路根本看不见控制台窗口，那这个 0 就毫无价值。
所以：

  1. 基线      —— 当前全机可见控制台窗口有几个；
  2. 阳性对照  —— 显式 `CREATE_NEW_CONSOLE` 拉起一个 cmd.exe，
                   看 harness 是否**多看见一个**可见的 `ConsoleWindowClass`；
  3. 阴性对照  —— 关掉它，看是否**收回**；
  4. 产品对照  —— 起已安装的 jax-pet.exe，看它的后代里有没有控制台窗口。
"""
import subprocess
import sys
import time

sys.path.insert(0, r"C:\Users\Administrator\AppData\Local\Temp\jax-pe")
import win_popup_capture as W  # noqa: E402

CREATE_NEW_CONSOLE = 0x00000010

out = []
def w(s=""):
    out.append(s)
    print(s, flush=True)


def console_report(tag):
    snap = W.snapshot(tag, health=False)
    w(f"[{tag}] trusted={snap['probe_trusted']} 控制台计数={snap['probe_control']} "
      f"全机进程={snap['probe_total']}")
    w(f"      全机可见控制台窗口 = {snap['visible_console_windows']}")
    for x in snap["visible_console_windows_all"]:
        w(f"        PID {x['pid']} {x['owner']} class={x['class_name']}")
    return snap


w("harness 自检：窗口枚举的阳性/阴性对照")
w("=" * 78)

s0 = console_report("基线")

w()
w("--- 阳性对照：显式拉一个可见控制台 ---")
p = subprocess.Popen(["cmd.exe", "/k", "echo CONSOLE-POSITIVE-CONTROL-HOLD"],
                     creationflags=CREATE_NEW_CONSOLE)
w(f"    拉起 cmd.exe PID={p.pid}")
time.sleep(3)
s1 = console_report("阳性对照")

w()
w("--- 阴性对照：关掉它 ---")
subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True)
time.sleep(3)
s2 = console_report("阴性对照")

w()
w("=" * 78)
delta_up = s1["visible_console_windows"] - s0["visible_console_windows"]
delta_down = s1["visible_console_windows"] - s2["visible_console_windows"]
w(f"阳性对照增量 = {delta_up}   （要求 >= 1）")
w(f"阴性对照回落 = {delta_down} （要求 >= 1）")
w(f"判定: {'尺子能用（看得见控制台窗口的增与消）' if delta_up >= 1 and delta_down >= 1 else '**尺子不灵：0 个不可信**'}")

w()
w("--- 产品对照：起已安装的 jax-pet.exe ---")
import pathlib, os  # noqa: E402
INSTALL = pathlib.Path(os.environ["LOCALAPPDATA"]) / "贾克斯·星核"
err = pathlib.Path(r"C:\Users\Administrator\WorkBuddy\监视app\outputs\2026-09-19-selftest-app-stderr.log")
with open(err, "wb") as fh:
    app = subprocess.Popen([str(INSTALL / "jax-pet.exe")], stdout=fh, stderr=fh,
                           stdin=subprocess.DEVNULL, cwd=str(INSTALL))
w(f"    PID={app.pid}，等 40s")
time.sleep(40)
s3 = W.snapshot("产品对照", health=True)
w(W.render(s3))

w()
w("--- 清场 ---")
pr = W.probe_processes()
pids = [x["pid"] for x in pr["procs"]
        if x["name"].lower() in W.PRODUCT_EXES or
        (x["name"].lower() == "conhost.exe" and x["ppid"] in
         {y["pid"] for y in pr["procs"] if y["name"].lower() in W.PRODUCT_EXES})]
for pid in pids:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
w(f"    杀掉: {pids}")
time.sleep(2)
w(f"    杀后 jax 进程: "
  f"{[p['pid'] for p in W.probe_processes()['procs'] if p['name'].lower() in W.PRODUCT_EXES]}")

sys.stdout.reconfigure(encoding="utf-8")
text = "\n".join(out) + "\n"
pathlib.Path(r"C:\Users\Administrator\AppData\Local\Temp\jax-pe\capture_selftest.txt").write_text(text, encoding="utf-8")

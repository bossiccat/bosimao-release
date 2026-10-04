# -*- coding: utf-8 -*-
"""_patch_nsi_v4n.py — 复刻定型配方第 4 步：installer.nsi 长路径二进制替换。

把 installer.nsi 中出现的仓库绝对路径前缀
    C:\\Users\\Administrator\\WorkBuddy\\监视app\\
替换为 subst 盘符
    X:\\
以绕开 makensis 对超长路径的处理失败。二进制级替换（bytes），不做行读写，
避免任何换行翻译。替换 0 处视为异常（配方：0 处 = 异常，停）。
"""
import sys

NSI = r"pet-ui/src-tauri/target/release/nsis/x64/installer.nsi"

LONG_PREFIX = "C:\\Users\\Administrator\\WorkBuddy\\监视app\\".encode("utf-8")
SHORT_PREFIX = b"X:\\"
LONG_BARE = "C:\\Users\\Administrator\\WorkBuddy\\监视app".encode("utf-8")
SHORT_BARE = b"X:"

with open(NSI, "rb") as f:
    data = f.read()

n1 = data.count(LONG_PREFIX)
data = data.replace(LONG_PREFIX, SHORT_PREFIX)
n2 = data.count(LONG_BARE)
data = data.replace(LONG_BARE, SHORT_BARE)

with open(NSI, "wb") as f:
    f.write(data)

total = n1 + n2
print(f"patched with-trailing-slash={n1}, bare={n2}, total={total}")
if total == 0:
    print("ABNORMAL: 0 replacements — abort per recipe")
    sys.exit(2)
print("OK")

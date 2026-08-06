"""pytest 共享夹具：把 trtc-sign 函数根加入 sys.path（扁平 SCF 结构，无 package）"""
import os
import sys

FUNC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if FUNC_ROOT not in sys.path:
    sys.path.insert(0, FUNC_ROOT)

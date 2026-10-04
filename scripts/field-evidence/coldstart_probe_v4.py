# -*- coding: utf-8 -*-
"""冷启动方框探针 v4：完整三阶段时间线。

阶段1 方框期：父窗口 visible 且 WRY_WEBVIEW 子窗口未创建 —— 桌面显示父窗口背景刷（方框）。
阶段2 透明期：子窗口在、WebView 未画内容（透明，桌面无内容）。
阶段3 内容期：宠物首帧。
测量：window_visible_ms / webview_created_ms / webview_first_paint_ms / 方框刷颜色 / 窗口DC帧。
用法: python coldstart_probe_v4.py <exe路径> <输出目录>
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time

from PIL import Image

user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
TITLE = "贾克斯 · 星核"
DURATION_MS = 6000
POLL_MS = 3
CAPTURE_MS = 25
PW_RENDERFULLCONTENT = 0x2
GCLP_HBRBACKGROUND = -10


class BMIH(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


def find_pet_hwnd():
    hits = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if buf.value == TITLE and user32.IsWindowVisible(hwnd):
            hits.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return hits[0] if hits else None


def find_webview_child(parent):
    out = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hk, _):
        cls = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hk, cls, 128)
        if cls.value == "WRY_WEBVIEW":
            out.append(hk)
        return True

    user32.EnumChildWindows(parent, cb, 0)
    return out[0] if out else None


def grab(hwnd, w, h, flag=PW_RENDERFULLCONTENT):
    hdc = user32.GetDC(0)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    ok = user32.PrintWindow(hwnd, mem, flag)
    bmi = BMIH(ctypes.sizeof(BMIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(0, hdc)
    if not ok:
        return None
    return Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1)


def bitblt_window_dc(hwnd, w, h):
    """GetDC(hwnd)+BitBlt：窗口自身 DC（不走屏幕合成）。"""
    hdc = user32.GetDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    ok = gdi32.BitBlt(mem, 0, 0, w, h, hdc, 0, 0, 0x00CC0020)
    bmi = BMIH(ctypes.sizeof(BMIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)
    if not ok:
        return None
    return Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1)


def frame_stats(img):
    rgb = img.convert("RGB")
    small = rgb.resize((48, 48))
    px = list(small.getdata())
    n = len(px)
    mean = tuple(sum(c[i] for c in px) // n for i in range(3))
    var = [0.0, 0.0, 0.0]
    for c in px:
        for i in range(3):
            var[i] += (c[i] - mean[i]) ** 2
    std = sum(v / n for v in var) ** 0.5 / 3.0
    whiteish = sum(1 for r, g, b in px if min(r, g, b) > 200) / n
    grayish = (
        sum(1 for r, g, b in px if abs(r - g) < 14 and abs(g - b) < 14 and 140 < r < 250) / n
    )
    black = sum(1 for r, g, b in px if max(r, g, b) < 8) / n
    brightness = sum(mean) / 3.0
    # 方框 = 近乎单色的非黑浅色（白/暖灰矩形 = 窗口背景刷裸露）
    is_box = std < 6.0 and 140 < brightness < 245
    is_black = black > 0.98
    return mean, round(std, 2), round(whiteish, 2), round(grayish, 2), is_box, is_black


def brush_color(hbr):
    """LOGBRUSH.lbColor of an HBRUSH."""
    class LOGBRUSH(ctypes.Structure):
        _fields_ = [("lbStyle", wt.UINT), ("lbColor", wt.COLORREF), ("lbHatch", ctypes.c_long)]

    lb = LOGBRUSH()
    if gdi32.GetObjectW(hbr, ctypes.sizeof(LOGBRUSH), ctypes.byref(lb)):
        c = lb.lbColor
        return (c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF)  # COLORREF -> RGB
    return None


def main():
    exe, outdir = sys.argv[1], sys.argv[2]
    os.makedirs(outdir, exist_ok=True)
    subprocess.run(["taskkill", "/F", "/IM", "jax-pet.exe"], capture_output=True)
    time.sleep(0.8)

    t0 = time.perf_counter()
    proc = subprocess.Popen([exe], cwd=os.path.dirname(exe))

    parent = None
    window_visible_ms = None
    while window_visible_ms is None and (time.perf_counter() - t0) * 1000 < 12000:
        parent = find_pet_hwnd()
        if parent:
            window_visible_ms = round((time.perf_counter() - t0) * 1000, 1)
        else:
            time.sleep(POLL_MS / 1000.0)
    if parent is None:
        print(json.dumps({"error": "pet window not visible within 12s"}))
        return

    # 方框期：父窗口 visible 但 webview 子窗口未创建
    webview = None
    webview_created_ms = None
    stage1_samples = []
    while webview is None and (time.perf_counter() - t0) * 1000 < DURATION_MS:
        t_ms = round((time.perf_counter() - t0) * 1000, 1)
        webview = find_webview_child(parent)
        if webview:
            webview_created_ms = t_ms
            break
        if len(stage1_samples) < 40:
            r = wt.RECT()
            user32.GetWindowRect(parent, ctypes.byref(r))
            w, h = r.right - r.left, r.bottom - r.top
            if w > 10:
                img = bitblt_window_dc(parent, w, h)
                if img is not None:
                    mean, std, _, _, is_box, is_black = frame_stats(img)
                    if len(stage1_samples) < 6 or is_box:
                        path = os.path.join(outdir, f"s1_{len(stage1_samples):02d}_{int(t_ms):05d}ms.png")
                        img.save(path)
                        stage1_samples.append(
                            {"t_ms": t_ms, "mean_rgb": mean, "std": std,
                             "is_box": is_box, "is_black": is_black, "png": path}
                        )
        time.sleep(POLL_MS / 1000.0)

    # 阶段2/3：子窗口在 → PrintWindow 抓帧直到内容帧出现
    frames = []
    while (time.perf_counter() - t0) * 1000 < DURATION_MS:
        t_ms = round((time.perf_counter() - t0) * 1000, 1)
        if not user32.IsWindow(webview):
            webview = find_webview_child(parent)
        if webview:
            r = wt.RECT()
            user32.GetWindowRect(parent, ctypes.byref(r))
            w, h = r.right - r.left, r.bottom - r.top
            if w > 10 and h > 10:
                img = grab(webview, w, h)
                if img is not None:
                    mean, std, whiteish, grayish, is_box, is_black = frame_stats(img)
                    path = os.path.join(outdir, f"f{len(frames):03d}_{int(t_ms):05d}ms.png")
                    img.save(path)
                    frames.append(
                        {"t_ms": t_ms, "size": [w, h], "mean_rgb": mean, "std": std,
                         "whiteish": whiteish, "grayish": grayish,
                         "is_box": is_box, "is_black": is_black, "png": path}
                    )
                    if not is_black and not is_box and len(frames) > 3:
                        # 内容帧已出现，再抓 2 帧收尾
                        if sum(1 for f in frames if not f["is_black"] and not f["is_box"]) >= 2:
                            break
        time.sleep(CAPTURE_MS / 1000.0)

    box_frames = [f for f in frames if f["is_box"]]
    black = [f for f in frames if f["is_black"]]
    content = [f for f in frames if not f["is_box"] and not f["is_black"]]
    hbr = user32.GetClassLongPtrW(parent, GCLP_HBRBACKGROUND)
    content_first = content[0]["t_ms"] if content else None
    summary = {
        "exe": exe,
        "pid": proc.pid,
        "window_visible_ms": window_visible_ms,
        "stage1_box_phase": {
            "duration_ms": (webview_created_ms - window_visible_ms) if webview_created_ms else None,
            "samples": stage1_samples,
        },
        "webview_created_ms": webview_created_ms,
        "webview_first_paint_ms": content_first,
        "transparent_phase_ms": (
            [f["t_ms"] for f in black][:8] if black else []
        ),
        "box_color_from_class_brush": brush_color(hbr) if hbr else None,
        "box_frame_ms": [f["t_ms"] for f in box_frames],
        "content_frame_ms": [f["t_ms"] for f in content],
        "frames": frames,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    with open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
简单录屏工具 —— Python 图形界面版
功能：全屏或自选区域录制屏幕，输出 MP4 视频。
用法：
    python 屏幕录制工具.py           启动图形界面
    python 屏幕录制工具.py --selftest  自测模式（录制 2 秒验证程序可用）
依赖：mss, opencv-python, numpy （pip install mss opencv-python）
"""
import os
import sys
import time
import datetime
import threading
import tempfile
import subprocess

import cv2
import mss
import numpy as np
import imageio_ffmpeg
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

DEFAULT_FPS = 10


def record_to_file(area, fps, out_path, stop_event=None, duration=None, on_progress=None):
    """
    核心录制函数：把指定屏幕区域录制为 MP4。
    area: (left, top, width, height)，屏幕坐标
    fps: 目标帧率
    out_path: 输出文件路径
    stop_event: threading.Event，设置后停止录制（GUI 用）
    duration: 录制时长（秒），到时间自动停止（自测用）
    on_progress: 录制/编码阶段回调，用于更新界面状态
    返回 (帧数, 实际平均帧率, 真实时长秒)；失败返回 None。

    实现分两遍：
      1. 第一遍按目标帧率抓帧，存为临时 mp4v 文件（不阻塞节奏）；
      2. 第二遍按真实平均帧率 + H.264(avc1) 重新编码为最终文件，
         这样播放速度与真实时间一致，且文件体积更小。
    """
    left, top, width, height = area
    # 编码要求宽高为偶数，否则写入会失败
    width -= width % 2
    height -= height % 2
    monitor = {"left": left, "top": top, "width": width, "height": height}
    tmp_path = out_path + ".raw.mp4"

    # ---- 第一遍：抓帧 ----
    raw_fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(tmp_path, raw_fourcc, fps, (width, height))
    if not writer.isOpened():
        return None

    interval = 1.0 / fps
    frames = 0
    t0 = time.time()
    try:
        with mss.MSS() as sct:
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                if duration is not None and time.time() - t0 >= duration:
                    break
                # mss 返回 BGRA 帧，取前 3 通道即 BGR，正是 OpenCV 需要的顺序
                frame = np.array(sct.grab(monitor))[:, :, :3]
                writer.write(frame)
                frames += 1
                # 按目标帧率安排下一帧时间
                remain = t0 + frames * interval - time.time()
                if remain > 0:
                    time.sleep(remain)
    finally:
        writer.release()

    real_duration = time.time() - t0
    if frames == 0 or real_duration <= 0:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return (0, 0.0, 0.0)

    # ---- 第二遍：用 ffmpeg + H.264(libx264) 重编码 ----
    actual_fps = max(frames / real_duration, 1.0)
    if on_progress:
        on_progress("正在编码视频…")

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg, "-y", "-i", tmp_path,
        "-c:v", "libx264", "-crf", "23", "-preset", "medium",
        "-r", f"{actual_fps:.3f}", "-pix_fmt", "yuv420p",
        out_path,
    ]
    proc = subprocess.run(cmd, capture_output=True)
    ffmpeg_ok = proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0

    if not ffmpeg_ok:
        # ffmpeg 不可用时回退到 OpenCV mp4v
        raw_fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(out_path, raw_fourcc, actual_fps, (width, height))
        if not writer.isOpened():
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            return None
        cap = cv2.VideoCapture(tmp_path)
        try:
            while True:
                ret, f = cap.read()
                if not ret:
                    break
                writer.write(f)
        finally:
            cap.release()
            writer.release()

    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    return (frames, actual_fps, real_duration)


class ScreenRecorderApp:
    def __init__(self, root):
        self.root = root
        root.title("简单录屏工具")
        root.geometry("500x320")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.area = None                      # (left, top, width, height)，None 表示全屏
        self.stop_event = threading.Event()
        self.recording = False
        self.start_time = 0.0
        self._countdown = 0
        self._record_area = None
        self._fps = DEFAULT_FPS
        self._out_path = ""

        self._build_ui()
        self.reset_area()

    # ---------------- 界面 ----------------
    def _build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        # 录制区域
        row1 = ttk.Frame(main)
        row1.pack(fill="x", pady=6)
        ttk.Label(row1, text="录制区域：").pack(side="left")
        self.area_var = tk.StringVar()
        ttk.Label(row1, textvariable=self.area_var, width=26).pack(side="left", padx=6)
        ttk.Button(row1, text="选择区域", command=self.select_area).pack(side="left")
        ttk.Button(row1, text="全屏", command=self.reset_area).pack(side="left", padx=4)

        # 帧率
        row2 = ttk.Frame(main)
        row2.pack(fill="x", pady=6)
        ttk.Label(row2, text="帧率 (fps)：").pack(side="left")
        self.fps_var = tk.IntVar(value=DEFAULT_FPS)
        ttk.Spinbox(row2, from_=1, to=60, textvariable=self.fps_var, width=6).pack(side="left", padx=6)
        ttk.Label(row2, text="默认 10，越高越流畅、文件越大").pack(side="left")

        # 保存位置
        row3 = ttk.Frame(main)
        row3.pack(fill="x", pady=6)
        ttk.Label(row3, text="保存到：").pack(side="left")
        self.path_var = tk.StringVar()
        ttk.Entry(row3, textvariable=self.path_var, width=34).pack(side="left", padx=6)
        ttk.Button(row3, text="浏览…", command=self.choose_path).pack(side="left")

        # 控制按钮
        row4 = ttk.Frame(main)
        row4.pack(fill="x", pady=12)
        self.start_btn = ttk.Button(row4, text="开始录制", command=self.start_recording, width=12)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(row4, text="停止录制", command=self.stop_recording,
                                   width=12, state="disabled")
        self.stop_btn.pack(side="left", padx=8)

        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(main, textvariable=self.status_var, foreground="#555").pack(anchor="w", pady=(4, 0))

    def _default_path(self):
        desk = os.path.join(os.path.expanduser("~"), "Desktop")
        os.makedirs(desk, exist_ok=True)
        name = "录屏_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".mp4"
        return os.path.join(desk, name)

    def choose_path(self):
        initial = os.path.basename(self.path_var.get()) or "录屏.mp4"
        p = filedialog.asksaveasfilename(
            title="选择保存位置", defaultextension=".mp4", initialfile=initial,
            filetypes=[("MP4 视频", "*.mp4")])
        if p:
            self.path_var.set(p)

    # ---------------- 录制区域 ----------------
    def reset_area(self):
        with mss.MSS() as sct:
            m = sct.monitors[1]  # 主显示器
        self.area = (m["left"], m["top"], m["width"], m["height"])
        self.area_var.set(f"全屏 {m['width']} × {m['height']}")

    def select_area(self):
        """全屏遮罩 + 鼠标拖拽框选录制区域"""
        with mss.MSS() as sct:
            monitor = sct.monitors[1]
            shot = sct.grab(monitor)
        base_left, base_top = monitor["left"], monitor["top"]

        ok, buf = cv2.imencode(".png", np.array(shot)[:, :, :3])
        if not ok:
            return

        top = tk.Toplevel(self.root)
        top.attributes("-fullscreen", True)
        top.attributes("-topmost", True)
        canvas = tk.Canvas(top, highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)

        photo = tk.PhotoImage(data=buf.tobytes())
        canvas.create_image(0, 0, anchor="nw", image=photo)
        canvas.photo = photo  # 防止被垃圾回收

        rect_id = None
        origin = {}

        def on_press(e):
            nonlocal rect_id
            origin["x"], origin["y"] = e.x, e.y
            rect_id = canvas.create_rectangle(e.x, e.y, e.x, e.y,
                                              outline="red", width=2)

        def on_drag(e):
            canvas.coords(rect_id, origin["x"], origin["y"], e.x, e.y)

        def on_release(e):
            x0, y0 = min(origin["x"], e.x), min(origin["y"], e.y)
            x1, y1 = max(origin["x"], e.x), max(origin["y"], e.y)
            w, h = x1 - x0, y1 - y0
            top.destroy()
            if w < 8 or h < 8:
                self.status_var.set("所选区域太小，已忽略")
                return
            self.area = (base_left + x0, base_top + y0, w, h)
            self.area_var.set(f"区域 {w} × {h}")

        def on_esc(e):
            top.destroy()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        top.bind("<Escape>", on_esc)
        top.focus_force()
        self.status_var.set("在屏幕上拖拽框选录制区域，按 Esc 取消")

    # ---------------- 录制控制 ----------------
    def start_recording(self):
        if self.recording:
            return
        fps = self.fps_var.get()
        if not (1 <= fps <= 60):
            messagebox.showwarning("提示", "帧率需在 1~60 之间")
            return
        if not self.path_var.get().strip():
            self.path_var.set(self._default_path())

        self._fps = fps
        self._out_path = self.path_var.get().strip()
        self._record_area = self.area

        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._countdown = 3
        self._tick_countdown()

    def _tick_countdown(self):
        if self._countdown > 0:
            self.status_var.set(f"{self._countdown} 秒后开始录制…（请切换到要录制的画面）")
            self._countdown -= 1
            self.root.after(1000, self._tick_countdown)
            return
        self.stop_event = threading.Event()
        self.start_time = time.time()
        self.recording = True
        self.status_var.set("录制中 00:00")
        threading.Thread(target=self._worker, daemon=True).start()
        self._update_timer()

    def _worker(self):
        def _progress(msg):
            self.root.after(0, lambda: self.status_var.set(msg))
        result = record_to_file(self._record_area, self._fps, self._out_path,
                                stop_event=self.stop_event, on_progress=_progress)
        if result is None:
            self.root.after(0, self._finish_error)
        else:
            self._record_result = result
            self.root.after(0, self._finish_ok)

    def stop_recording(self):
        if not self.recording:
            return
        self.stop_event.set()
        self.stop_btn.config(state="disabled")
        self.status_var.set("正在保存并编码…")

    def _update_timer(self):
        if not self.recording:
            return
        secs = int(time.time() - self.start_time)
        self.status_var.set(f"录制中 {secs // 60:02d}:{secs % 60:02d}")
        self.root.after(1000, self._update_timer)

    def _finish_ok(self):
        self.recording = False
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        frames, actual_fps, dur = self._record_result
        size_mb = os.path.getsize(self._out_path) / 1024 / 1024 if os.path.exists(self._out_path) else 0
        self.status_var.set(f"已保存（{int(dur)} 秒，{size_mb:.1f} MB，{actual_fps:.1f} fps）")

    def _finish_error(self):
        self.recording = False
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_var.set("保存失败：请检查保存路径是否可写")
        messagebox.showerror("错误", "无法写入视频文件，请检查保存路径。")

    def _on_close(self):
        if self.recording:
            self.stop_event.set()
            time.sleep(0.5)  # 给录制线程一点时间写入尾帧
        self.root.destroy()


def selftest():
    """录制 2 秒并验证输出文件，用于确认程序可用"""
    with mss.MSS() as sct:
        m = sct.monitors[1]
    out = os.path.join(tempfile.gettempdir(), "screen_recorder_selftest.mp4")
    print(f"自测：录制主屏 {m['width']}x{m['height']}，目标 10fps，2 秒 …")
    result = record_to_file((m["left"], m["top"], m["width"], m["height"]),
                            10, out, duration=2)
    if result is None:
        print("自测失败：无法创建视频文件")
        sys.exit(1)
    frames, actual_fps, real_dur = result
    cap = cv2.VideoCapture(out)
    readable = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    v_fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    video_dur = readable / v_fps if v_fps > 0 else 0
    size_mb = os.path.getsize(out) / 1024 / 1024
    print(f"自测通过：写入 {frames} 帧，实际 {actual_fps:.1f} fps，"
          f"视频时长 {video_dur:.1f}s（真实 {real_dur:.1f}s），大小 {size_mb:.1f} MB")
    print(f"输出：{out}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    root = tk.Tk()
    ScreenRecorderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

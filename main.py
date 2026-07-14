"""pmx2fbx - one-click PMX to FBX converter for Unreal Engine 4.27.

Usage:
  python main.py                    # launch the GUI
  python main.py input.pmx          # convert input.pmx to input.fbx (same dir)
  python main.py input.pmx out.fbx  # convert to a specific output path
  python main.py input.pmx --scale 8.0

Drag-and-drop on Windows: drag a .pmx file onto run.bat to convert it.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import traceback

# Ensure we can import our modules regardless of CWD
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run_cli(args) -> int:
    """Command-line conversion. Returns process exit code."""
    from convert import convert_pmx_to_fbx
    from fbx_writer import ConversionOptions

    if not args.input:
        print("error: no input file specified", file=sys.stderr)
        return 1

    in_path = os.path.abspath(args.input)
    if not os.path.isfile(in_path):
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 1

    out_path = args.output
    if out_path is None:
        out_path = os.path.splitext(in_path)[0] + ".fbx"
    out_path = os.path.abspath(out_path)

    options = ConversionOptions(
        scale=args.scale,
        copy_textures=not args.no_copy_textures,
        emit_morphs=not args.no_morphs,
        emit_bind_pose=not args.no_bind_pose,
    )

    try:
        result = convert_pmx_to_fbx(in_path, out_path, options=options, log=print)
        print(f"\n✓ Conversion complete: {result}")
        return 0
    except Exception as e:
        print(f"\n✗ Conversion failed: {e}", file=sys.stderr)
        traceback.print_exc()
        return 2


def run_gui() -> int:
    """Launch the tkinter GUI. Returns process exit code."""
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError:
        print(
            "error: tkinter is not available. Install it via your Python "
            "distribution or use the command line: python main.py input.pmx",
            file=sys.stderr,
        )
        return 1

    from convert import convert_pmx_to_fbx
    from fbx_writer import ConversionOptions

    root = tk.Tk()
    root.title("PMX → FBX 转换工具 (UE4.27)")
    root.geometry("720x520")
    root.minsize(640, 440)

    # --- State ---
    state = {
        "input_path": tk.StringVar(),
        "output_path": tk.StringVar(),
        "scale": tk.DoubleVar(value=8.0),
        "copy_textures": tk.BooleanVar(value=True),
        "emit_morphs": tk.BooleanVar(value=True),
        "emit_bind_pose": tk.BooleanVar(value=True),
        "converting": False,
    }

    # --- Layout ---
    # Input file row
    in_frame = ttk.LabelFrame(root, text="输入 PMX 文件 / Input PMX File", padding=8)
    in_frame.pack(fill="x", padx=10, pady=(10, 4))

    ttk.Entry(in_frame, textvariable=state["input_path"]).pack(
        side="left", fill="x", expand=True, padx=(0, 6)
    )
    ttk.Button(
        in_frame,
        text="浏览…",
        command=lambda: _browse_input(state),
    ).pack(side="right")

    # Output file row
    out_frame = ttk.LabelFrame(root, text="输出 FBX 文件 / Output FBX File", padding=8)
    out_frame.pack(fill="x", padx=10, pady=4)

    ttk.Entry(out_frame, textvariable=state["output_path"]).pack(
        side="left", fill="x", expand=True, padx=(0, 6)
    )
    ttk.Button(
        out_frame,
        text="浏览…",
        command=lambda: _browse_output(state),
    ).pack(side="right")

    # Options row
    opt_frame = ttk.LabelFrame(root, text="选项 / Options", padding=8)
    opt_frame.pack(fill="x", padx=10, pady=4)

    ttk.Label(opt_frame, text="缩放系数 Scale:").grid(row=0, column=0, sticky="w", padx=(0, 4))
    ttk.Spinbox(
        opt_frame,
        from_=0.01,
        to=1000.0,
        increment=1.0,
        textvariable=state["scale"],
        width=10,
    ).grid(row=0, column=1, sticky="w")
    ttk.Label(
        opt_frame,
        text="(1 PMX 单位 = N cm；默认 8)",
    ).grid(row=0, column=2, sticky="w", padx=(8, 0))

    ttk.Checkbutton(
        opt_frame, text="复制贴图到 FBX 旁 / Copy textures", variable=state["copy_textures"]
    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
    ttk.Checkbutton(
        opt_frame,
        text="导出顶点变形为 Blend Shape / Export morphs",
        variable=state["emit_morphs"],
    ).grid(row=2, column=0, columnspan=3, sticky="w")
    ttk.Checkbutton(
        opt_frame,
        text="写入 BindPose / Write bind pose",
        variable=state["emit_bind_pose"],
    ).grid(row=3, column=0, columnspan=3, sticky="w")

    # Convert button
    btn_frame = ttk.Frame(root)
    btn_frame.pack(fill="x", padx=10, pady=4)
    convert_btn = ttk.Button(btn_frame, text="▶ 转换 / Convert", command=lambda: _start_convert(state, log_widget, convert_btn))
    convert_btn.pack(side="left")
    ttk.Label(
        btn_frame,
        text="(也可以把 .pmx 文件直接拖到 run.bat 上)",
        foreground="gray",
    ).pack(side="left", padx=(10, 0))

    # Log
    log_frame = ttk.LabelFrame(root, text="日志 / Log", padding=4)
    log_frame.pack(fill="both", expand=True, padx=10, pady=(4, 10))

    log_widget = tk.Text(log_frame, height=12, wrap="word", state="disabled")
    log_widget.pack(side="left", fill="both", expand=True)
    scrollbar = ttk.Scrollbar(log_frame, command=log_widget.yview)
    scrollbar.pack(side="right", fill="y")
    log_widget.config(yscrollcommand=scrollbar.set)

    # Tag for emphasis
    log_widget.tag_config("error", foreground="#c00")
    log_widget.tag_config("success", foreground="#060")

    # If a file was passed as argv (drag-drop on the .py or via run.bat),
    # pre-fill the input.
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        state["input_path"].set(os.path.abspath(sys.argv[1]))
        _auto_fill_output(state)

    root.mainloop()
    return 0


def _browse_input(state) -> None:
    from tkinter import filedialog
    path = filedialog.askopenfilename(
        title="选择 PMX 文件",
        filetypes=[("PMX files", "*.pmx"), ("PMD files", "*.pmd"), ("All files", "*.*")],
    )
    if path:
        state["input_path"].set(path)
        _auto_fill_output(state)


def _browse_output(state) -> None:
    from tkinter import filedialog
    path = filedialog.asksaveasfilename(
        title="保存 FBX 文件",
        defaultextension=".fbx",
        filetypes=[("FBX files", "*.fbx"), ("All files", "*.*")],
    )
    if path:
        state["output_path"].set(path)


def _auto_fill_output(state) -> None:
    in_path = state["input_path"].get()
    if in_path and not state["output_path"].get():
        state["output_path"].set(os.path.splitext(in_path)[0] + ".fbx")


def _log(log_widget, msg: str, tag: str = "") -> None:
    log_widget.config(state="normal")
    log_widget.insert("end", msg + "\n", tag if tag else ())
    log_widget.see("end")
    log_widget.config(state="disabled")
    log_widget.update_idletasks()


def _start_convert(state, log_widget, convert_btn) -> None:
    if state["converting"]:
        return
    in_path = state["input_path"].get().strip().strip('"')
    out_path = state["output_path"].get().strip().strip('"')
    if not in_path or not os.path.isfile(in_path):
        from tkinter import messagebox
        messagebox.showerror("错误", "请选择有效的输入 PMX 文件。")
        return
    if not out_path:
        out_path = os.path.splitext(in_path)[0] + ".fbx"
        state["output_path"].set(out_path)

    state["converting"] = True
    convert_btn.config(state="disabled")

    # Clear log
    log_widget.config(state="normal")
    log_widget.delete("1.0", "end")
    log_widget.config(state="disabled")

    thread = threading.Thread(
        target=_convert_worker,
        args=(state, log_widget, convert_btn, in_path, out_path),
        daemon=True,
    )
    thread.start()


def _convert_worker(state, log_widget, convert_btn, in_path, out_path) -> None:
    from convert import convert_pmx_to_fbx
    from fbx_writer import ConversionOptions

    options = ConversionOptions(
        scale=state["scale"].get(),
        copy_textures=state["copy_textures"].get(),
        emit_morphs=state["emit_morphs"].get(),
        emit_bind_pose=state["emit_bind_pose"].get(),
    )

    def log(msg):
        log_widget.after(0, lambda: _log(log_widget, msg))

    try:
        result = convert_pmx_to_fbx(in_path, out_path, options=options, log=log)
        log("")
        log(f"✓ 转换完成 / Conversion complete: {result}", "success")
    except Exception as e:
        log("")
        log(f"✗ 转换失败 / Conversion failed: {e}", "error")
        import traceback
        log(traceback.format_exc(), "error")
    finally:
        def _reset():
            state["converting"] = False
            convert_btn.config(state="normal")
        log_widget.after(0, _reset)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="PMX to FBX converter for Unreal Engine 4.27",
    )
    parser.add_argument("input", nargs="?", help="input .pmx file (omit to launch GUI)")
    parser.add_argument("output", nargs="?", help="output .fbx file (default: same as input)")
    parser.add_argument("--scale", type=float, default=8.0, help="scale factor (default: 8.0, 1 PMX unit = 8 cm)")
    parser.add_argument("--no-copy-textures", action="store_true", help="do not copy textures next to FBX")
    parser.add_argument("--no-morphs", action="store_true", help="skip vertex morph export")
    parser.add_argument("--no-bind-pose", action="store_true", help="skip bind pose export")
    parser.add_argument("--gui", action="store_true", help="force GUI even if input is given")
    args = parser.parse_args()

    if args.input is None or args.gui:
        return run_gui()
    return run_cli(args)


if __name__ == "__main__":
    sys.exit(main())

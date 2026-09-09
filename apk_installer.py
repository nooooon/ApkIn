#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
APK一括書き込みツール（Mac / Apple Silicon専用）
- Tkinterで作成したGUIアプリ
- 同じフォルダー内の ./adb バイナリを使って、複数のAndroid端末に同じAPKを一括インストールする
"""

import os
import sys
import subprocess
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# ドラッグ&ドロップは tkinterdnd2 がインストールされている場合のみ有効化する
# （入っていなくてもアプリ自体は正常に動作し、ファイル選択ダイアログだけが使える）
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False


# プログラムと同じフォルダーにある「adb」の絶対パスを求める
# ・通常のPythonスクリプトとして実行した場合: このファイルがあるフォルダー
# ・PyInstallerで.app化した場合: 実行ファイルと同じフォルダー（sys._MEIPASS）
#   ※ .app化のビルド時に --add-binary で adb を同じ階層に埋め込む前提
if getattr(sys, "frozen", False):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ADB_PATH = os.path.join(BASE_DIR, "adb")

# ログ欄に保持しておく最大行数（これを超えたら古い行から削除し、メモリ肥大化を防ぐ）
MAX_LOG_LINES = 1000

# アプリのバージョン表示（画面右下に表示する）
APP_VERSION = "1.0.0"


class ApkInstallerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("APK一括書き込みツール")
        self.root.geometry("720x600")

        # 選択されたAPKファイルのパスを保持する変数
        self.apk_path = tk.StringVar(value="（未選択）")

        # デバイスごとのチェック状態を保持する辞書 { シリアル番号: BooleanVar }
        self.device_vars = {}

        # ワーカースレッドからログを安全にGUIへ渡すためのキュー
        self.log_queue = queue.Queue()

        # インストール処理が同時に何台分走っているかを数えるカウンタ
        self.install_running_count = 0
        self.install_lock = threading.Lock()

        self._build_widgets()

        # 100ミリ秒ごとにログキューを確認して、テキストエリアに反映する
        self.root.after(100, self._process_log_queue)

        # 起動時にadbバイナリが存在するかチェックしておく
        self._check_adb_exists()

    # ------------------------------------------------------------------
    # 画面構築
    # ------------------------------------------------------------------
    def _build_widgets(self):
        # ===== 1. APKファイル選択エリア =====
        apk_frame = ttk.LabelFrame(self.root, text="1. APKファイルの選択")
        apk_frame.pack(fill="x", padx=10, pady=(10, 5))

        select_btn = ttk.Button(apk_frame, text="APKファイルを選択", command=self.select_apk_file)
        select_btn.pack(side="left", padx=10, pady=10)

        # 選択中のAPKパスを表示するラベル（ドラッグ&ドロップの受け皿も兼ねる）
        self.apk_label = ttk.Label(
            apk_frame,
            textvariable=self.apk_path,
            relief="sunken",
            anchor="w",
            padding=8,
        )
        self.apk_label.pack(side="left", fill="x", expand=True, padx=(0, 10), pady=10)

        if DND_AVAILABLE:
            # tkinterdnd2 が使える場合のみ、ラベルへのドラッグ&ドロップを受け付ける
            self.apk_label.drop_target_register(DND_FILES)
            self.apk_label.dnd_bind("<<Drop>>", self._on_drop_apk)
        else:
            # 使えない場合はその旨を分かるようにしておく
            self.apk_label.configure(
                text="（tkinterdnd2未導入のためD&D無効。ボタンから選択してください）"
            )

        # ===== 2. デバイス一覧エリア =====
        device_frame = ttk.LabelFrame(self.root, text="2. デバイス一覧")
        device_frame.pack(fill="both", expand=True, padx=10, pady=5)

        device_btn_frame = ttk.Frame(device_frame)
        device_btn_frame.pack(fill="x", padx=10, pady=(10, 5))

        refresh_btn = ttk.Button(device_btn_frame, text="デバイスリスト更新", command=self.refresh_devices)
        refresh_btn.pack(side="left")

        select_all_btn = ttk.Button(device_btn_frame, text="すべて選択", command=self.select_all_devices)
        select_all_btn.pack(side="left", padx=5)

        deselect_all_btn = ttk.Button(device_btn_frame, text="すべて解除", command=self.deselect_all_devices)
        deselect_all_btn.pack(side="left")

        # デバイスのチェックボックスを並べるためのスクロール可能エリア
        list_container = ttk.Frame(device_frame)
        list_container.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.device_canvas = tk.Canvas(list_container, height=150, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=self.device_canvas.yview)
        self.device_list_frame = ttk.Frame(self.device_canvas)

        self.device_list_frame.bind(
            "<Configure>",
            lambda e: self.device_canvas.configure(scrollregion=self.device_canvas.bbox("all")),
        )
        self.device_canvas.create_window((0, 0), window=self.device_list_frame, anchor="nw")
        self.device_canvas.configure(yscrollcommand=scrollbar.set)

        self.device_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # ===== 3. 書き込み実行エリア =====
        install_frame = ttk.LabelFrame(self.root, text="3. 書き込み（インストール）実行")
        install_frame.pack(fill="x", padx=10, pady=5)

        self.install_btn = ttk.Button(
            install_frame, text="選択したデバイスに書き込み", command=self.install_selected
        )
        self.install_btn.pack(padx=10, pady=10)

        # ===== バージョン表示（画面右下） =====
        version_label = ttk.Label(self.root, text=f"ver {APP_VERSION}", foreground="gray")
        version_label.pack(side="bottom", anchor="e", padx=10, pady=(0, 4))

        # ===== ログ表示エリア =====
        log_frame = ttk.LabelFrame(self.root, text="ログ")
        log_frame.pack(fill="both", expand=True, padx=10, pady=(5, 10))

        self.log_text = scrolledtext.ScrolledText(log_frame, height=12, state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=10, pady=10)

    # ------------------------------------------------------------------
    # adbバイナリの存在チェック
    # ------------------------------------------------------------------
    def _check_adb_exists(self):
        if not os.path.isfile(ADB_PATH):
            messagebox.showwarning(
                "adbが見つかりません",
                f"プログラムと同じフォルダーに adb バイナリを配置してください。\n\n想定パス: {ADB_PATH}",
            )
            return

        # 実行権限が付いていない場合は付与しておく（初回配置時によくあるケースへの対応）
        if not os.access(ADB_PATH, os.X_OK):
            try:
                os.chmod(ADB_PATH, 0o755)
            except OSError as e:
                self._log(f"[警告] adbへの実行権限付与に失敗しました: {e}")

    # ------------------------------------------------------------------
    # 1. APKファイル選択
    # ------------------------------------------------------------------
    def select_apk_file(self):
        path = filedialog.askopenfilename(
            title="APKファイルを選択してください",
            filetypes=[("APKファイル", "*.apk"), ("すべてのファイル", "*.*")],
        )
        if path:
            self._set_apk_path(path)

    def _on_drop_apk(self, event):
        # ドラッグ&ドロップされたデータには複数ファイルが含まれる場合や
        # パスが {} で囲まれる場合があるため、tkinterdnd2 の splitlist で分解する
        paths = self.root.tk.splitlist(event.data)
        if not paths:
            return
        path = paths[0]
        if not path.lower().endswith(".apk"):
            messagebox.showerror("エラー", "APKファイルをドロップしてください。")
            return
        self._set_apk_path(path)

    def _set_apk_path(self, path):
        self.apk_path.set(path)
        self.apk_label.configure(text=path)
        self._log(f"APKファイルを選択しました: {path}")

    # ------------------------------------------------------------------
    # 2. デバイス一覧表示と選択
    # ------------------------------------------------------------------
    def refresh_devices(self):
        # adb devices はネットワークやUSB列挙待ちで時間がかかることがあるため、
        # GUIが固まらないようバックグラウンドスレッドで実行する
        threading.Thread(target=self._refresh_devices_thread, daemon=True).start()

    def _refresh_devices_thread(self):
        self._log("デバイス一覧を取得しています...")
        try:
            result = subprocess.run(
                [ADB_PATH, "devices", "-l"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except FileNotFoundError:
            self._log("[エラー] adbバイナリが見つかりません。配置場所を確認してください。")
            return
        except subprocess.TimeoutExpired:
            self._log("[エラー] adb devices がタイムアウトしました。")
            return

        # 出力例（-l オプションで機種名などの詳細も付与される）:
        # List of devices attached
        # ABCD1234    device usb:... product:... model:SC_51D device:... transport_id:1
        # EFGH5678    unauthorized
        devices = []  # [(シリアル番号, 機種名), ...]
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices"):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                serial = parts[0]
                # "model:xxx" のトークンから機種名を取り出す（見つからない場合は空文字）
                model = ""
                for token in parts[2:]:
                    if token.startswith("model:"):
                        model = token[len("model:"):]
                        break
                devices.append((serial, model))
            elif len(parts) >= 2:
                # unauthorized（未許可）や offline の端末はログに情報だけ出しておく
                self._log(f"[注意] {parts[0]} は状態 '{parts[1]}' のため一覧から除外しました。")

        # GUIの更新は必ずメインスレッド（Tkinterのイベントループ）から行う
        self.root.after(0, self._update_device_list, devices)

    def _update_device_list(self, devices):
        # 一旦既存のチェックボックスを全部消してから作り直す
        for widget in self.device_list_frame.winfo_children():
            widget.destroy()
        self.device_vars.clear()

        if not devices:
            ttk.Label(self.device_list_frame, text="接続されているデバイスがありません。").pack(anchor="w")
            self._log("接続中のデバイスは見つかりませんでした。")
            return

        for serial, model in devices:
            # 書き込み対象の判定はシリアル番号で行うが、表示上は機種名も併記する
            label = f"{serial} ({model})" if model else serial
            var = tk.BooleanVar(value=False)
            self.device_vars[serial] = var
            ttk.Checkbutton(self.device_list_frame, text=label, variable=var).pack(anchor="w", padx=5, pady=2)

        self._log(f"{len(devices)} 台のデバイスを検出しました。")

    def select_all_devices(self):
        for var in self.device_vars.values():
            var.set(True)

    def deselect_all_devices(self):
        for var in self.device_vars.values():
            var.set(False)

    # ------------------------------------------------------------------
    # 3. 書き込み（インストール）実行
    # ------------------------------------------------------------------
    def install_selected(self):
        apk_path = self.apk_path.get()
        if not apk_path or not os.path.isfile(apk_path):
            messagebox.showerror("エラー", "有効なAPKファイルを選択してください。")
            return

        target_serials = [serial for serial, var in self.device_vars.items() if var.get()]
        if not target_serials:
            messagebox.showerror("エラー", "書き込み先のデバイスを1台以上選択してください。")
            return

        if not os.path.isfile(ADB_PATH):
            messagebox.showerror("エラー", f"adbバイナリが見つかりません。\n想定パス: {ADB_PATH}")
            return

        # 実行中は誤って連打されないようボタンを無効化する
        self.install_btn.configure(state="disabled")
        self.install_running_count = len(target_serials)

        self._log(f"===== 書き込み開始（対象 {len(target_serials)} 台） =====")

        # 各デバイスへのインストールを、それぞれ別スレッドで並列に実行する
        for serial in target_serials:
            threading.Thread(
                target=self._install_on_device,
                args=(serial, apk_path),
                daemon=True,
            ).start()

    def _install_on_device(self, serial, apk_path):
        self._log(f"[{serial}] インストール開始...")
        try:
            result = subprocess.run(
                [ADB_PATH, "-s", serial, "install", "-r", apk_path],
                capture_output=True,
                text=True,
                timeout=180,
            )
            output = (result.stdout or "") + (result.stderr or "")

            if result.returncode == 0 and "Success" in output:
                self._log(f"[{serial}] Success")
            else:
                self._log(f"[{serial}] Failed\n{output.strip()}")

        except subprocess.TimeoutExpired:
            self._log(f"[{serial}] Failed（タイムアウトしました）")
        except Exception as e:
            self._log(f"[{serial}] Failed（例外発生: {e}）")
        finally:
            # 全デバイスの処理が完了したらボタンを再度押せるようにする
            with self.install_lock:
                self.install_running_count -= 1
                if self.install_running_count <= 0:
                    self.root.after(0, lambda: self.install_btn.configure(state="normal"))
                    self._log("===== 書き込み処理がすべて完了しました =====")

    # ------------------------------------------------------------------
    # ログ関連（ワーカースレッド → キュー → メインスレッドのテキストエリア）
    # ------------------------------------------------------------------
    def _log(self, message):
        # ワーカースレッドから直接Tkinterウィジェットを触るとクラッシュの原因になるため、
        # 一旦キューに入れて、メインスレッド側のafter処理で反映する
        self.log_queue.put(message)

    def _process_log_queue(self):
        # 溜まっているログをまず全部取り出してから、まとめて1回だけウィジェットに反映する
        # （1行ごとに有効化/無効化を繰り返すと、大量のログが一気に届いたときに無駄が多いため）
        messages = []
        while not self.log_queue.empty():
            messages.append(self.log_queue.get_nowait())

        if messages:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", "\n".join(messages) + "\n")

            # 行数が上限を超えていたら、古い行をまとめて削除してメモリ肥大化を防ぐ
            line_count = int(self.log_text.index("end-1c").split(".")[0])
            if line_count > MAX_LOG_LINES:
                self.log_text.delete("1.0", f"{line_count - MAX_LOG_LINES + 1}.0")

            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        # 100ミリ秒後にもう一度自分自身を呼び出して、キューの監視を続ける
        self.root.after(100, self._process_log_queue)


def main():
    # tkinterdnd2 が使える場合はドラッグ&ドロップ対応のTkルートを、
    # 使えない場合は通常のTkルートを作成する
    if DND_AVAILABLE:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()

    app = ApkInstallerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

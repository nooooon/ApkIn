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
import zipfile
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

        # 実行中のadbインストールプロセス（アプリ終了時に確実に止めるために保持する）
        self.running_procs = set()
        self.procs_lock = threading.Lock()

        # 画面操作がロックされているかどうか（ドラッグ&ドロップの受付可否にも使う）
        self.controls_enabled = True

        self._build_widgets()

        # 100ミリ秒ごとにログキューを確認して、テキストエリアに反映する
        self.root.after(100, self._process_log_queue)

        # 起動時にadbバイナリが存在するかチェックしておく
        self._check_adb_exists()

        # 書き込み中にウィンドウを閉じようとした場合は確認ダイアログを出す
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # 画面構築
    # ------------------------------------------------------------------
    def _build_widgets(self):
        # ===== 1. APKファイル選択エリア =====
        apk_frame = ttk.LabelFrame(self.root, text="1. APKファイルの選択")
        apk_frame.pack(fill="x", padx=10, pady=(10, 5))

        self.select_apk_btn = ttk.Button(apk_frame, text="APKファイルを選択", command=self.select_apk_file)
        self.select_apk_btn.pack(side="left", padx=10, pady=10)

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
            self.apk_path.set("（tkinterdnd2未導入のためD&D無効。ボタンから選択してください）")

        # ===== 2. デバイス一覧エリア =====
        device_frame = ttk.LabelFrame(self.root, text="2. デバイス一覧")
        device_frame.pack(fill="both", expand=True, padx=10, pady=5)

        device_btn_frame = ttk.Frame(device_frame)
        device_btn_frame.pack(fill="x", padx=10, pady=(10, 5))

        self.refresh_btn = ttk.Button(device_btn_frame, text="デバイスリスト更新", command=self.refresh_devices)
        self.refresh_btn.pack(side="left")

        self.select_all_btn = ttk.Button(device_btn_frame, text="すべて選択", command=self.select_all_devices)
        self.select_all_btn.pack(side="left", padx=5)

        self.deselect_all_btn = ttk.Button(device_btn_frame, text="すべて解除", command=self.deselect_all_devices)
        self.deselect_all_btn.pack(side="left")

        # 書き込み中はデバイスのチェックボックスも操作できないようにするため、参照を保持しておく
        # （シリアル番号 → ウィジェット。書き込み結果アイコンの反映にも使う）
        self.device_checkbuttons = {}
        self.device_base_labels = {}

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
        # 書き込み中・更新中は他の操作と同様にドロップも無視する
        if not self.controls_enabled:
            return
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

    def _is_valid_apk(self, path):
        # APKファイルの実体はZIPアーカイブで、中に AndroidManifest.xml を含む。
        # 拡張子が .apk でも中身が壊れている／別ファイルというケースを弾くための簡易チェック
        # （aaptなどを使った厳密な検証ではないが、明らかに違うファイルは検出できる）
        try:
            if not zipfile.is_zipfile(path):
                return False
            with zipfile.ZipFile(path) as zf:
                return "AndroidManifest.xml" in zf.namelist()
        except (OSError, zipfile.BadZipFile):
            return False

    def _set_apk_path(self, path):
        if not self._is_valid_apk(path):
            messagebox.showerror(
                "エラー",
                "選択されたファイルは有効なAPKファイルではないようです。\n中身が壊れているか、APK以外のファイルの可能性があります。",
            )
            self._log(f"[エラー] 無効なAPKファイルが選択されました: {path}")
            return

        self.apk_path.set(path)
        self._log(f"APKファイルを選択しました: {path}")

    # ------------------------------------------------------------------
    # 2. デバイス一覧表示と選択
    # ------------------------------------------------------------------
    def refresh_devices(self):
        # 更新中に連打されたり、書き込みが始まったりすると表示と状態が食い違うため、
        # 完了するまで画面上の操作をロックする（書き込み完了時と同じ仕組みを流用）
        self._set_controls_enabled(False)
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
            self.root.after(0, lambda: self._set_controls_enabled(True))
            return
        except subprocess.TimeoutExpired:
            self._log("[エラー] adb devices がタイムアウトしました。")
            self.root.after(0, lambda: self._set_controls_enabled(True))
            return
        except Exception as e:
            # 実行権限なし(PermissionError)など想定外のエラーでも、画面が操作不能にならないようにする
            self._log(f"[エラー] adb devices の実行に失敗しました: {e}")
            self.root.after(0, lambda: self._set_controls_enabled(True))
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
                serial, status = parts[0], parts[1]
                self._log(f"[注意] {serial} は状態 '{status}' のため一覧から除外しました。")
                if status == "unauthorized":
                    # 非エンジニアでも対処できるよう、具体的な操作方法を添えておく
                    self._log(
                        f"　→ {serial} の端末画面に表示されている"
                        "「USBデバッグを許可しますか？」のダイアログで「許可」をタップしてから、"
                        "もう一度「デバイスリスト更新」を押してください。"
                    )

        # GUIの更新は必ずメインスレッド（Tkinterのイベントループ）から行う
        self.root.after(0, self._update_device_list, devices)

    def _update_device_list(self, devices):
        # 更新前のチェック状態を覚えておき、再検出後も同じ端末なら選択状態を引き継ぐ
        previous_checked = {
            serial for serial, var in self.device_vars.items() if var.get()
        }

        # 一旦既存のチェックボックスを全部消してから作り直す
        for widget in self.device_list_frame.winfo_children():
            widget.destroy()
        self.device_vars.clear()
        # シリアル番号 → チェックボックスウィジェット（書き込み結果アイコンの反映に使う）
        self.device_checkbuttons = {}
        # シリアル番号 → アイコンを付ける前の元ラベル文字列
        self.device_base_labels = {}

        if not devices:
            ttk.Label(self.device_list_frame, text="接続されているデバイスがありません。").pack(anchor="w")
            self._log("接続中のデバイスは見つかりませんでした。")
        else:
            for serial, model in devices:
                # 書き込み対象の判定はシリアル番号で行うが、表示上は機種名も併記する
                label = f"{serial} ({model})" if model else serial
                var = tk.BooleanVar(value=(serial in previous_checked))
                self.device_vars[serial] = var
                checkbutton = ttk.Checkbutton(self.device_list_frame, text=label, variable=var)
                checkbutton.pack(anchor="w", padx=5, pady=2)
                self.device_checkbuttons[serial] = checkbutton
                self.device_base_labels[serial] = label

            self._log(f"{len(devices)} 台のデバイスを検出しました。")

        # 更新が終わったので操作ロックを解除する
        self._set_controls_enabled(True)

    def select_all_devices(self):
        for var in self.device_vars.values():
            var.set(True)

    def deselect_all_devices(self):
        for var in self.device_vars.values():
            var.set(False)

    def _set_controls_enabled(self, enabled):
        # 書き込み中は誤操作を防ぐため、APK選択・デバイス一覧関連・書き込みボタンをまとめて無効化する
        self.controls_enabled = enabled
        state = "normal" if enabled else "disabled"
        self.select_apk_btn.configure(state=state)
        self.refresh_btn.configure(state=state)
        self.select_all_btn.configure(state=state)
        self.deselect_all_btn.configure(state=state)
        self.install_btn.configure(state=state)
        for checkbutton in self.device_checkbuttons.values():
            checkbutton.configure(state=state)

    def _set_device_status_icon(self, serial, icon):
        # 書き込み結果に応じたアイコンを、該当デバイスのチェックボックスのラベルに付与する
        checkbutton = self.device_checkbuttons.get(serial)
        base_label = self.device_base_labels.get(serial)
        if checkbutton is None or base_label is None:
            # 書き込み中にリストが更新されて対象デバイスが消えていた場合は何もしない
            return
        checkbutton.configure(text=f"{base_label} {icon}" if icon else base_label)

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

        # 書き込み中に他の操作をされると状態が食い違ってしまうため、
        # 完了するまで画面上の操作をすべてロックする
        self._set_controls_enabled(False)
        self.install_running_count = len(target_serials)

        self._log(f"===== 書き込み開始（対象 {len(target_serials)} 台） =====")

        # 前回の書き込み結果アイコンが残っていると紛らわしいので、対象デバイスのぶんだけ消しておく
        for serial in target_serials:
            self._set_device_status_icon(serial, "")

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
            proc = subprocess.Popen(
                [ADB_PATH, "-s", serial, "install", "-r", apk_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            with self.procs_lock:
                self.running_procs.add(proc)
            try:
                stdout, stderr = proc.communicate(timeout=180)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise
            finally:
                with self.procs_lock:
                    self.running_procs.discard(proc)
            output = (stdout or "") + (stderr or "")

            if proc.returncode == 0 and "Success" in output:
                self._log(f"[{serial}] Success")
                self.root.after(0, lambda: self._set_device_status_icon(serial, "✅"))
            else:
                self._log(f"[{serial}] Failed\n{output.strip()}")
                self.root.after(0, lambda: self._set_device_status_icon(serial, "❌"))

        except subprocess.TimeoutExpired:
            self._log(f"[{serial}] Failed（タイムアウトしました）")
            self.root.after(0, lambda: self._set_device_status_icon(serial, "❌"))
        except Exception as e:
            self._log(f"[{serial}] Failed（例外発生: {e}）")
            self.root.after(0, lambda: self._set_device_status_icon(serial, "❌"))
        finally:
            # 全デバイスの処理が完了したらボタンを再度押せるようにする
            with self.install_lock:
                self.install_running_count -= 1
                if self.install_running_count <= 0:
                    self.root.after(0, lambda: self._set_controls_enabled(True))
                    self._log("===== 書き込み処理がすべて完了しました =====")

    def _on_close(self):
        # 書き込み処理が実行中の場合は、誤って中断してしまわないよう確認する
        if self.install_running_count > 0:
            if not messagebox.askyesno(
                "確認",
                "書き込み処理が実行中です。終了すると処理が中断されますが、よろしいですか？",
            ):
                return
            # ウィンドウを閉じてもadbの子プロセスが裏で動き続けないよう、確実に止める
            with self.procs_lock:
                for proc in list(self.running_procs):
                    proc.terminate()
        self.root.destroy()

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

#!/bin/bash
# =========================================================================
# APK一括書き込みツール .app ビルドスクリプト（Apple Silicon Mac専用）
#
# 使い方:
#   1. このプロジェクトフォルダーに Mac用 adb バイナリ（arm64版）を配置する
#      （ファイル名は "adb" のまま）
#   2. ターミナルで以下を実行する
#        chmod +x build_app.sh
#        ./build_app.sh
#   3. 成功すると dist/APK一括書き込みツール.app が生成される
# =========================================================================

set -e  # 途中でエラーが出たら即座に停止する

cd "$(dirname "$0")"

APP_NAME="ApkIn"
VENV_DIR=".venv"
PY="$VENV_DIR/bin/python"

echo "== 1. adbバイナリの存在チェック =="
if [ ! -f "./adb" ]; then
    echo "エラー: このフォルダーに adb バイナリが見つかりません。"
    echo "        Mac(arm64)用の adb を ./adb として配置してから再実行してください。"
    exit 1
fi
chmod +x ./adb

echo "== 2. adbがarm64バイナリかチェック =="
if ! file ./adb | grep -q "arm64"; then
    echo "警告: adb が arm64 (Apple Silicon) 用でない可能性があります。"
    echo "      file ./adb の結果: $(file ./adb)"
fi

echo "== 3. ビルド用venvの確認 =="
if [ ! -x "$PY" ]; then
    echo "エラー: ビルド用venv ($VENV_DIR) が見つかりません。"
    echo "        以下のコマンドで作成してください:"
    echo "        /opt/homebrew/bin/python3.13 -m venv .venv"
    echo "        ./.venv/bin/python -m pip install pyinstaller tkinterdnd2"
    exit 1
fi

echo "== 4. 古いビルド成果物の削除 =="
rm -rf build dist "${APP_NAME}.spec"

echo "== 5. PyInstallerでビルド =="
"$PY" -m PyInstaller \
    --windowed \
    --name "$APP_NAME" \
    --add-binary "./adb:." \
    --collect-all tkinterdnd2 \
    --noconfirm \
    apk_installer.py

echo "== 6. Ad-hoc署名（社内配布・Gatekeeper対策）=="
# 正式なApple Developer証明書がなくても、ad-hoc署名（-s -）をしておくことで
# 「壊れているため開けません」といったGatekeeperのエラーを避けやすくなる
codesign --force --deep --sign - "dist/${APP_NAME}.app"

echo ""
echo "===================================================="
echo " ビルド完了: dist/${APP_NAME}.app"
echo "===================================================="
echo ""
echo "【社内配布時の注意】"
echo "・AirDropやZIP圧縮してSlack/メール等で配布すると、"
echo "  macOSが「隔離属性(quarantine)」を付与し、初回起動時に警告が出ます。"
echo "・受け取った人は Finder で右クリック(またはControl+クリック) → 「開く」で"
echo "  1度だけ許可すれば、以降は通常通り起動できます。"
echo "・もし配布時に隔離属性を外したい場合は、受け取った側で以下を実行してもらってください:"
echo "    xattr -cr \"${APP_NAME}.app\""

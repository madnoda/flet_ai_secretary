#!/bin/bash
set -eu

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BUILD_TMP="$(mktemp -d)"
OUTPUT_DIR="$PROJECT_DIR/build"

trap 'rm -rf "$BUILD_TMP"' EXIT

# config.toml の [character] からディレクトリと画像名を取得する。
readarray -t CHARACTER_CFG < <(python3 - "$PROJECT_DIR/config.toml" <<'PYCFG'
import sys, tomllib
from pathlib import Path
p = Path(sys.argv[1])
with p.open("rb") as f:
    c = tomllib.load(f).get("character", {})
print(str(c.get("directory", "")))
print(str(c.get("normal_image", "secretary_normal.png")))
print(str(c.get("talking_image", "secretary_mouth_open.png")))
print(str(c.get("happy_image", "secretary_happy.png")))
print(str(c.get("warning_image", "secretary_warning.png")))
print(str(c.get("icon_image", "icon.png")))
PYCFG
)

CHARACTER_DIR="${CHARACTER_CFG[0]}"
NORMAL_IMAGE="${CHARACTER_CFG[1]}"
TALKING_IMAGE="${CHARACTER_CFG[2]}"
HAPPY_IMAGE="${CHARACTER_CFG[3]}"
WARNING_IMAGE="${CHARACTER_CFG[4]}"
ICON_IMAGE="${CHARACTER_CFG[5]}"

if [ -n "$CHARACTER_DIR" ]; then
    CHARACTER_PATH="$PROJECT_DIR/$CHARACTER_DIR"
    for image in "$NORMAL_IMAGE" "$TALKING_IMAGE" "$HAPPY_IMAGE" "$WARNING_IMAGE" "$ICON_IMAGE"; do
        if [ ! -f "$CHARACTER_PATH/$image" ]; then
            echo "エラー: 次のキャラクターファイルがありません"
            echo "$CHARACTER_PATH/$image"
            exit 1
        fi
    done
fi

echo "プロジェクト: $PROJECT_DIR"
echo "一時ビルド領域: $BUILD_TMP"

cp -a "$PROJECT_DIR/." "$BUILD_TMP/"
cd "$BUILD_TMP"

# APK用ネットワーク設定・キャラクター画像名を生成。
python3 generate_client_config.py

# オリジナルキャラクターをAPKのassetsへ取り込む。
if [ -n "$CHARACTER_DIR" ]; then
    mkdir -p src/assets
    cp "$CHARACTER_DIR/$NORMAL_IMAGE" src/assets/
    cp "$CHARACTER_DIR/$TALKING_IMAGE" src/assets/
    cp "$CHARACTER_DIR/$HAPPY_IMAGE" src/assets/
    cp "$CHARACTER_DIR/$WARNING_IMAGE" src/assets/
    cp "$CHARACTER_DIR/$ICON_IMAGE" src/assets/icon.png
fi

flet build apk

mkdir -p "$OUTPUT_DIR/apk"
cp -a build/apk/. "$OUTPUT_DIR/apk/"

echo "完成: $OUTPUT_DIR/apk/"

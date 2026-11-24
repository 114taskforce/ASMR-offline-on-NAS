#!/usr/bin/env bash
# 字幕生成依赖安装与自检（N100 等纯 CPU 环境）
# 用法： bash deploy/subtitle_setup.sh
set -u

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"

echo "== 1. Python 依赖（wheel，无需编译） =="
"$PY" -m pip install --user --upgrade faster-whisper pypdf python-docx

echo "== 3. 外部工具检查 =="
for bin in ffmpeg ffprobe fpcalc pdftotext; do
  if command -v "$bin" >/dev/null 2>&1; then
    echo "  [ok] $bin"
  else
    echo "  [缺失] $bin"
  fi
done
if ! command -v fpcalc >/dev/null 2>&1; then
  echo "  提示：指纹去重需要 fpcalc，可执行： sudo apt install -y libchromaprint-tools"
fi

echo "== 4. Python 模块自检 =="
"$PY" - <<'PY'
mods = ["faster_whisper", "pypdf", "docx"]
for m in mods:
    try:
        __import__(m)
        print(f"  [ok] {m}")
    except Exception as e:
        print(f"  [缺失] {m}: {e}")
PY

echo "== 5. 模型目录 =="
MODEL_ROOT="${ASMR_MODEL_DIR:-$APP_DIR/data/models}"
mkdir -p "$MODEL_ROOT"
echo "  模型根目录：$MODEL_ROOT"
echo "  - ASR：模型放 $MODEL_ROOT（vad_asmr_encdec.onnx 与 Haas-ja-v1.0/）"
echo "  提示：NAS 无法访问 HuggingFace 时，在 PC 下载后通过 SMB 放入上述目录。"

echo "== 完成 =="

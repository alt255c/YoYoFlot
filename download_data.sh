#!/usr/bin/env bash
# download_data.sh
# Скрипт для загрузки всех файлов из публичной папки Google Drive в ./data

set -euo pipefail

# URL папки Google Drive
FOLDER_URL="https://drive.google.com/drive/folders/19bCT5pKF-QnfW05FW0Eb2dUsMrrnbUSD?usp=sharing"

# Целевая директория (рядом со скриптом)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${SCRIPT_DIR}/data"

# Создаём папку data, если её нет
if [ ! -d "$TARGET_DIR" ]; then
    echo "📁 Создаю директорию $TARGET_DIR..."
    mkdir -p "$TARGET_DIR"
fi

# Проверяем наличие Python
echo "🔍 Проверяю наличие Python..."
if ! command -v python3 &>/dev/null && ! command -v python &>/dev/null; then
    echo "❌ Python не найден. Установите Python 3.10+ и добавьте его в PATH."
    exit 1
fi

# Определяем команду python (python3 или python)
if command -v python3 &>/dev/null; then
    PYTHON=python3
else
    PYTHON=python
fi

echo "✅ Найден: $($PYTHON --version)"

# Проверяем, установлен ли gdown
echo "🔍 Проверяю наличие gdown..."
if $PYTHON -m gdown --version &>/dev/null; then
    echo "✅ gdown уже установлен."
else
    echo "📦 gdown не найден. Устанавливаю..."
    $PYTHON -m venv venv
    source venv/bin/activate
    pip install --upgrade gdown
    if [ $? -ne 0 ]; then
        echo "❌ Не удалось установить gdown. Проверьте подключение к интернету и права доступа."
        exit 1
    fi
    echo "✅ gdown успешно установлен."
fi

# Загружаем папку
echo "⬇️  Начинаю загрузку файлов из Google Drive в $TARGET_DIR ..."
$PYTHON -m gdown --folder "$FOLDER_URL" -O "$TARGET_DIR"

if [ $? -eq 0 ]; then
    echo "✅ Загрузка завершена успешно! Все файлы находятся в $TARGET_DIR"
else
    echo "❌ Во время загрузки произошла ошибка."
    exit 1
fi

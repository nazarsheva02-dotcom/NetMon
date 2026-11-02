#!/usr/bin/env bash
# Установка NetMon: ставит tk, копирует программу и добавляет ярлык в меню.
set -e

if ! command -v python3 >/dev/null 2>&1; then
    echo "Нужен python3, установи его и запусти скрипт снова." >&2
    exit 1
fi

# tkinter нужен для окна
if ! python3 -c "import tkinter" >/dev/null 2>&1; then
    echo "Ставлю tkinter (понадобится пароль sudo)..."
    if command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --needed tk
    elif command -v apt >/dev/null 2>&1; then
        sudo apt install -y python3-tk
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y python3-tkinter
    else
        echo "Не знаю твой менеджер пакетов, поставь tkinter для python3 вручную." >&2
        exit 1
    fi
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
mkdir -p "$BIN" "$APPS"

cp "$HERE/netmon.py" "$BIN/netmon.py"
chmod +x "$BIN/netmon.py"

printf '[Desktop Entry]\nType=Application\nName=NetMon\nComment=Ping and jitter monitor\nExec=python3 %s/netmon.py\nIcon=network-wireless\nTerminal=false\nCategories=Network;Utility;\n' "$BIN" > "$APPS/netmon.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" || true

echo "Готово. Запуск: найди NetMon в меню приложений или выполни: python3 $BIN/netmon.py"

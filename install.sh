#!/usr/bin/env bash
# Установка NetMon: ставит tk, копирует программу и добавляет ярлык в меню.
set -e

if ! command -v python3 >/dev/null 2>&1; then
    echo "Нужен python3, установи его и запусти скрипт снова." >&2
    exit 1
fi

# tkinter нужен только для окна; терминальный режим работает и без него.
if ! python3 -c "import tkinter" >/dev/null 2>&1; then
    echo "Ставлю tkinter для графического окна (понадобится пароль sudo)..."
    if command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --needed tk || echo "tkinter не установлен — окно не запустится, но netmon --cli работает."
    elif command -v apt >/dev/null 2>&1; then
        sudo apt install -y python3-tk || echo "tkinter не установлен — окно не запустится, но netmon --cli работает."
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y python3-tkinter || echo "tkinter не установлен — окно не запустится, но netmon --cli работает."
    else
        echo "Не знаю твой менеджер пакетов. Терминальный режим будет работать и так." >&2
    fi
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
mkdir -p "$BIN" "$APPS"

# Основная команда: netmon
install -m 755 "$HERE/netmon.py" "$BIN/netmon"
# Совместимость со старым запуском: python3 ~/.local/bin/netmon.py
ln -sf "$BIN/netmon" "$BIN/netmon.py"

printf '[Desktop Entry]\nType=Application\nName=NetMon\nComment=Монитор пинга, джиттера и потерь пакетов\nExec=%s/netmon\nTryExec=%s/netmon\nIcon=network-wireless\nTerminal=false\nCategories=Network;Utility;\nKeywords=ping;network;latency;jitter;\n' "$BIN" "$BIN" > "$APPS/netmon.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" || true

# Добавляем ~/.local/bin в PATH, чтобы команда netmon находилась сразу.
add_path() {
    rc="$1"
    [ -f "$rc" ] || return 0
    if ! grep -q 'HOME/.local/bin' "$rc" 2>/dev/null; then
        printf '\n# NetMon: локальные программы\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$rc"
        echo "PATH: добавлен ~/.local/bin в $rc"
    fi
}
add_path "$HOME/.bashrc"
[ -f "$HOME/.zshrc" ] && add_path "$HOME/.zshrc"
[ -f "$HOME/.profile" ] && add_path "$HOME/.profile"

echo "Готово."
echo "  Окно:        netmon"
echo "  Терминал:    netmon --cli"
echo "  Один замер:  netmon --once"
echo "  Ярлык:       NetMon в меню приложений"
echo

# Самопроверка.
[ -x "$BIN/netmon" ] && echo "Проверка: файл $BIN/netmon установлен." || echo "ОШИБКА: $BIN/netmon не найден."
if python3 -c "import tkinter" >/dev/null 2>&1; then
    echo "Проверка: tkinter есть, окно должно запуститься."
else
    echo "Проверка: tkinter нет, окно не запустится; работает 'netmon --cli'."
fi
if "$BIN/netmon" --version >/dev/null 2>&1; then
    echo "Проверка: программа запускается ($("$BIN/netmon" --version 2>/dev/null | head -1))."
else
    echo "ОШИБКА: программа не запускается. Запусти: python3 $BIN/netmon --version"
fi

echo
case ":$PATH:" in
    *":$BIN:"*) echo "Команда netmon доступна в этом терминале." ;;
    *)
        echo "Чтобы команда netmon заработала, открой новый терминал или выполни:"
        echo "  source ~/.bashrc"
        echo "  # либо разово:"
        echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac

#!/usr/bin/env bash
# Установка NetMon: ставит tkinter при необходимости, копирует программу,
# прописывает ~/.local/bin в PATH и сразу проверяет запуск.
#
# Из папки проекта:   bash install.sh
# Без клонирования:   curl -fsSL https://raw.githubusercontent.com/nazarsheva02-dotcom/NetMon/main/install.sh | bash
set -euo pipefail

REPO_URL="https://github.com/nazarsheva02-dotcom/NetMon.git"
RAW_BASE="https://raw.githubusercontent.com/nazarsheva02-dotcom/NetMon/main"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
TMP_DIR=""
SRC=""

cleanup() { [ -n "$TMP_DIR" ] && rm -rf "$TMP_DIR"; }
trap cleanup EXIT

say() { printf '%s\n' "$*"; }
warn() { printf '! %s\n' "$*" >&2; }

# --- 1. Ищем или скачиваем исходник ----------------------------------------
find_source() {
    local here=""
    if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
        here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    fi
    if [ -n "$here" ] && [ -f "$here/netmon.py" ]; then
        SRC="$here/netmon.py"
        return 0
    fi

    say "Скачиваю NetMon..."
    TMP_DIR="$(mktemp -d)"
    if command -v git >/dev/null 2>&1 && git clone --depth 1 "$REPO_URL" "$TMP_DIR/NetMon" >/dev/null 2>&1; then
        SRC="$TMP_DIR/NetMon/netmon.py"
        return 0
    fi
    if command -v curl >/dev/null 2>&1 && curl -fsSL "$RAW_BASE/netmon.py" -o "$TMP_DIR/netmon.py"; then
        SRC="$TMP_DIR/netmon.py"
        return 0
    fi
    warn "Не удалось скачать netmon.py. Проверь интернет и попробуй снова."
    exit 1
}

# --- 2. Проверяем python3 ---------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    warn "Нужен python3, установи его и запусти скрипт снова."
    exit 1
fi

find_source

# --- 3. tkinter нужен только для окна --------------------------------------
run_sudo() {
    if sudo -n true 2>/dev/null; then
        sudo "$@"
    elif [ -r /dev/tty ]; then
        sudo "$@" </dev/tty
    else
        return 1
    fi
}

install_tk() {
    if command -v pacman >/dev/null 2>&1; then
        run_sudo pacman -S --needed --noconfirm tk
    elif command -v apt >/dev/null 2>&1; then
        run_sudo apt install -y python3-tk
    elif command -v dnf >/dev/null 2>&1; then
        run_sudo dnf install -y python3-tkinter
    else
        return 1
    fi
}

if ! python3 -c "import tkinter" >/dev/null 2>&1; then
    say "tkinter не найден — ставлю (может понадобиться пароль sudo)..."
    if ! install_tk; then
        warn "tkinter не установлен — окно не запустится, но 'netmon --cli' работает."
    fi
fi

# --- 4. Устанавливаем файлы -------------------------------------------------
mkdir -p "$BIN" "$APPS"
install -m 755 "$SRC" "$BIN/netmon"
# Совместимость со старым запуском: python3 ~/.local/bin/netmon.py
ln -sf "$BIN/netmon" "$BIN/netmon.py"

printf '[Desktop Entry]\nType=Application\nName=NetMon\nComment=Монитор пинга, джиттера и потерь пакетов\nExec=%s/netmon\nTryExec=%s/netmon\nIcon=network-wireless\nTerminal=false\nCategories=Network;Utility;\nKeywords=ping;network;latency;jitter;\n' "$BIN" "$BIN" > "$APPS/netmon.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" || true

# --- 5. Прописываем PATH ----------------------------------------------------
add_path_rc() {
    local rc="$1" line="$2"
    [ -f "$rc" ] || return 0
    grep -qF "$line" "$rc" 2>/dev/null && return 0
    printf '\n# NetMon: локальные программы\n%s\n' "$line" >> "$rc"
    say "PATH: добавил ~/.local/bin в $rc"
}

touch "$HOME/.bashrc" 2>/dev/null || true
add_path_rc "$HOME/.bashrc" 'export PATH="$HOME/.local/bin:$PATH"'
add_path_rc "$HOME/.zshrc" 'export PATH="$HOME/.local/bin:$PATH"'
add_path_rc "$HOME/.profile" 'export PATH="$HOME/.local/bin:$PATH"'

if [ -f "$HOME/.config/fish/config.fish" ]; then
    if ! grep -qF 'fish_add_path' "$HOME/.config/fish/config.fish" 2>/dev/null; then
        printf '\n# NetMon\nfish_add_path "$HOME/.local/bin"\n' >> "$HOME/.config/fish/config.fish"
        say "PATH: добавил ~/.local/bin в config.fish"
    fi
fi

path_has_bin() { case ":$PATH:" in *":$BIN:"*) return 0 ;; *) return 1 ;; esac; }

# Если ~/.local/bin ещё не в PATH, пробуем сделать ссылку в уже доступной
# домашней папке из PATH — тогда команда работает сразу, без перезапуска.
if ! path_has_bin; then
    while IFS= read -r dir; do
        case "$dir" in
            "$HOME"/*) ;;
            *) continue ;;
        esac
        [ "$dir" = "$BIN" ] && continue
        [ -d "$dir" ] && [ -w "$dir" ] || continue
        if ln -sf "$BIN/netmon" "$dir/netmon" 2>/dev/null; then
            say "PATH: создал ссылку $dir/netmon"
            break
        fi
    done < <(printf '%s\n' "$PATH" | tr ':' '\n')
fi

# --- 6. Самопроверка --------------------------------------------------------
say ""
if [ -x "$BIN/netmon" ]; then
    say "OK: $BIN/netmon установлен."
else
    warn "Файл $BIN/netmon не найден."
fi

if "$BIN/netmon" --version >/dev/null 2>&1; then
    say "OK: программа запускается ($("$BIN/netmon" --version 2>/dev/null | head -1))."
else
    warn "Программа не запускается. Проверь вручную: python3 $BIN/netmon --version"
    "$BIN/netmon" --version || true
fi

if python3 -c "import tkinter" >/dev/null 2>&1; then
    say "OK: tkinter есть — окно доступно."
else
    say "Внимание: tkinter нет — окно не откроется, но 'netmon --cli' работает."
fi

# --- 7. Итог ----------------------------------------------------------------
say ""
if command -v netmon >/dev/null 2>&1; then
    say "Готово! Команда netmon уже доступна."
else
    say "Готово! Программа установлена в $BIN/netmon."
    say "Чтобы команда netmon заработала, открой новый терминал или выполни:"
    say "  source ~/.bashrc"
    say ""
    say "Прямой запуск уже сейчас:"
    say "  $BIN/netmon"
fi
say ""
say "  Окно:        netmon"
say "  Терминал:    netmon --cli"
say "  Один замер:  netmon --once"
say "  Ярлык:       NetMon в меню приложений"

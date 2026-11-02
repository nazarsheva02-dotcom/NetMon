#!/usr/bin/env python3
"""NetMon 2.0 — монитор пинга, джиттера и потерь пакетов.

Что делает программа:
  * пингует роутер, Cloudflare, Google и игровой сервер (если указан);
  * рисует график пинга, считает джиттер, максимальный пинг и потери;
  * показывает сигнал Wi-Fi и текущую скорость трафика;
  * подсказывает, где проблема: Wi-Fi/роутер или дальше (провайдер);
  * записывает завершённые инциденты в ~/netmon_log.csv;
  * умеет работать как окно и как живой терминальный дашборд.

Запуск:
    netmon                         # графическое окно
    netmon --cli                   # терминальный дашборд
    netmon --once                  # один отчёт и выход
    netmon --server 203.0.113.5 --name Deadlock
    netmon --cli --server 203.0.113.5 --name Deadlock

Зависимости: python3 и tkinter (только для окна). Больше ничего не нужно.
"""

from __future__ import annotations

import argparse
import collections
import csv
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

try:
    import tkinter as tk
except Exception:  # терминальный режим должен работать и без tkinter
    tk = None

VERSION = "2.0"

INTERVAL = 1.0      # секунд между пингами
HISTORY = 120       # сколько точек на графике
WINDOW = 30         # по скольким последним замерам считаем джиттер
SPIKE_MS = 100      # пинг выше этого считаем скачком
CLOSE_AFTER = 10    # столько хороших пингов подряд закрывают инцидент
LOG_PATH = os.path.expanduser("~/netmon_log.csv")
SPEED_URL = "https://speed.cloudflare.com/__down?bytes=25000000"

COLORS = ["#4ade80", "#60a5fa", "#f472b6", "#fbbf24", "#c084fc"]

# Тёмная тема окна
BG = "#0b0f14"
PANEL = "#121923"
PANEL2 = "#0f151d"
BORDER = "#22303f"
GRID = "#1c2835"
TEXT = "#e6edf3"
MUTED = "#8b98a5"
ACCENT = "#38bdf8"
GREEN = "#4ade80"
BLUE = "#60a5fa"
PINK = "#f472b6"
AMBER = "#fbbf24"
RED = "#f87171"

# Цвета терминала (256 цветов)
ANSI_BY_HEX = {
    "#4ade80": "\033[38;5;114m",
    "#60a5fa": "\033[38;5;75m",
    "#f472b6": "\033[38;5;211m",
    "#fbbf24": "\033[38;5;221m",
    "#c084fc": "\033[38;5;183m",
    "#f87171": "\033[38;5;203m",
    "#aaaaaa": "\033[38;5;245m",
}
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"

EVENTS = collections.deque(maxlen=8)   # последние инциденты для окна
LOG_LOCK = threading.Lock()
STATE = {"signal": None, "rx": None, "tx": None, "iface": None}


# ---------------------------------------------------------------------------
# Сеть и измерения
# ---------------------------------------------------------------------------

def detect_iface():
    """Wi-Fi интерфейс, а если его нет — интерфейс маршрута по умолчанию."""
    try:
        for name in sorted(os.listdir("/sys/class/net")):
            if name == "lo":
                continue
            if os.path.isdir(f"/sys/class/net/{name}/wireless"):
                return name
        out = subprocess.check_output(["ip", "route"], text=True)
        m = re.search(r"default .*? dev (\S+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "wlan0"


def default_gateway():
    try:
        out = subprocess.check_output(["ip", "route"], text=True)
        m = re.search(r"default via (\S+)", out)
        return m.group(1) if m else None
    except Exception:
        return None


def local_ip(iface):
    try:
        out = subprocess.check_output(["ip", "-4", "addr", "show", iface], text=True)
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        return m.group(1) if m else None
    except Exception:
        return None


def ping_once(host):
    """Один пинг. Возвращает мс или None, если пакет потерян."""
    cmd = ["ping", "-n", "-c", "1", "-W", "1"]
    if ":" in host:
        cmd.insert(1, "-6")
    cmd.append(host)
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3,
        ).stdout
        m = re.search(r"time[=<]([\d.]+)", out)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def wifi_signal(iface):
    """Уровень Wi-Fi в dBm. Сначала iw, потом /proc/net/wireless."""
    try:
        out = subprocess.run(
            ["iw", "dev", iface, "link"],
            capture_output=True, text=True, timeout=2,
        ).stdout
        m = re.search(r"signal:\s*(-?\d+)", out)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    try:
        with open("/proc/net/wireless", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith(iface + ":"):
                    parts = line.split(":", 1)[1].split()
                    for token in parts[:4]:
                        if re.fullmatch(r"-\d+", token):
                            return int(token)
    except Exception:
        pass
    return None


def read_bytes(iface):
    """Счётчики принятых и отправленных байт интерфейса из /proc/net/dev."""
    try:
        with open("/proc/net/dev", encoding="utf-8") as f:
            for line in f:
                name, _, rest = line.partition(":")
                if name.strip() == iface:
                    parts = rest.split()
                    return int(parts[0]), int(parts[8])
    except Exception:
        pass
    return None


def speed_test(url=SPEED_URL, max_seconds=8):
    """Качает файл до max_seconds секунд, возвращает скорость в Мбит/с или None."""
    try:
        t0 = time.time()
        got = 0
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            while time.time() - t0 < max_seconds:
                chunk = r.read(65536)
                if not chunk:
                    break
                got += len(chunk)
        dt = time.time() - t0
        return got * 8 / dt / 1e6 if got and dt > 0 else None
    except Exception:
        return None


def signal_quality(dbm):
    if dbm is None:
        return "нет данных"
    if dbm >= -55:
        return "отличный"
    if dbm >= -65:
        return "хороший"
    if dbm >= -75:
        return "средний"
    return "слабый"


def format_uptime(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def short_path(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


def log_incident(inc, name):
    """Записывает закончившийся инцидент в CSV и в список для окна."""
    dur = inc["last_bad"] - inc["start"]
    sig = STATE["signal"]
    row = [
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(inc["start"])),
        time.strftime("%H:%M:%S", time.localtime(inc["last_bad"])),
        f"{dur:.0f}", name, inc["lost"], inc["total"], inc["spikes"],
        f"{inc['max']:.0f}" if inc["max"] else "", sig if sig is not None else "",
        f"{STATE['rx']:.1f}" if STATE["rx"] is not None else "",
        f"{STATE['tx']:.1f}" if STATE["tx"] is not None else "",
    ]
    try:
        with LOG_LOCK:
            if os.path.exists(LOG_PATH):
                with open(LOG_PATH, encoding="utf-8") as f:
                    if "вниз_Мбит/с" not in f.readline():
                        os.replace(LOG_PATH, LOG_PATH + ".old")  # старый формат
            new = not os.path.exists(LOG_PATH)
            with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["начало", "конец", "длит_с", "цель", "потеряно",
                                "замеров", "скачков", "макс_мс", "wifi_dBm",
                                "вниз_Мбит/с", "вверх_Мбит/с"])
                w.writerow(row)
    except OSError:
        pass
    mx = f"{inc['max']:.0f} мс" if inc["max"] else "-"
    EVENTS.append(
        f"{row[0][11:]}  {name[:12]:<12} {dur:>3.0f} с  потери {inc['lost']}/{inc['total']}"
        f"  скачков {inc['spikes']}  макс {mx}"
    )


class Target:
    def __init__(self, name, host, color):
        self.name, self.host, self.color = name, host, color
        self.samples = collections.deque(maxlen=HISTORY)
        self.inc = None   # текущий инцидент (пока сеть плохая)

    def run(self, stop=None):
        while not (stop and stop.is_set()):
            t0 = time.time()
            v = ping_once(self.host)
            self.samples.append(v)
            self.track(v, t0)
            remaining = max(0.0, INTERVAL - (time.time() - t0))
            if stop:
                stop.wait(remaining)
            else:
                time.sleep(remaining)

    def track(self, v, now):
        """Инцидент начинается с потери или скачка пинга и заканчивается
        после CLOSE_AFTER хороших пингов подряд."""
        bad = v is None or v > SPIKE_MS
        if bad:
            if self.inc is None:
                self.inc = {"start": now, "last_bad": now, "lost": 0,
                            "spikes": 0, "max": 0.0, "total": 0, "good": 0}
            i = self.inc
            i["last_bad"], i["good"] = now, 0
            i["total"] += 1
            if v is None:
                i["lost"] += 1
            else:
                i["spikes"] += 1
                i["max"] = max(i["max"], v)
        elif self.inc is not None:
            i = self.inc
            i["total"] += 1
            i["good"] += 1
            if i["good"] >= CLOSE_AFTER:
                i["total"] -= i["good"]
                self.inc = None
                log_incident(i, self.name)

    def stats(self):
        recent = list(self.samples)[-WINDOW:]
        if not recent:
            return None
        ok = [x for x in recent if x is not None]
        loss = 100.0 * (len(recent) - len(ok)) / len(recent)
        if not ok:
            return {"avg": None, "jit": None, "max": None, "loss": loss}
        # джиттер = средняя разница между соседними пингами
        jit = (sum(abs(a - b) for a, b in zip(ok, ok[1:]))
               / (len(ok) - 1)) if len(ok) > 1 else 0.0
        return {"avg": sum(ok) / len(ok), "jit": jit, "max": max(ok), "loss": loss}


class LinkMonitor:
    """Сигнал Wi-Fi и текущий трафик интерфейса."""

    def __init__(self, iface):
        self.iface = iface
        self.ip = local_ip(iface)
        self.signal = None
        self.rx = None
        self.tx = None
        self.prev_bytes = None
        self.prev_time = 0.0
        self.last_signal_check = 0.0
        STATE["iface"] = iface

    def poll(self, force_signal=False):
        now = time.monotonic()
        b = read_bytes(self.iface)
        if b and self.prev_bytes and now > self.prev_time:
            dt = now - self.prev_time
            self.rx = max(0.0, (b[0] - self.prev_bytes[0]) * 8 / dt / 1e6)
            self.tx = max(0.0, (b[1] - self.prev_bytes[1]) * 8 / dt / 1e6)
        self.prev_bytes, self.prev_time = b, now
        if force_signal or now - self.last_signal_check > 3:
            self.signal = wifi_signal(self.iface)
            self.last_signal_check = now
        STATE["signal"], STATE["rx"], STATE["tx"] = (
            self.signal, self.rx, self.tx,
        )


def diagnose(gateway, others):
    """Возвращает (текст, цвет)."""
    g = gateway.stats() if gateway else None
    if g and (g["loss"] > 2 or (g["jit"] or 0) > 5):
        return "Лагает Wi-Fi: проблема уже между ПК и роутером", RED
    bad = []
    for t in others:
        s = t.stats()
        if s and (s["loss"] > 2 or (s["jit"] or 0) > 10):
            bad.append(t.name)
    if bad:
        return "Роутер в порядке, лагает дальше: " + ", ".join(bad) + " (провайдер или маршрут)", AMBER
    if g is None:
        if gateway is None:
            return "Шлюз не найден: проверь подключение", AMBER
        return "Собираю данные...", MUTED
    return "Сеть в порядке", GREEN


def build_targets(args):
    gateway = default_gateway()
    specs = []
    if gateway:
        specs.append(("Роутер", gateway))
    specs += [("Cloudflare", "1.1.1.1"), ("Google", "8.8.8.8")]
    if args.server:
        specs.append((args.name, args.server))
    targets = [Target(n, h, COLORS[i % len(COLORS)]) for i, (n, h) in enumerate(specs)]
    return gateway, targets


def start_targets(targets):
    stop = threading.Event()
    for t in targets:
        threading.Thread(target=t.run, args=(stop,), daemon=True).start()
    return stop


# ---------------------------------------------------------------------------
# Терминальный режим
# ---------------------------------------------------------------------------

def ansi(text, color, enabled):
    if not enabled or not color:
        return text
    return ANSI_BY_HEX.get(color, "") + text + ANSI_RESET


def sparkline(samples, width):
    """Мини-график для терминала: ▁▂▃▄▅▆▇█."""
    vals = list(samples)[-width:]
    if not vals:
        return " " * width
    ok = [v for v in vals if v is not None]
    top = max(50.0, (max(ok) if ok else 50.0) * 1.15)
    blocks = "▁▂▃▄▅▆▇█"
    out = []
    for v in vals:
        if v is None:
            out.append("·")
        else:
            idx = int(min(max(v, 0.0) / top, 1.0) * (len(blocks) - 1))
            out.append(blocks[idx])
    return " " * (width - len(vals)) + "".join(out)


def render_terminal(targets, gateway, link, started, color=False, live=False):
    cols = shutil.get_terminal_size((100, 30)).columns
    width = max(60, min(cols, 124))
    sep = "─" * width
    now = time.monotonic()

    gw_target = targets[0] if gateway else None
    others = targets[1:] if gateway else targets
    verdict, verdict_color = diagnose(gw_target, others)

    title = f"NETMON {VERSION}"
    right = f"интервал {INTERVAL:.0f} с  ·  {format_uptime(now - started)}"
    lines = [
        f"{ANSI_BOLD if color else ''}{title}{ANSI_RESET if color else ''}"
        f"{' ' * max(1, width - len(title) - len(right) - 2)}{ANSI_DIM if color else ''}{right}"
        f"{ANSI_RESET if color else ''}",
        sep,
        ansi("●  " + verdict, verdict_color, color),
        "",
        f"{ANSI_DIM if color else ''}{'Цель':<16}{'Пинг':>7}{'Джиттер':>9}{'Потери':>8}   График"
        f"{ANSI_RESET if color else ''}",
    ]

    spark_w = max(10, width - 62)
    for t in targets:
        s = t.stats()
        if not s or s["avg"] is None:
            value, jitter, loss = "—", "—", "100%" if s else "—"
        else:
            value = f"{s['avg']:.1f}"
            jitter = f"{s['jit']:.1f}"
            loss = f"{s['loss']:.0f}%"
        dot = ansi("●", t.color, color)
        name = t.name[:14]
        spark = sparkline(t.samples, spark_w)
        spark = ansi(spark, t.color, color)
        loss_txt = ansi(f"{loss:>7}", RED if loss not in ("0%", "—") else None, color)
        lines.append(f" {dot} {name:<14}{value:>7}{jitter:>9}{loss_txt}   {spark}")

    lines.append(sep)

    if link.signal is not None:
        wifi = f"{link.signal} dBm ({signal_quality(link.signal)})"
    else:
        wifi = "нет данных"
    traffic = (f"↓ {link.rx:.1f}  ↑ {link.tx:.1f} Мбит/с"
               if link.rx is not None else "измеряю...")
    lines += [
        f" Wi-Fi      {wifi}",
        f" Трафик     {traffic}",
        f" Интерфейс  {link.iface}  ·  IP {link.ip or '—'}",
        f" Шлюз       {gateway or 'не найден'}",
        f" Лог        {short_path(LOG_PATH)}",
    ]

    ev = list(EVENTS)[-4:]
    lines.append("")
    lines.append(" Инциденты:" if ev else " Инциденты: пока нет — это хорошо")
    for item in ev:
        lines.append("   " + item)
    if live:
        lines += ["", f"{ANSI_DIM if color else ''}Ctrl+C — выход{ANSI_RESET if color else ''}"]
    return "\n".join(lines)


def run_once(args):
    gateway, targets = build_targets(args)
    iface = args.iface or detect_iface()
    link = LinkMonitor(iface)
    link.poll(force_signal=True)  # базовый замер трафика
    started = time.monotonic()

    def probe(t):
        t0 = time.time()
        v = ping_once(t.host)
        t.samples.append(v)
        t.track(v, t0)

    threads = [threading.Thread(target=probe, args=(t,), daemon=True) for t in targets]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=4)

    link.poll()
    color = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
    print(render_terminal(targets, gateway, link, started, color=color, live=False))
    return 0


def run_cli(args):
    gateway, targets = build_targets(args)
    iface = args.iface or detect_iface()
    link = LinkMonitor(iface)
    stop = start_targets(targets)
    started = time.monotonic()
    tty = sys.stdout.isatty()
    color = tty and not args.no_color and "NO_COLOR" not in os.environ

    if not tty:
        # Если вывод перенаправлен, показываем один срез и выходим.
        link.poll(force_signal=True)
        time.sleep(1.4)
        link.poll()
        print(render_terminal(targets, gateway, link, started, color=False, live=False))
        stop.set()
        return 0

    link.poll(force_signal=True)
    sys.stdout.write("\033[?25l")  # спрятать курсор
    try:
        while True:
            link.poll()
            text = render_terminal(targets, gateway, link, started, color=color, live=True)
            sys.stdout.write("\033[H\033[2J" + text)
            sys.stdout.flush()
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        sys.stdout.write("\033[?25h\n")
        sys.stdout.flush()
    return 0


# ---------------------------------------------------------------------------
# Графическое окно
# ---------------------------------------------------------------------------

def nice_limit(value):
    target = max(50.0, value)
    if target <= 100:
        return float(math.ceil(target / 10) * 10)
    if target <= 300:
        return float(math.ceil(target / 25) * 25)
    if target <= 1000:
        return float(math.ceil(target / 100) * 100)
    return float(math.ceil(target / 500) * 500)


class App:
    def __init__(self, root, targets, gateway, iface, started, stop):
        import tkinter.font as tkfont

        self.root = root
        self.targets = targets
        self.gateway = gateway
        self.gateway_target = targets[0] if gateway else None
        self.others = targets[1:] if gateway else targets
        self.iface = iface
        self.link = LinkMonitor(iface)
        self.started = started
        self.stop = stop
        self.closed = False
        self.testing = False
        self.speed_text = "не запускался"
        self.geom = None
        self.card_ui = []
        self.status_full = (f"лог: {short_path(LOG_PATH)}   ·   Space — тест скорости"
                            "   ·   Ctrl+R — сброс")
        self.status_short = f"лог: {short_path(LOG_PATH)}"

        fams = set(tkfont.families(root))
        self.f_sans = next((f for f in ("Inter", "Noto Sans", "DejaVu Sans", "Liberation Sans")
                            if f in fams), "TkDefaultFont")
        self.f_mono = next((f for f in ("JetBrains Mono", "Cascadia Mono", "DejaVu Sans Mono",
                                       "Liberation Mono") if f in fams), "TkFixedFont")

        root.title(f"NetMon {VERSION} — монитор сети")
        root.configure(bg=BG)
        root.minsize(760, 520)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w = min(1080, max(800, sw - 120))
        h = min(850, max(620, sh - 140))
        root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        root.protocol("WM_DELETE_WINDOW", self.close)

        self.build_ui()
        root.bind("<space>", lambda _e: self.start_speed())
        root.bind("<Control-r>", lambda _e: self.reset())
        root.bind("<Control-q>", lambda _e: self.close())
        self.tick()

    def make_button(self, parent, text, command, accent=False):
        bg = ACCENT if accent else PANEL2
        fg = "#071018" if accent else TEXT
        hover = "#7dd3fc" if accent else "#1b2836"
        btn = tk.Button(
            parent, text=text, command=command, bg=bg, fg=fg,
            activebackground=hover, activeforeground=fg,
            disabledforeground=MUTED, relief="flat", bd=0,
            padx=12, pady=5, cursor="hand2", highlightthickness=0,
            font=(self.f_sans, 9, "bold"),
        )
        btn.bind("<Enter>", lambda _e: btn.config(bg=hover) if btn["state"] != "disabled" else None)
        btn.bind("<Leave>", lambda _e: btn.config(bg=bg) if btn["state"] != "disabled" else None)
        return btn

    def build_ui(self):
        main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True, padx=12, pady=8)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(2, weight=1)

        # ---- шапка -------------------------------------------------------
        header = tk.Frame(main, bg=BG)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        left = tk.Frame(header, bg=BG)
        left.grid(row=0, column=0, sticky="w")
        tk.Label(left, text="NETMON", font=(self.f_sans, 19, "bold"),
                 fg=TEXT, bg=BG).pack(anchor="w")
        tk.Label(left, text="Пинг · джиттер · потери · Wi-Fi",
                 font=(self.f_sans, 8), fg=MUTED, bg=BG).pack(anchor="w")

        right = tk.Frame(header, bg=BG)
        right.grid(row=0, column=1, sticky="e")
        self.verdict = tk.Label(
            right, text="Собираю данные...", font=(self.f_sans, 9, "bold"),
            fg=MUTED, bg=PANEL, padx=10, pady=5,
            highlightbackground=BORDER, highlightthickness=1,
            wraplength=520, justify="right",
        )
        self.verdict.pack(side="right")
        self.uptime_label = tk.Label(right, text="00:00", font=(self.f_mono, 9),
                                     fg=MUTED, bg=BG)
        self.uptime_label.pack(side="right", padx=(0, 12))

        # ---- карточки целей ---------------------------------------------
        cards = tk.Frame(main, bg=BG)
        cards.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        for i, target in enumerate(self.targets):
            cards.grid_columnconfigure(i, weight=1, uniform="cards")
            card = tk.Frame(cards, bg=PANEL, highlightbackground=BORDER,
                            highlightthickness=1)
            card.grid(row=0, column=i, sticky="nsew",
                      padx=(0 if i == 0 else 9, 0))
            body = tk.Frame(card, bg=PANEL)
            body.pack(fill="both", expand=True, padx=10, pady=7)

            top = tk.Frame(body, bg=PANEL)
            top.pack(fill="x")
            dot = tk.Canvas(top, width=12, height=12, bg=PANEL,
                            highlightthickness=0)
            dot.create_oval(1, 1, 11, 11, fill=target.color, outline="")
            dot.pack(side="left")
            tk.Label(top, text=target.name[:18], font=(self.f_sans, 9, "bold"),
                     fg=TEXT, bg=PANEL).pack(side="left", padx=(7, 0))

            ping = tk.Label(body, text="—", font=(self.f_sans, 15, "bold"),
                            fg=target.color, bg=PANEL, anchor="w")
            ping.pack(fill="x", pady=(3, 0))
            meta = tk.Label(body, text="собираю данные...", font=(self.f_sans, 8),
                            fg=MUTED, bg=PANEL, anchor="w")
            meta.pack(fill="x")
            self.card_ui.append({"ping": ping, "meta": meta})

        # ---- график ------------------------------------------------------
        graph = tk.Frame(main, bg=PANEL, highlightbackground=BORDER,
                         highlightthickness=1)
        graph.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        graph.grid_columnconfigure(0, weight=1)
        graph.grid_rowconfigure(1, weight=1)

        graph_head = tk.Frame(graph, bg=PANEL)
        graph_head.grid(row=0, column=0, sticky="ew", padx=12, pady=(7, 0))
        tk.Label(graph_head, text="ПИНГ, МС", font=(self.f_sans, 8, "bold"),
                 fg=MUTED, bg=PANEL).pack(side="left")
        self.graph_hint = tk.Label(
            graph_head, text="наведи мышью — значения по времени",
            font=(self.f_sans, 8), fg=MUTED, bg=PANEL,
        )
        self.graph_hint.pack(side="right")

        self.canvas = tk.Canvas(graph, bg=PANEL, highlightthickness=0, height=120)
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.canvas.bind("<Configure>", lambda _e: self.draw())
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<Leave>", self.on_leave)

        # ---- соединение и инциденты --------------------------------------
        bottom = tk.Frame(main, bg=BG)
        bottom.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        bottom.grid_columnconfigure(0, weight=1, uniform="bottom")
        bottom.grid_columnconfigure(1, weight=1, uniform="bottom")

        conn = tk.Frame(bottom, bg=PANEL, highlightbackground=BORDER,
                        highlightthickness=1)
        conn.grid(row=0, column=0, sticky="nsew", padx=(0, 9))
        conn_body = tk.Frame(conn, bg=PANEL)
        conn_body.pack(fill="both", expand=True, padx=10, pady=6)
        tk.Label(conn_body, text="СОЕДИНЕНИЕ", font=(self.f_sans, 8, "bold"),
                 fg=MUTED, bg=PANEL).pack(anchor="w", pady=(0, 5))
        conn_grid = tk.Frame(conn_body, bg=PANEL)
        conn_grid.pack(fill="x")
        conn_grid.grid_columnconfigure(1, weight=1)
        self.conn_values = {}
        fields = [
            ("Интерфейс", "iface"),
            ("Шлюз", "gateway"),
            ("Wi-Fi", "wifi"),
            ("Трафик", "traffic"),
            ("Скорость", "speed"),
        ]
        for row, (caption, key) in enumerate(fields):
            tk.Label(conn_grid, text=caption, font=(self.f_sans, 8),
                     fg=MUTED, bg=PANEL, anchor="w").grid(
                row=row, column=0, sticky="w", padx=(0, 10), pady=0)
            value = tk.Label(conn_grid, text="—", font=(self.f_sans, 8),
                             fg=TEXT, bg=PANEL, anchor="w", justify="left")
            value.grid(row=row, column=1, sticky="w", pady=0)
            self.conn_values[key] = value
        self.conn_values["speed"].config(wraplength=360)

        events = tk.Frame(bottom, bg=PANEL, highlightbackground=BORDER,
                          highlightthickness=1)
        events.grid(row=0, column=1, sticky="nsew")
        events_body = tk.Frame(events, bg=PANEL)
        events_body.pack(fill="both", expand=True, padx=10, pady=6)
        tk.Label(events_body, text="ИНЦИДЕНТЫ", font=(self.f_sans, 8, "bold"),
                 fg=MUTED, bg=PANEL).pack(anchor="w", pady=(0, 5))
        self.events_label = tk.Label(
            events_body, text="пока нет — это хороший знак", font=(self.f_mono, 8),
            fg=PINK, bg=PANEL, anchor="nw", justify="left",
        )
        self.events_label.pack(fill="both", expand=True, anchor="nw")
        events.bind("<Configure>", lambda e: self.events_label.config(
            wraplength=max(220, e.width - 30)))

        # ---- кнопки и статус ---------------------------------------------
        actions = tk.Frame(main, bg=BG)
        actions.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        self.speed_btn = self.make_button(actions, "Тест скорости",
                                          self.start_speed, accent=True)
        self.speed_btn.pack(side="left")
        self.reset_btn = self.make_button(actions, "Сбросить график", self.reset)
        self.reset_btn.pack(side="left", padx=(8, 0))
        self.copy_btn = self.make_button(actions, "Копировать отчёт", self.copy_report)
        self.copy_btn.pack(side="left", padx=(8, 0))
        self.status = tk.Label(
            actions,
            text=self.status_full,
            font=(self.f_sans, 8), fg=MUTED, bg=BG,
        )
        self.status.pack(side="right")
        self.root.bind("<Configure>", self.on_resize)

    def on_resize(self, event):
        if event.widget is self.root:
            self.status.config(text=self.status_full if event.width >= 900 else self.status_short)

    # ---- обновление окна -------------------------------------------------
    def tick(self):
        if self.closed:
            return
        self.link.poll()
        self.refresh()
        self.root.after(500, self.tick)

    def refresh(self):
        if self.closed:
            return

        text, color = diagnose(self.gateway_target, self.others)
        self.verdict.config(text="●  " + text, fg=color)
        self.uptime_label.config(text=format_uptime(time.monotonic() - self.started))

        for target, ui in zip(self.targets, self.card_ui):
            s = target.stats()
            if not s:
                ui["ping"].config(text="—", fg=MUTED)
                ui["meta"].config(text="собираю данные...", fg=MUTED)
            elif s["avg"] is None:
                ui["ping"].config(text="нет ответа", fg=RED)
                ui["meta"].config(text=f"потери {s['loss']:.0f}%", fg=RED)
            else:
                ui["ping"].config(text=f"{s['avg']:.1f} мс", fg=target.color)
                ui["meta"].config(
                    text=f"джиттер {s['jit']:.1f} · потери {s['loss']:.0f}%",
                    fg=RED if s["loss"] else MUTED,
                )

        self.conn_values["iface"].config(
            text=f"{self.iface or '—'}  ·  {self.link.ip or '—'}")
        self.conn_values["gateway"].config(text=self.gateway or "не найден")
        if self.link.signal is None:
            self.conn_values["wifi"].config(text="нет данных", fg=MUTED)
        else:
            q = signal_quality(self.link.signal)
            sc = GREEN if q in ("отличный", "хороший") else (AMBER if q == "средний" else RED)
            self.conn_values["wifi"].config(text=f"{self.link.signal} dBm ({q})", fg=sc)
        if self.link.rx is None:
            self.conn_values["traffic"].config(text="измеряю...", fg=MUTED)
        else:
            self.conn_values["traffic"].config(
                text=f"↓ {self.link.rx:.1f}  ↑ {self.link.tx:.1f} Мбит/с", fg=TEXT)
        self.conn_values["speed"].config(text=self.speed_text, fg=TEXT)

        ev = list(EVENTS)[-5:]
        self.events_label.config(
            text="\n".join(ev) if ev else "пока нет — это хороший знак",
            fg=PINK if ev else MUTED,
        )
        self.draw()

    # ---- действия --------------------------------------------------------
    def start_speed(self):
        if self.testing:
            return
        self.testing = True
        self.speed_text = "идёт тест скорости..."
        self.speed_btn.config(state="disabled", text="Идёт тест...")

        def work():
            value = speed_test()
            stamp = time.strftime("%H:%M:%S")
            text = (f"{stamp}: вниз {value:.0f} Мбит/с" if value
                    else f"{stamp}: не удалось")
            try:
                self.root.after(0, lambda: self.finish_speed(text))
            except tk.TclError:
                pass

        threading.Thread(target=work, daemon=True).start()

    def finish_speed(self, text):
        if self.closed:
            return
        self.testing = False
        self.speed_text = text
        self.speed_btn.config(state="normal", text="Тест скорости")
        self.refresh()

    def reset(self):
        for target in self.targets:
            target.samples.clear()
            target.inc = None
        EVENTS.clear()
        self.started = time.monotonic()
        if not self.testing:
            self.speed_text = "не запускался"
        self.refresh()

    def report_text(self):
        lines = [
            f"NetMon {VERSION} — отчёт {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]
        for target in self.targets:
            s = target.stats()
            if not s:
                lines.append(f"{target.name}: нет данных")
            elif s["avg"] is None:
                lines.append(f"{target.name}: нет ответа, потери {s['loss']:.0f}%")
            else:
                lines.append(
                    f"{target.name}: пинг {s['avg']:.1f} мс, джиттер {s['jit']:.1f} мс, "
                    f"макс {s['max']:.1f} мс, потери {s['loss']:.0f}%"
                )
        lines += [
            "",
            f"Интерфейс: {self.iface}",
            f"IP-адрес: {self.link.ip or '—'}",
            f"Шлюз: {self.gateway or '—'}",
        ]
        if self.link.signal is not None:
            lines.append(f"Wi-Fi: {self.link.signal} dBm ({signal_quality(self.link.signal)})")
        if self.link.rx is not None:
            lines.append(f"Трафик: вниз {self.link.rx:.1f}, вверх {self.link.tx:.1f} Мбит/с")
        lines.append(f"Тест скорости: {self.speed_text}")
        verdict, _ = diagnose(self.gateway_target, self.others)
        lines += ["", f"Диагноз: {verdict}"]
        ev = list(EVENTS)[-5:]
        if ev:
            lines.append("")
            lines.append("Инциденты:")
            lines += ["  " + item for item in ev]
        return "\n".join(lines)

    def copy_report(self):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.report_text())
            self.copy_btn.config(text="Скопировано ✓")
            self.root.after(1500, lambda: self.copy_btn.config(text="Копировать отчёт")
                            if not self.closed else None)
        except tk.TclError:
            pass

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        self.root.destroy()

    # ---- отрисовка графика ----------------------------------------------
    def draw(self):
        if self.closed:
            return
        c = self.canvas
        c.delete("all")
        width = c.winfo_width()
        height = c.winfo_height()
        if width < 100:
            width = 840
        if height < 60:
            return
        left, right, top, bottom = 54, width - 16, 30, height - 26
        if right - left < 80 or bottom - top < 40:
            return
        step = (right - left) / (HISTORY - 1)

        # Легенда
        lx = left
        for target in self.targets:
            c.create_oval(lx, 8, lx + 8, 16, fill=target.color, outline="")
            item = c.create_text(lx + 12, 12, text=target.name[:14], anchor="w",
                                 fill=MUTED, font=(self.f_mono, 8))
            box = c.bbox(item)
            lx = (box[2] if box else lx + 80) + 18

        all_values = [v for t in self.targets for v in t.samples if v is not None]
        peak = max(all_values) if all_values else 0.0
        ymax = nice_limit(peak * 1.15)

        for frac in (0, 0.25, 0.5, 0.75, 1):
            gy = bottom - frac * (bottom - top)
            c.create_line(left, gy, right, gy, fill=GRID,
                          dash=(2, 5) if frac not in (0, 1) else None)
            c.create_text(left - 8, gy, text=f"{ymax * frac:.0f}", anchor="e",
                          fill=MUTED, font=(self.f_mono, 8))

        for seconds in (HISTORY - 1, 90, 60, 30, 0):
            x = right - seconds * step
            c.create_line(x, top, x, bottom, fill=GRID, dash=(2, 5))
            if seconds == 0:
                label, anchor = "сейчас", "e"
            elif seconds == HISTORY - 1:
                label, anchor = f"−{seconds} с", "w"
            else:
                label, anchor = f"−{seconds}", "center"
            c.create_text(x, bottom + 6, text=label, anchor="n" if anchor == "center"
                          else anchor, fill=MUTED, font=(self.f_mono, 8))

        any_data = False
        for target in self.targets:
            data = list(target.samples)
            n = len(data)
            if n:
                any_data = True
            prev = None
            for i, v in enumerate(data):
                x = right - (n - 1 - i) * step
                if v is None:
                    c.create_line(x, bottom - 9, x, bottom, fill=RED, width=2)
                    prev = None
                    continue
                yv = bottom - (min(v, ymax) / ymax) * (bottom - top)
                point = (x, yv)
                if prev is not None:
                    c.create_line(prev[0], prev[1], point[0], point[1],
                                  fill=target.color, width=2)
                prev = point
            if data and data[-1] is not None:
                yv = bottom - (min(data[-1], ymax) / ymax) * (bottom - top)
                c.create_oval(right - 3, yv - 3, right + 3, yv + 3,
                              fill=target.color, outline=PANEL)

        if not any_data:
            c.create_text((left + right) / 2, (top + bottom) / 2,
                          text="Собираю данные...", fill=MUTED,
                          font=(self.f_sans, 11))
        self.geom = (left, right, top, bottom, ymax, step)

    def on_motion(self, event):
        if not self.geom:
            return
        left, right, top, bottom, _ymax, step = self.geom
        if not (left <= event.x <= right) or step <= 0:
            self.on_leave()
            return
        c = self.canvas
        c.delete("hover")
        back = int(round((right - event.x) / step))
        back = max(0, min(HISTORY - 1, back))
        x = right - back * step
        c.create_line(x, top, x, bottom, fill="#3b4b5c", dash=(3, 3),
                      tags="hover")

        rows = ["сейчас" if back == 0 else f"−{back} с"]
        for target in self.targets:
            data = list(target.samples)
            if back < len(data):
                value = data[len(data) - 1 - back]
                rows.append(f"{target.name}: " + ("потеря" if value is None
                                                  else f"{value:.1f} мс"))
            else:
                rows.append(f"{target.name}: —")

        text = "\n".join(rows)
        box_w = max(128, max(len(row) for row in rows) * 7 + 18)
        box_h = 14 + 16 * len(rows)
        tx = x + 12 if x + 12 + box_w < right else x - box_w - 12
        tx = max(2, min(tx, right - box_w - 2))
        ty = max(2, min(top + 4, bottom - box_h - 4))
        c.create_rectangle(tx, ty, tx + box_w, ty + box_h, fill=PANEL2,
                           outline=BORDER, tags="hover")
        c.create_text(tx + 8, ty + 7, text=text, anchor="nw", fill=TEXT,
                      font=(self.f_mono, 8), justify="left", tags="hover")

    def on_leave(self, _event=None):
        self.canvas.delete("hover")


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

def run_gui(args):
    if tk is None:
        print("Tkinter не установлен — запускаю терминальный режим.", file=sys.stderr)
        return run_cli(args)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Не удалось открыть окно: {exc}\nЗапускаю терминальный режим...",
              file=sys.stderr)
        return run_cli(args)

    gateway, targets = build_targets(args)
    iface = args.iface or detect_iface()
    stop = start_targets(targets)
    app = App(root, targets, gateway, iface, time.monotonic(), stop)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.close()
    stop.set()
    return 0


def build_parser():
    ap = argparse.ArgumentParser(
        prog="netmon",
        description="Монитор пинга, джиттера и потерь пакетов для игр.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  netmon                                  окно\n"
            "  netmon --cli                            живой терминальный дашборд\n"
            "  netmon --once                           один отчёт и выход\n"
            "  netmon --server 203.0.113.5 --name Deadlock\n"
            "  netmon --cli --server 203.0.113.5 --name Deadlock\n"
        ),
    )
    ap.add_argument("--server", help="IP или домен игрового сервера (необязательно)")
    ap.add_argument("--name", default="Игра", help="как назвать сервер на графике")
    ap.add_argument("--iface", default=None,
                    help="сетевой интерфейс (по умолчанию определяется сам)")
    ap.add_argument("--log", default=LOG_PATH, help="файл лога инцидентов (CSV)")
    ap.add_argument("--spike", type=float, default=SPIKE_MS,
                    help="пинг в мс, выше которого считаем скачком")
    ap.add_argument("--cli", "--terminal", dest="cli", action="store_true",
                    help="не открывать окно, работать в терминале")
    ap.add_argument("--no-gui", dest="cli", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--once", action="store_true",
                    help="сделать один замер, напечатать отчёт и выйти")
    ap.add_argument("--no-color", action="store_true",
                    help="без ANSI-цветов в терминале")
    ap.add_argument("--version", action="version", version=f"NetMon {VERSION}")
    return ap


def main(argv=None):
    global LOG_PATH, SPIKE_MS
    args = build_parser().parse_args(argv)
    LOG_PATH = os.path.expanduser(args.log)
    SPIKE_MS = args.spike

    if not shutil.which("ping"):
        print("Не найден ping. Установи iputils (Arch: pacman -S iputils).",
              file=sys.stderr)
        return 1

    if args.once:
        return run_once(args)
    if args.cli:
        return run_cli(args)

    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if not has_display:
        print("Графическое окружение не найдено — запускаю терминальный режим.",
              file=sys.stderr)
        return run_cli(args)
    return run_gui(args)


if __name__ == "__main__":
    raise SystemExit(main())

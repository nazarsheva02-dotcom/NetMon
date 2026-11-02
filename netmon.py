#!/usr/bin/env python3
"""netmon.py: график пинга и джиттера до роутера, интернета и игрового сервера.

Нужно: python и tk (sudo pacman -S tk). Больше ничего не нужно.
Запуск:  python netmon.py
         python netmon.py --server 203.0.113.5 --name Deadlock --iface wlan0
"""
import argparse
import collections
import csv
import os
import re
import subprocess
import threading
import time
import tkinter as tk
import urllib.request

INTERVAL = 1.0      # секунд между пингами
HISTORY = 120       # сколько точек на графике
WINDOW = 30         # по скольким последним замерам считаем джиттер
W, H, PAD = 720, 280, 40
COLORS = ["#4ade80", "#60a5fa", "#f472b6", "#fbbf24"]
SPIKE_MS = 100      # пинг выше этого считаем скачком
CLOSE_AFTER = 10    # столько хороших пингов подряд закрывают инцидент
LOG_PATH = os.path.expanduser("~/netmon_log.csv")
EVENTS = collections.deque(maxlen=8)   # последние инциденты для окна
LOG_LOCK = threading.Lock()
STATE = {"signal": None, "rx": None, "tx": None}   # для лога: Wi-Fi и текущий трафик
SPEED_URL = "https://speed.cloudflare.com/__down?bytes=25000000"


def detect_iface():
    """Wi-Fi интерфейс, а если его нет, интерфейс маршрута по умолчанию."""
    try:
        for name in sorted(os.listdir("/sys/class/net")):
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


def ping_once(host):
    """Один пинг, возвращает мс или None, если пакет потерян."""
    try:
        out = subprocess.run(
            ["ping", "-n", "-c", "1", "-W", "1", host],
            capture_output=True, text=True, timeout=3,
        ).stdout
        m = re.search(r"time[=<]([\d.]+)", out)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def wifi_signal(iface):
    try:
        out = subprocess.run(
            ["iw", "dev", iface, "link"], capture_output=True, text=True, timeout=2
        ).stdout
        m = re.search(r"signal:\s*(-?\d+)", out)
        return int(m.group(1)) if m else None
    except Exception:
        return None


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
        f"{row[0][11:]}  {name:<10} {dur:>3.0f} с  потери {inc['lost']}/{inc['total']}"
        f"  скачков {inc['spikes']}  макс {mx}"
    )


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
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=5) as r:
            while time.time() - t0 < max_seconds:
                chunk = r.read(65536)
                if not chunk:
                    break
                got += len(chunk)
        dt = time.time() - t0
        return got * 8 / dt / 1e6 if got and dt > 0 else None
    except Exception:
        return None


class Target:
    def __init__(self, name, host, color):
        self.name, self.host, self.color = name, host, color
        self.samples = collections.deque(maxlen=HISTORY)
        self.inc = None   # текущий инцидент (пока сеть плохая)

    def run(self):
        while True:
            t0 = time.time()
            v = ping_once(self.host)
            self.samples.append(v)
            self.track(v, t0)
            time.sleep(max(0.0, INTERVAL - (time.time() - t0)))

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
        jit = (sum(abs(a - b) for a, b in zip(ok, ok[1:])) / (len(ok) - 1)) if len(ok) > 1 else 0.0
        return {"avg": sum(ok) / len(ok), "jit": jit, "max": max(ok), "loss": loss}


def diagnose(gateway, others):
    """Возвращает (текст, цвет)."""
    g = gateway.stats() if gateway else None
    if g and (g["loss"] > 2 or (g["jit"] or 0) > 5):
        return "Лагает Wi-Fi: проблема уже между ПК и роутером", "#f87171"
    bad = []
    for t in others:
        s = t.stats()
        if s and (s["loss"] > 2 or (s["jit"] or 0) > 10):
            bad.append(t.name)
    if bad:
        return "Роутер в порядке, лагает дальше: " + ", ".join(bad) + " (провайдер или маршрут)", "#fbbf24"
    if g is None:
        return "Собираю данные...", "#aaaaaa"
    return "Сеть в порядке", "#4ade80"


class App:
    def __init__(self, root, targets, gateway, iface):
        self.root, self.targets, self.gateway, self.iface = root, targets, gateway, iface
        self.signal = None
        self.last_signal_check = 0.0
        self.prev_bytes = None
        self.prev_time = 0.0
        self.speed_text = "Тест скорости: не запускался"
        self.testing = False
        root.title("netmon")
        root.configure(bg="#111")
        self.verdict = tk.Label(root, text="", font=("monospace", 13, "bold"), bg="#111", fg="#aaa")
        self.verdict.pack(anchor="w", padx=10, pady=(8, 0))
        self.canvas = tk.Canvas(root, width=W, height=H, bg="#111", highlightthickness=0)
        self.canvas.pack(padx=10, pady=6)
        self.info = tk.Label(root, text="", font=("monospace", 10), bg="#111", fg="#ddd", justify="left")
        self.info.pack(anchor="w", padx=10, pady=(0, 4))
        self.btn = tk.Button(root, text="Тест скорости (нагружает канал, не жми в матче)",
                             command=self.start_speed, bg="#222", fg="#ddd", relief="flat")
        self.btn.pack(anchor="w", padx=10, pady=(0, 4))
        self.events = tk.Label(root, text="", font=("monospace", 9), bg="#111", fg="#f0abfc", justify="left")
        self.events.pack(anchor="w", padx=10, pady=(0, 8))
        self.tick()

    def start_speed(self):
        if self.testing:
            return
        self.testing = True
        self.speed_text = "Тест скорости: идёт..."

        def work():
            v = speed_test()
            stamp = time.strftime("%H:%M:%S")
            self.speed_text = (f"Тест скорости ({stamp}): вниз {v:.0f} Мбит/с" if v
                               else f"Тест скорости ({stamp}): не удалось")
            self.testing = False

        threading.Thread(target=work, daemon=True).start()

    def tick(self):
        now = time.time()
        b = read_bytes(self.iface)
        if b and self.prev_bytes and now > self.prev_time:
            dt = now - self.prev_time
            STATE["rx"] = (b[0] - self.prev_bytes[0]) * 8 / dt / 1e6
            STATE["tx"] = (b[1] - self.prev_bytes[1]) * 8 / dt / 1e6
        self.prev_bytes, self.prev_time = b, now
        if now - self.last_signal_check > 3:
            self.signal = wifi_signal(self.iface)
            STATE["signal"] = self.signal
            self.last_signal_check = now
        self.draw()
        self.root.after(1000, self.tick)

    def draw(self):
        c = self.canvas
        c.delete("all")
        vals = [v for t in self.targets for v in t.samples if v is not None]
        ymax = min(300.0, max(50.0, (max(vals) * 1.1) if vals else 50.0))
        step = (W - 2 * PAD) / (HISTORY - 1)
        right = W - PAD

        def y(v):
            return H - PAD - (min(v, ymax) / ymax) * (H - 2 * PAD)

        for frac in (0, 0.5, 1):
            gy = y(ymax * frac)
            c.create_line(PAD, gy, W - PAD, gy, fill="#2a2a2a")
            c.create_text(PAD - 6, gy, text=f"{ymax * frac:.0f}", fill="#888", anchor="e", font=("monospace", 8))
        c.create_text(PAD, 10, text="мс", fill="#888", anchor="w", font=("monospace", 8))

        for t in self.targets:
            data = list(t.samples)
            n = len(data)
            prev = None
            for i, v in enumerate(data):
                x = right - (n - 1 - i) * step
                if v is None:
                    c.create_line(x, H - PAD, x, H - PAD - 14, fill="#ef4444", width=2)  # потеря пакета
                    prev = None
                    continue
                p = (x, y(v))
                if prev:
                    c.create_line(*prev, *p, fill=t.color, width=2)
                prev = p

        lines = []
        for t in self.targets:
            s = t.stats()
            if not s:
                continue
            if s["avg"] is None:
                lines.append(f"{t.name:<12} нет ответа (потери {s['loss']:.0f}%)")
            else:
                lines.append(
                    f"{t.name:<12} пинг {s['avg']:5.1f}  джиттер {s['jit']:4.1f}  "
                    f"макс {s['max']:5.1f}  потери {s['loss']:3.0f}%"
                )
        if self.signal is not None:
            q = "хороший" if self.signal >= -55 else "средний" if self.signal >= -67 else "слабый"
            lines.append(f"Wi-Fi сигнал: {self.signal} dBm ({q})")
        if STATE["rx"] is not None:
            lines.append(f"Трафик сейчас: вниз {STATE['rx']:.1f}  вверх {STATE['tx']:.1f} Мбит/с")
        lines.append(self.speed_text)
        self.info.config(text="\n".join(lines))
        ev = list(EVENTS)[-6:]
        self.events.config(
            text=f"Инциденты (лог: {LOG_PATH}):\n" + ("\n".join(ev) if ev else "пока нет")
        )
        text, color = diagnose(self.targets[0] if self.gateway else None, self.targets[1:] if self.gateway else self.targets)
        self.verdict.config(text=text, fg=color)


def main():
    global LOG_PATH, SPIKE_MS
    ap = argparse.ArgumentParser(description="Монитор пинга и джиттера для игр")
    ap.add_argument("--server", help="IP игрового сервера (необязательно)")
    ap.add_argument("--name", default="Игра", help="как назвать сервер на графике")
    ap.add_argument("--iface", default=None, help="сетевой интерфейс (по умолчанию определяется сам)")
    ap.add_argument("--log", default=LOG_PATH, help="файл лога инцидентов (CSV)")
    ap.add_argument("--spike", type=float, default=SPIKE_MS, help="пинг в мс, выше которого считаем скачком")
    args = ap.parse_args()
    LOG_PATH, SPIKE_MS = os.path.expanduser(args.log), args.spike

    gw = default_gateway()
    specs = []
    if gw:
        specs.append(("Роутер", gw))
    specs += [("Cloudflare", "1.1.1.1"), ("Google", "8.8.8.8")]
    if args.server:
        specs.append((args.name, args.server))

    targets = [Target(n, h, COLORS[i % len(COLORS)]) for i, (n, h) in enumerate(specs)]
    for t in targets:
        threading.Thread(target=t.run, daemon=True).start()

    root = tk.Tk()
    App(root, targets, gw, args.iface or detect_iface())
    root.mainloop()


if __name__ == "__main__":
    main()

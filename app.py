#!/usr/bin/env python3
"""Dashboard + Telegram bot with proactive alerting"""

import os
import time
import json
import secrets
import hashlib
import base64
import psutil
import subprocess
import requests
import threading
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

OLLAMA_URL = "http://localhost:11434"
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8791345433:AAFPbDEZYOBRWN9xzI8HCOoZCIgjxz-PHyw")
CHAT_ID = os.environ.get("CHAT_ID", "1988483132")

# ── History buffer for graphs (last 120 points = ~6 min at 3s) ──
import collections
HISTORY_MAX = 120
history = collections.deque(maxlen=HISTORY_MAX)
history_lock = threading.Lock()

# ── Alert thresholds ──
THRESHOLDS = {
    "cpu_high": 70,
    "cpu_critical": 90,
    "mem_high": 70,
    "mem_critical": 90,
    "disk_warning": 80,
    "disk_critical": 90,
    "swap_warning": 70,
    "load_critical_multiplier": 2.0,  # load > cores * multiplier
}

# ── Alert cooldown (seconds) — prevent spam ──
COOLDOWN = {
    "high": 300,       # 5 min between repeated warnings
    "critical": 60,    # 1 min between repeated critical
    "service": 120,    # 2 min between service alerts
    "recovery": 600,   # 10 min between recovery confirmations
}


class AlertManager:
    def __init__(self):
        self._last_alert = {}  # key -> datetime of last sent
        self._active = set()   # currently active alerts
        self._lock = threading.Lock()

    def should_send(self, key, level="high"):
        now = datetime.now()
        with self._lock:
            last = self._last_alert.get(key)
            cd = COOLDOWN.get(level, 300)
            if last and (now - last).total_seconds() < cd:
                return False
            self._last_alert[key] = now
            return True

    def set_active(self, key):
        with self._lock:
            self._active.add(key)

    def clear_active(self, key):
        with self._lock:
            self._active.discard(key)

    def is_active(self, key):
        with self._lock:
            return key in self._active


alerts = AlertManager()


def send_telegram(text, parse_mode="HTML"):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram send error: {e}")


def get_system_info():
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    cpu_percent = psutil.cpu_percent(interval=0.5)
    cpu_freq = psutil.cpu_freq()
    load = os.getloadavg()
    temps = {}
    try:
        t = psutil.sensors_temperatures()
        for name, entries in t.items():
            temps[name] = [{"label": e.label or name, "current": e.current} for e in entries]
    except Exception:
        pass

    net = psutil.net_io_counters()
    processes = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "status"]):
        try:
            info = p.info
            if info["cpu_percent"] and info["cpu_percent"] > 0:
                processes.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    processes.sort(key=lambda x: x.get("cpu_percent", 0), reverse=True)

    uptime_sec = time.time() - psutil.boot_time()
    days = int(uptime_sec // 86400)
    hours = int((uptime_sec % 86400) // 3600)
    mins = int((uptime_sec % 3600) // 60)

    return {
        "cpu_percent": cpu_percent,
        "cpu_count": psutil.cpu_count(),
        "cpu_freq": round(cpu_freq.current, 0) if cpu_freq else 0,
        "load_1": round(load[0], 2),
        "load_5": round(load[1], 2),
        "load_15": round(load[2], 2),
        "memory_total": round(mem.total / (1024**3), 2),
        "memory_used": round(mem.used / (1024**3), 2),
        "memory_percent": mem.percent,
        "swap_total": round(swap.total / (1024**3), 2),
        "swap_used": round(swap.used / (1024**3), 2),
        "swap_percent": swap.percent,
        "disk_total": round(disk.total / (1024**3), 2),
        "disk_used": round(disk.used / (1024**3), 2),
        "disk_percent": disk.percent,
        "disk_free": round(disk.free / (1024**3), 2),
        "net_sent": round(net.bytes_sent / (1024**2), 1),
        "net_recv": round(net.bytes_recv / (1024**2), 1),
        "temperatures": temps,
        "uptime": f"{days}d {hours}h {mins}m",
        "processes": processes[:15],
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def get_ollama_info():
    info = {"status": "offline", "models": [], "running": []}
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        if r.ok:
            data = r.json()
            info["status"] = "online"
            info["models"] = data.get("models", [])
    except Exception:
        pass

    try:
        r = requests.get(f"{OLLAMA_URL}/api/ps", timeout=3)
        if r.ok:
            data = r.json()
            info["running"] = data.get("models", [])
    except Exception:
        pass

    return info


def check_services():
    """Check critical services are running"""
    services = {}
    for svc in ["ollama.service"]:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=5,
            )
            services[svc] = result.stdout.strip()
        except Exception:
            services[svc] = "unknown"
    return services


def check_oom_kills():
    """Check for recent OOM kills in dmesg/journal"""
    try:
        result = subprocess.run(
            ["journalctl", "-k", "--since", "10 min ago", "-q", "--no-pager"],
            capture_output=True, text=True, timeout=5,
        )
        if "Out of memory" in result.stdout or "oom-kill" in result.stdout.lower():
            return True
    except Exception:
        pass
    return False


def format_bar(pct):
    filled = int(pct / 5)
    return "█" * filled + "░" * (20 - filled)


def alert_color(pct, warn=70, crit=90):
    if pct >= crit:
        return "🔴", "red"
    if pct >= warn:
        return "🟡", "yellow"
    return "🟢", "green"


# ── Proactive alert checker ──
def check_alerts():
    """Runs every 30 seconds — checks all thresholds and sends alerts"""
    sys_info = get_system_info()
    ollama_info = get_ollama_info()
    now_str = datetime.now().strftime("%H:%M:%S")

    # ── CPU ──
    cpu = sys_info["cpu_percent"]
    if cpu >= THRESHOLDS["cpu_critical"]:
        key = "cpu_critical"
        if not alerts.is_active(key) or alerts.should_send(key, "critical"):
            alerts.set_active(key)
            top = sys_info["processes"][:3]
            procs = "\n".join(f"  • {p.get('name','?')[:20]}: {p.get('cpu_percent',0):.1f}%" for p in top)
            send_telegram(
                f"🚨 <b>КРИТИЧНО: CPU {cpu}%</b>\n"
                f"<i>{now_str}</i>\n\n"
                f"Навантаження: {sys_info['load_1']} / {sys_info['load_5']} / {sys_info['load_15']}\n"
                f"<code>{format_bar(cpu)}</code>\n\n"
                f"<b>Топ-3 навантаження:</b>\n{procs}"
            )
    elif cpu >= THRESHOLDS["cpu_high"]:
        key = "cpu_high"
        if alerts.is_active("cpu_critical"):
            alerts.clear_active("cpu_critical")
        if not alerts.is_active(key) or alerts.should_send(key, "high"):
            alerts.set_active(key)
            send_telegram(
                f"⚠️ <b>УВАГА: CPU {cpu}%</b>\n"
                f"<i>{now_str}</i>\n"
                f"Навантаження: {sys_info['load_1']} / {sys_info['load_5']} / {sys_info['load_15']}\n"
                f"<code>{format_bar(cpu)}</code>"
            )
    else:
        if alerts.is_active("cpu_high") or alerts.is_active("cpu_critical"):
            alerts.clear_active("cpu_high")
            alerts.clear_active("cpu_critical")
            if alerts.should_send("cpu_recovery", "recovery"):
                alerts.set_active("cpu_recovery")
                send_telegram(f"✅ <b>CPU нормалізувався</b> — зараз {cpu}%\n<i>{now_str}</i>")

    # ── RAM ──
    mem = sys_info["memory_percent"]
    if mem >= THRESHOLDS["mem_critical"]:
        key = "mem_critical"
        if not alerts.is_active(key) or alerts.should_send(key, "critical"):
            alerts.set_active(key)
            top = sys_info["processes"][:3]
            procs = "\n".join(f"  • {p.get('name','?')[:20]}: {p.get('memory_percent',0):.1f}%" for p in top)
            send_telegram(
                f"🚨 <b>КРИТИЧНО: RAM {mem}%</b>\n"
                f"<i>{now_str}</i>\n\n"
                f"Використано: {sys_info['memory_used']}/{sys_info['memory_total']}GB\n"
                f"Swap: {sys_info['swap_percent']}% ({sys_info['swap_used']}/{sys_info['swap_total']}GB)\n"
                f"<code>{format_bar(mem)}</code>\n\n"
                f"<b>Топ-3 споживачі пам'яті:</b>\n{procs}"
            )
    elif mem >= THRESHOLDS["mem_high"]:
        key = "mem_high"
        if alerts.is_active("mem_critical"):
            alerts.clear_active("mem_critical")
        if not alerts.is_active(key) or alerts.should_send(key, "high"):
            alerts.set_active(key)
            send_telegram(
                f"⚠️ <b>УВАГА: RAM {mem}%</b>\n"
                f"<i>{now_str}</i>\n"
                f"Використано: {sys_info['memory_used']}/{sys_info['memory_total']}GB\n"
                f"Swap: {sys_info['swap_percent']}%\n"
                f"<code>{format_bar(mem)}</code>"
            )
    else:
        if alerts.is_active("mem_high") or alerts.is_active("mem_critical"):
            alerts.clear_active("mem_high")
            alerts.clear_active("mem_critical")
            if alerts.should_send("mem_recovery", "recovery"):
                alerts.set_active("mem_recovery")
                send_telegram(f"✅ <b>RAM нормалізувалася</b> — зараз {mem}%\n<i>{now_str}</i>")

    # ── Disk ──
    disk = sys_info["disk_percent"]
    if disk >= THRESHOLDS["disk_critical"]:
        key = "disk_critical"
        if not alerts.is_active(key) or alerts.should_send(key, "critical"):
            alerts.set_active(key)
            send_telegram(
                f"🚨 <b>КРИТИЧНО: ДИСК {disk}%</b>\n"
                f"<i>{now_str}</i>\n\n"
                f"Лише <b>{sys_info['disk_free']}ГБ вільно</b> з {sys_info['disk_total']}ГБ\n"
                f"<code>{format_bar(disk)}</code>\n\n"
                f"⚡ Терміново потрібно звільнити місце!"
            )
    elif disk >= THRESHOLDS["disk_warning"]:
        key = "disk_warning"
        if alerts.is_active("disk_critical"):
            alerts.clear_active("disk_critical")
        if not alerts.is_active(key) or alerts.should_send(key, "high"):
            alerts.set_active(key)
            send_telegram(
                f"⚠️ <b>УВАГА: ДИСК {disk}%</b>\n"
                f"<i>{now_str}</i>\n"
                f"{sys_info['disk_free']}ГБ вільно з {sys_info['disk_total']}ГБ\n"
                f"<code>{format_bar(disk)}</code>"
            )
    else:
        if alerts.is_active("disk_warning") or alerts.is_active("disk_critical"):
            alerts.clear_active("disk_warning")
            alerts.clear_active("disk_critical")
            if alerts.should_send("disk_recovery", "recovery"):
                alerts.set_active("disk_recovery")
                send_telegram(f"✅ <b>Диск нормалізувався</b> — вільно {sys_info['disk_free']}ГБ ({disk}%)\n<i>{now_str}</i>")

    # ── Swap ──
    swap = sys_info["swap_percent"]
    if swap >= THRESHOLDS["swap_warning"]:
        key = "swap_high"
        if not alerts.is_active(key) or alerts.should_send(key, "high"):
            alerts.set_active(key)
            send_telegram(
                f"⚠️ <b>УВАГА: SWAP {swap}%</b>\n"
                f"<i>{now_str}</i>\n"
                f"Використано: {sys_info['swap_used']}/{sys_info['swap_total']}GB\n"
                f"Ймовірно, тиск на RAM — перевірте споживачі пам'яті"
            )
    else:
        if alerts.is_active("swap_high"):
            alerts.clear_active("swap_high")

    # ── Load average ──
    cores = sys_info["cpu_count"]
    load1 = sys_info["load_1"]
    if load1 > cores * THRESHOLDS["load_critical_multiplier"]:
        key = "load_critical"
        if not alerts.is_active(key) or alerts.should_send(key, "critical"):
            alerts.set_active(key)
            send_telegram(
                f"🚨 <b>ВИСОКЕ НАВАНТАЖЕННЯ: {load1}</b> (ядра: {cores})\n"
                f"<i>{now_str}</i>\n"
                f"Співвідношення навантаження/ядра: {round(load1/cores, 1)}x\n"
                f"Система перевантажена!"
            )
    else:
        if alerts.is_active("load_critical"):
            alerts.clear_active("load_critical")

    # ── Ollama offline ──
    if ollama_info["status"] == "offline":
        key = "ollama_offline"
        if not alerts.is_active(key) or alerts.should_send(key, "service"):
            alerts.set_active(key)
            send_telegram(
                f"🔴 <b>Ollama НЕ В МЕРЕЖІ</b>\n"
                f"<i>{now_str}</i>\n"
                f"AI-движок не відповідає на {OLLAMA_URL}"
            )
    else:
        if alerts.is_active("ollama_offline"):
            alerts.clear_active("ollama_offline")
            if alerts.should_send("ollama_recovery", "recovery"):
                alerts.set_active("ollama_recovery")
                send_telegram(f"✅ <b>Ollama знову ONLINE</b>\n<i>{now_str}</i>")

    # ── Service status ──
    services = check_services()
    for svc, status in services.items():
        key = f"svc_{svc}"
        if status != "active":
            if not alerts.is_active(key) or alerts.should_send(key, "service"):
                alerts.set_active(key)
                send_telegram(
                    f"🔴 <b>Сервіс НЕ ПРАЦЮЄ: {svc}</b>\n"
                    f"<i>{now_str}</i>\n"
                    f"Статус: <code>{status}</code>\n"
                    f"Можливо потрібен автоперезапуск"
                )
        else:
            if alerts.is_active(key):
                alerts.clear_active(key)
                if alerts.should_send(f"{key}_recovery", "recovery"):
                    alerts.set_active(f"{key}_recovery")
                    send_telegram(f"✅ <b>{svc} знову ПРАЦЮЄ</b>\n<i>{now_str}</i>")

    # ── OOM kills ──
    if check_oom_kills():
        key = "oom_kill"
        if not alerts.is_active(key) or alerts.should_send(key, "critical"):
            alerts.set_active(key)
            send_telegram(
                f"💀 <b>ВИЯВЛЕНО OOM KILL</b>\n"
                f"<i>{now_str}</i>\n"
                f"Ядро вбило процес через брак пам'яті!\n"
                f"RAM: {mem}% | Swap: {sys_info['swap_percent']}%"
            )

    # ── Process crashes (zombie / stopped processes) ──
    zombie_count = 0
    for p in psutil.process_iter(["pid", "name", "status"]):
        try:
            if p.info["status"] == psutil.STATUS_ZOMBIE:
                zombie_count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if zombie_count > 5:
        key = "zombie_procs"
        if not alerts.is_active(key) or alerts.should_send(key, "service"):
            alerts.set_active(key)
            send_telegram(
                f"⚠️ <b>ЗОМБІ-ПРОЦЕСИ: {zombie_count}</b>\n"
                f"<i>{now_str}</i>\n"
                f"Виявлено багато зомбі-процесів — батьківський процес міг збанкрутувати"
            )
    else:
        if alerts.is_active("zombie_procs"):
            alerts.clear_active("zombie_procs")


def format_telegram_report(sys_info, ollama_info):
    cpu_bar = format_bar(sys_info["cpu_percent"])
    mem_bar = format_bar(sys_info["memory_percent"])
    disk_bar = format_bar(sys_info["disk_percent"])
    swap_bar = format_bar(sys_info["swap_percent"])

    emoji_cpu = "🟢" if sys_info["cpu_percent"] < 50 else ("🟡" if sys_info["cpu_percent"] < 80 else "🔴")
    emoji_mem = "🟢" if sys_info["memory_percent"] < 70 else ("🟡" if sys_info["memory_percent"] < 90 else "🔴")
    emoji_disk = "🟢" if sys_info["disk_percent"] < 70 else ("🟡" if sys_info["disk_percent"] < 90 else "🔴")

    models_list = ""
    for m in ollama_info.get("models", []):
        size_gb = m.get("size", 0) / (1024**3)
        models_list += f"  • <code>{m['name']}</code> ({size_gb:.1f}GB)\n"

    running_list = ""
    for m in ollama_info.get("running", []):
        running_list += f"  • <code>{m.get('name', '?')}</code>\n"

    top_procs = ""
    for p in sys_info["processes"][:5]:
        name = (p.get("name") or "?")[:15]
        top_procs += f"  • {name}: CPU {p.get('cpu_percent', 0):.1f}% | RAM {p.get('memory_percent', 0):.1f}%\n"

    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    msg = f"""<b>🖥 Звіт по серверу</b>
<i>{now}</i>

━━━━━━━━━━━━━━━━━━━━━━

<b>{emoji_cpu} CPU</b>  <code>{sys_info['cpu_percent']}%</code>
<code>{cpu_bar}</code>
  Ядра: {sys_info['cpu_count']} | Частота: {sys_info['cpu_freq']}MHz
  Навантаження: {sys_info['load_1']} / {sys_info['load_5']} / {sys_info['load_15']}

<b>{emoji_mem} RAM</b>  <code>{sys_info['memory_used']}/{sys_info['memory_total']}GB ({sys_info['memory_percent']}%)</code>
<code>{mem_bar}</code>

<b>🔄 Swap</b>  <code>{sys_info['swap_used']}/{sys_info['swap_total']}GB ({sys_info['swap_percent']}%)</code>
<code>{swap_bar}</code>

<b>{emoji_disk} Диск</b>  <code>{sys_info['disk_used']}/{sys_info['disk_total']}GB ({sys_info['disk_percent']}%)</code>
<code>{disk_bar}</code>

<b>🌐 Мережа</b>
  ↑ {sys_info['net_sent']}MB | ↓ {sys_info['net_recv']}MB

<b>⏱ Аптайм:</b> <code>{sys_info['uptime']}</code>

━━━━━━━━━━━━━━━━━━━━━━

<b>🤖 Ollama</b>  {'<code>ONLINE</code>' if ollama_info['status'] == 'online' else '<code>OFFLINE</code>'}

<b>📦 Моделі ({len(ollama_info.get("models", []))}):</b>
{models_list if models_list else "  (немає)\n"}

<b>🏃 Завантажені:</b>
{running_list if running_list else "  (немає)\n"}

━━━━━━━━━━━━━━━━━━━━━━

<b>⚙️ Топ процеси:</b>
{top_procs if top_procs else "  (немає)\n"}"""

    return msg


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/graphs")
def graphs():
    return render_template("graphs.html")


@app.route("/api/data")
def api_data():
    sys_info = get_system_info()
    ollama_info = get_ollama_info()
    # Record history for graphs
    with history_lock:
        history.append({
            "t": sys_info["timestamp"],
            "cpu": sys_info["cpu_percent"],
            "ram": sys_info["memory_percent"],
            "swap": sys_info["swap_percent"],
            "disk": sys_info["disk_percent"],
            "net_sent": sys_info["net_sent"],
            "net_recv": sys_info["net_recv"],
            "load": sys_info["load_1"],
        })
    return jsonify({"system": sys_info, "ollama": ollama_info})


@app.route("/api/history")
def api_history():
    with history_lock:
        data = list(history)
    return jsonify(data)


# ── Update monitor endpoints ──
# One-time tokens for secure password exchange
_update_tokens = {}  # token -> expiry timestamp
_update_lock = threading.Lock()
_update_running = False
_update_log = []


@app.route("/api/update/token")
def update_token():
    """Generate a one-time token for password encryption (valid 60s)"""
    token = secrets.token_hex(32)
    with _update_lock:
        _update_tokens[token] = time.time() + 60
    return jsonify({"token": token})


def _encrypt_password(password, token):
    """XOR encrypt password bytes with token bytes, return base64"""
    p = password.encode("utf-8")
    t = token.encode("utf-8")
    encrypted = bytes(a ^ b for a, b in zip(p, t * (len(p) // len(t) + 1)))
    return base64.b64encode(encrypted).decode("ascii")


def _decrypt_password(enc_b64, token):
    """Reverse XOR decryption"""
    encrypted = base64.b64decode(enc_b64)
    t = token.encode("utf-8")
    decrypted = bytes(a ^ b for a, b in zip(encrypted, t * (len(encrypted) // len(t) + 1)))
    return decrypted.decode("utf-8", errors="replace")


@app.route("/api/update/run", methods=["POST"])
def update_run():
    """Run system update with encrypted password"""
    global _update_running
    data = request.get_json()
    if not data or "enc_pwd" not in data or "token" not in data:
        return jsonify({"ok": False, "error": "missing params"}), 400

    token = data["token"]
    # Validate token
    with _update_lock:
        if token not in _update_tokens or time.time() > _update_tokens[token]:
            return jsonify({"ok": False, "error": "invalid or expired token"}), 403
        del _update_tokens[token]  # consume — one-time use

    password = _decrypt_password(data["enc_pwd"], token)

    if _update_running:
        return jsonify({"ok": False, "error": "update already in progress"}), 409

    def run_update():
        global _update_running
        _update_running = True
        _update_log.clear()
        try:
            # Check for updates
            _update_log.append({"t": datetime.now().strftime("%H:%M:%S"), "msg": "Перевірка оновлень..."})
            result = subprocess.run(
                ["sudo", "-S", "dnf", "check-update", "--refresh"],
                input=password + "\n", capture_output=True, text=True, timeout=120,
            )
            updates = [l for l in result.stdout.strip().split("\n") if l and not l.startswith("Last") and not l.startswith("Obsoleting") and l.strip()]
            if result.returncode == 0:
                _update_log.append({"t": datetime.now().strftime("%H:%M:%S"), "msg": "Оновлень не знайдено ✓"})
                _update_running = False
                return

            _update_log.append({"t": datetime.now().strftime("%H:%M:%S"), "msg": f"Знайдено {len(updates)} пакетів для оновлення"})

            # Apply updates
            _update_log.append({"t": datetime.now().strftime("%H:%M:%S"), "msg": "Запуск оновлення..."})
            result = subprocess.run(
                ["sudo", "-S", "dnf", "upgrade", "-y"],
                input=password + "\n", capture_output=True, text=True, timeout=600,
            )

            for line in result.stdout.split("\n"):
                line = line.strip()
                if line and ("Upgrading" in line or "Installing" in line or "Complete" in line or "Error" in line):
                    _update_log.append({"t": datetime.now().strftime("%H:%M:%S"), "msg": line})

            if result.returncode == 0:
                _update_log.append({"t": datetime.now().strftime("%H:%M:%S"), "msg": "Оновлення завершено успішно ✓"})
            else:
                _update_log.append({"t": datetime.now().strftime("%H:%M:%S"), "msg": f"Помилка: {result.stderr[-200:]}"})

        except subprocess.TimeoutExpired:
            _update_log.append({"t": datetime.now().strftime("%H:%M:%S"), "msg": "Таймаут оновлення (10 хв)"})
        except Exception as e:
            _update_log.append({"t": datetime.now().strftime("%H:%M:%S"), "msg": f"Помилка: {str(e)}"})
        finally:
            _update_running = False

    t = threading.Thread(target=run_update, daemon=True)
    t.start()
    return jsonify({"ok": True, "msg": "Оновлення запущено"})


@app.route("/api/update/status")
def update_status():
    """Get update progress"""
    return jsonify({"running": _update_running, "log": list(_update_log)})


# ── System logs endpoint ──
@app.route("/api/logs")
def api_logs():
    lines = int(request.args.get("lines", 100))
    unit = request.args.get("unit", "")
    try:
        cmd = ["journalctl", "--no-pager", "-n", str(lines), "--output=short-iso"]
        if unit:
            cmd.extend(["-u", unit])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        entries = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split(None, 5)
            if len(parts) >= 5:
                entries.append({
                    "time": parts[0] + " " + parts[1],
                    "host": parts[2],
                    "service": parts[3].rstrip(":"),
                    "msg": parts[4] if len(parts) > 4 else ""
                })
            else:
                entries.append({"time": "", "host": "", "service": "", "msg": line})
        return jsonify({"entries": entries})
    except Exception as e:
        return jsonify({"entries": [{"time": "", "host": "", "service": "error", "msg": str(e)}]})


# ── Reboot endpoints ──
_reboot_tokens = {}
_reboot_running = False
_reboot_log = []


@app.route("/api/reboot/token")
def reboot_token():
    token = secrets.token_hex(32)
    with _update_lock:
        _update_tokens[token] = time.time() + 60
    return jsonify({"token": token})


@app.route("/api/reboot/run", methods=["POST"])
def reboot_run():
    global _reboot_running
    data = request.get_json()
    if not data or "enc_pwd" not in data or "token" not in data:
        return jsonify({"ok": False, "error": "missing params"}), 400

    token = data["token"]
    with _update_lock:
        if token not in _update_tokens or time.time() > _update_tokens[token]:
            return jsonify({"ok": False, "error": "invalid or expired token"}), 403
        del _update_tokens[token]

    password = _decrypt_password(data["enc_pwd"], token)

    if _reboot_running:
        return jsonify({"ok": False, "error": "reboot already in progress"}), 409

    def do_reboot():
        global _reboot_running
        _reboot_running = True
        _reboot_log.clear()
        try:
            _reboot_log.append({"t": datetime.now().strftime("%H:%M:%S"), "msg": "Перезавантаження системи..."})
            _reboot_log.append({"t": datetime.now().strftime("%H:%M:%S"), "msg": "Сервер перезавантажується. Сторінка буде недоступна."})
            send_telegram("🔄 <b>СИСТЕМА ПЕРЕЗАВАНТАЖУЄТЬСЯ</b>\n<i>" + datetime.now().strftime("%H:%M:%S") + "</i>\n\nСервер перезавантажується за запитом адміністратора.")
            subprocess.Popen(
                ["sudo", "-S", "reboot"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            _reboot_log.append({"t": datetime.now().strftime("%H:%M:%S"), "msg": f"Помилка: {str(e)}"})
            _reboot_running = False

    t = threading.Thread(target=do_reboot, daemon=True)
    t.start()
    return jsonify({"ok": True, "msg": "Перезавантаження запущено"})


@app.route("/api/reboot/status")
def reboot_status():
    return jsonify({"running": _reboot_running, "log": list(_reboot_log)})


@app.route("/api/telegram")
def api_telegram():
    sys_info = get_system_info()
    ollama_info = get_ollama_info()
    msg = format_telegram_report(sys_info, ollama_info)
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
        return jsonify({"ok": r.ok, "status": r.status_code})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def alert_monitor():
    """Background thread: check for anomalies every 30 seconds"""
    while True:
        try:
            check_alerts()
        except Exception as e:
            print(f"Alert check error: {e}")
        time.sleep(30)


def telegram_reporter():
    """Background thread: send full report every 30 minutes"""
    while True:
        time.sleep(1800)
        try:
            sys_info = get_system_info()
            ollama_info = get_ollama_info()
            msg = format_telegram_report(sys_info, ollama_info)
            send_telegram(msg)
        except Exception as e:
            print(f"Report error: {e}")


if __name__ == "__main__":
    # Start alert monitor (every 30s)
    t1 = threading.Thread(target=alert_monitor, daemon=True)
    t1.start()

    # Start periodic report (every 30 min)
    t2 = threading.Thread(target=telegram_reporter, daemon=True)
    t2.start()

    print("Dashboard started with proactive alerting")
    print(f"  Alert check: every 30s")
    print(f"  Full report: every 30min")
    print(f"  CPU threshold: {THRESHOLDS['cpu_high']}% warn / {THRESHOLDS['cpu_critical']}% crit")
    print(f"  RAM threshold: {THRESHOLDS['mem_high']}% warn / {THRESHOLDS['mem_critical']}% crit")
    print(f"  Disk threshold: {THRESHOLDS['disk_warning']}% warn / {THRESHOLDS['disk_critical']}% crit")

    app.run(host="0.0.0.0", port=8080)

"""
HomeOS — modules/sysinfo.py
Détection OS, hostname, ressources système (CPU, RAM, disque, température).
Fonctionne sur Linux (RPi) et macOS/Windows — s'adapte automatiquement.
"""

import platform
import socket
import os
import subprocess
import time
import shutil

try:
    import psutil
    HAS_PSUTIL = True
    # Amorce le compteur cpu_percent en mode non-bloquant : le premier appel
    # renvoie 0.0, les suivants renvoient le % CPU écoulé depuis l'appel précédent.
    # Évite le time.sleep(interval) bloquant à chaque tick (~200 ms gaspillés/tick
    # sur RPi). La fenêtre = l'intervalle réel entre ticks update_system (~8 s),
    # ce qui donne une moyenne plus représentative qu'un échantillon de 0,2 s.
    psutil.cpu_percent(interval=None)
except ImportError:
    HAS_PSUTIL = False


def get_host_info() -> dict:
    """Retourne un dict avec hostname, os, os_name, arch, python et is_rpi."""
    uname = platform.uname()
    return {
        "hostname": socket.gethostname(),
        "os":       f"{uname.system} {uname.release}",
        "os_name":  uname.system,          # 'Linux', 'Darwin', 'Windows'
        "arch":     uname.machine,         # 'aarch64', 'x86_64', ...
        "python":   platform.python_version(),
        "is_rpi":   os.path.exists("/proc/device-tree/model"),
    }


def get_rpi_model() -> str:
    """Lit le modèle RPi depuis /proc/device-tree/model si disponible."""
    try:
        with open("/proc/device-tree/model", "r") as f:
            return f.read().strip().replace("\x00", "")
    except Exception:
        return ""


def get_system_label() -> str:
    """
    Retourne un label court pour affichage dans le footer / topbar.
    Ex: 'raspberrypi · Raspberry Pi 4 Model B' ou 'DESKTOP-XYZ · Windows 11'
    """
    info = get_host_info()
    host = info["hostname"]
    rpi_model = get_rpi_model()
    if rpi_model:
        # Raccourcir le modèle RPi
        short = rpi_model.replace("Raspberry Pi ", "RPi ").split(" Rev")[0]
        return f"{host} · {short}"
    return f"{host} · {info['os']}"


def get_resources() -> dict:
    """
    Retourne les métriques système.
    Utilise psutil si disponible, sinon fallback /proc.
    """
    if HAS_PSUTIL:
        cpu = psutil.cpu_percent(interval=None)  # non-bloquant (cf. amorçage à l'import)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        # Lecture directe d'un seul fichier sysfs d'abord (1 open vs scan de tous
        # les hwmon par psutil.sensors_temperatures, ~33 ms sur RPi). Fallback psutil
        # si le chemin thermal_zone n'existe pas (macOS, certains Linux).
        temp_cpu = _get_cpu_temp_proc()
        if temp_cpu is None:
            temp_cpu = _get_cpu_temp_psutil()
        net = psutil.net_io_counters()
        return {
            "cpu_pct":    round(cpu),
            "ram_pct":    round(ram.percent),
            "ram_used_mb": round(ram.used / 1024 / 1024),
            "ram_total_mb": round(ram.total / 1024 / 1024),
            "disk_pct":   round(disk.percent),
            "disk_used_gb": round(disk.used / 1024**3, 1),
            "disk_total_gb": round(disk.total / 1024**3, 1),
            "temp_cpu":   temp_cpu,
            "net_sent_mb": round(net.bytes_sent / 1024 / 1024, 1),
            "net_recv_mb": round(net.bytes_recv / 1024 / 1024, 1),
            "available":  True,
        }
    else:
        return _resources_fallback()


def _get_cpu_temp_psutil() -> float | None:
    """Température CPU via psutil (RPi, Linux, macOS avec outils tiers)."""
    try:
        temps = psutil.sensors_temperatures()
        if not temps:
            return None
        # RPi : 'cpu_thermal', Linux générique : 'coretemp', macOS : absent
        for key in ("cpu_thermal", "coretemp", "k10temp", "acpitz"):
            if key in temps and temps[key]:
                return round(temps[key][0].current, 1)
        # Fallback : premier capteur disponible
        for entries in temps.values():
            if entries:
                return round(entries[0].current, 1)
    except Exception:
        pass
    return _get_cpu_temp_proc()


def _get_cpu_temp_proc() -> float | None:
    """Lecture directe de la température via /sys (Linux/RPi uniquement)."""
    paths = [
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/devices/virtual/thermal/thermal_zone0/temp",
    ]
    for p in paths:
        try:
            with open(p) as f:
                return round(int(f.read().strip()) / 1000, 1)
        except Exception:
            continue
    return None


def _resources_fallback() -> dict:
    """Fallback minimal sans psutil (lecture /proc)."""
    cpu = _read_proc_cpu()
    ram_pct, ram_used, ram_total = _read_proc_mem()
    disk = shutil.disk_usage("/")
    return {
        "cpu_pct":    cpu,
        "ram_pct":    ram_pct,
        "ram_used_mb": ram_used,
        "ram_total_mb": ram_total,
        "disk_pct":   round(disk.used / disk.total * 100),
        "disk_used_gb": round(disk.used / 1024**3, 1),
        "disk_total_gb": round(disk.total / 1024**3, 1),
        "temp_cpu":   _get_cpu_temp_proc(),
        "net_sent_mb": 0.0,
        "net_recv_mb": 0.0,
        "available":  True,
    }


def _read_proc_cpu() -> int:
    """Calcule l'usage CPU (%) depuis /proc/stat en prenant deux mesures à 200 ms d'intervalle."""
    try:
        with open("/proc/stat") as f:
            line = f.readline().split()
        vals = [int(x) for x in line[1:]]
        idle1 = vals[3]
        total1 = sum(vals)
        time.sleep(0.2)
        with open("/proc/stat") as f:
            line = f.readline().split()
        vals = [int(x) for x in line[1:]]
        idle2 = vals[3]
        total2 = sum(vals)
        return round(100 * (1 - (idle2 - idle1) / (total2 - total1)))
    except Exception:
        return 0


def _read_proc_mem() -> tuple:
    """Lit MemTotal/MemAvailable dans /proc/meminfo. Retourne (pct, used_mb, total_mb)."""
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k.strip()] = int(v.strip().split()[0])
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", 0)
        used = total - available
        pct = round(used / total * 100) if total else 0
        return pct, round(used / 1024), round(total / 1024)
    except Exception:
        return 0, 0, 0


def check_systemctl(service: str) -> str:
    """
    Interroge systemctl pour l'état d'un service Linux.
    Retourne 'active', 'inactive', 'failed', ou 'unknown' (systemctl absent / erreur).
    """
    try:
        r = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True, text=True, timeout=1,
        )
        return r.stdout.strip()
    except FileNotFoundError:
        return "unknown"   # systemctl non disponible (macOS, Windows…)
    except Exception:
        return "unknown"


def check_process(name: str) -> bool:
    """Retourne True si un processus dont le nom contient `name` est en cours d'exécution."""
    if not HAS_PSUTIL:
        return False
    try:
        for proc in psutil.process_iter(["name"]):
            if proc.info["name"] and name.lower() in proc.info["name"].lower():
                return True
    except Exception:
        pass
    return False


def check_tcp(host: str, port: int, timeout: float = 0.8) -> bool:
    """Retourne True si une connexion TCP vers host:port s'établit dans le délai imparti."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def get_system_uptime() -> int:
    """
    Retourne l'uptime machine en secondes (depuis le dernier démarrage de l'OS).
    Utilise psutil.boot_time() si disponible, sinon lit /proc/uptime (Linux).
    """
    if HAS_PSUTIL:
        return int(time.time() - psutil.boot_time())
    # Fallback Linux : première colonne de /proc/uptime = secondes depuis boot
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except Exception:
        return 0


# Singleton — infos statiques chargées une fois au démarrage
HOST_INFO = get_host_info()
SYSTEM_LABEL = get_system_label()

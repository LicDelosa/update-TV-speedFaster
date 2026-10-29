#!/usr/bin/env python3
"""Локальный оптимизатор своих Android TV по уже включённому сетевому ADB.

Подходит для Linux, Windows и Android/Termux: нужен только Python 3.8+ и adb.
Не ставит root, не использует эксплойты и не обходит запрос отладки на экране.
Samsung, LG webOS, Philips без ADB и Titan OS только показываются, но не меняются.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ADB_PORT = 5555
PROBE_PORTS = {
    5555: "ADB",
    8008: "Android/Cast",
    8009: "Android/Cast TLS",
    1925: "Philips JointSpace",
    8001: "Samsung",
    8002: "Samsung TLS",
    3000: "LG webOS?",
    3001: "LG webOS?",
}

# Пакеты, которые нельзя гасить: без них пропадает меню, пульт или магазин.
PROTECTED = {
    "android",
    "com.android.systemui",
    "com.android.tv",
    "com.android.tv.settings",
    "com.android.settings",
    "com.android.vending",
    "com.android.bluetooth",
    "com.android.providers.settings",
    "com.android.providers.tv",
    "com.google.android.gms",
    "com.google.android.gsf",
    "com.google.android.webview",
    "com.google.android.youtube.tv",
    "com.asd.launcher",
    "com.asd.ohdkeyboard",
    "com.asd.dlnaprovider",
    "com.asd.plugin.internet",
    "com.asd.plugin.video",
    "com.asd.plugin.explorer",
    "abox.plugin.youtube",
    "com.hikeen.menu",
    "com.hikeen.Quickmenu",
    "com.hikeen.scrollhelper",
    "com.realtek.dtv",
    "com.realtek.tv.atv",
    "com.realtek.tv.settings",
    "com.realtek.providers.tv",
    "com.rtk.android.miracast",
}

# Проверено на Telefunken TF-LED65S02T2SU / Zeasn / Hikeen / Realtek.
ZEASN_DISABLE = [
    "abox.statistic",
    "abox.internetsearch",
    "abox.videocatalog",
    "abox.playlayer",
    "abox.store.client",
    "abox.license",
    "com.zeasn.services.general",
    "com.zeasn.services.general.overlay",
    "com.zeasn.deviceportal.asdprovider.hikeen",
    "cn.zeasn.overseas.tv.hikeen.orion",
    "cn.zeasn.overseas.tv.hikeen.orion.overlay",
    "com.hikeen.cleanmaster",
    "com.hikeen.hotelmenu",
    "com.hikeen.setupwizard",
    "com.microimpulse.payment",
    "com.adups.fota",
    "com.adups.fota.overlay",
    "com.asd.urlshower",
    "com.asd.httpimagebrowser",
    "com.asd.ohdcloudprovider",
    "com.asd.wallpaper.slideshow",
    "ru.openhd.widgets",
    "ag.tv.a24h",
    "com.fitstars.tv",
    "com.teamsmart.videomanager.tv",
    "karaoke.plugin.asd.com.karaoke",
    "com.google.android.leanbacklauncher.recommendations",
    "com.google.android.tungsten.setupwraith",
    "com.android.traceur",
    "com.google.android.katniss",
]

# На неизвестной прошивке гасим только явно служебный мусор.
GENERIC_DISABLE = [
    "com.android.traceur",
    "com.google.android.tungsten.setupwraith",
]

# Не удалять: пользователь может ими пользоваться. Только запрет фона.
BACKGROUND_LIMIT = [
    "ru.kinopoisk.tv",
    "ru.start.androidtv",
    "ru.ivi.client",
    "tv.okko.androidtv",
    "gpm.tnt_premier",
    "com.vk.tv",
    "ru.openhd.video",
    "com.android.dreams.phototable",
    "com.android.dreams.basic",
]

PROP_KEYS = [
    "ro.product.brand",
    "ro.product.manufacturer",
    "ro.product.model",
    "ro.product.device",
    "ro.build.version.release",
    "ro.build.version.sdk",
    "ro.build.type",
    "ro.debuggable",
    "ro.hardware",
    "ro.board.platform",
]


def log_dir() -> Path:
    path = Path(__file__).resolve().parent / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_adb(explicit: str | None) -> str:
    if explicit:
        if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return explicit
        raise SystemExit(f"adb не найден: {explicit}")
    found = shutil.which("adb")
    if found:
        return found
    candidates = [
        "/tmp/adb-tools/platform-tools/adb",
        os.path.expanduser("~/platform-tools/adb"),
        os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
        r"C:\platform-tools\adb.exe",
        os.path.expanduser(r"~\AppData\Local\Android\Sdk\platform-tools\adb.exe"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    raise SystemExit(
        "adb не найден в PATH.\n"
        "Linux: sudo apt install adb\n"
        "Windows: https://developer.android.com/tools/releases/platform-tools "
        "и добавьте platform-tools в PATH\n"
        "Termux: pkg install android-tools"
    )


def run(cmd: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def local_networks() -> list[ipaddress.IPv4Network]:
    networks: list[ipaddress.IPv4Network] = []
    if sys.platform.startswith("linux") or sys.platform == "darwin":
        proc = run(["ip", "-4", "-o", "addr", "show"], timeout=8)
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                parts = line.split()
                if "inet" not in parts:
                    continue
                iface = parts[1]
                if iface.startswith(("lo", "docker", "tun", "br-", "veth", "zt")):
                    continue
                cidr = parts[parts.index("inet") + 1]
                try:
                    net = ipaddress.ip_network(cidr, strict=False)
                except ValueError:
                    continue
                if net.is_private and net.prefixlen >= 16:
                    networks.append(net)
    if sys.platform.startswith("win"):
        proc = run(["ipconfig"], timeout=8)
        current_ip = None
        for raw in proc.stdout.splitlines():
            line = raw.strip()
            if "IPv4" in line and ":" in line:
                current_ip = line.split(":")[-1].strip()
            elif line.startswith("Subnet Mask") and current_ip:
                mask = line.split(":")[-1].strip()
                try:
                    iface = ipaddress.IPv4Interface(f"{current_ip}/{mask}")
                    if iface.network.is_private:
                        networks.append(iface.network)
                except ValueError:
                    pass
                current_ip = None
    if not networks:
        # Запасной вариант: адрес, с которого уходит маршрут в LAN, плюс /24.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("192.168.0.1", 80))
            ip = sock.getsockname()[0]
            networks.append(ipaddress.ip_network(f"{ip}/24", strict=False))
        except OSError:
            pass
        finally:
            sock.close()
    unique: list[ipaddress.IPv4Network] = []
    seen = set()
    for net in networks:
        if net.prefixlen < 22:
            # Гостевые и VPN /16 не сканируем целиком.
            continue
        if net not in seen:
            seen.add(net)
            unique.append(net)
    return unique


def probe_port(ip: str, port: int, timeout: float) -> bool:
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((ip, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def scan_hosts(networks: list[ipaddress.IPv4Network], timeout: float, workers: int) -> dict[str, list[int]]:
    hosts = []
    own = set()
    for net in networks:
        # Свой адрес не считаем телевизором.
        try:
            hostname_ip = socket.gethostbyname(socket.gethostname())
            own.add(hostname_ip)
        except OSError:
            pass
        for host in net.hosts():
            hosts.append(str(host))
    found: dict[str, list[int]] = {}
    tasks = [(ip, port) for ip in hosts for port in PROBE_PORTS]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(probe_port, ip, port, timeout): (ip, port) for ip, port in tasks
        }
        for future in as_completed(future_map):
            ip, port = future_map[future]
            try:
                opened = future.result()
            except Exception:
                opened = False
            if opened and ip not in own:
                found.setdefault(ip, []).append(port)
    for ports in found.values():
        ports.sort()
    return dict(sorted(found.items(), key=lambda item: ipaddress.ip_address(item[0])))


def adb_connect(adb: str, ip: str) -> tuple[bool, str]:
    serial = f"{ip}:{ADB_PORT}"
    proc = run([adb, "connect", serial], timeout=12)
    text = (proc.stdout + proc.stderr).strip()
    state = run([adb, "-s", serial, "get-state"], timeout=8)
    ok = state.stdout.strip() == "device"
    return ok, text or state.stdout.strip() or state.stderr.strip()


def adb_shell(adb: str, serial: str, script: str, timeout: int = 40) -> str:
    proc = run([adb, "-s", serial, "shell", script], timeout=timeout)
    return (proc.stdout or "") + (proc.stderr or "")


def device_props(adb: str, serial: str) -> dict[str, str]:
    script = "; ".join(f'echo {key}=$(getprop {key})' for key in PROP_KEYS)
    out = adb_shell(adb, serial, script, timeout=20)
    props = {}
    for line in out.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            props[key.strip()] = value.strip()
    return props


def package_sets(adb: str, serial: str) -> tuple[set[str], set[str]]:
    enabled = set()
    disabled = set()
    out = adb_shell(adb, serial, "pm list packages -e; echo '---'; pm list packages -d", timeout=40)
    bucket = enabled
    for line in out.splitlines():
        if line.strip() == "---":
            bucket = disabled
            continue
        if line.startswith("package:"):
            bucket.add(line.split(":", 1)[1].strip())
    return enabled, disabled


def detect_profile(packages: set[str], props: dict[str, str]) -> str:
    markers = ("com.zeasn.", "abox.", "com.hikeen.", "com.asd.launcher")
    if any(pkg == marker or pkg.startswith(marker) for pkg in packages for marker in markers):
        return "zeasn-hikeen"
    return "android-tv-generic"


def build_plan(profile: str, enabled: set[str], limit_background: bool) -> dict:
    source = ZEASN_DISABLE if profile == "zeasn-hikeen" else GENERIC_DISABLE
    disable = [pkg for pkg in source if pkg in enabled and pkg not in PROTECTED]
    skipped = [pkg for pkg in source if pkg in enabled and pkg in PROTECTED]
    background = [pkg for pkg in BACKGROUND_LIMIT if pkg in enabled] if limit_background else []
    return {"disable": disable, "background": background, "protected_skip": skipped}


def apply_plan(adb: str, serial: str, plan: dict) -> list[str]:
    lines = ["set +e"]
    for pkg in plan["disable"]:
        lines.append(
            f'pm disable-user --user 0 {pkg} >/dev/null 2>&1 && echo OFF {pkg} || echo FAIL {pkg}'
        )
        lines.append(f"am force-stop {pkg} >/dev/null 2>&1")
    for pkg in plan["background"]:
        lines.append(f"cmd appops set {pkg} RUN_IN_BACKGROUND ignore >/dev/null 2>&1 || true")
        lines.append(f"cmd appops set {pkg} RUN_ANY_IN_BACKGROUND ignore >/dev/null 2>&1 || true")
        lines.append(f"am force-stop {pkg} >/dev/null 2>&1")
        lines.append(f"echo BG {pkg}")
    lines.append("settings put global window_animation_scale 0")
    lines.append("settings put global transition_animation_scale 0")
    lines.append("settings put global animator_duration_scale 0")
    lines.append("echo ANIM_OK")
    lines.append(
        "cmd package resolve-activity --brief -a android.intent.action.MAIN "
        "-c android.intent.category.HOME | tail -n 1"
    )
    return adb_shell(adb, serial, "\n".join(lines), timeout=90).splitlines()


def save_log(ip: str, props: dict, profile: str, plan: dict, results: list[str]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = log_dir() / f"{ip.replace('.', '_')}-{stamp}.json"
    path.write_text(
        json.dumps(
            {
                "ip": ip,
                "time": stamp,
                "props": props,
                "profile": profile,
                "plan": plan,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def revert(adb: str, log_path: Path) -> None:
    data = json.loads(log_path.read_text(encoding="utf-8"))
    ip = data["ip"]
    ok, message = adb_connect(adb, ip)
    if not ok:
        raise SystemExit(f"Нет ADB к {ip}: {message}")
    serial = f"{ip}:{ADB_PORT}"
    pkgs = data.get("plan", {}).get("disable", [])
    lines = ["set +e"]
    for pkg in pkgs:
        lines.append(f'pm enable --user 0 {pkg} >/dev/null 2>&1 && echo ON {pkg} || echo FAIL {pkg}')
    for pkg in data.get("plan", {}).get("background", []):
        lines.append(f"cmd appops set {pkg} RUN_IN_BACKGROUND allow >/dev/null 2>&1 || true")
        lines.append(f"echo BG_ALLOW {pkg}")
    lines.append("settings put global window_animation_scale 1")
    lines.append("settings put global transition_animation_scale 1")
    lines.append("settings put global animator_duration_scale 1")
    print(adb_shell(adb, serial, "\n".join(lines), timeout=90))


def describe(ip: str, ports: list[int], info: dict | None) -> str:
    labels = ", ".join(PROBE_PORTS[port] for port in ports if port in PROBE_PORTS)
    if not info:
        return f"{ip:16}  порты: {labels or '-'}  — ADB нет, прошивка не Android-отладка, изменений не будет"
    model = info["props"].get("ro.product.model") or "?"
    brand = info["props"].get("ro.product.brand") or "?"
    release = info["props"].get("ro.build.version.release") or "?"
    return (
        f"{ip:16}  {brand} {model}  Android {release}  "
        f"профиль {info['profile']}  ADB"
    )


def collect(adb: str, found: dict[str, list[int]]) -> tuple[list[dict], list[tuple[str, list[int]]]]:
    ready = []
    skipped = []
    for ip, ports in found.items():
        if ADB_PORT not in ports:
            skipped.append((ip, ports))
            continue
        ok, message = adb_connect(adb, ip)
        if not ok:
            skipped.append((ip, ports))
            print(f"  {ip}: порт 5555 открыт, но adb не пустил ({message})", file=sys.stderr)
            continue
        serial = f"{ip}:{ADB_PORT}"
        props = device_props(adb, serial)
        enabled, disabled = package_sets(adb, serial)
        profile = detect_profile(enabled | disabled, props)
        ready.append(
            {
                "ip": ip,
                "serial": serial,
                "props": props,
                "enabled": enabled,
                "disabled": disabled,
                "profile": profile,
            }
        )
    return ready, skipped


def ask(prompt: str, default: str | None = None) -> str:
    if not sys.stdin.isatty():
        if default is None:
            raise SystemExit("Нет интерактивного терминала. Укажите --ip и --yes.")
        return default
    value = input(prompt).strip()
    return value or (default or "")


def upgrade_one(adb: str, item: dict, assume_yes: bool, limit_background: bool, do_reboot: bool) -> None:
    plan = build_plan(item["profile"], item["enabled"], limit_background)
    print()
    print(describe(item["ip"], [ADB_PORT], item))
    print(f"К отключению: {len(plan['disable'])}")
    for pkg in plan["disable"]:
        print(f"  - {pkg}")
    if plan["background"]:
        print("Запрет фоновой работы, приложения остаются:")
        for pkg in plan["background"]:
            print(f"  ~ {pkg}")
    if not plan["disable"] and not plan["background"]:
        print("Подходящих включённых пакетов нет. Возможно, чистка уже сделана.")
        return
    if item["profile"] != "zeasn-hikeen":
        print("Это не профиль Zeasn/Hikeen. Будут выключены только безопасные служебные пакеты.")
    if not assume_yes:
        answer = ask("Применить? [y/N] ", "n").lower()
        if answer not in {"y", "yes", "д", "да"}:
            print("Отменено.")
            return
    results = apply_plan(adb, item["serial"], plan)
    print("\n".join(results))
    path = save_log(item["ip"], item["props"], item["profile"], plan, results)
    print(f"Журнал отката: {path}")
    home = [line for line in results if "launcher" in line.lower() or "/" in line]
    if home:
        print(f"Главный экран: {home[-1]}")
    if do_reboot or (
        not assume_yes and ask("Перезагрузить телевизор? [y/N] ", "n").lower() in {"y", "yes", "д", "да"}
    ):
        run([adb, "-s", item["serial"], "reboot"], timeout=15)
        print("Телевизор перезагружается. ADB снова появится через 20–40 секунд.")


def cmd_scan(args: argparse.Namespace) -> int:
    networks = local_networks()
    if not networks:
        print("Локальная частная сеть не найдена.")
        return 1
    print("Сканирую: " + ", ".join(str(net) for net in networks))
    found = scan_hosts(networks, args.timeout, args.workers)
    if not found:
        print("Устройств с портами Smart TV не видно.")
        print("Телевизор может быть выключен, спать, сидеть в гостевой сети или не иметь сетевой отладки.")
        return 0
    adb = find_adb(args.adb)
    ready, skipped = collect(adb, found)
    print("\nМожно оптимизировать:")
    if not ready:
        print("  нет Android TV с доступным ADB")
    for index, item in enumerate(ready, 1):
        print(f"  {index}. {describe(item['ip'], found.get(item['ip'], []), item)}")
    if skipped:
        print("\nВидны, но эта программа их не меняет:")
        for ip, ports in skipped:
            print(f"  - {describe(ip, ports, None)}")
    return 0


def cmd_upgrade(args: argparse.Namespace) -> int:
    adb = find_adb(args.adb)
    if args.ip:
        found = {args.ip: [ADB_PORT]}
    else:
        networks = local_networks()
        if not networks:
            print("Локальная частная сеть не найдена.")
            return 1
        print("Сканирую: " + ", ".join(str(net) for net in networks))
        found = scan_hosts(networks, args.timeout, args.workers)
    ready, skipped = collect(adb, found)
    if skipped:
        print("Без ADB, пропуск:")
        for ip, ports in skipped:
            print(f"  - {describe(ip, ports, None)}")
    if not ready:
        print("Нет телевизора, к которому пускает ADB.")
        print("На Android TV: Настройки → О телевизоре → 7 нажатий на сборку → отладка по сети.")
        print("На guest Wi-Fi компьютер по кабелю телевизор не увидит.")
        return 1
    chosen = ready
    if not args.ip:
        print("\nТелевизоры:")
        for index, item in enumerate(ready, 1):
            print(f"  {index}. {describe(item['ip'], [ADB_PORT], item)}")
        if len(ready) == 1 and args.yes:
            pick = "1"
        else:
            pick = ask("Номер телевизора: ")
        if not pick.isdigit() or not (1 <= int(pick) <= len(ready)):
            print("Неверный номер.")
            return 1
        chosen = [ready[int(pick) - 1]]
    for item in chosen:
        upgrade_one(adb, item, args.yes, not args.no_background, args.reboot)
    return 0


def cmd_revert(args: argparse.Namespace) -> int:
    adb = find_adb(args.adb)
    revert(adb, Path(args.log))
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--adb", help="Путь к adb, если его нет в PATH")
    common.add_argument("--timeout", type=float, default=0.35, help="Таймаут проверки порта, секунды")
    common.add_argument("--workers", type=int, default=80, help="Число параллельных проверок")
    parser = argparse.ArgumentParser(
        description="Поиск своих Android TV в LAN и обратимое отключение рекламных пакетов через ADB.",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", help="Только найти устройства", parents=[common])
    scan.set_defaults(func=cmd_scan)

    upgrade = sub.add_parser("upgrade", help="Найти, выбрать и применить профиль", parents=[common])
    upgrade.add_argument("--ip", help="Не сканировать, взять этот адрес")
    upgrade.add_argument("--yes", action="store_true", help="Не спрашивать подтверждение")
    upgrade.add_argument("--reboot", action="store_true", help="Перезагрузить после применения")
    upgrade.add_argument(
        "--no-background",
        action="store_true",
        help="Не ограничивать фон Кинопоиска, START, Иви и похожих приложений",
    )
    upgrade.set_defaults(func=cmd_upgrade)

    back = sub.add_parser("revert", help="Включить пакеты из журнала", parents=[common])
    back.add_argument("log", help="Файл logs/*.json")
    back.set_defaults(func=cmd_revert)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "func", None):
        args = parser.parse_args(["upgrade", *sys.argv[1:]])
    try:
        return args.func(args)
    except subprocess.TimeoutExpired as exc:
        print(f"Команда зависла: {' '.join(exc.cmd)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nОстановлено.")
        return 130


if __name__ == "__main__":
    sys.exit(main())

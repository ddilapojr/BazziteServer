from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect

import asyncio
import os
import re
import shutil
import subprocess
from typing import Dict, List, Optional, Set

import psutil


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Bazzite Steam Gaming Mode services.
#
# Different Bazzite / Steam Gaming Mode versions can use different units.
GAME_MODE_SERVICES = (
    "gamescope-session-plus@steam.service",
    "gamescope-session-plus@ogui-steam.service",
)

# Steam installation.
STEAM_ROOT = os.path.expanduser(
    "~/.local/share/Steam"
)

# Steam library metadata locations.
STEAM_APPMANIFEST_NAME = "appmanifest_{appid}.acf"

# Steam non-Steam shortcuts database.
STEAM_SHORTCUTS_FILE = os.path.join(
    STEAM_ROOT,
    "userdata",
)

# Temperature polling does not require network access.
CPU_TEMP_MIN = 0
CPU_TEMP_MAX = 120

# Steam games are only reported while Gaming Mode is active when this is True.
# False also reports Steam games launched from Desktop Mode.
STEAM_REQUIRES_GAME_MODE = False

# Apps shown as the current activity when no Steam game is running.
# First match wins, so put the apps you care about most at the top.
#
#   (display name, type, process names, Flatpak IDs)
#
# type is "game" (turns the In Game sensor on) or "app" (does not).
# Process names are lowercase and are matched exactly. Flatpak IDs are matched
# against the command line of Flatpak's own launcher processes only (not of
# every process, so a shell command that merely mentions an app doesn't count).
# Add, remove or reorder freely.
FLATPAK_LAUNCHERS = ("bwrap", "flatpak")

KNOWN_APPS = [
    ("Jellyfin", "app",
     ["jellyfin-desktop", "jellyfinmediaplayer"],
     ["org.jellyfin.JellyfinDesktop", "org.jellyfin.JellyfinMediaPlayer"]),
    ("Plex", "app",
     ["plex", "plex htpc", "plexmediaplayer"],
     ["tv.plex.PlexDesktop", "tv.plex.PlexHTPC"]),
    ("Kodi", "app",
     ["kodi", "kodi.bin", "kodi-standalone"],
     ["tv.kodi.Kodi"]),
    ("VLC", "app",
     ["vlc"],
     ["org.videolan.VLC"]),
    ("Moonlight", "app",
     ["moonlight", "moonlight-qt"],
     ["com.moonlight_stream.Moonlight"]),
    ("RetroArch", "game", ["retroarch"], ["org.libretro.RetroArch"]),
    ("Dolphin", "game", ["dolphin-emu"], ["org.DolphinEmu.dolphin-emu"]),
    ("PCSX2", "game", ["pcsx2", "pcsx2-qt"], ["net.pcsx2.PCSX2"]),
    ("RPCS3", "game", ["rpcs3"], ["net.rpcs3.RPCS3"]),
    ("Heroic Games Launcher", "app", ["heroic"], ["com.heroicgameslauncher.hgl"]),
    ("Lutris", "app", ["lutris"], ["net.lutris.Lutris"]),
    ("Spotify", "app", ["spotify"], ["com.spotify.Client"]),
    ("Discord", "app", ["discord"], ["com.discordapp.Discord"]),
    ("Firefox", "app", ["firefox"], ["org.mozilla.firefox"]),
    ("Chrome", "app", ["chrome", "google-chrome"], ["com.google.Chrome"]),
    ("Brave", "app", ["brave", "brave-browser"], ["com.brave.Browser"]),
]


# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------

app = FastAPI()

websocket_connections: List[WebSocket] = []

last_status: Optional[str] = None
last_cpu_temp: Optional[float] = None
last_activity_type: Optional[str] = None

power_state = "on"  # "on", "sleep" or "shutdown"
inhibitor_proc = None
background_tasks: List[asyncio.Task] = []


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def run_command(
    command: List[str],
    timeout: float = 1.0,
) -> Optional[str]:
    """
    Run a local command and return stdout.

    Failure is treated as None.
    """

    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
        )

        if result.returncode != 0:
            return None

        return result.stdout.strip()

    except (
        OSError,
        subprocess.SubprocessError,
    ):
        return None


# ---------------------------------------------------------------------------
# Steam Gaming Mode detection
# ---------------------------------------------------------------------------

def is_game_mode_active() -> bool:
    """
    Return True only when Bazzite Gaming Mode is active.

    Steam itself is NOT considered proof of Gaming Mode.

    We intentionally don't use:
        - DISPLAY
        - WAYLAND_DISPLAY
        - Steam process existence
        - gamescope process existence
    """

    for service in GAME_MODE_SERVICES:
        output = run_command(
            [
                "systemctl",
                "--user",
                "is-active",
                service,
            ],
            timeout=0.75,
        )

        if output == "active":
            return True

    return False


# ---------------------------------------------------------------------------
# Steam process helpers
# ---------------------------------------------------------------------------

def get_process_commandline(
    proc: psutil.Process,
) -> Optional[List[str]]:
    """
    Safely obtain a process command line.
    """

    try:
        cmdline = proc.cmdline()

        if not cmdline:
            return None

        return cmdline

    except (
        psutil.NoSuchProcess,
        psutil.AccessDenied,
        psutil.ZombieProcess,
        OSError,
    ):
        return None


def get_steam_launch_processes() -> List[psutil.Process]:
    """
    Find Steam's local launch/reaper processes.

    Steam commonly launches applications through something similar to:

        reaper SteamLaunch AppId=123456 -- <command>

    This works for both:

        - normal Steam games
        - Proton games
        - Steam non-Steam games
        - Steam-launched Flatpak applications

    No network access is used.
    """

    processes: List[psutil.Process] = []

    for proc in psutil.process_iter(
        ["pid", "name", "cmdline"]
    ):
        try:
            cmdline = proc.info.get("cmdline")

            if not cmdline:
                continue

            command = " ".join(cmdline)

            # Steam's launch wrapper.
            if "SteamLaunch" not in command:
                continue

            if "AppId=" not in command:
                continue

            # Normally the executable is "reaper".
            if "reaper" not in command.lower():
                continue

            processes.append(proc)

        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            psutil.ZombieProcess,
            OSError,
        ):
            continue

    return processes


# ---------------------------------------------------------------------------
# Steam launch parsing
# ---------------------------------------------------------------------------

def parse_steam_launch(
    cmdline: List[str],
) -> Optional[Dict[str, str]]:
    """
    Parse a Steam launch command.

    Example:

        reaper SteamLaunch AppId=4284628683 -- \
        /usr/bin/flatpak run \
        --branch=stable \
        --arch=x86_64 \
        --command=jellyfin-desktop \
        org.jellyfin.JellyfinDesktop

    Returns:

        {
            "appid": "4284628683",
            "command": "/usr/bin/flatpak run ..."
        }
    """

    if not cmdline:
        return None

    command = " ".join(cmdline)

    match = re.search(
        r"\bSteamLaunch\s+AppId=([0-9]+)\s+--\s+(.+)$",
        command,
    )

    if not match:
        return None

    appid = match.group(1).strip()
    launch_command = match.group(2).strip()

    if not appid:
        return None

    if not launch_command:
        return None

    return {
        "appid": appid,
        "command": launch_command,
    }


# ---------------------------------------------------------------------------
# Steam local AppID validation
# ---------------------------------------------------------------------------

def is_valid_steam_launch_id(
    value: Optional[str],
) -> bool:
    """
    Validate a Steam launch identifier.

    IMPORTANT:

    We do NOT assume this is a Steam Store AppID.

    Steam non-Steam shortcuts can use IDs that don't correspond to
    Steam Store applications.

    Therefore this only validates that it is a positive decimal ID.
    """

    if value is None:
        return False

    value = str(value).strip()

    if not re.fullmatch(
        r"[1-9][0-9]*",
        value,
    ):
        return False

    try:
        return int(value) > 0

    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Local Steam metadata
# ---------------------------------------------------------------------------

def parse_acf_value(
    text: str,
    key: str,
) -> Optional[str]:
    """
    Extract a simple Steam ACF value.

    Example:

        "appid"      "123456"
        "name"       "Example Game"
    """

    pattern = (
        r'"'
        + re.escape(key)
        + r'"\s+"([^"]*)"'
    )

    match = re.search(
        pattern,
        text,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    value = match.group(1).strip()

    return value or None


def get_library_roots() -> List[str]:
    """
    Find local Steam library roots.

    Starts with the main Steam library and also reads libraryfolders.vdf
    when available.

    Everything is local.
    """

    roots: Set[str] = set()

    main_root = os.path.join(
        STEAM_ROOT,
        "steamapps",
    )

    if os.path.isdir(main_root):
        roots.add(main_root)

    library_file = os.path.join(
        STEAM_ROOT,
        "steamapps",
        "libraryfolders.vdf",
    )

    try:
        with open(
            library_file,
            "r",
            encoding="utf-8",
            errors="replace",
        ) as file:
            text = file.read()

        paths = re.findall(
            r'"path"\s+"([^"]+)"',
            text,
            flags=re.IGNORECASE,
        )

        for path in paths:
            path = path.replace(
                "\\\\",
                "\\",
            )

            path = os.path.expanduser(path)

            steamapps = os.path.join(
                path,
                "steamapps",
            )

            if os.path.isdir(steamapps):
                roots.add(steamapps)

    except (
        OSError,
        UnicodeError,
    ):
        pass

    return list(roots)


def get_local_steam_game_name(
    appid: str,
) -> Optional[str]:
    """
    Look up a normal Steam game's name using local appmanifest files.

    No Steam Web API is used.

    Returns None if the AppID isn't represented by a local installed
    Steam library manifest.
    """

    if not is_valid_steam_launch_id(appid):
        return None

    filename = STEAM_APPMANIFEST_NAME.format(
        appid=appid
    )

    for steamapps in get_library_roots():
        manifest = os.path.join(
            steamapps,
            filename,
        )

        try:
            if not os.path.isfile(manifest):
                continue

            with open(
                manifest,
                "r",
                encoding="utf-8",
                errors="replace",
            ) as file:
                text = file.read()

            manifest_appid = parse_acf_value(
                text,
                "appid",
            )

            name = parse_acf_value(
                text,
                "name",
            )

            if manifest_appid != appid:
                continue

            if name:
                return name

        except (
            OSError,
            UnicodeError,
        ):
            continue

    return None


# ---------------------------------------------------------------------------
# Steam non-Steam shortcut support
# ---------------------------------------------------------------------------

def get_steam_userdata_roots() -> List[str]:
    """
    Find Steam userdata directories.

    Steam normally stores non-Steam shortcuts at:

        ~/.local/share/Steam/userdata/<steam-user-id>/config/shortcuts.vdf

    There can be multiple userdata IDs, so all local userdata directories
    are checked.
    """

    roots: List[str] = []

    try:
        if not os.path.isdir(STEAM_SHORTCUTS_FILE):
            return roots

        for entry in os.listdir(
            STEAM_SHORTCUTS_FILE
        ):
            if not entry.isdigit():
                continue

            path = os.path.join(
                STEAM_SHORTCUTS_FILE,
                entry,
            )

            if os.path.isdir(path):
                roots.append(path)

    except OSError:
        pass

    return roots


def decode_vdf_string(
    data: bytes,
) -> str:
    """
    Decode a Steam binary-VDF string.

    Steam's shortcuts.vdf is a binary VDF file.

    UTF-8 is preferred, with replacement for malformed data.
    """

    return data.decode(
        "utf-8",
        errors="replace",
    )


def parse_shortcuts_vdf(
    data: bytes,
) -> List[Dict[str, str]]:
    """
    Parse Steam's binary shortcuts.vdf.

    We only extract the fields required to identify a non-Steam
    shortcut:

        appid
        AppName / appname

    Steam stores shortcut entries inside:

        shortcuts
            0
                appid
                AppName
                Exe
                StartDir
                ...
            1
                ...

    This parser intentionally handles the binary VDF format directly
    and does not require an external package.

    Binary VDF field types:

        0x00 = nested object
        0x01 = string
        0x02 = integer
        0x08 = end object

    The parser is intentionally limited to shortcut entries.
    """

    shortcuts: List[Dict[str, str]] = []

    if not data:
        return shortcuts

    length = len(data)
    pos = 0

    current_shortcut: Optional[Dict[str, str]] = None
    object_depth = 0

    try:
        while pos < length:
            field_type = data[pos]
            pos += 1

            # End of object.
            if field_type == 0x08:
                if object_depth > 0:
                    object_depth -= 1

                if (
                    object_depth == 1
                    and current_shortcut is not None
                ):
                    shortcuts.append(
                        current_shortcut
                    )
                    current_shortcut = None

                continue

            # Read null-terminated field name.
            name_end = data.find(
                b"\x00",
                pos,
            )

            if name_end == -1:
                break

            field_name = decode_vdf_string(
                data[pos:name_end]
            )

            pos = name_end + 1

            # Nested object.
            if field_type == 0x00:
                object_depth += 1

                # The first nested object is normally "shortcuts".
                if (
                    field_name.isdigit()
                    and object_depth >= 2
                ):
                    current_shortcut = {}

                continue

            # String.
            if field_type == 0x01:
                value_end = data.find(
                    b"\x00",
                    pos,
                )

                if value_end == -1:
                    break

                value = decode_vdf_string(
                    data[pos:value_end]
                )

                pos = value_end + 1

                if current_shortcut is not None:
                    current_shortcut[
                        field_name
                    ] = value

                continue

            # Integer.
            if field_type == 0x02:
                if pos + 4 > length:
                    break

                value = int.from_bytes(
                    data[pos:pos + 4],
                    byteorder="little",
                    signed=False,
                )

                pos += 4

                if current_shortcut is not None:
                    current_shortcut[
                        field_name
                    ] = str(value)

                continue

            # Unknown field type.
            #
            # Stop rather than guessing the size of an unknown binary
            # field and potentially corrupting the parser position.
            break

    except (
        IndexError,
        ValueError,
        UnicodeError,
    ):
        return shortcuts

    return shortcuts


def get_non_steam_shortcut_name(
    appid: str,
) -> Optional[str]:
    """
    Find the Steam entry name for a non-Steam shortcut.

    IMPORTANT:

    The name returned here is the name configured in Steam's
    "Add a Non-Steam Game" / shortcut entry.

    It is NOT derived from:

        - executable filename
        - Flatpak ID
        - process name
        - command line

    Example:

        Steam entry:
            "Jellyfin"

        Executable:
            jellyfin-desktop

        Returned name:
            "Jellyfin"

    No network access is used.
    """

    if not is_valid_steam_launch_id(
        appid
    ):
        return None

    for userdata_root in get_steam_userdata_roots():
        shortcuts_file = os.path.join(
            userdata_root,
            "config",
            "shortcuts.vdf",
        )

        try:
            if not os.path.isfile(
                shortcuts_file
            ):
                continue

            with open(
                shortcuts_file,
                "rb",
            ) as file:
                data = file.read()

            shortcuts = parse_shortcuts_vdf(
                data
            )

            for shortcut in shortcuts:
                shortcut_appid = shortcut.get(
                    "appid"
                )

                if shortcut_appid != appid:
                    continue

                # Steam normally uses AppName.
                #
                # Some versions/tools may expose it with different
                # capitalization, so check the common alternatives.
                name = (
                    shortcut.get("AppName")
                    or shortcut.get("appname")
                    or shortcut.get("name")
                )

                if name:
                    name = name.strip()

                if name:
                    return name

        except (
            OSError,
            UnicodeError,
        ):
            continue

    return None


# ---------------------------------------------------------------------------
# Process identity helpers
# ---------------------------------------------------------------------------

def get_descendant_processes(
    root: psutil.Process,
) -> List[psutil.Process]:
    """
    Get descendants of a Steam launch process.

    Used only as supporting local information.
    """

    try:
        return root.children(
            recursive=True
        )

    except (
        psutil.NoSuchProcess,
        psutil.AccessDenied,
        psutil.ZombieProcess,
    ):
        return []


def get_process_names(
    processes: List[psutil.Process],
) -> List[str]:
    """
    Return executable/process names from a process list.
    """

    names: List[str] = []

    for proc in processes:
        try:
            name = proc.name()

            if name:
                names.append(name)

        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            psutil.ZombieProcess,
        ):
            continue

    return names


# ---------------------------------------------------------------------------
# Steam launch name resolution
# ---------------------------------------------------------------------------

def resolve_steam_launch_name(
    appid: str,
    command: str,
    root_process: psutil.Process,
) -> Optional[str]:
    """
    Resolve a Steam-launched application entirely locally.

    Priority:

        1. Local Steam appmanifest name
        2. Steam non-Steam shortcut name

    For non-Steam entries, Steam's shortcut name is authoritative.

    We deliberately do NOT fall back to executable/process/Flatpak names
    for non-Steam entries because the requested result is the Steam
    library/entry name.

    No network access.
    """

    # ---------------------------------------------------------------
    # 1. Normal Steam game
    # ---------------------------------------------------------------

    local_name = get_local_steam_game_name(
        appid
    )

    if local_name:
        return local_name

    # ---------------------------------------------------------------
    # 2. Non-Steam Steam entry
    # ---------------------------------------------------------------

    shortcut_name = (
        get_non_steam_shortcut_name(
            appid
        )
    )

    if shortcut_name:
        return shortcut_name

    # ---------------------------------------------------------------
    # No Steam-local name found.
    #
    # Do not return executable/Flatpak/process names because the
    # requested behavior is specifically the Steam entry name.
    # ---------------------------------------------------------------

    return None


# ---------------------------------------------------------------------------
# Main local activity detector
# ---------------------------------------------------------------------------

def detect_steam_launch() -> Optional[Dict[str, str]]:
    """
    The Steam-launched game or app, as {"name": ..., "command": ...},
    or None. Name resolution is local only.
    """

    if STEAM_REQUIRES_GAME_MODE and not is_game_mode_active():
        return None

    launches = []

    for proc in get_steam_launch_processes():
        cmdline = get_process_commandline(proc)

        if not cmdline:
            continue

        parsed = parse_steam_launch(cmdline)

        if not parsed:
            continue

        if not is_valid_steam_launch_id(parsed["appid"]):
            continue

        launches.append((proc, parsed))

    # Nothing launched, or several different launches (ambiguous).
    if not launches:
        return None

    if len({parsed["appid"] for _, parsed in launches}) != 1:
        return None

    proc, parsed = launches[0]

    name = resolve_steam_launch_name(
        parsed["appid"],
        parsed["command"],
        proc,
    )

    if not name:
        return None

    return {"name": name, "command": parsed["command"]}


def match_known_app(
    proc_name: str,
    cmdline_text: str,
) -> Optional[int]:
    """Index of the first KNOWN_APPS entry matching a process, or None."""

    for index, (_, _, names, flatpak_ids) in enumerate(KNOWN_APPS):
        if proc_name in names:
            return index

        if proc_name in FLATPAK_LAUNCHERS:
            for flatpak_id in flatpak_ids:
                if flatpak_id in cmdline_text:
                    return index

    return None


def find_running_app() -> Optional[Dict[str, str]]:
    """
    The highest-priority KNOWN_APPS entry that is running, as
    {"name": ..., "type": ...}, or None.
    """

    best: Optional[int] = None

    for proc in psutil.process_iter(["name", "cmdline", "status"]):
        try:
            # A finished app can linger as a zombie; it is not running.
            if proc.info.get("status") == psutil.STATUS_ZOMBIE:
                continue

            proc_name = (proc.info.get("name") or "").lower()
            cmdline_text = " ".join(proc.info.get("cmdline") or [])

            index = match_known_app(proc_name, cmdline_text)

            if index is not None and (best is None or index < best):
                best = index

                if best == 0:
                    break

        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            psutil.ZombieProcess,
            OSError,
        ):
            continue

    if best is None:
        return None

    name, kind, _, _ = KNOWN_APPS[best]

    return {"name": name, "type": kind}


def detect_activity():
    """
    Return (status, activity_type).

        Steam game                  -> (Steam name, "game")
        Steam-launched known app    -> (Steam name, "app")
        Known app / emulator        -> (display name, "app" or "game")
        Nothing                     -> ("idle", "idle")

    Everything is local; no network access.
    """

    launch = detect_steam_launch()

    if launch:
        kind = "game"

        # A Steam shortcut for a known app (e.g. Jellyfin) is an app, not a game.
        for _, app_kind, names, flatpak_ids in KNOWN_APPS:
            command = launch["command"].lower()

            if (
                any(flatpak_id.lower() in command for flatpak_id in flatpak_ids)
                or any(
                    os.path.basename(token) in names
                    for token in command.split()
                )
            ):
                kind = app_kind
                break

        return launch["name"], kind

    app = find_running_app()

    if app:
        return app["name"], app["type"]

    return "idle", "idle"


# ---------------------------------------------------------------------------
# CPU temperature
# ---------------------------------------------------------------------------

def get_cpu_temperature() -> Optional[float]:
    """
    Read CPU temperature locally through psutil.
    """

    try:
        temps = psutil.sensors_temperatures()

        if not temps:
            return None

        # Prefer Intel package temperature.
        if "coretemp" in temps:
            for entry in temps["coretemp"]:
                if (
                    entry.label == "Package id 0"
                    and entry.current is not None
                ):
                    return float(
                        entry.current
                    )

        # AMD / generic fallback.
        for entries in temps.values():
            for entry in entries:
                if entry.current is None:
                    continue

                current = float(
                    entry.current
                )

                if (
                    CPU_TEMP_MIN
                    < current
                    < CPU_TEMP_MAX
                ):
                    return current

    except Exception as exc:
        print(
            f"CPU temperature error: {exc}"
        )

    return None


# ---------------------------------------------------------------------------
# Power events
# ---------------------------------------------------------------------------
#
# systemd-logind announces PrepareForSleep / PrepareForShutdown before the
# machine goes down, whatever triggered it (Home Assistant, the desktop menu,
# the power button). We hold a "delay" inhibitor lock so logind waits a moment
# (5 s by default) while we tell Home Assistant, then we release the lock.

async def broadcast(message):
    for ws in list(websocket_connections):  # copy: the list changes while we iterate
        try:
            await ws.send_json(message)
            print(f"Sent update to clients: {message}")
        except Exception as exc:
            print(f"Error sending to client, removing it: {exc}")
            if ws in websocket_connections:
                websocket_connections.remove(ws)


async def take_inhibitor():
    global inhibitor_proc
    if inhibitor_proc is not None and inhibitor_proc.returncode is None:
        return
    try:
        inhibitor_proc = await asyncio.create_subprocess_exec(
            "systemd-inhibit", "--what=sleep:shutdown", "--mode=delay",
            "--who=bazzite-server",
            "--why=Telling Home Assistant the PC is going down",
            "sleep", "infinity",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as exc:
        print(f"Could not take inhibitor lock (power events may arrive late): {exc}")


def release_inhibitor():
    if inhibitor_proc is not None and inhibitor_proc.returncode is None:
        inhibitor_proc.terminate()


async def handle_power_event(kind, going_down):
    """kind is 'sleep' or 'shutdown'; going_down False means resumed/cancelled."""
    global power_state
    if going_down:
        power_state = kind
        await broadcast({"power": power_state})
        await asyncio.sleep(0.3)  # let the frame leave before the machine freezes
        release_inhibitor()
    else:
        power_state = "on"
        await take_inhibitor()
        await broadcast({"power": "on"})


async def power_monitor():
    await take_inhibitor()
    cmd = ["gdbus", "monitor", "--system", "--dest", "org.freedesktop.login1"]
    if shutil.which("stdbuf"):
        cmd = ["stdbuf", "-oL"] + cmd  # line-buffer gdbus output when piped
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            async for raw in proc.stdout:
                line = raw.decode(errors="ignore")
                kind = None
                if "PrepareForSleep" in line:
                    kind = "sleep"
                elif "PrepareForShutdown" in line:
                    kind = "shutdown"
                if kind:
                    await handle_power_event(kind, "(true," in line)
            await proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Power monitor error: {exc}")
        print("Power monitor stopped, retrying in 5 s")
        await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.post("/command")
async def post_command(command: dict):
    cmd = command.get("command")

    if cmd == "shutdown":
        subprocess.Popen(
            ["systemctl", "poweroff"]
        )

    elif cmd == "sleep":
        subprocess.Popen(
            ["systemctl", "suspend"]
        )

    return {
        "result": "ok"
    }


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
):
    global websocket_connections

    await websocket.accept()

    websocket_connections.append(
        websocket
    )

    # Send current state immediately.
    message = {"power": power_state}

    if last_status is not None:
        message["status"] = last_status

    if last_activity_type is not None:
        message["activity_type"] = last_activity_type

    if last_cpu_temp is not None:
        message["cpu_temperature"] = (
            last_cpu_temp
        )

    if message:
        try:
            await websocket.send_json(
                message
            )

            print(
                "Sent initial state to "
                f"new client: {message}"
            )

        except Exception:
            pass

    try:
        while True:
            await asyncio.sleep(1000)

    except WebSocketDisconnect:
        pass

    except Exception:
        pass

    finally:
        if websocket in websocket_connections:
            websocket_connections.remove(
                websocket
            )


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

async def game_monitor():
    global last_status
    global last_cpu_temp
    global last_activity_type

    first_time = True

    while True:
        try:
            status, activity_type = detect_activity()

            cpu_temp = (
                get_cpu_temperature()
            )

            message = {}

            # Always send first state.
            if (
                first_time
                or status != last_status
            ):
                message["status"] = status

                last_status = status

            if (
                first_time
                or activity_type != last_activity_type
            ):
                message["activity_type"] = activity_type

                last_activity_type = activity_type

            # Temperature only when changed.
            if (
                first_time
                or cpu_temp != last_cpu_temp
            ):
                message[
                    "cpu_temperature"
                ] = cpu_temp

                last_cpu_temp = cpu_temp

            if message:
                for ws in list(
                    websocket_connections
                ):
                    try:
                        await ws.send_json(
                            message
                        )

                        print(
                            "Sent update to "
                            f"client: {message}"
                        )

                    except WebSocketDisconnect:
                        if ws in websocket_connections:
                            websocket_connections.remove(
                                ws
                            )

                    except Exception as exc:
                        print(
                            "WebSocket send "
                            f"error: {exc}"
                        )

                        if ws in websocket_connections:
                            websocket_connections.remove(
                                ws
                            )

            first_time = False

        except Exception as exc:
            # Detection must never kill monitor.
            print(
                f"Game monitor error: {exc}"
            )

            if last_status != "none":
                last_status = "none"

                for ws in list(
                    websocket_connections
                ):
                    try:
                        await ws.send_json(
                            {
                                "status": "none"
                            }
                        )

                    except Exception:
                        if ws in websocket_connections:
                            websocket_connections.remove(
                                ws
                            )

        await asyncio.sleep(1)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event():
    background_tasks.append(asyncio.create_task(game_monitor()))
    background_tasks.append(asyncio.create_task(power_monitor()))


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {
        "message": "Bazzite Server Running!"
    }

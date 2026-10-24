from __future__ import annotations

import os
import shutil
import subprocess


def copy_native(text: str) -> bool:
    """Copy via an installed desktop clipboard helper; OSC 52 is handled by Textual."""
    commands: list[list[str]] = []
    if os.environ.get("WAYLAND_DISPLAY"):
        commands.append(["wl-copy"])
    if os.environ.get("DISPLAY"):
        commands.extend([["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]])
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        # KDE ships this helper even when wl-copy/xclip are absent.
        for helper in ("qdbus6", "qdbus"):
            commands.append([helper, "org.kde.klipper", "/klipper", "setClipboardContents", text])
    for command in commands:
        executable = shutil.which(command[0])
        if executable is None:
            continue
        try:
            result = subprocess.run(
                [executable, *command[1:]],
                input=text,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
        except OSError, subprocess.TimeoutExpired:
            continue
        if result.returncode == 0:
            return True
    return False

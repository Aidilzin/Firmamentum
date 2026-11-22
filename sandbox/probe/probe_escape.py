"""Planted by `orch sandbox test --probe` as tests/test_escape_probe.py in a scratch copy. Never part of a real run.

Each test passes only when the attempt FAILS inside the sandbox, and prints one `BLOCKED:` line saying how. A green run
therefore means: the repo's real tests passed, and a test that tries to write outside, open a socket, read a host
environment variable and read a real host file got nowhere. A test that escapes fails with `ESCAPED`.

The first fixture refuses to run anything outside a container, so this file does no harm if it is ever run by hand.
"""

import json
import os
import socket
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def only_inside_a_container():
    if not os.path.exists("/.dockerenv"):
        pytest.fail("not inside a container: the escape probe refuses to run on the host")


def test_cannot_write_outside_the_working_copy():
    targets = ["/src/orch-probe.txt", "/orch-probe.txt", "/etc/orch-probe.txt", "/work/../orch-probe-up.txt"]
    blocked, escaped = [], []
    for path in targets:
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("planted by the escape probe")
            escaped.append(path)
        except OSError as e:
            blocked.append(f"{path} ({e.strerror})")
    assert not escaped, f"ESCAPED: wrote {escaped}"
    print("BLOCKED: write: " + ", ".join(blocked))


def test_cannot_open_a_socket():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect(("1.1.1.1", 53))
    except OSError as e:
        print(f"BLOCKED: network: 1.1.1.1:53 ({e.strerror or type(e).__name__})")
        return
    finally:
        s.close()
    pytest.fail("ESCAPED: connected to 1.1.1.1:53")


def test_host_environment_and_drives_are_not_visible():
    names = ("ORCH_PROBE_CANARY", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "ANTHROPIC_API_KEY")
    leaked = [n for n in names if n in os.environ]
    # /mnt exists, empty, in the base image, so existing is fine. A host drive would be a mount point or hold something.
    candidates = ("/c", "/mnt", "/mnt/c", "/mnt/host", "/host_mnt", "/host_mnt/c")
    drives = []
    for p in candidates:
        if not os.path.lexists(p):
            continue
        if os.path.islink(p) or os.path.ismount(p) or not os.path.isdir(p) or os.listdir(p):
            drives.append(p)
    assert not leaked, f"ESCAPED: host environment variables visible: {leaked}"
    assert not drives, f"ESCAPED: host drives visible (mounted, linked or not empty): {drives}"
    print("BLOCKED: host env: " + ", ".join(names) + " are not set, and none of " + ", ".join(candidates)
          + " is a mount, a link or holds anything")


def test_a_host_file_cannot_be_read():
    """`probe_target.json` (planted next to tests/) names a canary file that really exists on the host, outside everything
    that is mounted, spelled the way Docker Desktop's file sharing could expose it. Every spelling must fail to open."""
    target_file = Path(__file__).resolve().parents[1] / "probe_target.json"
    if not target_file.is_file():
        pytest.fail(f"ESCAPED: no probe_target.json at {target_file}; the host file check could not run")
    paths = json.loads(target_file.read_text(encoding="utf-8"))["paths"]
    assert paths, "probe_target.json lists no path"
    readable = []
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                f.read(1)
            readable.append(path)
        except OSError:
            pass
    assert not readable, f"ESCAPED: a host file was readable through {readable}"
    print(f"BLOCKED: host file: none of {len(paths)} spellings of the host canary could be opened")

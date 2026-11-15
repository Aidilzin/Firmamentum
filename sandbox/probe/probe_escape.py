"""Planted by `orch sandbox test --probe` as tests/test_escape_probe.py in a scratch copy. Never part of a real run.

Each test passes only when the attempt FAILS inside the sandbox, and prints one `BLOCKED:` line saying how. A green run
therefore means: the repo's real tests passed, and a test that tries to write outside, open a socket and read a host
environment variable got nowhere. A test that escapes fails with `ESCAPED`.

The first fixture refuses to run anything outside a container, so this file does no harm if it is ever run by hand.
"""

import os
import socket

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

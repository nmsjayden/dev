#!/usr/bin/env python3

"""
ChromeOS DM policy tool: fetch / dump / inject / apply / local overrides.

See --help for subcommands. State under /root/policy_editor_state.

Made By: Aro_Moon / Nmsjayden
"""

import argparse
import atexit
import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.parse

from pathlib import Path

def _drain_stdin():
    if not sys.stdin.isatty():
        return ""
    import select
    chunks = []
    while True:
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if not r:
            break
        try:
            chunk = os.read(sys.stdin.fileno(), 4096)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode(errors="ignore")

_ESC_CANCEL = object()

def _visible_len(s):
    return len(re.sub(r'\x1b\[[0-9;]*m', '', s))

def _cursor_col(fd):
    import select
    try:
        os.write(1, b"\x1b[6n")
        data = b""
        while True:
            r, _, _ = select.select([fd], [], [], 0.3)
            if not r:
                break
            c = os.read(fd, 1)
            if not c:
                break
            data += c
            if c == b'R':
                break
        m = re.search(rb"\x1b\[\d+;(\d+)R", data)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None

def _read_line_raw(prompt="", prefill=""):
    if not sys.stdin.isatty():
        return input(prompt).strip()
    _enter_raw()
    import select
    fd = sys.stdin.fileno()

    pre, nl, online = prompt.rpartition('\n')
    if nl:
        sys.stdout.write(pre + '\n')
    sys.stdout.write(online)
    sys.stdout.flush()

    plen = _visible_len(online)
    col_now = _cursor_col(fd)
    if col_now is not None:
        plen = col_now - 1
    cols = max(1, shutil.get_terminal_size((80, 24)).columns)

    buf = list(prefill)
    pos = len(buf)
    oldpos = 0
    oldrows = 1

    def refresh():
        nonlocal oldpos, oldrows
        length = len(buf)
        rows = max(1, (plen + length + cols - 1) // cols)
        rpos = (plen + oldpos + cols) // cols
        out = []
        if oldrows - rpos > 0:
            out.append("\x1b[%dB" % (oldrows - rpos))
        for _ in range(oldrows - 1):
            out.append("\r\x1b[0K\x1b[1A")
        out.append("\r\x1b[0K")
        out.append(online)
        out.append("".join(buf))
        if pos == length and length and (plen + length) % cols == 0:
            out.append("\n\r")
            rows += 1
        rpos2 = (plen + pos + cols) // cols
        if rows - rpos2 > 0:
            out.append("\x1b[%dA" % (rows - rpos2))
        c = (plen + pos) % cols
        out.append("\r\x1b[%dC" % c if c else "\r")
        oldpos = pos
        oldrows = rows
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def finish(ret):
        nonlocal pos
        pos = len(buf)
        refresh()
        sys.stdout.write("\n")
        sys.stdout.flush()
        return ret

    refresh()

    while True:
        ch = os.read(fd, 1).decode(errors="ignore")
        if ch == '\x1b':
            seq = ""
            while True:
                r, _, _ = select.select([fd], [], [], 0.01)
                if not r:
                    break
                nxt = os.read(fd, 1).decode(errors="ignore")
                if not nxt:
                    break
                seq += nxt
                if seq[-1].isalpha() or seq[-1] == '~':
                    break
                if len(seq) >= 6:
                    break
            if seq == "":
                return finish(_ESC_CANCEL)
            if seq in ('[D', 'OD'):
                if pos > 0:
                    pos -= 1
                    refresh()
            elif seq in ('[C', 'OC'):
                if pos < len(buf):
                    pos += 1
                    refresh()
            elif seq in ('[H', 'OH', '[1~', '[7~'):
                if pos != 0:
                    pos = 0
                    refresh()
            elif seq in ('[F', 'OF', '[4~', '[8~'):
                if pos != len(buf):
                    pos = len(buf)
                    refresh()
            elif seq == '[3~':
                if pos < len(buf):
                    buf.pop(pos)
                    refresh()
            continue
        if ch in ('\r', '\n'):
            return finish("".join(buf).strip())
        if ch in ('\x7f', '\x08'):
            if pos > 0:
                buf.pop(pos - 1)
                pos -= 1
                refresh()
            continue
        if ch == '\x01':
            if pos != 0:
                pos = 0
                refresh()
            continue
        if ch == '\x05':
            if pos != len(buf):
                pos = len(buf)
                refresh()
            continue
        if ch == '\x0b':
            if pos < len(buf):
                del buf[pos:]
                refresh()
            continue
        if ch == '\x15':
            if pos > 0:
                del buf[:pos]
                pos = 0
                refresh()
            continue
        if ch == '\x03':
            raise KeyboardInterrupt
        if ch == '\x04':
            raise EOFError
        if ch and ch.isprintable():
            buf.insert(pos, ch)
            pos += 1
            refresh()

def _eline(prompt, current):
    if not sys.stdin.isatty():
        typed = input(prompt).strip()
        return typed if typed else current
    result = _read_line_raw(prompt, prefill=current)
    if result is _ESC_CANCEL:
        return current
    return result if result else current

def _confirm(prompt):
    if not sys.stdin.isatty():
        return input(prompt).strip().lower() == 'y'
    result = _read_line_raw(prompt)
    if result is _ESC_CANCEL:
        return False
    return result.lower() == 'y'

_COLOR = sys.stdout.isatty()

def _color(text, code):
    if not _COLOR:
        return text
    return "\033[%sm%s\033[0m" % (code, text)

_RESET   = "\033[0m" if _COLOR else ""
_BOLD    = "\033[1m" if _COLOR else ""
_DIM     = "\033[2m" if _COLOR else ""
_REVERSE = "\033[7m" if _COLOR else ""
_RED     = "\033[31m" if _COLOR else ""
_YELLOW  = "\033[33m" if _COLOR else ""
_CYAN    = "\033[36m" if _COLOR else ""
_WHITE   = "\033[37m" if _COLOR else ""
_BGREEN   = "\033[92m" if _COLOR else ""
_BYELLOW  = "\033[93m" if _COLOR else ""
_BBLUE    = "\033[94m" if _COLOR else ""
_BMAGENTA = "\033[95m" if _COLOR else ""
_BCYAN    = "\033[96m" if _COLOR else ""
_BWHITE   = "\033[97m" if _COLOR else ""

def _bold(t): return _color(t, "1")
def _dim(t): return _color(t, "2")

_RAW_OLD_ATTRS = None

def _enter_raw():
    # stay in raw mode for the whole interactive session
    global _RAW_OLD_ATTRS
    if not sys.stdin.isatty() or _RAW_OLD_ATTRS is not None:
        return
    import termios
    import tty
    fd = sys.stdin.fileno()
    _RAW_OLD_ATTRS = termios.tcgetattr(fd)
    tty.setraw(fd, termios.TCSANOW)
    attrs = termios.tcgetattr(fd)
    attrs[1] |= (termios.OPOST | termios.ONLCR)
    termios.tcsetattr(fd, termios.TCSANOW, attrs)

def _exit_raw():
    global _RAW_OLD_ATTRS
    if _RAW_OLD_ATTRS is None:
        return
    import termios
    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _RAW_OLD_ATTRS)
    _RAW_OLD_ATTRS = None

atexit.register(_exit_raw)

_real_input = input

def input(prompt=""):
    # drop raw mode before line-buffered reads
    _exit_raw()
    return _real_input(prompt)

def _key():
    # single keypress (arrows, enter, esc, ...)
    if not sys.stdin.isatty():
        return input().strip() or "ENTER"
    import select
    _enter_raw()
    fd = sys.stdin.fileno()
    while True:
        ch = os.read(fd, 1).decode(errors="ignore")
        if ch == '\x1b':
            seq = ""
            while len(seq) < 2:
                r, _, _ = select.select([fd], [], [], 0.01)
                if not r:
                    break
                seq += os.read(fd, 2 - len(seq)).decode(errors="ignore")
            if seq == "":
                return 'ESC'
            result = {'[A': 'UP', '[B': 'DOWN', '[C': 'RIGHT', '[D': 'LEFT'}.get(seq)
            if result:
                return result
            while select.select([fd], [], [], 0)[0]:
                os.read(fd, 1)
            continue
        if ch in ('\r', '\n'):
            return 'ENTER'
        if ch in ('\x7f', '\x08'):
            return 'BACKSPACE'
        if ch == '\x03':
            raise KeyboardInterrupt
        return ch

def _menu(title, options, extra_lines=None, subtitle=None):
    items = [o if isinstance(o, tuple) else (o, _BWHITE) for o in options]
    sel = 0
    n = len(items)
    if n == 0:
        return None
    while True:
        _cls()
        labels = [label for label, _ in items]
        width = max(44, len(title) + 6, max((len(o) for o in labels), default=0) + 8)
        print(_BMAGENTA + "+" + "-" * width + "+" + _RESET)
        print(_BMAGENTA + "|" + _RESET + _bold(_BWHITE + title.center(width) + _RESET) + _BMAGENTA + "|" + _RESET)
        print(_BMAGENTA + "+" + "-" * width + "+" + _RESET)
        if subtitle:
            print(_DIM + subtitle.center(width) + _RESET)
        print()
        for i, (label, color) in enumerate(items):
            if i == sel:
                bar = (" " + label).ljust(width - 3)
                print("   %s%s%s%s%s" % (color, _REVERSE, _BOLD, bar, _RESET))
            else:
                print("    %s%s%s" % (color, label, _RESET))
        print()
        if extra_lines:
            print("  " + _DIM + "-" * (width - 2) + _RESET)
            for line in extra_lines:
                print("  " + line)
        print("\n  [%s] move   [%s] select   [%s] back" % (
              "up/down", "enter", "esc"))
        _cls_end()

        key = _key()
        if key == 'UP':
            sel = (sel - 1) % n
        elif key == 'DOWN':
            sel = (sel + 1) % n
        elif key == 'ENTER':
            return sel
        elif key == 'ESC':
            return None
        elif isinstance(key, str) and key.isdigit():
            idx = int(key) - 1
            if 0 <= idx < n:
                sel = idx

def _pick_state_dir():
    env = os.environ.get("DM_POLICY_STATE")
    if env:
        return Path(env)
    cands = [Path("/root/policy_editor_state"), Path("/usr/local/policy_editor_state"),
             Path("/tmp/policy_editor_state")]
    for c in cands:
        if c.exists() and os.access(str(c), os.W_OK):
            return c
    for c in cands:
        try:
            c.mkdir(parents=True, exist_ok=True)
            return c
        except OSError:
            continue
    return cands[0]

def _mkdir(p):
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

STATE_DIR       = _pick_state_dir()
PROFILES_DIR    = STATE_DIR.joinpath("profiles")
SNAPSHOTS_DIR   = STATE_DIR.joinpath("managed-user")
MANAGED_DIR     = Path('/etc/opt/chrome/policies/managed')

_SELECTED_USER = None
_NOTED_USERS = False

def _read_dbus_str(body, pos):
    pos += -pos % 4
    ln = int.from_bytes(body[pos:pos + 4], "little")
    return body[pos + 4:pos + 4 + ln].decode(errors="replace"), pos + 5 + ln

def _signed_in_users():
    try:
        body = _dbus_call("RetrieveActiveSessions", "", [])["body"]
        end = 8 + int.from_bytes(body[:4], "little")
        pos, out = 8, []
        while pos < end:
            pos += -pos % 8
            email, pos = _read_dbus_str(body, pos)
            sanitized, pos = _read_dbus_str(body, pos)
            out.append((email, sanitized))
        return out
    except Exception:
        return []

def _primary_user():
    try:
        body = _dbus_call("RetrievePrimarySession", "", [])["body"]
        email, pos = _read_dbus_str(body, 0)
        sanitized, pos = _read_dbus_str(body, pos)
        return email, sanitized
    except Exception:
        return None, None

def _user_dir():
    global _NOTED_USERS
    base = Path("/run/daemon-store/session_manager")
    users = [(e, h) for e, h in _signed_in_users() if (base / h / "policy" / "policy").exists()]
    want = _SELECTED_USER or os.environ.get("DM_POLICY_USER")
    if want:
        for e, h in users:
            if e.lower() == want.lower() or h.startswith(want.lower()):
                return base / h / "policy"
        print("ERROR: %s is not signed in. Signed in: %s" % (
            want, ", ".join(e for e, h in users) or "nobody"), file=sys.stderr)
        sys.exit(1)
    if users:
        primary = _primary_user()[1]
        chosen = next((u for u in users if u[1] == primary), users[0])
        if len(users) > 1 and not _NOTED_USERS:
            _NOTED_USERS = True
            print("Several users are signed in. Using %s (also: %s). "
                  "Pick another with --user EMAIL." % (
                      chosen[0], ", ".join(e for e, h in users if e != chosen[0])),
                  file=sys.stderr)
        return base / chosen[1] / "policy"
    if base.exists():
        for d in sorted(base.iterdir()):
            if (d / "policy" / "policy").exists():
                return d / "policy"
    return None

def _live_pol():
    d = _user_dir()
    return d / "policy" if d is not None else None

DM_ENDPOINT  = 'https://m.google.com/devicemanagement/data/api'
DM_AUTH_HDR  = 'Authorization'
DM_TOKEN_PFX = 'GoogleDMToken token='

def _lsb_release():
    info = {}
    try:
        for line in Path("/etc/lsb-release").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip()
    except Exception:
        pass
    return info

def _chrome_agent():
    version = "0.0.0.0"
    try:
        import subprocess
        out = subprocess.run(["/opt/google/chrome/chrome", "--version"],
                              capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"\d+\.\d+\.\d+\.\d+", out)
        if m:
            version = m.group(0)
    except Exception:
        pass
    return "Google Chrome %s" % version

def _chrome_plat():
    lsb = _lsb_release()
    board = lsb.get("CHROMEOS_RELEASE_BOARD", "unknown")
    version = lsb.get("CHROMEOS_RELEASE_VERSION", "0.0.0")
    arch = os.uname().machine
    hwid = ""
    try:
        import subprocess
        hwid = subprocess.run(["crossystem", "hwid"], capture_output=True,
                               text=True, timeout=5).stdout.strip()
    except Exception:
        pass
    return "Linux,CrOS,%s|%s,%s|%s" % (board, arch, hwid, version)

PFR_TYPE    = 1
PFR_SIG = 3   # 2 = SHA256_RSA
PFR_PKV = 4
DMR_USER   = 3
DMR_REQ    = 3
DMRESP_USER   = 5
DPOL_FETCH    = 3

DM_SERVER_HOST = 'm.google.com'

SENSITIVE_FIELDS = {3, 8, 10}  # token / device_id fields — never print these
INJECT_KEY_FILE   = SNAPSHOTS_DIR / "inject.key.pem"
DM_KEY_BACKUP     = SNAPSHOTS_DIR / "dm.key.pub.bak"  # original DM public key
INJECT_STATE_FILE = SNAPSHOTS_DIR / "inject_state.json"
DM_BLOCK_STATE_FILE = SNAPSHOTS_DIR / "dm_block_state.json"
SYNCED_STATE_FILE = SNAPSHOTS_DIR / "synced_state.json"

ACTION_LOG_FILE = SNAPSHOTS_DIR / "actions.log"

def _alog(msg):
    try:
        SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        with open(ACTION_LOG_FILE, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:
        pass

POLICY_ID_MAP_FILE  = SNAPSHOTS_DIR / "chrome_policy_id_map.json"
POLICY_OVERRIDE_FILE = SNAPSHOTS_DIR / "chrome_policy_id_overrides.json"
CHUNKED_MAP_FILE = SNAPSHOTS_DIR / "chrome_policy_chunked_map.json"
CHUNKED_POLICIES_FILE = SNAPSHOTS_DIR / "chrome_policy_chunked_names.json"  # legacy
MAPPING_META_FILE = SNAPSHOTS_DIR / "mapping_meta.json"

DEFAULT_OVR = {}

# Chromium packs policy ids: <=1040 stay top-level (field = id+2);
# higher ids go into SubProto chunks of 800.
POLICY_ID_OFFSET = 2
POLICY_LAST_TOP_LEVEL_ID = 1040
POLICY_CHUNK_SIZE = 800

def _chunk_and_field(yaml_id):
    """Map a policy id to (chunk, field)."""
    if yaml_id <= POLICY_LAST_TOP_LEVEL_ID:
        return 0, yaml_id + POLICY_ID_OFFSET
    chunk = (yaml_id - POLICY_LAST_TOP_LEVEL_ID - 1) // POLICY_CHUNK_SIZE + 1
    field = (yaml_id - POLICY_LAST_TOP_LEVEL_ID - 1) % POLICY_CHUNK_SIZE + 1
    return chunk, field

def _subproto_cs_field(chunk):
    """Field number for a sub-proto chunk."""
    return POLICY_LAST_TOP_LEVEL_ID + POLICY_ID_OFFSET + chunk

def _load_pn2f():
    """Map policy name to (chunk, field)."""
    out = {}
    id_to_name = {}
    if POLICY_ID_MAP_FILE.exists():
        id_to_name.update(json.loads(POLICY_ID_MAP_FILE.read_text()))
    if POLICY_OVERRIDE_FILE.exists():
        id_to_name.update(json.loads(POLICY_OVERRIDE_FILE.read_text()))
    else:
        id_to_name.update(DEFAULT_OVR)
    for fnum, name in id_to_name.items():
        if name:
            out[name] = (0, int(fnum))
    if CHUNKED_MAP_FILE.exists():
        raw = json.loads(CHUNKED_MAP_FILE.read_text())
        for name, loc in raw.items():
            if not name or name in ("''", '""') or not name[0].isalpha():
                continue
            if isinstance(loc, dict):
                out[name] = (int(loc["chunk"]), int(loc["field"]))
            else:
                out[name] = (int(loc[0]), int(loc[1]))
    return out

_PN2F = _load_pn2f()
_CHUNKED = {n for n, (c, _) in _PN2F.items() if c > 0}

def _reload_mapping():
    global _PN2F, _CHUNKED
    _PN2F = _load_pn2f()
    _CHUNKED = {n for n, (c, _) in _PN2F.items() if c > 0}

_CHROME_VERSION = None

def _chrome_version():
    global _CHROME_VERSION
    if _CHROME_VERSION is None:
        _CHROME_VERSION = _chrome_agent().rsplit(" ", 1)[-1]
    return _CHROME_VERSION

def _mapping_note():
    if not _PN2F:
        return "No policy mapping yet."
    try:
        saved = json.loads(MAPPING_META_FILE.read_text()).get("chrome", "")
    except Exception:
        return None
    now = _chrome_version()
    if saved and saved.split(".")[0] != now.split(".")[0]:
        return "Chrome updated (%s to %s). Refresh the policy mapping." % (saved, now)
    return None

def _ptype(value):
    if type(value) is bool:
        return 'bool'
    if type(value) == int:
        return 'int'
    if isinstance(value, str):
        if value.lower() in ('true', 'false'):
            return 'bool'
        try:
            int(value)
            return 'int'
        except ValueError:
            return 'string'
    return 'string'
def _enc_varint(n):
    # protobuf varint
    out = bytearray()
    while True:
        b = n & 0x7F; n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n: return bytes(out)

def _enc_field(fno, wire, val):
    tag = _enc_varint((fno << 3) | wire)
    if wire == 0: return tag + _enc_varint(val)
    if wire == 2: return tag + _enc_varint(len(val)) + val
    raise ValueError("unsupported wire %s" % wire)

def _dec_varint(data, pos):
    result, shift = 0, 0
    while True:
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80): break
        shift += 7
    return result, pos

def _get_field(fields, fnum):
    for f, w, v in fields:
        if f == fnum:
            return w, v
    return None, None

def _parse_raw(data):
    # -> list of (field, wire, val)
    fields = []
    pos = 0
    while pos < len(data):
        tag, pos = _dec_varint(data, pos)
        fnum = tag >> 3; wtype = tag & 7
        if   wtype == 0: val, pos = _dec_varint(data, pos)
        elif wtype == 2:
            length, pos = _dec_varint(data, pos)
            val = data[pos:pos+length]; pos += length
        elif wtype == 1: val = data[pos:pos+8]; pos += 8
        elif wtype == 5: val = data[pos:pos+4]; pos += 4
        else: raise ValueError("unknown wire type %s at offset %s" % (wtype, pos-1))
        fields.append((fnum, wtype, val))
    return fields

def _reencode(fields, overrides):
    out = b''; applied = set()
    for fnum, wtype, val in fields:
        if fnum in overrides:
            new_val = overrides[fnum]
            if new_val is None:
                applied.add(fnum)  # drop this field
                continue
            if fnum not in applied:
                out += _enc_field(fnum, 2, new_val)
                applied.add(fnum)
        else:
            if   wtype == 0: out += _enc_field(fnum, 0, val)
            elif wtype == 2: out += _enc_field(fnum, 2, val)
            elif wtype == 1: out += _enc_varint((fnum << 3) | 1) + val
            elif wtype == 5: out += _enc_varint((fnum << 3) | 5) + val
    for fnum, nv in overrides.items():
        if fnum not in applied and nv is not None:
            out += _enc_field(fnum, 2, nv)
    return out

def _user_pol_files():
    d = _user_dir()
    if d is not None and (d / "key").exists():
        return d / "policy", d / "key"
    hr = Path("/home/root")
    if hr.exists():
        for d in sorted(hr.iterdir()):
            if not d.is_dir(): continue
            pdir = d / "session_manager" / "policy"
            p, k = pdir / "policy", pdir / "key"
            if p.exists() and k.exists():
                return p, k
    return None, None

def _mk_pfr(policy_type, public_key_version=1):
    body = _enc_field(PFR_TYPE, 2, policy_type.encode())
    body += (_enc_field(PFR_SIG, 0, 2)  # SHA256_RSA
             + _enc_field(PFR_PKV, 0, public_key_version))
    return body

def _mk_dm_req(policy_type, public_key_version=1):
    pfr = _mk_pfr(policy_type, public_key_version)
    dpr = _enc_field(DMR_REQ, 2, pfr)
    return _enc_field(DMR_USER, 2, dpr)

class _Credentials:
    __slots__ = ("dm_token", "device_id", "public_key_version", "policy_type")

    def __init__(self, path):
        raw = Path(path).read_bytes()
        fields = _parse_raw(raw)
        _, pd_bytes = _get_field(fields, 3)
        if pd_bytes is None:
            raise ValueError("No policy_data in %s" % path)
        pdf = _parse_raw(pd_bytes)
        dm_token = None; device_id = None; pkv = 1; pt = None
        for f, w, v in pdf:
            if f == 3:  dm_token = v if isinstance(v, bytes) else None
            if f == 8:  device_id = v if isinstance(v, bytes) else None
            if f == 6:  pkv = v if isinstance(v, int) else 1
            if f == 1:  pt = v.decode() if isinstance(v, bytes) else None
        if dm_token == None:
            raise ValueError("Could not extract DM token (field 3) from blob")
        self.dm_token = dm_token.decode() if isinstance(dm_token, bytes) else dm_token
        self.device_id = (device_id.decode() if isinstance(device_id, bytes)
                          else (device_id or ""))
        self.public_key_version = pkv
        self.policy_type = pt or "google/chromeos/user"

SNAPSHOT_KEEP = 20  # prune older snapshots after each save

def _prune_snaps():
    snaps = sorted(SNAPSHOTS_DIR.glob("*.bin"), key=lambda p: p.stat().st_mtime)
    for old in snaps[:-SNAPSHOT_KEEP]:
        old.unlink()

def cmd_fetch(args):
    # pull from DM and save a snapshot
    live = _live_pol()
    if live is None:
        print("ERROR: no live user policy found under /run/daemon-store/.\n"
              "Sign in as the managed user first.", file=sys.stderr)
        sys.exit(1)

    print("Reading credentials from %s ..." % live)
    try:
        creds = _Credentials(live)
    except Exception as e:
        print("ERROR: %s" % e, file=sys.stderr)
        sys.exit(1)

    print("  policy_type:       %s" % creds.policy_type)
    print("  public_key_version:%s" % creds.public_key_version)
    print("  dm_token:          [redacted, %d chars]" % len(creds.dm_token))
    print("  device_id:         [redacted, %d chars]" % len(creds.device_id))
    machine_id = None
    try:
        machine_id = Path("/sys/firmware/vpd/ro/serial_number").read_text().strip()
    except Exception:
        pass
    params = urllib.parse.urlencode({
        "retry":      "false",
        "agent":      _chrome_agent(),
        "apptype":    "Chrome",  # not "chromeos" — routes to user policy
        "deviceid":   creds.device_id,
        "devicetype": "2",
        "oauth_token": "",
        "platform":   _chrome_plat(),
        "request":    "policy",
    })
    url = "%s?%s" % (DM_ENDPOINT, params)
    body = _mk_dm_req(creds.policy_type, creds.public_key_version)

    dm_auth = DM_TOKEN_PFX + creds.dm_token
    print("  machine_id:        %s" % (machine_id or "(not found)"))
    import gzip as _gzip, ssl as _ssl, http.client as _http

    print("\nFetching from DM server (body: %d bytes) ..." % len(body))
    try:
        ctx = _ssl.create_default_context()
        conn = _http.HTTPSConnection("m.google.com", 443, context=ctx, timeout=30)
        parsed = urllib.parse.urlparse(url)
        conn.request("POST", parsed.path + "?" + parsed.query, body=body,
                     headers={
                         DM_AUTH_HDR: dm_auth,
                         "Content-Type": "application/protobuf",
                         "Content-Length": str(len(body)),
                         "Accept-Encoding": "gzip, deflate",
                         "Connection": "close",
                     })
        resp = conn.getresponse()
        raw_response = resp.read()
        status = resp.status
        conn.close()
    except Exception as e:
        print("ERROR: %s" % e, file=sys.stderr)
        sys.exit(1)
    if raw_response[:2] == b'\x1f\x8b':
        raw_response = _gzip.decompress(raw_response)

    print("  Response: HTTP %d, %d decoded bytes" % (status, len(raw_response)))

    if status != 200:
        print("ERROR: unexpected HTTP %d" % status, file=sys.stderr)
        if raw_response:
            fields = _parse_raw(raw_response)
            for f, w, v in fields:
                if isinstance(v, bytes):
                    print("  Server: {}".format(v.decode("utf-8", errors="replace")), file=sys.stderr)
        sys.exit(1)
    if not raw_response:
        print("  Server: no change (policy already current for this token/device).")
        pfr_bytes = live.read_bytes()
        if DM_KEY_BACKUP.exists():
            live_pfr = _parse_raw(pfr_bytes)
            if not _verify_pfr(live_pfr, DM_KEY_BACKUP.read_bytes()):
                print("ERROR: the live on-disk policy doesn't verify against the "
                      "saved real DM key, so it can't be trusted as a snapshot.\n"
                      "It's likely mid-edit. Run `eject` (or `sign-out`) first, "
                      "then `fetch` again.", file=sys.stderr)
                sys.exit(1)
        print("  Snapshotting live on-disk policy file: %s" % live)
    else:
        pfr_bytes = None
        outer = _parse_raw(raw_response)
        for f, w, v in outer:
            if f == DMRESP_USER and isinstance(v, bytes):
                inner = _parse_raw(v)
                for f2, w2, v2 in inner:
                    if f2 == DPOL_FETCH and isinstance(v2, bytes):
                        pfr_bytes = v2
                        break
                break

        if pfr_bytes == None:
            pfr_bytes = raw_response
        live_key = _user_pol_files()[1]
        if live_key is not None and _verify_pfr(_parse_raw(pfr_bytes), live_key.read_bytes()):
            SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            DM_KEY_BACKUP.write_bytes(live_key.read_bytes())

        try:
            _, pd = _get_field(_parse_raw(pfr_bytes), 3)
            if pd:
                pdf = _parse_raw(pd)
                pt = next((v for f,w,v in pdf if f==1), None)
                print("  policy_type in response: %s" % pt)
        except Exception:
            pass
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(SNAPSHOTS_DIR.glob("*.bin"), key=lambda p: p.stat().st_mtime)
    if existing and existing[-1].read_bytes() == pfr_bytes:
        print("\nUnchanged since last snapshot (" + existing[-1].name + "), not saving.")
    else:
        ts = time.strftime("%Y%m%dT%H%M%S")
        sha = hashlib.sha256(pfr_bytes).hexdigest()[:12]
        fname = SNAPSHOTS_DIR / ("%s-dm-fetch-%s.bin" % (ts, sha))
        fname.write_bytes(pfr_bytes)
        print("\nSaved: %s" % fname)
        _prune_snaps()
    print("Run `dump` to inspect it, or `diff` to compare with a prior snapshot.")

def cmd_dump(args):
    if args.snapshot:
        path = Path(args.snapshot)
        if not path.exists():
            matches = sorted(SNAPSHOTS_DIR.glob("*%s*" % args.snapshot))
            if not matches:
                print("No snapshot matching '%s'" % args.snapshot, file=sys.stderr)
                sys.exit(1)
            path = matches[-1]
    else:
        snaps = (sorted(SNAPSHOTS_DIR.glob("*.bin"), key=lambda p: p.stat().st_mtime)
                 if SNAPSHOTS_DIR.exists() else [])
        if not snaps:
            print("No snapshots found. Run `fetch` first or sign in to get a live copy.",
                  file=sys.stderr)
            sys.exit(1)
        path = snaps[-1]

    print("Dumping %s\n" % path)
    raw = path.read_bytes()
    pfr = _parse_raw(raw)
    print("PolicyFetchResponse (%d bytes), top-level fields:" % len(raw))
    for f, w, v in pfr:
        size = f"{len(v)} bytes" if isinstance(v, bytes) else str(v)
        print("  field %d (wire %d): %s" % (f, w, size))

    _, pd_bytes = _get_field(pfr, 3)
    if pd_bytes == None or pd_bytes == b"":
        print("\nNo PolicyData (field 3) found.")
        return
    pdf = _parse_raw(pd_bytes)
    print("\nPolicyData (%d bytes):" % len(pd_bytes))
    for f, w, v in pdf:
        if f in SENSITIVE_FIELDS:
            print("  field %d: [redacted, %d bytes]" % (f, len(v)))
        elif isinstance(v, bytes):
            try:
                print("  field %d: %r" % (f, v.decode("utf-8")))
            except UnicodeDecodeError:
                print("  field %d: <%d bytes>" % (f, len(v)))
        else:
            print("  field %d: %s" % (f, v))

    _, pv_bytes = _get_field(pdf, 4)
    if not pv_bytes:
        print("\nNo policy_value (field 4) found.")
        return
    pv = _parse_raw(pv_bytes)
    top_names = {f: n for n, (c, f) in _PN2F.items() if c == 0}
    chunk_names = {}
    for n, (c, f) in _PN2F.items():
        if c > 0:
            chunk_names[(c, f)] = n
    print("\nCloudPolicySettings (%d bytes, %d top-level fields):" % (len(pv_bytes), len(pv)))
    for f, w, v in sorted(pv, key=lambda t: t[0]):
        if f >= POLICY_LAST_TOP_LEVEL_ID + POLICY_ID_OFFSET + 1 and isinstance(v, bytes):
            chunk = f - (POLICY_LAST_TOP_LEVEL_ID + POLICY_ID_OFFSET)
            try:
                sub = _parse_raw(v)
            except Exception:
                print("  [%4d] subProto%d <unparseable %d bytes>" % (f, chunk, len(v)))
                continue
            print("  [%4d] subProto%d (%d policies):" % (f, chunk, len(sub)))
            for sf, sw, sv in sorted(sub, key=lambda t: t[0]):
                name = chunk_names.get((chunk, sf), "?")
                kind, val = _dec_pol(sv)
                print("         [%3d] %-43s %r  (%s)" % (sf, name, val, kind))
            continue
        name = top_names.get(f, "?")
        kind, val = _dec_pol(v)
        print("  [%4d] %-45s %r  (%s)" % (f, name, val, kind))

def _resolve_snapshot(label):
    path = Path(label)
    if path.exists():
        return path
    matches = sorted(SNAPSHOTS_DIR.glob("*%s*" % label))
    if not matches:
        print("No snapshot matching '%s'" % label, file=sys.stderr)
        sys.exit(1)
    return matches[-1]

def cmd_diff(args):
    path_a, path_b = _resolve_snapshot(args.a), _resolve_snapshot(args.b)
    print("A: %s" % path_a)
    print("B: %s\n" % path_b)

    def cs_fields(path):
        pfr = _parse_raw(path.read_bytes())
        _, pd_bytes = _get_field(pfr, 3)
        pdf = _parse_raw(pd_bytes) if pd_bytes else []
        _, pv_bytes = _get_field(pdf, 4)
        pv = _parse_raw(pv_bytes) if pv_bytes else []
        return {f: v for f, w, v in pv}

    a, b = cs_fields(path_a), cs_fields(path_b)
    id_to_name = {f: n for n, (c, f) in _PN2F.items() if c == 0}
    all_fields = sorted(set(a) | set(b))
    changed = 0
    for f in all_fields:
        if a.get(f) != b.get(f):
            changed += 1
            name = id_to_name.get(f, "?")
            _, va = _dec_pol(a.get(f))
            _, vb = _dec_pol(b.get(f))
            print("  [%4d] %-45s %r  ->  %r" % (f, name, va, vb))

    if changed == 0:
        print("No differences in CloudPolicySettings.")
    else:
        print("\n%d policy field(s) differ." % changed)

def _load_profile(name):
    p = PROFILES_DIR / ("%s.json" % name)
    if not p.exists():
        return {}
    return json.loads(p.read_text())

def _save_profile(name, policies):
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    p = PROFILES_DIR / ("%s.json" % name)
    p.write_text(json.dumps(policies, indent=2, sort_keys=True))
    print("Saved profile '%s' -> %s" % (name, p))

def _current_overrides():
    """Current inject changes, or None."""
    if not INJECT_STATE_FILE.exists():
        return None
    try:
        state = json.loads(INJECT_STATE_FILE.read_text())
    except Exception:
        return None
    overrides = state.get("overrides", {})
    if not overrides:
        return None
    return {k: (None if v == "UNSET" else v) for k, v in overrides.items()}

def _active_profile_name():
    """Name of the active inject profile, or None."""
    if not INJECT_STATE_FILE.exists():
        return None
    try:
        state = json.loads(INJECT_STATE_FILE.read_text())
    except Exception:
        return None
    return state.get("active_profile")

def _live_policy_as_profile():
    """All live policies as a dict. Returns (None, 0) if none."""
    pv_raw, err = _load_cs_raw()
    if err:
        return None, 0

    first_by_field = {}
    first_w2_by_field = {}
    for f, w, v in pv_raw:
        if f not in first_by_field:
            first_by_field[f] = v
        if w == 2 and f not in first_w2_by_field:
            first_w2_by_field[f] = v

    chunk_cache = {}
    result = {}
    skipped = 0
    for name in sorted(_PN2F.keys()):
        chunk, fnum = _PN2F[name]
        if chunk == 0:
            raw = first_by_field.get(fnum)
        else:
            if chunk not in chunk_cache:
                sub_bytes = first_w2_by_field.get(_subproto_cs_field(chunk))
                chunk_cache[chunk] = _parse_raw(sub_bytes) if sub_bytes is not None else None
            sub_raw = chunk_cache[chunk]
            raw = next((v for f, w, v in sub_raw if f == fnum), None) if sub_raw is not None else None
        kind, val = _dec_pol(raw)
        if kind == 'unset':
            continue
        if kind == 'raw':
            skipped += 1
            continue
        result[name] = val
    return result, skipped

def _backup_overrides(label):
    """Save current inject changes as a profile."""
    policies = _current_overrides()
    if policies is None:
        return None
    name = "backup-%s-%s" % (label, time.strftime("%Y%m%dT%H%M%S"))
    _save_profile(name, policies)
    return name

def cmd_local_list(args):
    print("=== Active local-override files in managed/ ===")
    _mkdir(MANAGED_DIR)
    files = list(MANAGED_DIR.glob("*.json"))
    if not files:
        print("  (none, Chrome is applying server policy only)")
        return
    for f in files:
        try:
            data = json.loads(f.read_text())
            print("  %s: %s" % (f.name, list(data.keys())[:8]))
        except Exception:
            print("  %s: (unreadable)" % f.name)

def cmd_local_apply(args):
    profile = _load_profile(args.name)
    if not profile:
        print("No profile '%s' found. Use profiles/edit to create one." % args.name, file=sys.stderr)
        sys.exit(1)
    _mkdir(MANAGED_DIR)
    dest = MANAGED_DIR / ("%s.json" % args.name)
    dest.write_text(json.dumps(profile, indent=2, sort_keys=True))
    print("Applied profile '%s' -> %s" % (args.name, dest))
    print("This applies to ALL users on this device, not just the current one.")
    print("Reload chrome://policy (or wait for Chrome's auto-refresh) to activate.")

def cmd_local_remove(args):
    dest = MANAGED_DIR / ("%s.json" % args.name)
    if dest.exists():
        dest.unlink()
        print("Removed %s" % dest)
    else:
        print("Not found: %s" % dest)

def cmd_local_clear(args):
    _mkdir(MANAGED_DIR)
    removed = 0
    for f in MANAGED_DIR.glob("*.json"):
        f.unlink()
        removed += 1
    print("Cleared %d file(s) from %s (these applied to ALL users)" % (removed, MANAGED_DIR))
    print("Chrome will now apply server policy only on next reload.")

def cmd_profiles(args):
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(PROFILES_DIR.glob("*.json"))
    if not files:
        print("No profiles saved yet. Use `edit --name <n> --set KEY=VALUE` to create one.")
        return
    print("=== Saved profiles ===")
    for f in files:
        try:
            data = json.loads(f.read_text())
            keys = list(data.keys())
            print("  %-20s  %d policies: %s%s" % (f.stem, len(keys), keys[:5], " ..." if len(keys)>5 else ""))
        except Exception:
            print("  %s: (unreadable)" % f.stem)

def cmd_profile_from_policy(args):
    print("Capturing current live policy...")
    sys.stdout.flush()
    policies, skipped = _live_policy_as_profile()
    if policies is None:
        print("ERROR: no live user policy found. Sign in first.", file=sys.stderr)
        sys.exit(1)
    _save_profile(args.name, policies)
    print("%d policies captured." % len(policies))
    if skipped:
        print("(%d value(s) couldn't be decoded and were skipped)" % skipped)

def cmd_edit(args):
    profile = _load_profile(args.name) if args.name else {}

    if not args.set and not args.unset:
        if profile:
            print("Profile '%s':" % args.name)
            for k, v in sorted(profile.items()):
                print("  %s = %s" % (k, json.dumps(v)))
        else:
            print("Profile '%s' is empty or does not exist yet." % args.name)
        print("\nUse --set KEY=VALUE (or --unset KEY) to modify it.")
        return

    changed = False
    for kv in (args.set or []):
        if "=" not in kv:
            print("  --set: expected KEY=VALUE, got '%s'" % kv, file=sys.stderr)
            continue
        k, v = kv.split("=", 1)
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError:
            parsed = v  # keep as string if not valid JSON
        profile[k] = parsed
        print("  SET %s = %s" % (k, json.dumps(parsed)))
        changed = True

    for k in (args.unset or []):
        if k in profile:
            del profile[k]
            print("  UNSET %s" % k)
            changed = True
        else:
            print("  %s not in profile (nothing to unset)" % k)

    if changed:
        _save_profile(args.name, profile)

def _user_email():
    live = _live_pol()
    if not live:
        return None
    raw = live.read_bytes()
    fields = _parse_raw(raw)
    _, pd = _get_field(fields, 3)
    if not pd:
        return None
    pdf = _parse_raw(pd)
    return next((v.decode() for f, w, v in pdf if f == 7 and isinstance(v, bytes)), None)

def cmd_status(args):
    _refresh_dm_key_backup_if_safe()
    inj_on = INJECT_STATE_FILE.exists()
    state = {}
    if inj_on:
        try:
            state = json.loads(INJECT_STATE_FILE.read_text())
        except Exception:
            state = {}

    title = "Status"
    width = 62
    print(_BCYAN + "+" + "-" * width + "+" + _RESET)
    print(_BCYAN + "|" + _RESET + _bold(title.center(width)) + _BCYAN + "|" + _RESET)
    print(_BCYAN + "+" + "-" * width + "+" + _RESET)
    print()

    def row(label, value):
        print("  %s%s%s %s" % (_DIM, label.ljust(17), _RESET, value))

    live = _live_pol()
    if live:
        user = _user_email() or "?"
        raw = live.read_bytes()
        fields = _parse_raw(raw)
        _, pd = _get_field(fields, 3)
        ts_str = ""
        if pd:
            pdf = _parse_raw(pd)
            ts_ms = next((v for f, w, v in pdf if f == 2 and isinstance(v, int)), None)
            if ts_ms:
                import datetime
                dt = datetime.datetime.utcfromtimestamp(ts_ms / 1000)
                ts_str = "  (last fetch %s UTC)" % dt.strftime("%Y-%m-%d %H:%M:%S")
        row("Account", _bold(user))
        row("Session", _BGREEN + "signed in" + _RESET + ts_str)
        pf, kf = _user_pol_files()
        if pf is not None:
            ok = _verify_live_pair(pf, kf)
            row("Live policy", (_BGREEN + "verifies" + _RESET) if ok else
                                (_BYELLOW + "does not verify, run `eject`" + _RESET))
    else:
        row("Session", _RED + "not signed in" + _RESET)

    print()
    overrides = state.get("overrides", {})
    if inj_on and overrides:
        resync = _need_resignin(state)
        word = (_BYELLOW + "not synced" + _RESET) if resync else (_BGREEN + "synced" + _RESET)
        row("Overrides", "%d changed, %s" % (len(overrides), word))
    else:
        row("Overrides", _dim("none, normal policy in effect"))

    active = state.get("active_profile") if inj_on else None
    row("Active profile", _bold(active) if active else _dim("none"))

    print()
    _mkdir(MANAGED_DIR)
    local_files = list(MANAGED_DIR.glob("*.json"))
    row("Local overrides", ("%d active" % len(local_files)) if local_files else _dim("none"))

    snaps = (sorted(SNAPSHOTS_DIR.glob("*.bin"), key=lambda p: p.stat().st_mtime)
             if SNAPSHOTS_DIR.exists() else [])
    row("Snapshots", ("%d saved (latest: %s)" % (len(snaps), snaps[-1].name)) if snaps
                       else _dim("none"))

    if not DM_KEY_BACKUP.exists():
        row("DM key backup", _dim("none yet"))
    elif snaps and _verify_pfr(_parse_raw(snaps[-1].read_bytes()), DM_KEY_BACKUP.read_bytes()):
        row("DM key backup", _BGREEN + "verifies latest snapshot" + _RESET)
    elif snaps:
        row("DM key backup", _RED + "Verification against latest snapshot failed" + _RESET)
    else:
        row("DM key backup", _dim("present"))

    profiles = sorted(PROFILES_DIR.glob("*.json")) if PROFILES_DIR.exists() else []
    row("Profiles saved", str(len(profiles)) if profiles else _dim("0"))

    blk = _blk_load()
    ips = blk.get("v4", []) + blk.get("v6", [])
    if ips:
        row("DM server", _BGREEN + "blocked (%d address%s)" %
                          (len(ips), "" if len(ips) == 1 else "es") + _RESET)
    elif inj_on and overrides and not _need_resignin(state):
        row("DM server", _RED + "not blocked, synced edits are unprotected" + _RESET)
    else:
        row("DM server", _dim("not blocked"))

    print()
    row("Policy mapping", "%d policies known" % len(_PN2F))
    note = _mapping_note()
    if note:
        print("  %s%s%s" % (_BYELLOW, note, _RESET))

def _atomic_write(path, data):
    """Write a file safely."""
    tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
    tmp.write_bytes(data)
    os.replace(str(tmp), str(path))

def _verify_live_pair(policy_file, key_file):
    """True if the key and policy files match."""
    try:
        pfr_raw = _parse_raw(policy_file.read_bytes())
        return _verify_pfr(pfr_raw, key_file.read_bytes())
    except Exception:
        return False

def _refresh_dm_key_backup_if_safe():
    """Update the saved DM key if inject is off."""
    if INJECT_STATE_FILE.exists():
        return
    pf, kf = _user_pol_files()
    if pf is None or kf is None:
        return
    if not _verify_live_pair(pf, kf):
        return
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    DM_KEY_BACKUP.write_bytes(kf.read_bytes())

def _verify_pfr(pfr_raw, pub_der):
    """True if the policy signature matches the public key."""
    try:
        from cryptography.hazmat.primitives import serialization, hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        pd_bytes = next((v for f, w, v in pfr_raw if f == 3 and w == 2), None)
        sig = next((v for f, w, v in pfr_raw if f == 4 and w == 2), None)
        if pd_bytes is None or sig is None:
            return False
        pub = serialization.load_der_public_key(pub_der)
        pub.verify(sig, pd_bytes, padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False

def _try_crypto():
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    return rsa, serialization, hashes, padding

def _need_crypto():
    try:
        return _try_crypto()
    except ImportError:
        pass

    print("The 'cryptography' package is missing. Trying to install it...")
    import subprocess
    attempts = [
        [sys.executable, "-m", "pip", "install", "--quiet", "cryptography"],
        [sys.executable, "-m", "ensurepip", "--default-pip"],
        ["emerge", "-q", "dev-python/cryptography"],
        ["apt-get", "install", "-y", "python3-cryptography"],
    ]
    for cmd in attempts:
        try:
            print("  trying: %s" % ' '.join(cmd))
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        except Exception:
            continue
        try:
            result = _try_crypto()
            print("  installed.")
            return result
        except ImportError:
            continue
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "cryptography"],
                           check=True, capture_output=True, timeout=120)
            return _try_crypto()
        except Exception:
            continue

    print("ERROR: could not install 'cryptography' automatically.\n"
          "Install it yourself, e.g. one of:\n"
          "  python3 -m pip install cryptography\n"
          "  emerge dev-python/cryptography\n"
          "  apt-get install python3-cryptography", file=sys.stderr)
    sys.exit(1)

def cmd_inject(args):
    # rewrite + resign the blob; use apply to push it live
    rsa_m, serial, hashes_m, pad = _need_crypto()

    policy_file, key_file = _user_pol_files()
    if policy_file == None:
        print("ERROR: User policy files not found.\n"
              "Sign in as the managed user first.",
              file=sys.stderr)
        sys.exit(1)
    print("  user: %s" % (_user_email() or "?"))

    overrides_json = {}

    if args.profile:
        if not (PROFILES_DIR / ("%s.json" % args.profile)).exists():
            print("ERROR: Profile '%s' not found." % args.profile, file=sys.stderr)
            sys.exit(1)
        overrides_json.update(_load_profile(args.profile))

    if args.also_active:
        if MANAGED_DIR.exists():
            for f in sorted(MANAGED_DIR.glob("*.json")):
                try:
                    overrides_json.update(json.loads(f.read_text()))
                except Exception:
                    pass

    recommended_names = set()
    for kv in (args.set or []):
        if "=" not in kv:
            print("  --set: expected KEY=VALUE, got '%s'" % kv, file=sys.stderr)
            continue
        k, v = kv.split("=", 1)
        if k.endswith("@recommended"):
            k = k[:-len("@recommended")]
            recommended_names.add(k)
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError:
            loc = _PN2F.get(k)
            cur_kind = None
            if loc is not None:
                raw, err = _cs_field(loc)
                if err != "no_live_policy":
                    cur_kind, _ = _dec_pol(raw)
            if cur_kind == 'list':
                print("ERROR: %s is a list, but '%s' isn't valid JSON "
                      "(use [\"a\", \"b\"], double quotes, no trailing comma)." % (k, v),
                      file=sys.stderr)
                sys.exit(1)
            parsed = v
        overrides_json[k] = parsed

    unset_names = list(getattr(args, "unset", None) or [])

    for name in [k for k, v in overrides_json.items() if v is None]:
        del overrides_json[name]
        unset_names.append(name)

    if not overrides_json and not unset_names:
        print("No overrides to inject.\n"
              "Use --profile NAME, `local apply NAME`, --set KEY=VALUE, or --unset KEY.",
              file=sys.stderr)
        sys.exit(1)

    _alog("inject requested: set=%s unset=%s" % (overrides_json, unset_names))

    loc_overrides = {}
    skipped = []
    for name, value in overrides_json.items():
        if name not in _PN2F:
            skipped.append(name)
            continue
        loc = _PN2F[name]
        loc_overrides[loc] = _encode_pol_value(value, name in recommended_names)
        chunk, fnum = loc
        print("  %s = %r%s" % (name, value, " (recommended)" if name in recommended_names else ""))

    for name in unset_names:
        if name not in _PN2F:
            skipped.append(name)
            continue
        loc = _PN2F[name]
        loc_overrides[loc] = None
        chunk, fnum = loc
        print("  %s = unset" % name)

    if skipped:
        print("  Skipped (unknown policy name): %s" % skipped)

    if not loc_overrides:
        print("No injectable fields found; run refresh-mapping?", file=sys.stderr)
        sys.exit(1)

    if INJECT_STATE_FILE.exists() and not args.force:
        state = json.loads(INJECT_STATE_FILE.read_text())
        print("\nWARNING: inject is already active (since %s)." % state.get('injected_at','?'))
        print("Run with --force to re-inject, or `eject` first.")
        sys.exit(1)

    pfr_raw  = _parse_raw(policy_file.read_bytes())
    pd_bytes = next((v for f,w,v in pfr_raw if f==3 and w==2), None)
    if pd_bytes is None:
        print("ERROR: no PolicyData (field 3) in policy file.", file=sys.stderr)
        sys.exit(1)
    pd_raw   = _parse_raw(pd_bytes)
    pv_bytes = next((v for f,w,v in pd_raw  if f==4 and w==2), None)
    if pv_bytes is None:
        print("ERROR: no policy_value (field 4) in PolicyData.", file=sys.stderr)
        sys.exit(1)
    pv_raw   = _parse_raw(pv_bytes)

    new_pv_bytes = _apply_loc_overrides(pv_raw, loc_overrides)
    new_pd_bytes = _reencode(pd_raw, {4: new_pv_bytes})

    key_created = False
    if INJECT_KEY_FILE.exists() and not args.new_key:
        priv_key = serial.load_pem_private_key(
            INJECT_KEY_FILE.read_bytes(), password=None)
    else:
        print("Generating key...")
        key_created = True
        priv_key = rsa_m.generate_private_key(public_exponent=65537, key_size=2048)
        pem = priv_key.private_bytes(
            serial.Encoding.PEM,
            serial.PrivateFormat.PKCS8,
            serial.NoEncryption(),
        )
        SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        INJECT_KEY_FILE.write_bytes(pem)
        INJECT_KEY_FILE.chmod(0o600)

    pub_key = priv_key.public_key()
    pub_der = pub_key.public_bytes(
        serial.Encoding.DER,
        serial.PublicFormat.SubjectPublicKeyInfo,
    )

    if not DM_KEY_BACKUP.exists():
        live_key_bytes = key_file.read_bytes()
        if _verify_pfr(pfr_raw, live_key_bytes):
            DM_KEY_BACKUP.write_bytes(live_key_bytes)
        else:
            print("ERROR: the current on-disk key doesn't verify the current "
                  "on-disk policy, so it can't be trusted as the real DM key.\n"
                  "Run `fetch` once while nothing is injected, then try again.",
                  file=sys.stderr)
            sys.exit(1)

    prev_state = {}
    if INJECT_STATE_FILE.exists():
        try:
            prev_state = json.loads(INJECT_STATE_FILE.read_text())
        except Exception:
            pass
    synced_hash = _synced_hash(pv_bytes, INJECT_STATE_FILE.exists())
    resignin_needed = (
        key_created
        or bool(getattr(args, "new_key", False))
        or hashlib.sha256(new_pv_bytes).hexdigest() != synced_hash
    )

    new_sig      = priv_key.sign(new_pd_bytes, pad.PKCS1v15(), hashes_m.SHA256())
    new_pfr_bytes = _reencode(pfr_raw, {3: new_pd_bytes, 4: new_sig,
                                        5: None, 6: None, 7: None, 8: None, 9: None})

    pfr_check = _parse_raw(new_pfr_bytes)
    pd_check  = next((v for f,w,v in pfr_check if f==3 and w==2), None)
    sig_check = next((v for f,w,v in pfr_check if f==4 and w==2), None)
    pub_check = serial.load_der_public_key(pub_der)
    pub_check.verify(sig_check, pd_check, pad.PKCS1v15(), hashes_m.SHA256())

    snaps = (sorted(SNAPSHOTS_DIR.glob("*.bin"), key=lambda p: p.stat().st_mtime)
             if SNAPSHOTS_DIR.exists() else [])
    state = {
        "injected_at":   time.strftime("%Y-%m-%dT%H:%M:%S"),
        "base_snapshot": str(snaps[-1]) if snaps else "",
        "policy_file":   str(policy_file),
        "key_file":      str(key_file),
        "overrides":     {**{k: str(v) for k, v in overrides_json.items()},
                          **{k: "UNSET" for k in unset_names}},
        "resignin_needed": resignin_needed,
        "chrome_pid_at_inject": _chrome_pid(),
        "active_profile": args.profile or prev_state.get("active_profile"),
    }

    _atomic_write(policy_file, new_pfr_bytes)
    _atomic_write(key_file, pub_der)
    INJECT_STATE_FILE.write_text(json.dumps(state, indent=2))
    SYNCED_STATE_FILE.write_text(json.dumps(
        {"hash": synced_hash, "pid": _chrome_pid()}))

    print("\n  Key file updated:    %s" % key_file)
    print("  Policy file updated: %s" % policy_file)
    print("\n" + "="*60)
    print("INJECTED.")
    print("  Saved to disk. `apply` to push it live, or `sign-out` to be sure.")
    print("  `eject` undoes this.")
    print("="*60)

def cmd_backup(args):
    name = _backup_overrides(args.label or "manual")
    if name is None:
        print("Nothing injected right now, nothing to back up.")
        return
    print("Bring it back later with:")
    print("  inject --profile %s --force" % name)

def cmd_eject(args):
    # undo inject: restore original key and policy
    if not INJECT_STATE_FILE.exists():
        print("No active inject found (no inject_state.json).", file=sys.stderr)
        sys.exit(1)

    state = json.loads(INJECT_STATE_FILE.read_text())
    print("=== Policy eject (injected at %s) ===" % state.get('injected_at', '?'))

    policy_file = Path(state.get("policy_file", ""))
    key_file    = Path(state.get("key_file", ""))

    if not policy_file.exists() or not key_file.exists():
        policy_file, key_file = _user_pol_files()

    if policy_file is not None and not _verify_live_pair(policy_file, key_file):
        print("WARNING: the current on-disk key/policy pair doesn't verify "
              "itself (torn by an interrupted write, sign-out, or restart). "
              "Restoring the real DM key now to fix that.", file=sys.stderr)

    if policy_file == None:
        print("No live policy mount right now. Waiting for you to sign in "
              "(up to 60s)...")
        for _ in range(300):
            time.sleep(0.2)
            policy_file, key_file = _user_pol_files()
            if policy_file is not None:
                print("  mount appeared, ejecting now.")
                break
    if policy_file is None:
        print("ERROR: still no live policy mount after waiting. Nothing to eject "
              "right now. Re-run `eject` once you've signed in.", file=sys.stderr)
        sys.exit(1)

    if not DM_KEY_BACKUP.exists():
        print("ERROR: DM key backup not found. Cannot eject without it.",
              file=sys.stderr)
        sys.exit(1)

    dm_pub = DM_KEY_BACKUP.read_bytes()

    base_snap = state.get("base_snapshot", "")
    restore_path = Path(base_snap) if base_snap and Path(base_snap).exists() else None
    if restore_path is None:
        snaps = sorted(SNAPSHOTS_DIR.glob("*.bin"),
                       key=lambda p: p.stat().st_mtime)
        inject_mtime = INJECT_STATE_FILE.stat().st_mtime
        pre = [s for s in snaps if s.stat().st_mtime < inject_mtime]
        if pre:
            restore_path = pre[-1]
        elif snaps:
            print("  WARNING: all snapshots post-date the inject; using latest anyway.")
            restore_path = snaps[-1]

    if restore_path is None:
        print("  WARNING: no snapshots found; policy file NOT restored.")
        print("  After sign-in Chrome will try to fetch fresh from the DM server.")
    else:
        restore_bytes = restore_path.read_bytes()
        if not _verify_pfr(_parse_raw(restore_bytes), dm_pub):
            print("ERROR: Verification against latest snapshot failed.\n"
                  "Run `sign-out` (or Sign out now in the menu), then sign back in.",
                  file=sys.stderr)
            sys.exit(1)
        _atomic_write(key_file, dm_pub)
        print("  Restored DM key: %s" % key_file)
        _atomic_write(policy_file, restore_bytes)
        print("  Restored policy: %s" % restore_path)

    backup_name = _backup_overrides("eject")

    INJECT_STATE_FILE.unlink()
    if SYNCED_STATE_FILE.exists():
        SYNCED_STATE_FILE.unlink()

    cmd_dm_block_stop()

    print("\n" + "="*60)
    print("EJECTED. Sign out and back in to activate the original DM policy.")
    if backup_name:
        print("Your changes were backed up. Bring them back with:")
        print("  inject --profile %s --force" % backup_name)
    print("="*60)
    return backup_name

def cmd_sign_out(args):
    import subprocess
    cmd_dm_block_stop()
    print("Ending the session now...")
    result = subprocess.run([
        "dbus-send", "--system", "--print-reply",
        "--dest=org.chromium.SessionManager",
        "/org/chromium/SessionManager",
        "org.chromium.SessionManagerInterface.StopSession", "string:",
    ], capture_output=True, text=True)
    if result.returncode != 0:
        print("Sign-out call failed:", file=sys.stderr)
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        sys.exit(1)

def cmd_repair_signin(args):
    print("=== Sign-in loop repair ===")
    print("For when the device keeps repeating sign-in after a policy edit")
    print("(a key-mismatch/PubkeySetIllegal crash loop).")
    print()

    email = args.user or _user_email()
    if not email:
        try:
            email = _eline("Account email to repair: ", "")
        except EOFError:
            print("Cancelled.", file=sys.stderr)
            sys.exit(1)
    if not email:
        print("ERROR: no account email given.", file=sys.stderr)
        sys.exit(1)

    print()
    print("Clearing local inject state...")
    if INJECT_STATE_FILE.exists():
        try:
            cmd_eject(argparse.Namespace())
        except SystemExit:
            print("  eject refused (key mismatch); clearing tracked state directly instead.")
            INJECT_STATE_FILE.unlink(missing_ok=True)
            if SYNCED_STATE_FILE.exists():
                SYNCED_STATE_FILE.unlink()
    cmd_dm_block_stop()
    print("  done. Try signing in again now, before going any further.")
    print()
    print("Still looping? Step 2 wipes ALL local data for %s" % email)
    print("(downloads, cached files, local app state, everything)")
    print("and wipes account on next reboot. This cannot be undone.")
    print()
    try:
        if _read_line_raw("Read the above carefully, then press Enter to continue, or esc to stop...") is _ESC_CANCEL:
            print("Stopped.")
            return
    except EOFError:
        return
    try:
        typed = _confirm("Wipe and reprovision %s? [y/N]: " % email)
    except EOFError:
        return
    if not typed:
        print("Stopped.")
        return

    import subprocess
    print("Removing local profile for %s ..." % email)
    result = subprocess.run(
        ["cryptohome", "--action=remove", "--user=%s" % email, "--force"],
        capture_output=True, text=True)
    if result.returncode != 0:
        print("ERROR: cryptohome removal failed:", file=sys.stderr)
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        sys.exit(1)

    if DM_KEY_BACKUP.exists():
        DM_KEY_BACKUP.unlink()
    for snap in SNAPSHOTS_DIR.glob("*.bin"):
        snap.unlink()
    if INJECT_STATE_FILE.exists():
        INJECT_STATE_FILE.unlink()
    if SYNCED_STATE_FILE.exists():
        SYNCED_STATE_FILE.unlink()

    print("Done. Sign in again to get a completely fresh profile.")

def _pol_desc(account_id):
    def varint(n):
        out = bytearray()
        while True:
            b = n & 0x7f
            n >>= 7
            if n:
                out.append(b | 0x80)
            else:
                out.append(b)
                break
        return bytes(out)

    def field_varint(fnum, val):
        return varint((fnum << 3) | 0) + varint(val)

    def field_bytes(fnum, b):
        return varint((fnum << 3) | 2) + varint(len(b)) + b

    return (field_varint(1, 1)
            + field_bytes(2, account_id.encode())
            + field_varint(3, 0))

def _dbus_call(member, sig, args):
    import socket
    import struct

    def enc_str(v):
        raw = v.encode()
        return struct.pack("<I", len(raw)) + raw + b"\0"

    def build(serial, dest, path, iface, mem, body_sig, body):
        fields = b""
        for code, t, v in ((1, "o", path), (2, "s", iface), (3, "s", mem), (6, "s", dest)):
            entry = bytes([code, 1]) + t.encode() + b"\0" + enc_str(v)
            fields += b"\0" * (-len(fields) % 8) + entry
        if body_sig:
            entry = bytes([8, 1]) + b"g\0" + bytes([len(body_sig)]) + body_sig.encode() + b"\0"
            fields += b"\0" * (-len(fields) % 8) + entry
        head = struct.pack("<BBBBII", ord("l"), 1, 0, 1, len(body), serial)
        head += struct.pack("<I", len(fields)) + fields
        return head + b"\0" * (-len(head) % 8) + body

    def marshal(body_sig, values):
        out = b""
        for a in values:
            out += b"\0" * (-len(out) % 4)
            if isinstance(a, bytes):
                out += struct.pack("<I", len(a)) + a
            else:
                out += enc_str(a)
        return out

    def read_exact(sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise OSError("bus closed")
            buf += chunk
        return buf

    def read_msg(sock):
        head = read_exact(sock, 16)
        body_len, serial, flen = struct.unpack("<III", head[4:16])
        fields = read_exact(sock, flen)
        read_exact(sock, -(16 + flen) % 8)
        body = read_exact(sock, body_len)
        info = {"type": head[1], "body": body}
        pos = 0
        while pos + 4 <= len(fields):
            pos += -pos % 8
            code, slen = fields[pos], fields[pos + 1]
            vsig = fields[pos + 2:pos + 2 + slen].decode()
            pos += 3 + slen
            if vsig in ("s", "o"):
                pos += -pos % 4
                ln = struct.unpack_from("<I", fields, pos)[0]
                info[code] = fields[pos + 4:pos + 4 + ln].decode()
                pos += 5 + ln
            elif vsig == "u":
                pos += -pos % 4
                info[code] = struct.unpack_from("<I", fields, pos)[0]
                pos += 4
            elif vsig == "g":
                ln = fields[pos]
                info[code] = fields[pos + 1:pos + 1 + ln].decode()
                pos += 2 + ln
            else:
                break
        return info

    def call(sock, serial, dest, path, iface, mem, body_sig, values):
        sock.sendall(build(serial, dest, path, iface, mem, body_sig, marshal(body_sig, values)))
        while True:
            msg = read_msg(sock)
            if msg["type"] in (2, 3) and msg.get(5) == serial:
                return msg

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(30)
    for path in ("/run/dbus/system_bus_socket", "/var/run/dbus/system_bus_socket"):
        try:
            sock.connect(path)
            break
        except OSError:
            continue
    else:
        raise OSError("no system bus socket")
    sock.sendall(b"\0AUTH EXTERNAL " + str(os.getuid()).encode().hex().encode() + b"\r\n")
    if read_exact(sock, 3) != b"OK ":
        raise OSError("dbus auth failed")
    while sock.recv(1) != b"\n":
        pass
    sock.sendall(b"BEGIN\r\n")
    call(sock, 1, "org.freedesktop.DBus", "/org/freedesktop/DBus",
         "org.freedesktop.DBus", "Hello", "", [])
    reply = call(sock, 2, "org.chromium.SessionManager", "/org/chromium/SessionManager",
                 "org.chromium.SessionManagerInterface", member, sig, args)
    sock.close()
    return reply

def _store_pol(descriptor_bytes, policy_bytes):
    try:
        reply = _dbus_call("StorePolicyEx", "ayay", [descriptor_bytes, policy_bytes])
    except Exception:
        reply = None
    if reply is not None:
        if reply["type"] == 2:
            return True, None
        body = reply["body"]
        msg = body[4:4 + int.from_bytes(body[:4], "little")].decode(errors="replace") if len(body) > 4 else ""
        return False, "Error: GDBus.Error:%s: %s" % (reply.get(4, "unknown"), msg)
    return _store_pol_gdbus(descriptor_bytes, policy_bytes)

def _store_pol_gdbus(descriptor_bytes, policy_bytes):
    import subprocess

    def literal(b):
        return "[byte " + ", ".join("0x%02x" % x for x in b) + "]"

    result = subprocess.run([
        "gdbus", "call", "--system",
        "--dest", "org.chromium.SessionManager",
        "--object-path", "/org/chromium/SessionManager",
        "--method", "org.chromium.SessionManagerInterface.StorePolicyEx",
        literal(descriptor_bytes), literal(policy_bytes),
    ], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return False, (result.stderr.strip() or result.stdout.strip())
    return True, None

def _restart_chrome(timeout=20):
    import signal

    old_pid = _chrome_pid()
    if old_pid is None:
        print("No live Chrome browser process found.", file=sys.stderr)
        return False
    try:
        os.kill(old_pid, signal.SIGTERM)
    except PermissionError:
        print("ERROR: not allowed to signal the Chrome process (pid %d, "
              "euid %d). Run this tool as root, or use `sign-out` instead."
              % (old_pid, os.geteuid()), file=sys.stderr)
        return False
    except ProcessLookupError:
        print("Chrome process disappeared before it could be restarted.", file=sys.stderr)
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        new_pid = _chrome_pid()
        if new_pid is not None and new_pid != old_pid:
            return True
        time.sleep(0.2)
    print("Chrome did not respawn within the timeout.", file=sys.stderr)
    return False

def cmd_apply(args):
    # StorePolicyEx + restart chrome
    if not INJECT_STATE_FILE.exists():
        print("Nothing injected. Use inject/toggle/unset first.", file=sys.stderr)
        sys.exit(1)

    policy_file, key_file = _user_pol_files()
    if policy_file is None:
        print("ERROR: user policy files not found. Sign in first.", file=sys.stderr)
        sys.exit(1)

    if not _verify_live_pair(policy_file, key_file):
        print("ERROR: the on-disk key/policy pair doesn't verify itself. "
              "Run `eject` first, then redo your edit.", file=sys.stderr)
        sys.exit(1)

    account_id = _user_email()
    if not account_id:
        print("ERROR: could not determine the signed-in account.", file=sys.stderr)
        sys.exit(1)

    descriptor = _pol_desc(account_id)
    policy_bytes = policy_file.read_bytes()

    print("Pushing...")
    ok, err = _store_pol(descriptor, policy_bytes)
    if not ok:
        print("Rejected: %s" % err, file=sys.stderr)
        print("session_manager doesn't trust this key yet. `sign-out` once "
              "to fix that.", file=sys.stderr)
        sys.exit(1)

    print("Accepted. Restarting Chrome...")
    if not _restart_chrome():
        sys.exit(1)

    cmd_dm_block_start()
    print("Done. Check chrome://policy.")

def cmd_restart_chrome(args):
    print("Restarting Chrome...")
    if not _restart_chrome():
        sys.exit(1)
    print("Done.")

class _Rec:
    def __init__(self, value):
        self.value = value

def _do_preset(changes):
    sets, unsets, skipped = [], [], []
    for name, val in changes.items():
        if name not in _PN2F:
            skipped.append(name)
            continue
        if val is None:
            unsets.append(name)
        elif isinstance(val, _Rec):
            sets.append("%s@recommended=%s" % (name, json.dumps(val.value)))
        else:
            sets.append("%s=%s" % (name, json.dumps(val)))
    if skipped:
        print("Skipping (not in the current mapping): %s" % ', '.join(skipped))
    if not sets and not unsets:
        print("Nothing in this preset applies right now.")
        return True
    cmd_inject(argparse.Namespace(
        profile=None, also_active=False, set=sets or None, unset=unsets or None,
        force=True, new_key=False))
    return _offer_so()

def _inject_profile(name):
    cmd_inject(argparse.Namespace(
        profile=name, also_active=False, set=None, unset=None,
        force=True, new_key=False))
    return _offer_so()

def _mirror_active_profile(active, name, value):
    if not active:
        return
    prof = _load_profile(active)
    prof[name] = value
    _save_profile(active, prof)

def _live_edit_profile_arg(active):
    """Profile to tag live edits with, or None."""
    if not active or _active_profile_name() == active:
        return None
    if not (PROFILES_DIR / ("%s.json" % active)).exists():
        _save_profile(active, {})
    return active

def _try_apply():
    if not INJECT_STATE_FILE.exists():
        print("Nothing injected. Use inject/toggle/unset first.")
        return True
    try:
        cmd_apply(argparse.Namespace())
        return True
    except SystemExit:
        pass
    print("\n%sSigning out...%s" % (_RED, _RESET))
    cmd_sign_out(argparse.Namespace())
    print("\nSign back in, then come back here.")
    _cls_end()
    try:
        input("\n%sPress Enter to continue...%s" % (_DIM, _RESET))
    except EOFError:
        pass
    return False

def _offer_so():
    if not INJECT_STATE_FILE.exists():
        return True
    try:
        state = json.loads(INJECT_STATE_FILE.read_text())
    except Exception:
        return True
    if not _need_resignin(state):
        return True
    print()
    print("%sNot synced yet. Trying `apply`...%s" % (_BYELLOW, _RESET))
    return _try_apply()

def _do_fetch():
    backup_name = None
    if INJECT_STATE_FILE.exists():
        backup_name = cmd_eject(argparse.Namespace())
    cmd_fetch(argparse.Namespace())
    return backup_name

MAPPING_FALLBACK_URL = "https://raw.githubusercontent.com/nmsjayden/dev/main/policy_mapping_fallback.json"
TOOL_UPDATE_URL = "https://raw.githubusercontent.com/nmsjayden/dev/main/dm_policy_tool.py"

def _parse_policies_yaml(text):
    import re as _re
    mapping = {}
    chunked_map = {}
    in_policies = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "policies:":
            in_policies = True
            continue
        if stripped == "atomic_groups:" or (line and not line.startswith(" ") and stripped != "policies:"):
            in_policies = False
            continue
        if in_policies:
            m = _re.match(r'\s*(\d+):\s*(\S+)', line)
            if m:
                pid, name = int(m.group(1)), m.group(2)
                if name in ("''", '""', 'None', '') or not name:
                    continue
                if name[0] in ("'", '"'):
                    continue
                if not name[0].isalpha() and name[0] != '_':
                    continue
                chunk, field = _chunk_and_field(pid)
                if chunk == 0:
                    mapping[field] = name
                else:
                    chunked_map[name] = {"chunk": chunk, "field": field}
    return mapping, chunked_map

def cmd_refresh_mapping(args):
    import urllib.request
    import base64

    url = ("https://chromium.googlesource.com/chromium/src/+/refs/heads/main/"
           "components/policy/resources/templates/policies.yaml?format=TEXT")
    mapping, chunked_map = {}, {}
    print("Fetching %s ..." % url)
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            raw = base64.b64decode(resp.read())
        mapping, chunked_map = _parse_policies_yaml(raw.decode())
    except Exception as e:
        print("  failed: %s" % e, file=sys.stderr)

    if len(mapping) < 500:
        print("Falling back to %s ..." % MAPPING_FALLBACK_URL)
        try:
            with urllib.request.urlopen(MAPPING_FALLBACK_URL, timeout=20) as resp:
                data = json.loads(resp.read())
            mapping = {int(k): v for k, v in data["mapping"].items()}
            chunked_map = data["chunked_map"]
            print("  using fallback saved %s (Chrome %s)" % (data.get("saved", "?"), data.get("chrome", "?")))
        except Exception as e:
            print("  fallback failed too: %s" % e, file=sys.stderr)

    if len(mapping) < 500:
        print("ERROR: only parsed %d top-level entries." % len(mapping), file=sys.stderr)
        sys.exit(1)

    if getattr(args, "save_fallback", None):
        Path(args.save_fallback).write_text(json.dumps({
            "chrome": _chrome_version(),
            "saved": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "mapping": mapping,
            "chunked_map": chunked_map,
        }, indent=0, sort_keys=True))
        print("Wrote fallback copy: %s" % args.save_fallback)

    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    POLICY_ID_MAP_FILE.write_text(json.dumps(mapping, indent=0))
    CHUNKED_MAP_FILE.write_text(json.dumps(chunked_map, indent=0, sort_keys=True))
    CHUNKED_POLICIES_FILE.write_text(json.dumps(sorted(chunked_map.keys()), indent=0))
    MAPPING_META_FILE.write_text(json.dumps(
        {"chrome": _chrome_version(), "saved": time.strftime("%Y-%m-%dT%H:%M:%S")}))
    if mapping.get(3) != "HomepageLocation":
        print("WARNING: field numbering doesn't match what this tool expects "
              "(HomepageLocation should be field 3). Chrome may have changed "
              "the layout; check for a tool update before injecting.", file=sys.stderr)
    print("Saved %d top-level + %d chunked mappings" % (len(mapping), len(chunked_map)))
    print("Restart tool (or re-run) to pick up.")

def cmd_update(args):
    import urllib.request

    try:
        own_path = Path(os.path.realpath(__file__))
    except NameError:
        print("ERROR: can't tell where this script is installed.", file=sys.stderr)
        sys.exit(1)

    print("Checking %s ..." % TOOL_UPDATE_URL)
    try:
        with urllib.request.urlopen(TOOL_UPDATE_URL, timeout=20) as resp:
            new_bytes = resp.read()
    except Exception as e:
        print("ERROR: could not reach GitHub: %s" % e, file=sys.stderr)
        sys.exit(1)

    if len(new_bytes) < 1000 or b"def cmd_status" not in new_bytes:
        print("ERROR: what came back doesn't look like the tool. Not installing it.",
              file=sys.stderr)
        sys.exit(1)

    try:
        own_bytes = own_path.read_bytes()
    except Exception as e:
        print("ERROR: can't read %s: %s" % (own_path, e), file=sys.stderr)
        sys.exit(1)

    if hashlib.sha256(new_bytes).digest() == hashlib.sha256(own_bytes).digest():
        print("Already up to date: %s" % own_path)
        return

    mode = own_path.stat().st_mode
    try:
        _atomic_write(own_path, new_bytes)
        own_path.chmod(mode)
    except PermissionError:
        print("ERROR: no write permission on %s. Try with sudo." % own_path, file=sys.stderr)
        sys.exit(1)

    print("Updated %s (%d -> %d bytes). Re-run to use the new version."
          % (own_path, len(own_bytes), len(new_bytes)))

def cmd_fix_mapping(args):
    overrides = {}
    if POLICY_OVERRIDE_FILE.exists():
        overrides = json.loads(POLICY_OVERRIDE_FILE.read_text())
    else:
        overrides.update(DEFAULT_OVR)

    old_name = next((n for n, (c, f) in _PN2F.items() if c == 0 and f == args.field), None)
    overrides[str(args.field)] = args.name
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    POLICY_OVERRIDE_FILE.write_text(json.dumps(overrides, indent=0))

    if old_name and old_name != args.name:
        print("field %s: %s -> %s" % (args.field, old_name, args.name))
    else:
        print("field %s: %s" % (args.field, args.name))
    print("Saved to %s. Restart the tool to pick it up." % POLICY_OVERRIDE_FILE)

LIST_RE = re.compile(
    r'(List|Urls?|Origins?|Forcelist|Blocklist|Allowlist|Whitelist|Blacklist|Extensions)$')
BOOL_RE = re.compile(
    r'(Enabled|Disabled|Allowed|Blocked|Required)$')

def _real_snap():
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    snaps = sorted(SNAPSHOTS_DIR.glob("*.bin"), key=lambda p: p.stat().st_mtime)
    if not DM_KEY_BACKUP.exists() or not snaps:
        return None
    dm_pub = serialization.load_der_public_key(DM_KEY_BACKUP.read_bytes())
    for snap in reversed(snaps):
        pfr = _parse_raw(snap.read_bytes())
        _, pd = _get_field(pfr, 3)
        _, sig = _get_field(pfr, 4)
        if not pd or not sig:
            continue
        try:
            dm_pub.verify(sig, pd, padding.PKCS1v15(), hashes.SHA256())
            return snap, pd
        except Exception:
            continue
    return None

def _cs_from_pd(pd_bytes):
    """Top-level policy fields from policy data."""
    pdf = _parse_raw(pd_bytes)
    _, pv_bytes = _get_field(pdf, 4)
    return {f: v for f, w, v in _parse_raw(pv_bytes)} if pv_bytes else {}

def _cs_locs_from_pd(pd_bytes):
    """All policy fields from policy data."""
    pdf = _parse_raw(pd_bytes)
    _, pv_bytes = _get_field(pdf, 4)
    if not pv_bytes:
        return {}
    out = {}
    for f, w, v in _parse_raw(pv_bytes):
        if (isinstance(v, bytes) and w == 2
                and f >= POLICY_LAST_TOP_LEVEL_ID + POLICY_ID_OFFSET + 1):
            chunk = f - (POLICY_LAST_TOP_LEVEL_ID + POLICY_ID_OFFSET)
            try:
                for sf, sw, sv in _parse_raw(v):
                    out[(chunk, sf)] = sv
            except Exception:
                continue
        else:
            out[(0, f)] = v
    return out

def cmd_verify_mapping(args):
    real_snap = _real_snap()
    if real_snap is None:
        print("Need at least one saved snapshot that verifies against the real "
              "DM key. Run `fetch` while NOT injected to get one.", file=sys.stderr)
        sys.exit(1)
    snap_path, pd_bytes = real_snap
    print("Checking against real snapshot: %s\n" % snap_path.name)
    raw_fields = {f: _dec_pol(v) for f, v in _cs_from_pd(pd_bytes).items()}
    id_to_name = {f: n for n, (c, f) in _PN2F.items() if c == 0}

    if args.export:
        export = json.loads(Path(args.export).read_text())
        cloud_policies = {
            name: info["value"]
            for name, info in export.get("policyValues", {}).get("chrome", {}).get("policies", {}).items()
            if info.get("source") == "cloud"
        }
        agree, disagree = [], []
        for name, exp_val in cloud_policies.items():
            for fnum, (kind, val) in raw_fields.items():
                matched = (
                    (kind == 'string' and isinstance(exp_val, str) and val == exp_val) or
                    (kind == 'list' and isinstance(exp_val, list)
                     and all(isinstance(x, str) for x in exp_val)
                     and set(val) == set(exp_val) and len(val) == len(exp_val))
                )
                if matched:
                    mapped_name = id_to_name.get(fnum)
                    (agree if mapped_name == name else disagree).append((fnum, name, mapped_name))

        for fnum, name, mapped_name in sorted(agree):
            print(f"  OK       field {fnum:>4}  {name}")
        for fnum, name, mapped_name in sorted(disagree):
            print(f"  WRONG    field {fnum:>4}  real={name!r}  current mapping says={mapped_name!r}")
            print("           fix with: %s fix-mapping %s %s" % (sys.argv[0], fnum, name))
        print("%d confirmed correct, %d wrong" % (len(agree), len(disagree)))
        return

    flagged = 0
    for f, (kind, val) in sorted(raw_fields.items()):
        name = id_to_name.get(f)
        if not name:
            continue
        looks_like_list = LIST_RE.search(name) is not None
        looks_like_bool = BOOL_RE.search(name) is not None
        is_short_scalar = kind in ('bool', 'int', 'unset')
        is_long_string = kind == 'string' and isinstance(val, str) and len(val) > 60
        if (looks_like_list and is_short_scalar) or (looks_like_bool and is_long_string):
            flagged += 1
            print(f"  SUSPECT  [{f:>4}] {name:<40} looks like {'a list' if looks_like_list else 'a bool'}, "
                  f"actually {kind}: {repr(val)[:70]}")

    if flagged == 0:
        print("No mismatches found among the fields this account currently has set.")
        print("This is a naming heuristic, not proof. Pass --export for a real check.")
    else:
        print("\n%s suspect mapping(s), unconfirmed. Check with `get`, then:" % flagged)
        print("  %s fix-mapping FIELD CorrectPolicyName" % sys.argv[0])

def cmd_list_policies(args):
    if not _PN2F:
        print("No mapping loaded. Run `%s refresh-mapping` first." % sys.argv[0],
              file=sys.stderr)
        sys.exit(1)
    names = sorted(_PN2F.keys())
    filt = getattr(args, "filter", None)
    if filt:
        names = [n for n in names if filt.lower() in n.lower()]
    for n in names:
        chunk, fnum = _PN2F[n]
        print("  %s  %s" % (_field_tag(chunk, fnum), n))
    suffix = " matching '%s'" % filt if filt else ""
    print("\n%d policies%s" % (len(names), suffix))

def _load_cs_raw():
    """Load live policy fields."""
    policy_file, _ = _user_pol_files()
    if policy_file is None:
        return None, "no_live_policy"
    pfr_raw = _parse_raw(policy_file.read_bytes())
    pd_bytes = next((v for f, w, v in pfr_raw if f == 3 and w == 2), None)
    if pd_bytes is None:
        return None, "no_policy_data"
    pd_raw = _parse_raw(pd_bytes)
    pv_bytes = next((v for f, w, v in pd_raw if f == 4 and w == 2), None)
    if pv_bytes is None:
        return None, "no_policy_value"
    return _parse_raw(pv_bytes), None

def _cs_field(loc):
    """Get one policy field from the live blob."""
    if isinstance(loc, tuple):
        chunk, fnum = loc
    else:
        chunk, fnum = 0, loc
    pv_raw, err = _load_cs_raw()
    if err:
        return None, err
    if chunk == 0:
        raw = next((v for f, w, v in pv_raw if f == fnum), None)
        return raw, None
    sub_f = _subproto_cs_field(chunk)
    sub_bytes = next((v for f, w, v in pv_raw if f == sub_f and w == 2), None)
    if sub_bytes is None:
        return None, None  # unset
    sub_raw = _parse_raw(sub_bytes)
    raw = next((v for f, w, v in sub_raw if f == fnum), None)
    return raw, None

def _encode_pol_value(value, recommended=False):
    """Encode a policy value."""
    body = _encode_pol_body(value)
    if recommended:
        return _enc_field(1, 2, _enc_field(1, 0, 1)) + body
    return body

def _encode_pol_body(value):
    ptype = _ptype(value)
    if ptype == 'bool':
        bval = value if isinstance(value, bool) else str(value).lower() == 'true'
        return _enc_field(2, 0, 1 if bval else 0)
    if ptype == 'int':
        return _enc_field(2, 0, int(value))
    if isinstance(value, list) and all(isinstance(e, str) for e in value):
        entries = b"".join(_enc_field(1, 2, e.encode()) for e in value)
        return _enc_field(2, 2, entries)
    str_value = value if isinstance(value, str) else (
        json.dumps(value) if isinstance(value, (dict, list)) else str(value))
    return _enc_field(2, 2, str_value.encode())

def _apply_loc_overrides(pv_raw, loc_overrides):
    """Apply field overrides to policy data."""
    top = {}
    by_chunk = {}
    for (chunk, fnum), val in loc_overrides.items():
        if chunk == 0:
            top[fnum] = val
        else:
            by_chunk.setdefault(chunk, {})[fnum] = val

    fields = list(pv_raw)
    for chunk, inner_ov in by_chunk.items():
        sub_f = _subproto_cs_field(chunk)
        sub_bytes = next((v for f, w, v in fields if f == sub_f and w == 2), None)
        sub_raw = _parse_raw(sub_bytes) if sub_bytes else []
        new_sub = _reencode(sub_raw, inner_ov)
        fields = _parse_raw(_reencode(fields, {sub_f: new_sub}))
    return _reencode(fields, top)

def _dec_strlist(field2_bytes):
    try:
        nested = _parse_raw(field2_bytes)
    except (ValueError, IndexError):
        nested = []
    if nested and all(f == 1 and w == 2 for f, w, v in nested):
        try:
            return ('list', [v.decode('utf-8') for f, w, v in nested])
        except UnicodeDecodeError:
            pass
    try:
        return ('string', field2_bytes.decode('utf-8'))
    except UnicodeDecodeError:
        return ('raw', field2_bytes)

def _dec_pol(raw_bytes):
    if raw_bytes is None:
        return ('unset', None)
    fields = _parse_raw(raw_bytes)
    for f, w, v in fields:
        if f == 2:  # value field on *PolicyProto
            if w == 0:
                return ('int', v)
            elif w == 2:
                return _dec_strlist(v)
    return ('raw', raw_bytes)

def cmd_get(args):
    if args.name not in _PN2F:
        print("ERROR: unknown policy %s (try refresh-mapping)" % args.name, file=sys.stderr)
        sys.exit(1)
    loc = _PN2F[args.name]
    chunk, fnum = loc
    raw, err = _cs_field(loc)
    if err == "no_live_policy":
        print("ERROR: no live user policy found. Sign in first.", file=sys.stderr)
        sys.exit(1)
    kind, val = _dec_pol(raw)
    print("%s  (field %d)" % (args.name, fnum))
    if kind == 'unset':
        print("  not set on this blob, Chrome uses its built-in default")
    elif kind == 'int' and val in (0, 1) and _looks_bool(args.name):
        print("  current value: %s" % ("true" if val else "false"))
    elif kind == 'string':
        print("  current value: %s" % val)
    elif kind == 'list':
        print("  current value: %s" % (val if val else "[]"))
    else:
        print("  current value: %r  (%s)" % (val, kind))

def cmd_toggle(args):
    if args.name not in _PN2F:
        print("ERROR: unknown policy %s" % args.name, file=sys.stderr)
        sys.exit(1)
    loc = _PN2F[args.name]
    raw, err = _cs_field(loc)
    if err == "no_live_policy":
        print("ERROR: no live user policy found. Sign in first.", file=sys.stderr)
        sys.exit(1)
    kind, val = _dec_pol(raw)
    if kind == 'unset':
        cur = 0
    elif kind == 'int' and val in (0, 1):
        cur = int(val)
    else:
        print("ERROR: %s is not a 0/1 value (%s=%r), set it explicitly with inject" % (
            args.name, kind, val), file=sys.stderr)
        sys.exit(1)
    new_val = 0 if cur else 1
    print("%s: %s -> %s" % (args.name, cur, new_val))
    inject_args = argparse.Namespace(
        profile=None, also_active=False,
        set=["%s=%s" % (args.name, new_val)], unset=None, force=True, new_key=False)
    cmd_inject(inject_args)

def cmd_unset(args):
    if args.name not in _PN2F:
        print("ERROR: unknown policy %s" % args.name, file=sys.stderr)
        sys.exit(1)
    loc = _PN2F[args.name]
    raw, err = _cs_field(loc)
    if err == "no_live_policy":
        print("ERROR: no live user policy found. Sign in first.", file=sys.stderr)
        sys.exit(1)
    kind, _ = _dec_pol(raw)
    if kind == 'unset':
        print("%s is already unset." % args.name)
        return
    inject_args = argparse.Namespace(
        profile=None, also_active=False,
        set=None, unset=[args.name], force=True, new_key=False)
    cmd_inject(inject_args)

def _chrome_pid():
    try:
        entries = [p.name for p in Path("/proc").iterdir() if p.name.isdigit()]
    except OSError:
        return None
    for pid in entries:
        try:
            cmdline = Path("/proc/%s/cmdline" % pid).read_bytes()
        except OSError:
            continue
        parts = cmdline.split(b"\x00")
        if not parts or not parts[0].endswith(b"/chrome/chrome"):
            continue
        if any(p.startswith(b"--type=") for p in parts):
            continue
        return int(pid)
    return None

def _synced_hash(pv_bytes, injected):
    current = _chrome_pid()
    try:
        rec = json.loads(SYNCED_STATE_FILE.read_text())
        if (injected and rec.get("hash")
                and (rec.get("pid") is None or current is None
                     or rec["pid"] == current)):
            return rec["hash"]
    except Exception:
        pass
    return hashlib.sha256(pv_bytes).hexdigest()

def _need_resignin(state):
    if not state.get("resignin_needed"):
        return False
    recorded = state.get("chrome_pid_at_inject")
    current = _chrome_pid()
    if recorded is not None and current is not None and current != recorded:
        return False
    return True

def _dm_addrs():
    import socket as _socket
    v4, v6 = set(), set()
    for fam, _, _, _, sockaddr in _socket.getaddrinfo(DM_SERVER_HOST, 443, proto=_socket.IPPROTO_TCP):
        if fam == _socket.AF_INET:
            v4.add(sockaddr[0])
        elif fam == _socket.AF_INET6:
            v6.add(sockaddr[0])
    return v4, v6

def _blk_load():
    if DM_BLOCK_STATE_FILE.exists():
        try:
            return json.loads(DM_BLOCK_STATE_FILE.read_text())
        except Exception:
            pass
    return {"v4": [], "v6": []}

def _blk_save(state):
    DM_BLOCK_STATE_FILE.write_text(json.dumps(state, indent=2))

def _blk_add(binary, ip, port):
    import subprocess
    check = subprocess.run([binary, "-C", "OUTPUT", "-d", ip, "-p", "tcp",
                            "--dport", str(port), "-j", "REJECT"],
                           capture_output=True)
    if check.returncode != 0:
        subprocess.run([binary, "-I", "OUTPUT", "1", "-d", ip, "-p", "tcp",
                        "--dport", str(port), "-j", "REJECT"], check=True)

def _blk_rm(binary, ip, port):
    import subprocess
    subprocess.run([binary, "-D", "OUTPUT", "-d", ip, "-p", "tcp",
                    "--dport", str(port), "-j", "REJECT"], capture_output=True)

def cmd_dm_block_start(args=None):
    v4_addrs, v6_addrs = _dm_addrs()
    state = _blk_load()
    for ip in v4_addrs:
        for port in (80, 443):
            _blk_add("iptables", ip, port)
        if ip not in state["v4"]:
            state["v4"].append(ip)
    for ip in v6_addrs:
        for port in (80, 443):
            _blk_add("ip6tables", ip, port)
        if ip not in state["v6"]:
            state["v6"].append(ip)
    _blk_save(state)
    ips = state["v4"] + state["v6"]
    print(f"DM server blocked ({len(ips)} address{'es' if len(ips) != 1 else ''}).")

def cmd_dm_block_stop(args=None):
    state = _blk_load()
    had_any = bool(state.get("v4") or state.get("v6"))
    for ip in state.get("v4", []):
        for port in (80, 443):
            _blk_rm("iptables", ip, port)
    for ip in state.get("v6", []):
        for port in (80, 443):
            _blk_rm("ip6tables", ip, port)
    if DM_BLOCK_STATE_FILE.exists():
        DM_BLOCK_STATE_FILE.unlink()
    if had_any:
        print("DM server block removed.")

def cmd_dm_block_status(args=None):
    state = _blk_load()
    ips = state.get("v4", []) + state.get("v6", [])
    if not ips:
        print("DM server (%s): not blocked" % DM_SERVER_HOST)
    else:
        print("DM server (%s): BLOCKED, %s" % (DM_SERVER_HOST, ', '.join(ips)))

def cmd_dm_block(args):
    {"start": cmd_dm_block_start, "stop": cmd_dm_block_stop,
     "status": cmd_dm_block_status}[args.block_cmd](args)

def _field_tag(chunk, fnum):
    if chunk == 0:
        return "%5d" % fnum
    return "%d:%03d" % (chunk, fnum)

def _looks_bool(name):
    """True if the policy name looks like a boolean."""
    if name.endswith(("Settings", "Availability", "Behavior", "Mode")):
        if name.endswith(("Enabled", "Disabled", "Allowed")):
            return True
        return False
    boolish = (
        "Enabled", "Disabled", "Allowed", "Required", "Managed",
        "Deleted", "Blocked", "Forced", "Visible", "Hidden",
    )
    if any(name.endswith(s) for s in boolish):
        return True
    if name.startswith(("Allow", "Disable", "Show", "Hide")):
        return True
    return False

def _fmt_val(name, kind, val):
    if kind == 'unset':
        return _dim("not set")
    if kind == 'int' and val in (0, 1) and _looks_bool(name):
        return (_BGREEN + "true" + _RESET) if val else (_RED + "false" + _RESET)
    if kind == 'int':
        return _bold(str(val))
    if kind == 'string':
        low = val.lower() if isinstance(val, str) else str(val)
        if low in ("true", "enabled", "allow", "allowed"):
            return _BGREEN + val + _RESET
        if low in ("false", "disabled", "disallow", "disallowed", "block", "blocked"):
            return _RED + val + _RESET
        return _bold(val if isinstance(val, str) else str(val))
    if kind == 'list':
        if not val:
            return _dim("[]")
        if len(val) <= 3 and all(isinstance(x, str) and len(x) < 24 for x in val):
            return _bold("[" + ", ".join(val) + "]")
        return _bold("[%d items]" % len(val))
    if kind == 'raw':
        return _dim("<%d bytes>" % len(val))
    return _bold(repr(val))

def _row_lbl(name, chunk, fnum, kind, val, width):
    tag = _field_tag(chunk, fnum)
    val_str = _fmt_val(name, kind, val)
    return "%s[%s]%s %s   %s" % (_DIM, tag, _RESET, name.ljust(width), val_str)

def _py_kind(val):
    if val is None:
        return ('unset', None)
    if isinstance(val, bool):
        return ('int', 1 if val else 0)
    if isinstance(val, int):
        return ('int', val)
    if isinstance(val, list):
        return ('list', val)
    return ('string', val if isinstance(val, str) else str(val))

def _browse(profile_name=None):
    all_names = sorted(_PN2F.keys())
    if not all_names:
        print("No policy mapping loaded. Run `refresh-mapping` first.")
        try:
            input("\n%sPress Enter to continue...%s" % (_DIM, _RESET))
        except EOFError:
            pass
        return

    jump_map = {}
    for n, (c, f) in _PN2F.items():
        jump_map[_field_tag(c, f).strip()] = n
        if c == 0:
            jump_map[str(f)] = n

    filtered = all_names
    sel = 0
    window_start = 0
    query = ""
    digit_buf = ""
    page_size = max(5, shutil.get_terminal_size((80, 24)).lines - 12)
    pending_auto_profile = ("live-%s" % time.strftime("%Y%m%dT%H%M%S")) if profile_name is None else None

    while True:
        if not filtered:
            filtered = all_names
        sel = max(0, min(sel, len(filtered) - 1))
        if sel < window_start:
            window_start = sel
        if sel >= window_start + page_size:
            window_start = sel - page_size + 1
        window_start = max(0, min(window_start, max(0, len(filtered) - page_size)))
        visible = filtered[window_start:window_start + page_size]

        name_width = 60

        profile = _load_profile(profile_name) if profile_name else None
        active = _active_profile_name() if profile_name is None else None
        if profile_name is None and active is None:
            active = pending_auto_profile

        # index live policy once per frame (not per row)
        live_first = {}
        live_first_w2 = {}
        live_chunk_cache = {}
        live_err = None
        if profile_name is None:
            pv_raw, live_err = _load_cs_raw()
            if live_err is None:
                for f, w, v in pv_raw:
                    if f not in live_first:
                        live_first[f] = v
                    if w == 2 and f not in live_first_w2:
                        live_first_w2[f] = v

        def _live_raw(chunk, fnum):
            if chunk == 0:
                return live_first.get(fnum)
            if chunk not in live_chunk_cache:
                sub_bytes = live_first_w2.get(_subproto_cs_field(chunk))
                live_chunk_cache[chunk] = _parse_raw(sub_bytes) if sub_bytes is not None else None
            sub_raw = live_chunk_cache[chunk]
            if sub_raw is None:
                return None
            return next((v for f, w, v in sub_raw if f == fnum), None)

        rows = []
        for n in visible:
            chunk, fnum = _PN2F[n]
            if profile_name is not None:
                kind, val = _py_kind(profile.get(n))
            else:
                if live_err == "no_live_policy":
                    _cls()
                    print(_RED + "No live user policy found. Sign in first." + _RESET)
                    _cls_end()
                    try:
                        input("\n%sPress Enter to continue...%s" % (_DIM, _RESET))
                    except EOFError:
                        pass
                    return
                kind, val = _dec_pol(_live_raw(chunk, fnum))
            rows.append((n, chunk, fnum, kind, val))

        _cls()
        if profile_name is not None:
            title = "Profile: %s  %d / %d" % (profile_name, sel + 1, len(filtered))
        else:
            title = "Policies  %d / %d" % (sel + 1, len(filtered))
            if active:
                title += "  (profile: %s)" % active
        if query:
            title += "  [%s]" % query
        content_w = name_width + 28
        width = max(52, len(title) + 4, content_w)
        print(_BCYAN + "+" + "-" * width + "+" + _RESET)
        print(_BCYAN + "|" + _RESET + _bold(title.center(width)) + _BCYAN + "|" + _RESET)
        print(_BCYAN + "+" + "-" * width + "+" + _RESET)
        print()
        for i, (n, chunk, fnum, kind, val) in enumerate(rows):
            label = _row_lbl(n, chunk, fnum, kind, val, name_width)
            row_idx = window_start + i
            if row_idx == sel:
                print("  %s>%s %s" % (_BGREEN, _RESET, label))
            else:
                print("    %s" % label)
        print()

        if profile_name is not None:
            word = "%d %s in this profile" % (len(profile), "policy" if len(profile) == 1 else "policies")
            print((_DIM + word + _RESET).center(width + len(_DIM) + len(_RESET)))
        else:
            inj_state = {}
            if INJECT_STATE_FILE.exists():
                try:
                    inj_state = json.loads(INJECT_STATE_FILE.read_text())
                except Exception:
                    pass
            if inj_state and _need_resignin(inj_state):
                word, color = "not synced", _YELLOW
            elif INJECT_STATE_FILE.exists():
                word, color = "synced", _BGREEN
            else:
                word, color = "no changes", _DIM
            padded = word.center(width)
            print(padded.replace(word, "%s%s%s" % (color, word, _RESET), 1))
        if digit_buf:
            print(("go %s" % digit_buf).center(width))
        if profile_name is not None:
            print(_DIM + "\n  [up/down] move  [enter] edit  [/] search  [x] remove  "
                         "[a] inject  [id+enter] jump  [esc] back" + _RESET)
        else:
            print(_DIM + "\n  [up/down] move  [enter] edit  [/] search  [x] unset  "
                         "[a] apply  [id+enter] jump  [esc] back" + _RESET)
        _cls_end()

        key = _key()
        if key == 'UP':
            sel -= 1
            digit_buf = ""
        elif key == 'DOWN':
            sel += 1
            digit_buf = ""
        elif key == 'ESC':
            if query:
                query = ""
                filtered = all_names
                sel = 0
                window_start = 0
                digit_buf = ""
            else:
                return
        elif key == '/':
            try:
                query = _eline("\n%sSearch: %s" % (_CYAN, _RESET), "").strip()
            except EOFError:
                return
            filtered = ([n for n in all_names if query.lower() in n.lower()]
                        if query else all_names)
            sel = 0
            window_start = 0
            digit_buf = ""
        elif key == 'BACKSPACE':
            digit_buf = digit_buf[:-1]
        elif key == ':':
            digit_buf += ':'
        elif isinstance(key, str) and key.isdigit():
            digit_buf += key
        elif key in ('x', 'X'):
            name, chunk, fnum, kind, val = rows[sel - window_start]
            if profile_name is not None:
                if name in profile:
                    del profile[name]
                    _save_profile(profile_name, profile)
            elif kind != 'unset':
                with _quiet():
                    cmd_inject(argparse.Namespace(
                        profile=_live_edit_profile_arg(active), also_active=False,
                        set=None, unset=[name], force=True, new_key=False))
                _mirror_active_profile(active, name, None)
        elif key in ('a', 'A'):
            _cls()
            _cls_end()
            if profile_name is not None:
                if not profile:
                    print("Profile is empty, nothing to inject.")
                    _cls_end()
                    try:
                        input("\n%sPress Enter to continue...%s" % (_DIM, _RESET))
                    except EOFError:
                        return
                    continue
                try:
                    ok = _inject_profile(profile_name)
                except SystemExit:
                    ok = False
            else:
                ok = _try_apply()
            _cls_end()
            if ok:
                try:
                    input("\n%sPress Enter to continue...%s" % (_DIM, _RESET))
                except EOFError:
                    return
        elif key == 'ENTER':
            if digit_buf:
                target_name = jump_map.get(digit_buf.strip())
                digit_buf = ""
                if target_name is None:
                    continue
                if target_name not in filtered:
                    query = ""
                    filtered = all_names
                sel = filtered.index(target_name)
                window_start = max(0, sel - page_size // 2)
                continue
            name, chunk, fnum, kind, val = rows[sel - window_start]
            if kind == 'int' and val in (0, 1):
                if profile_name is not None:
                    profile[name] = not bool(val)
                    _save_profile(profile_name, profile)
                else:
                    with _quiet():
                        cmd_inject(argparse.Namespace(
                            profile=_live_edit_profile_arg(active), also_active=False,
                            set=["%s=%s" % (name, 0 if val else 1)],
                            unset=None, force=True, new_key=False))
                    _mirror_active_profile(active, name, not bool(val))
            else:
                _cls()
                if kind == 'unset':
                    default = ""
                elif kind == 'list':
                    default = json.dumps(val)
                else:
                    default = str(val)
                print("Editing %s:" % _bold(name))
                if kind == 'list':
                    print(_DIM + 'list: keep it as a JSON array, e.g. ["a", "b"]' + _RESET)
                _cls_end()
                new_val = _eline("> ", default)
                if not new_val:
                    new_val = default
                if new_val:
                    if profile_name is not None:
                        try:
                            parsed = json.loads(new_val)
                        except json.JSONDecodeError:
                            parsed = new_val
                        profile[name] = parsed
                        _save_profile(profile_name, profile)
                    else:
                        with _quiet():
                            cmd_inject(argparse.Namespace(
                                profile=_live_edit_profile_arg(active), also_active=False,
                                set=["%s=%s" % (name, new_val)],
                                unset=None, force=True, new_key=False))
                        try:
                            parsed = json.loads(new_val)
                        except json.JSONDecodeError:
                            parsed = new_val
                        _mirror_active_profile(active, name, parsed)

class _EolStdout:
    def __init__(self, real):
        self._real = real

    def write(self, s):
        return self._real.write(s.replace("\n", "\033[K\n"))

    def __getattr__(self, name):
        return getattr(self._real, name)

_REAL_STDOUT = sys.stdout

@contextlib.contextmanager
def _quiet():
    old = sys.stdout
    try:
        sys.stdout = open(os.devnull, "w")
        yield
    finally:
        sys.stdout.close()
        sys.stdout = old

def _cls():
    if _COLOR:
        sys.stdout = _EolStdout(_REAL_STDOUT)
        _REAL_STDOUT.write("\033[H")
        _REAL_STDOUT.flush()

def _cls_end():
    if _COLOR:
        sys.stdout = _REAL_STDOUT
        sys.stdout.write("\033[J")
        sys.stdout.flush()

UNMANAGE_SET = {
    "DeveloperToolsAvailability": 1,
    "ExtensionDeveloperModeSettings": 0,
    "VmManagementCliAllowed": True,
    "SystemTerminalSshAllowed": True,
    "CrostiniAllowed": True,
    "CrostiniRootAccessAllowed": True,
    "CrostiniExportImportUIAllowed": True,
    "CrostiniPortForwardingAllowed": True,
    "CrostiniArcAdbSideloadingAllowed": True,
    "ArcEnabled": True,
    "UserBorealisAllowed": True,
    "UserPluginVmAllowed": True,
    "PluginVmAllowed": True,
    "InstantTetheringAllowed": True,
    "SmsMessagesAllowed": True,
    "NearbyShareAllowed": True,
    "SmartLockSigninAllowed": True,
    "PhoneHubAllowed": True,
    "ClassManagementEnabled": "disabled",
    "ClassManagementViewScreenEnabled": False,
    "ClassManagementCaptionsEnabled": False,
    "ClassManagementClassroomIntegrationEnabled": False,
    "ClassManagementNetworkRestrictionEnabled": False,
    "CastReceiverEnabled": True,
    "AccessCodeCastEnabled": True,
    "ChromeOsMultiProfileUserBehavior": "unrestricted",
    "ShowFullUrlsInAddressBar": True,
}

UNMANAGE_UNLOCK = {
    "AllowDinosaurEasterEgg": _Rec(True),
    "AllowPopupsDuringPageUnload": _Rec(False),
    "AllowSyncXHRInPageDismissal": _Rec(False),
    "AllowedLocalAuthFactors": _Rec(['ALL']),
    "ArcBackupRestoreServiceEnabled": _Rec(0),
    "ArcGoogleLocationServicesEnabled": _Rec(0),
    "CaptivePortalAuthenticationIgnoresProxy": _Rec(False),
    "DnsOverHttpsMode": _Rec('automatic'),
    "EasyUnlockAllowed": _Rec(True),
    "EmojiPickerGifSupportEnabled": _Rec(True),
    "EmojiSuggestionEnabled": _Rec(False),
    "FastPairEnabled": _Rec(True),
    "FocusModeSoundsEnabled": _Rec('disabled'),
    "GenAiDefaultSettings": _Rec(0),
    "GlanceablesEnabled": _Rec(True),
    "GoogleWorkspaceCloudUpload": _Rec('allowed'),
    "LacrosAllowed": _Rec(False),
    "LacrosAvailability": _Rec('user_choice'),
    "LacrosSecondaryProfilesAllowed": _Rec(True),
    "LacrosSelection": _Rec('user_choice'),
    "LoginDisplayPasswordButtonEnabled": _Rec(True),
    "MicrosoftOfficeCloudUpload": _Rec('allowed'),
    "MicrosoftOneDriveAccountRestrictions": _Rec(['common']),
    "MicrosoftOneDriveMount": _Rec('allowed'),
    "NTLMShareAuthenticationEnabled": _Rec(True),
    "NTPCustomBackgroundEnabled": _Rec(True),
    "NativeClientForceAllowed": _Rec(False),
    "NetBiosShareDiscoveryEnabled": _Rec(True),
    "OrcaEnabled": _Rec(True),
    "OsColorMode": _Rec('light'),
    "PinUnlockAutosubmitEnabled": _Rec(False),
    "QuickOfficeForceFileDownloadEnabled": _Rec(True),
    "QuickUnlockModeAllowlist": _Rec(['all']),
    "QuickUnlockModeWhitelist": _Rec(['all']),
    "RecoveryFactorBehavior": _Rec(True),
    "ShowAiIntroScreenEnabled": _Rec(True),
    "ShowCastSessionsStartedByOtherDevices": _Rec(True),
    "ShowDisplaySizeScreenEnabled": _Rec(True),
    "ShowGeminiIntroScreenEnabled": _Rec(True),
    "ShowHumanPresenceSensorScreenEnabled": _Rec(True),
    "ShowTouchpadScrollScreenEnabled": _Rec(True),
    "SuggestedContentEnabled": _Rec(True),
    "WifiSyncAndroidAllowed": _Rec(False),
}

def _unmanage():
    """Clear live policies, then set the Unmanage defaults."""
    pol_file, _ = _user_pol_files()
    if pol_file is None:
        return {**UNMANAGE_UNLOCK, **UNMANAGE_SET}
    _, pd_bytes = _get_field(_parse_raw(pol_file.read_bytes()), 3)
    if not pd_bytes:
        return {**UNMANAGE_UNLOCK, **UNMANAGE_SET}
    locs = _cs_locs_from_pd(pd_bytes)
    loc_to_name = {loc: n for n, loc in _PN2F.items()}
    overrides = {loc_to_name[loc]: None for loc in locs if loc in loc_to_name}
    overrides.update(UNMANAGE_UNLOCK)
    overrides.update(UNMANAGE_SET)
    return overrides

PRESETS = {
    "Unmanage": _unmanage,
    "Dev tools": {
        "DeveloperToolsDisabled": False,
        "DeveloperToolsAvailability": None,
        "VmManagementCliAllowed": True,
        "SystemTerminalSshAllowed": True,
        "CrostiniAllowed": True,
        "CrostiniPortForwardingAllowed": True,
    },
    "Extensions": {
        "ExtensionInstallBlocklist": None,
        "ExtensionInstallForcelist": None,
        "ExtensionAllowedTypes": None,
        "ExtensionSettings": None,
        "IncognitoModeAvailability": None,
    },
    "Wi-Fi": {
        "OpenNetworkConfiguration": None,
        "ProxySettings": None,
        "ProxyMode": None,
        "ProxyServerMode": None,
        "ProxyServer": None,
        "ProxyPacUrl": None,
        "ProxyBypassList": None,
        "ProxyOverrideRules": None,
        "EnableProxyOverrideRulesForAllUsers": None,
        "SystemProxySettings": None,
        "VpnConfigAllowed": None,
        "AlwaysOnVpnPreConnectUrlAllowlist": None,
        "CaptivePortalAuthenticationIgnoresProxy": None,
        "DnsOverHttpsMode": _Rec("automatic"),
        "DnsOverHttpsTemplates": None,
        "WifiSyncAndroidAllowed": True,
        "InstantTetheringAllowed": True,
    },
    "Accounts": {
        "BrowserGuestModeEnabled": True,
        "BrowserGuestModeEnforced": None,
        "BrowserAddPersonEnabled": None,
        "SigninAllowed": None,
        "BrowserSignin": None,
        "ForceBrowserSignin": None,
        "SecondaryGoogleAccountSigninAllowed": None,
        "SecondaryGoogleAccountUsage": None,
        "RestrictSigninToPattern": None,
        "RestrictAccountsToPatterns": None,
        "ManagedAccountsSigninRestriction": None,
        "SigninInterceptionEnabled": None,
        "ProfileSeparationSettings": None,
        "ProfileSeparationDomainExceptionList": None,
        "ProfileSeparationDataMigrationSettings": None,
        "ForceEphemeralProfiles": None,
        "ProfilePickerOnStartupAvailability": None,
        "ProfileReauthPrompt": None,
        "SyncDisabled": None,
        "SyncTypesListDisabled": None,
        "EnableSyncConsent": None,
        "SupervisedUserCreationEnabled": None,
        "SupervisedUsersEnabled": None,
        "UserAvatarCustomizationSelectorsEnabled": None,
        "ChromeOsMultiProfileUserBehavior": "unrestricted",
        "LacrosSecondaryProfilesAllowed": True,
        "AllowScreenLock": None,
        "ChromeOsLockOnIdleSuspend": None,
        "ScreenLockDelayAC": None,
        "ScreenLockDelayBattery": None,
        "ScreenLockDelays": None,
        "LockScreenReauthenticationEnabled": None,
        "LockScreenAutoStartOnlineReauth": None,
        "GaiaOfflineSigninTimeLimitDays": None,
        "GaiaLockScreenOfflineSigninTimeLimitDays": None,
        "SamlLockScreenOfflineSigninTimeLimitDays": None,
        "SAMLOfflineSigninTimeLimit": None,
        "EasyUnlockAllowed": True,
        "SmartLockSigninAllowed": True,
        "RecoveryFactorBehavior": True,
        "LoginDisplayPasswordButtonEnabled": True,
        "PinUnlockAutosubmitEnabled": True,
        "PinUnlockMinimumLength": None,
        "PinUnlockMaximumLength": None,
        "PinUnlockWeakPinsAllowed": None,
        "QuickUnlockTimeout": None,
        "QuickUnlockModeAllowlist": ["all"],
        "QuickUnlockModeWhitelist": ["all"],
        "AllowedLocalAuthFactors": ["ALL"],
        "KerberosAddAccountsAllowed": None,
    },
    "Privacy": {
        "MetricsReportingEnabled": False,
        "UrlKeyedAnonymizedDataCollectionEnabled": False,
        "PasswordLeakDetectionEnabled": False,
        "NetworkPredictionOptions": None,
        "SearchSuggestEnabled": False,
        "AttestationEnabledForUser": False,
        "SafeBrowsingExtendedReportingEnabled": False,
    },
}

def _profiles_menu():
    while True:
        PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        names = sorted(p.stem for p in PROFILES_DIR.glob("*.json"))
        options = [("+ New profile", _BGREEN)]
        for n in names:
            options.append((n, _BMAGENTA))
        idx = _menu("Profiles", options, subtitle="%d saved" % len(names))
        if idx is None:
            return
        if idx == 0:
            _cls()
            _cls_end()
            try:
                name = _eline("\nNew profile name: ", "")
            except EOFError:
                return
            name = name.strip()
            if not name:
                continue
            if not _load_profile(name):
                _cls()
                print("Start %s from:" % _bold(name))
                print(_DIM + "  [1] empty" + _RESET)
                print(_DIM + "  [2] Fetched Server Policy" + _RESET)
                _cls_end()
                key = _key()
                if key == '2':
                    _drain_stdin()
                    _cls()
                    print("Capturing current live policy...")
                    _cls_end()
                    policies, skipped = _live_policy_as_profile()
                    _drain_stdin()
                    _cls()
                    if policies is None:
                        print("No live user policy found. Sign in first, "
                              "starting empty instead.")
                        _save_profile(name, {})
                    else:
                        _save_profile(name, policies)
                        if skipped:
                            print("(%d value(s) couldn't be decoded and were "
                                  "skipped)" % skipped)
                    _cls_end()
                    try:
                        input("\n%sPress Enter to continue...%s" % (_DIM, _RESET))
                    except EOFError:
                        return
                else:
                    _save_profile(name, {})
            _profile_detail(name)
        else:
            _profile_detail(names[idx - 1])

def _profile_detail(name):
    window_start = 0
    while True:
        profile = _load_profile(name)
        keys = sorted(profile.keys())
        name_width = 55
        content_w = name_width + 28

        page_size = max(5, shutil.get_terminal_size((80, 24)).lines - 13)
        window_start = max(0, min(window_start, max(0, len(keys) - page_size)))
        visible = keys[window_start:window_start + page_size]

        _cls()
        title = "Profile: %s  (%d)" % (name, len(keys))
        width = max(52, len(title) + 4, content_w)
        print(_BCYAN + "+" + "-" * width + "+" + _RESET)
        print(_BCYAN + "|" + _RESET + _bold(title.center(width)) + _BCYAN + "|" + _RESET)
        print(_BCYAN + "+" + "-" * width + "+" + _RESET)
        print()
        for k in visible:
            chunk, fnum = _PN2F.get(k, (0, -1))
            kind, val = _py_kind(profile[k])
            print("  %s" % _row_lbl(k, chunk, fnum, kind, val, name_width))
        if not keys:
            print(_dim("  (empty, press [e] to edit)"))
        print()
        if len(keys) > page_size:
            rng = "%d-%d of %d" % (window_start + 1, window_start + len(visible), len(keys))
            print((_DIM + rng + _RESET).center(width + len(_DIM) + len(_RESET)))
        print(_DIM + "[up/down] scroll  [e] edit  [s] save  [i] inject  "
                     "[r] rename  [d] delete  [esc] back" + _RESET)
        _cls_end()

        key = _key()
        if key == 'UP':
            window_start -= 1
        elif key == 'DOWN':
            window_start += 1
        elif key == 'ESC':
            return
        elif key in ('e', 'E'):
            _browse(profile_name=name)
        elif key in ('s', 'S'):
            current = _current_overrides()
            if current is None:
                print("Nothing injected right now, nothing to save.")
                _cls_end()
                try:
                    input("\n%sPress Enter to continue...%s" % (_DIM, _RESET))
                except EOFError:
                    return
                continue
            profile.update(current)
            _save_profile(name, profile)
        elif key in ('i', 'I'):
            if not profile:
                print("Profile is empty, nothing to inject.")
                _cls_end()
                try:
                    input("\n%sPress Enter to continue...%s" % (_DIM, _RESET))
                except EOFError:
                    return
                continue
            _cls()
            _cls_end()
            try:
                already_paused = not _inject_profile(name)
            except SystemExit:
                already_paused = False
            _cls_end()
            if not already_paused:
                try:
                    input("\n%sPress Enter to continue...%s" % (_DIM, _RESET))
                except EOFError:
                    return
        elif key in ('r', 'R'):
            _cls()
            print("Renaming %s:" % _bold(name))
            _cls_end()
            try:
                new_name = _eline("New name: ", name)
            except EOFError:
                continue
            new_name = new_name.strip()
            if not new_name or new_name == name:
                continue
            if (PROFILES_DIR / ("%s.json" % new_name)).exists():
                print("A profile named '%s' already exists." % new_name)
                _cls_end()
                try:
                    input("\n%sPress Enter to continue...%s" % (_DIM, _RESET))
                except EOFError:
                    return
                continue
            (PROFILES_DIR / ("%s.json" % name)).rename(PROFILES_DIR / ("%s.json" % new_name))
            name = new_name
        elif key in ('d', 'D'):
            _cls()
            _cls_end()
            try:
                confirm = _confirm("Delete profile '%s'? [y/N]: " % name)
            except EOFError:
                return
            if confirm:
                (PROFILES_DIR / ("%s.json" % name)).unlink(missing_ok=True)
                return

def _preset_menu():
    while True:
        options = list(PRESETS.keys())
        idx = _menu("Presets", [(name, _BMAGENTA) for name in options])
        if idx is None:
            return
        _cls()
        _cls_end()
        changes = PRESETS[options[idx]]
        if callable(changes):
            changes = changes()
            if not changes:
                print("No real snapshot to compare against yet. Run `fetch` first.")
                _cls_end()
                try:
                    input("\n%sPress Enter to continue...%s" % (_DIM, _RESET))
                except EOFError:
                    return
                continue
        already_paused = not _do_preset(changes)
        _cls_end()
        if not already_paused:
            try:
                input("\n%sPress Enter to continue...%s" % (_DIM, _RESET))
            except EOFError:
                return

def cmd_interactive(args=None):
    if not _PN2F:
        try:
            cmd_refresh_mapping(argparse.Namespace())
            _reload_mapping()
        except (SystemExit, Exception):
            pass
    while True:
        _refresh_dm_key_backup_if_safe()
        inj_on = INJECT_STATE_FILE.exists()
        extra = [
            "%s%s policies known%s" % (_DIM, len(_PN2F), _RESET),
            "%sMade By: Aro_Moon / Nmsjayden%s" % (_DIM, _RESET),
        ]
        note = _mapping_note()
        if note:
            extra.append("%s%s%s" % (_YELLOW, note, _RESET))
        resignin_needed = False
        if inj_on:
            try:
                inj_state = json.loads(INJECT_STATE_FILE.read_text())
            except Exception:
                inj_state = {}
            resignin_needed = _need_resignin(inj_state)
            if resignin_needed:
                extra.append(
                    '%sPolicies not synced. Press %sApply now%s to sync, '
                    'or %sSign out now%s if that fails.%s'
                    % (_YELLOW, _BGREEN, _YELLOW, _RED, _YELLOW, _RESET))

        items = [
            ("Browse and edit policies", "browse", _BCYAN),
            ("Profiles", "profiles", _BYELLOW),
            ("Presets", "presets", _BMAGENTA),
        ]
        if resignin_needed:
            items.append(("Apply now", "apply_live", _BGREEN))
            items.append(("Sign out now", "sign_out", _RED))
        items += [
            ("Fetch fresh policy", "fetch_fresh", _BBLUE),
            ("Refresh policy mapping (advanced)", "refresh_mapping", _DIM),
            ("Fix sign-in loop (advanced)", "repair_signin", _DIM),
            ("Check for updates", "update", _DIM),
            ("Status", "status", _BCYAN),
            ("Exit", "exit", _WHITE),
        ]

        idx = _menu("ChromeOS Policy Editor",
                    [(label, color) for label, _, color in items],
                    extra_lines=extra, subtitle=_user_email())
        if idx is None or items[idx][1] == "exit":
            _cls()
            print("Bye.")
            _cls_end()
            break

        action = items[idx][1]
        _cls()
        _cls_end()
        try:
            if action == "browse":
                _browse()
                continue
            elif action == "presets":
                _preset_menu()
                continue
            elif action == "profiles":
                _profiles_menu()
                continue
            elif action == "apply_live":
                if not _try_apply():
                    continue
            elif action == "sign_out":
                if _confirm("Are you sure you want to sign out? All unsaved progress "
                             "and such will be lost. [y/N]: "):
                    cmd_sign_out(argparse.Namespace())
                else:
                    print("Cancelled.")
            elif action == "fetch_fresh":
                active = _active_profile_name() if inj_on else None
                if inj_on:
                    try:
                        state = json.loads(INJECT_STATE_FILE.read_text())
                    except Exception:
                        state = {}
                    overrides = state.get("overrides", {})
                    print("This pulls real policy from DM, replacing your current edits.")
                    if active:
                        print("Profile '%s' is in use; it'll be re-applied on top "
                              "of the fresh policy." % active)
                    elif overrides:
                        print("Your current edits aren't in a profile; they'll be "
                              "saved into a new one and re-applied.")
                    if overrides:
                        print("Currently changed (%s):" % len(overrides))
                        shown_items = list(overrides.items())[:10]
                        for name, val in shown_items:
                            shown = "unset" if val == "UNSET" else val
                            print("  %s%s = %s%s" % (_DIM, name, shown, _RESET))
                        if len(overrides) > len(shown_items):
                            print(_dim("  ...and %d more" % (len(overrides) - len(shown_items))))
                        print()
                    _cls_end()
                    confirm = _confirm("Continue? [y/N]: ")
                    if not confirm:
                        print("Cancelled.")
                        raise SystemExit
                else:
                    print("%sLocal policy file already normal.%s\n" % (_DIM, _RESET))
                backup_name = _do_fetch()
                target = active or backup_name
                if target:
                    try:
                        _inject_profile(target)
                    except SystemExit:
                        pass
            elif action == "refresh_mapping":
                cmd_refresh_mapping(argparse.Namespace())
            elif action == "repair_signin":
                cmd_repair_signin(argparse.Namespace(user=_SELECTED_USER))
            elif action == "update":
                cmd_update(argparse.Namespace())
            elif action == "status":
                cmd_status(argparse.Namespace())
        except SystemExit:
            pass

        _cls_end()
        try:
            input("\n%sPress Enter to continue...%s" % (_DIM, _RESET))
        except EOFError:
            break

def _root_mnt():
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[1] == "/":
                    return parts[0], set(parts[3].split(","))
    except OSError:
        pass
    return None, None

def _verity_on():
    device, _ = _root_mnt()
    if device is None:
        return None
    return device.startswith("/dev/dm-")

def _missing_bins():
    needed = ["iptables", "ip6tables"]
    return [b for b in needed if shutil.which(b) is None]

def _env_check():
    missing = _missing_bins()
    if missing:
        print(f"WARNING: missing from PATH: {', '.join(missing)}. "
              "On a new dev-mode image, `sudo dev_install` usually gets them.",
              file=sys.stderr)

    verification = _verity_on()

    if verification:
        print("\nNote: rootfs verification is on. This tool only writes under "
              "/run, /home/root, or /root",
              file=sys.stderr)

def main():
    _env_check()
    ap = argparse.ArgumentParser(
        description="ChromeOS DM policy tool")
    ap.add_argument("--user", metavar="EMAIL",
                    help="signed-in user to edit (default: primary)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("fetch", help="Fetch policy from the DM server")

    p_dump = sub.add_parser("dump", help="Decode a policy snapshot")
    p_dump.add_argument("snapshot", nargs="?", help="Path or label (default: latest)")

    p_diff = sub.add_parser("diff", help="Diff two snapshots")
    p_diff.add_argument("a")
    p_diff.add_argument("b")

    p_pfp = sub.add_parser("profile-from-policy",
        help="Save live policies as a new profile")
    p_pfp.add_argument("name")

    p_edit = sub.add_parser("edit", help="Edit a local-override profile")
    p_edit.add_argument("--name", default="default",
                        help="Profile name (default: default)")
    p_edit.add_argument("--set", action="append", metavar="KEY=VALUE",
                        help="Set a policy (repeatable)")
    p_edit.add_argument("--unset", action="append", metavar="KEY",
                        help="Remove a policy from the profile")

    p_local = sub.add_parser("local", help="Manage local-override files")
    local_sub = p_local.add_subparsers(dest="local_cmd", required=True)
    local_sub.add_parser("list", help="List active overrides")
    p_la = local_sub.add_parser("apply", help="Apply a profile to managed/")
    p_la.add_argument("name")
    p_lr = local_sub.add_parser("remove", help="Remove a profile from managed/")
    p_lr.add_argument("name")
    local_sub.add_parser("clear", help="Clear all local overrides")

    sub.add_parser("profiles", help="List saved profiles")
    sub.add_parser("status", help="Show current status")

    p_inject = sub.add_parser(
        "inject",
        help="Inject local overrides into the DM policy blob")
    p_inject.add_argument("--profile", metavar="NAME",
                          help="Profile to apply (default: active managed/ files)")
    p_inject.add_argument("--also-active", action="store_true",
                          help="Also include active managed/ overrides")
    p_inject.add_argument("--set", action="append", metavar="KEY=VALUE",
                          help="Extra override (repeatable)")
    p_inject.add_argument("--unset", action="append", metavar="NAME",
                          help="Unset a policy (repeatable)")
    p_inject.add_argument("--force", action="store_true",
                          help="Re-inject even if already active")
    p_inject.add_argument("--new-key", action="store_true",
                          help="Generate a fresh key pair")

    p_backup = sub.add_parser("backup",
                              help="Save current inject overrides as a profile")
    p_backup.add_argument("--label", default=None, help="Backup name suffix (default: manual)")

    sub.add_parser("eject", help="Restore original DM key and policy")
    sub.add_parser("sign-out", help="End the current session")
    sub.add_parser("repair-signin", help="Fix a sign-in crash loop")
    sub.add_parser("apply", help="Push the current injection live")
    sub.add_parser("restart-chrome", help="Restart Chrome in the same session")

    p_list = sub.add_parser("list", help="List known policy names")
    p_list.add_argument("filter", nargs="?", help="Substring filter")

    p_get = sub.add_parser("get", help="Show a policy's current value")
    p_get.add_argument("name")

    p_toggle = sub.add_parser("toggle", help="Flip a boolean policy")
    p_toggle.add_argument("name")

    p_unset = sub.add_parser("unset", help="Unset a policy to its default")
    p_unset.add_argument("name")

    sub.add_parser("update", help="Check for and install updates")

    p_refresh = sub.add_parser("refresh-mapping",
                   help="Refresh the policy name/field map from Chromium")
    p_refresh.add_argument("--save-fallback", metavar="PATH",
                           help="Also write a fallback JSON copy")

    p_verify = sub.add_parser("verify-mapping",
                              help="Check the name/field map against a policy export")
    p_verify.add_argument("--export", help="chrome://policy Export to JSON file")

    p_fix = sub.add_parser("fix-mapping",
                           help="Correct one field's mapped name")
    p_fix.add_argument("field", type=int)
    p_fix.add_argument("name")

    p_block = sub.add_parser("dm-block",
                             help="Block or unblock the DM server")
    block_sub = p_block.add_subparsers(dest="block_cmd", required=True)
    block_sub.add_parser("start", help="Block the DM server")
    block_sub.add_parser("stop", help="Unblock the DM server")
    block_sub.add_parser("status", help="Show block status")

    sub.add_parser("interactive", help="Interactive menu")

    if not sys.argv[1:] and not sys.stdin.isatty():
        print("No subcommand given and stdin isn't a terminal.", file=sys.stderr)
        sys.exit(1)
    argv = sys.argv[1:] if sys.argv[1:] else ["interactive"]
    args = ap.parse_args(argv)
    global _SELECTED_USER
    _SELECTED_USER = args.user
    _refresh_dm_key_backup_if_safe()

    if   args.cmd == "fetch":    cmd_fetch(args)
    elif args.cmd == "dump":     cmd_dump(args)
    elif args.cmd == "edit":     cmd_edit(args)
    elif args.cmd == "profiles": cmd_profiles(args)
    elif args.cmd == "profile-from-policy": cmd_profile_from_policy(args)
    elif args.cmd == "status":   cmd_status(args)
    elif args.cmd == "inject":   cmd_inject(args)
    elif args.cmd == "backup":   cmd_backup(args)
    elif args.cmd == "eject":    cmd_eject(args)
    elif args.cmd == "sign-out": cmd_sign_out(args)
    elif args.cmd == "repair-signin": cmd_repair_signin(args)
    elif args.cmd == "apply":    cmd_apply(args)
    elif args.cmd == "restart-chrome": cmd_restart_chrome(args)
    elif args.cmd == "list":     cmd_list_policies(args)
    elif args.cmd == "get":      cmd_get(args)
    elif args.cmd == "toggle":   cmd_toggle(args)
    elif args.cmd == "unset":    cmd_unset(args)
    elif args.cmd == "refresh-mapping": cmd_refresh_mapping(args)
    elif args.cmd == "update": cmd_update(args)
    elif args.cmd == "verify-mapping": cmd_verify_mapping(args)
    elif args.cmd == "fix-mapping": cmd_fix_mapping(args)
    elif args.cmd == "dm-block": cmd_dm_block(args)
    elif args.cmd == "interactive": cmd_interactive(args)
    elif args.cmd == "local":
        if   args.local_cmd == "list":   cmd_local_list(args)
        elif args.local_cmd == "apply":  cmd_local_apply(args)
        elif args.local_cmd == "remove": cmd_local_remove(args)
        elif args.local_cmd == "clear":  cmd_local_clear(args)
    elif args.cmd == "diff":     cmd_diff(args)

if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
    except OSError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        sys.exit(1)

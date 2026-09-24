#!/usr/bin/env python3

"""
ChromeOS DM policy tool: fetch / dump / inject / apply / local overrides.

See --help for subcommands. State under /root/policy_editor_state.

Made By: Aro_Moon / Nmsjayden
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.parse

try:
    import readline
    _HAS_RL = True
except ImportError:
    _HAS_RL = False
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

def _eline(prompt, current):
    pending = _drain_stdin()
    if pending:
        if "\r" in pending or "\n" in pending:
            line = pending.replace("\r", "\n").split("\n", 1)[0].strip()
            sys.stdout.write("%s%s\n" % (prompt, line))
            sys.stdout.flush()
            return line if line else current
        current = pending
    if _HAS_RL == False:
        typed = input("%s[%s] " % (prompt, current)).strip()
        return typed if typed else current
    readline.set_startup_hook(lambda: readline.insert_text(current))
    try:
        return input(prompt).strip()
    finally:
        readline.set_startup_hook(None)


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
_GREEN   = "\033[32m" if _COLOR else ""
_YELLOW  = "\033[33m" if _COLOR else ""
_BLUE    = "\033[34m" if _COLOR else ""
_MAGENTA = "\033[35m" if _COLOR else ""
_CYAN    = "\033[36m" if _COLOR else ""
_WHITE   = "\033[37m" if _COLOR else ""
_BRED     = "\033[91m" if _COLOR else ""
_BGREEN   = "\033[92m" if _COLOR else ""
_BYELLOW  = "\033[93m" if _COLOR else ""
_BBLUE    = "\033[94m" if _COLOR else ""
_BMAGENTA = "\033[95m" if _COLOR else ""
_BCYAN    = "\033[96m" if _COLOR else ""
_BWHITE   = "\033[97m" if _COLOR else ""

def _bold(t): return _color(t, "1")
def _dim(t): return _color(t, "2")

def _key():
    # single keypress, arrows etc
    if not sys.stdin.isatty():
        return input().strip() or "ENTER"
    import termios
    import tty
    import select
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd, termios.TCSANOW)
        attrs = termios.tcgetattr(fd)
        attrs[1] |= (termios.OPOST | termios.ONLCR)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        while True:
            ch = os.read(fd, 1).decode(errors="ignore")
            if ch == '\x1b':
                seq = os.read(fd, 2).decode(errors="ignore")
                result = {'[A': 'UP', '[B': 'DOWN', '[C': 'RIGHT', '[D': 'LEFT'}.get(seq)
                if result:
                    return result
                while select.select([fd], [], [], 0)[0]:
                    os.read(fd, 1)
                continue  # not an arrow key, just noise, keep waiting
            if ch in ('\r', '\n'):
                return 'ENTER'
            if ch in ('\x7f', '\x08'):
                return 'BACKSPACE'
            if ch == '\x03':  # Ctrl-C
                raise KeyboardInterrupt
            return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

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
              "up/down", "enter", "q"))
        _cls_end()

        key = _key()
        if key == 'UP':
            sel = (sel - 1) % n
        elif key == 'DOWN':
            sel = (sel + 1) % n
        elif key == 'ENTER':
            return sel
        elif key in ('q', 'Q'):
            return None
        elif isinstance(key, str) and key.isdigit():
            idx = int(key) - 1
            if 0 <= idx < n:
                sel = idx


STATE_DIR       = Path('/root/policy_editor_state')
PROFILES_DIR    = STATE_DIR.joinpath("profiles")
SNAPSHOTS_DIR   = STATE_DIR.joinpath("managed-user")
MANAGED_DIR     = Path('/etc/opt/chrome/policies/managed')
RECOMMENDED_DIR = Path('/etc/opt/chrome/policies/recommended')

def _live_pol():
    base = Path("/run/daemon-store/session_manager")
    for p in base.glob("*/policy/policy"):
        return p
    return None
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

PFR_TYPE    = 1   # string
PFR_SIG = 3   # enum: 2 = SHA256_RSA
PFR_PKV = 4   # int32
DMR_USER   = 3
DMR_REQ    = 3   # DevicePolicyRequest.requests[0]
DMRESP_USER   = 5
DPOL_FETCH    = 3

DM_SERVER_HOST = 'm.google.com'  # host in DM_ENDPOINT

SENSITIVE_FIELDS = {3, 8, 10}  # token, device_id, etc  # PolicyData: request_token, device_id, device_dm_token
INJECT_KEY_FILE   = SNAPSHOTS_DIR / "inject.key.pem"   # our RSA private key (0600)
DM_KEY_BACKUP     = SNAPSHOTS_DIR / "dm.key.pub.bak"   # backup of original DM public key
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
        pass  # never let logging itself break the actual operation

POLICY_ID_MAP_FILE  = SNAPSHOTS_DIR / "chrome_policy_id_map.json"
POLICY_OVERRIDE_FILE = SNAPSHOTS_DIR / "chrome_policy_id_overrides.json"
CHUNKED_MAP_FILE = SNAPSHOTS_DIR / "chrome_policy_chunked_map.json"
# legacy filename still written empty for older tooling
CHUNKED_POLICIES_FILE = SNAPSHOTS_DIR / "chrome_policy_chunked_names.json"

DEFAULT_OVR = {}

# Chromium: top-level ids <= 1040 use field = id+2 on CloudPolicySettings.
# Higher ids live in CloudPolicySubProtoN as field ((id-1041)%800)+1,
# with subProtoN itself at CloudPolicySettings field 1040+2+N = 1042+N.
POLICY_ID_OFFSET = 2
POLICY_LAST_TOP_LEVEL_ID = 1040
POLICY_CHUNK_SIZE = 800

def _chunk_and_field(yaml_id):
    """Return (chunk, field) for a policies.yaml id."""
    if yaml_id <= POLICY_LAST_TOP_LEVEL_ID:
        return 0, yaml_id + POLICY_ID_OFFSET
    chunk = (yaml_id - POLICY_LAST_TOP_LEVEL_ID - 1) // POLICY_CHUNK_SIZE + 1
    field = (yaml_id - POLICY_LAST_TOP_LEVEL_ID - 1) % POLICY_CHUNK_SIZE + 1
    return chunk, field

def _subproto_cs_field(chunk):
    """CloudPolicySettings field number for CloudPolicySubProto{chunk}."""
    return POLICY_LAST_TOP_LEVEL_ID + POLICY_ID_OFFSET + chunk

def _load_pn2f():
    """name -> (chunk, field). chunk 0 = top-level CloudPolicySettings field."""
    out = {}
    # top-level map is still {field_num_str: name} for compat
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
    # chunked map: {name: {"chunk": N, "field": F}} or {name: [N, F]}
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
# names that are chunked (for list display)
_CHUNKED = {n for n, (c, _) in _PN2F.items() if c > 0}

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
SM_POL_DIRS = [
    "/run/daemon-store/session_manager",
]


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
    # returns list of (field, wire, val)
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
                applied.add(fnum)  # handled: dropped, never re-added below
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
    ds = Path("/run/daemon-store/session_manager")
    if ds.exists():
        for d in sorted(ds.iterdir()):
            if not d.is_dir(): continue
            pdir = d / "policy"
            p, k = pdir / "policy", pdir / "key"
            if p.exists() and k.exists():
                return p, k
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
    body += (_enc_field(PFR_SIG, 0, 2)      # SHA256_RSA
             + _enc_field(PFR_PKV, 0, public_key_version))
    return body

def _mk_dm_req(policy_type, public_key_version=1):
    pfr = _mk_pfr(policy_type, public_key_version)
    dpr = _enc_field(DMR_REQ, 2, pfr)   # DevicePolicyRequest.requests[0]
    return _enc_field(DMR_USER, 2, dpr)  # DeviceManagementRequest.policy_request


class _Credentials:
    __slots__ = ("dm_token", "device_id", "public_key_version", "policy_type")

    def __init__(self, path):
        raw = Path(path).read_bytes()
        fields = _parse_raw(raw)
        _, pd_bytes = _get_field(fields, 3)  # PolicyFetchResponse.policy_data
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


SNAPSHOT_KEEP = 20  # oldest ones beyond this get pruned after every save

def _prune_snaps():
    snaps = sorted(SNAPSHOTS_DIR.glob("*.bin"), key=lambda p: p.stat().st_mtime)
    for old in snaps[:-SNAPSHOT_KEEP]:
        old.unlink()

def cmd_fetch(args):
    # pull from DM, save snapshot
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
        "apptype":    "Chrome",          # NOT "chromeos", routes to user policy handler
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
    chunk_names = {}  # (chunk, field) -> name
    for n, (c, f) in _PN2F.items():
        if c > 0:
            chunk_names[(c, f)] = n
    print("\nCloudPolicySettings (%d bytes, %d top-level fields):" % (len(pv_bytes), len(pv)))
    for f, w, v in sorted(pv, key=lambda t: t[0]):
        # subProto fields are 1043, 1044, ...
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

def cmd_local_list(args):
    print("=== Active local-override files in managed/ ===")
    MANAGED_DIR.mkdir(parents=True, exist_ok=True)
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
    MANAGED_DIR.mkdir(parents=True, exist_ok=True)
    dest = MANAGED_DIR / ("%s.json" % args.name)
    dest.write_text(json.dumps(profile, indent=2, sort_keys=True))
    print("Applied profile '%s' -> %s" % (args.name, dest))
    print("Reload chrome://policy (or wait for Chrome's auto-refresh) to activate.")

def cmd_local_remove(args):
    dest = MANAGED_DIR / ("%s.json" % args.name)
    if dest.exists():
        dest.unlink()
        print("Removed %s" % dest)
    else:
        print("Not found: %s" % dest)

def cmd_local_clear(args):
    MANAGED_DIR.mkdir(parents=True, exist_ok=True)
    removed = 0
    for f in MANAGED_DIR.glob("*.json"):
        f.unlink()
        removed += 1
    print("Cleared %d file(s) from %s" % (removed, MANAGED_DIR))
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
            parsed = v  # treat as string
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
    inj_on = INJECT_STATE_FILE.exists()

    if inj_on:
        try:
            state = json.loads(INJECT_STATE_FILE.read_text())
        except Exception:
            state = {}
        overrides = state.get("overrides", {})
        if overrides:
            print("%d %s changed:" % (len(overrides), "policy" if len(overrides)==1 else "policies"))
            for name, val in overrides.items():
                shown = "unset" if val == "UNSET" else val
                print("  %s = %s" % (name, shown))
            if _need_resignin(state):
                print("  %snot synced yet%s %s(run apply or sign-out)%s" % (_YELLOW, _RESET, _DIM, _RESET))
    else:
        print("%snothing changed, normal policy in effect%s" % (_DIM, _RESET))

    print()
    live = _live_pol()
    if live:
        raw = live.read_bytes()
        fields = _parse_raw(raw)
        _, pd = _get_field(fields, 3)
        if pd:
            pdf = _parse_raw(pd)
            ts_ms = next((v for f,w,v in pdf if f==2 and isinstance(v,int)), None)
            user  = _user_email() or "?"
            if ts_ms:
                import datetime
                dt = datetime.datetime.utcfromtimestamp(ts_ms/1000)
                print("Signed in: %s  (last fetch %s UTC)" % (user, dt.strftime("%Y-%m-%d %H:%M:%S")))
            else:
                print("Signed in: %s" % user)
        pf, kf = _user_pol_files()
        if pf is not None and not _verify_live_pair(pf, kf):
            print("%sWARNING: the live key/policy pair doesn't verify itself. "
                  "Run `eject` to fix it.%s" % (_BYELLOW, _RESET))
    else:
        print("Not signed in (no live policy mount found)")

    MANAGED_DIR.mkdir(parents=True, exist_ok=True)
    local_files = list(MANAGED_DIR.glob("*.json"))
    if local_files:
        print("Local override files: %d active in %s" % (len(local_files), MANAGED_DIR))

    snaps = (sorted(SNAPSHOTS_DIR.glob("*.bin"), key=lambda p: p.stat().st_mtime)
             if SNAPSHOTS_DIR.exists() else [])
    print("Saved snapshots: %d%s" % (len(snaps), (" (latest: %s)" % snaps[-1].name) if snaps else ""))


def _atomic_write(path, data):
    """Write data to path via a temp file + rename, so a mid-write restart or
    sign-out can never leave a truncated/partial file behind."""
    tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
    tmp.write_bytes(data)
    os.replace(str(tmp), str(path))


def _verify_live_pair(policy_file, key_file):
    """True if the current on-disk key/policy pair verify each other. False
    (not an exception) for any read/parse/verify failure, including a pair
    torn mid-write by a sign-out or restart."""
    try:
        pfr_raw = _parse_raw(policy_file.read_bytes())
        return _verify_pfr(pfr_raw, key_file.read_bytes())
    except Exception:
        return False


def _verify_pfr(pfr_raw, pub_der):
    """True if this PolicyFetchResponse's policy_data_signature verifies
    against the given DER-encoded public key. Never raises."""
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
    # install cryptography if missing
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
    # rewrite blob + resign. does NOT push to chrome (use apply)
    rsa_m, serial, hashes_m, pad = _need_crypto()

    policy_file, key_file = _user_pol_files()
    if policy_file == None:
        print("ERROR: User policy files not found.\n"
              "Sign in as the managed user first.",
              file=sys.stderr)
        sys.exit(1)

    overrides_json = {}

    if args.profile:
        prof = _load_profile(args.profile)
        if not prof:
            print("ERROR: Profile '%s' not found." % args.profile, file=sys.stderr)
            sys.exit(1)
        overrides_json.update(prof)

    if not args.profile or args.also_active:
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
    new_pfr_bytes = _reencode(pfr_raw, {3: new_pd_bytes, 4: new_sig})

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


def cmd_eject(args):
    # undo inject
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
            print("ERROR: the saved DM key doesn't verify %s, so restoring "
                  "them together would leave a broken pair.\n"
                  "Run `fetch` once while signed in to re-verify, then eject "
                  "again." % restore_path.name, file=sys.stderr)
            sys.exit(1)
        _atomic_write(key_file, dm_pub)
        print("  Restored DM key: %s" % key_file)
        _atomic_write(policy_file, restore_bytes)
        print("  Restored policy: %s" % restore_path)

    INJECT_STATE_FILE.unlink()
    if SYNCED_STATE_FILE.exists():
        SYNCED_STATE_FILE.unlink()

    cmd_dm_block_stop()

    print("\n" + "="*60)
    print("EJECTED. Sign out and back in to activate the original DM policy.")
    print("="*60)


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


def _store_pol(descriptor_bytes, policy_bytes):
    import subprocess

    def literal(b):
        return "[" + ", ".join(f"byte 0x{x:02x}" for x in b) + "]"

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
    import subprocess

    old_pid = _chrome_pid()
    if old_pid is None:
        print("No live Chrome browser process found.", file=sys.stderr)
        return False
    subprocess.run(["kill", "-TERM", str(old_pid)])
    deadline = time.time() + timeout
    while time.time() < deadline:
        new_pid = _chrome_pid()
        if new_pid is not None and new_pid != old_pid:
            return True
        time.sleep(0.2)
    print("Chrome did not respawn within the timeout.", file=sys.stderr)
    return False


def cmd_apply(args):
    # StorePolicyEx + chrome restart
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


def _try_apply():
    if not INJECT_STATE_FILE.exists():
        print("Nothing injected. Use inject/toggle/unset first.")
        return True
    try:
        cmd_apply(argparse.Namespace())
        return True
    except SystemExit:
        pass
    print("\n%sSigning out...%s" % (_YELLOW, _RESET))
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
    if INJECT_STATE_FILE.exists():
        cmd_eject(argparse.Namespace())
    cmd_fetch(argparse.Namespace())


def cmd_refresh_mapping(args):
    import urllib.request
    import base64
    import re as _re

    url = ("https://chromium.googlesource.com/chromium/src/+/refs/heads/main/"
           "components/policy/resources/templates/policies.yaml?format=TEXT")
    print("Fetching %s ..." % url)
    with urllib.request.urlopen(url, timeout=20) as resp:
        raw = base64.b64decode(resp.read())
    text = raw.decode()

    mapping = {}      # top-level: field_num -> name
    chunked_map = {}  # name -> {"chunk": N, "field": F}
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
                # skip retired/placeholder slots
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

    if len(mapping) < 500:
        print("ERROR: only parsed %d top-level entries." % len(mapping), file=sys.stderr)
        sys.exit(1)

    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    POLICY_ID_MAP_FILE.write_text(json.dumps(mapping, indent=0))
    CHUNKED_MAP_FILE.write_text(json.dumps(chunked_map, indent=0, sort_keys=True))
    CHUNKED_POLICIES_FILE.write_text(json.dumps(sorted(chunked_map.keys()), indent=0))
    print("Saved %d top-level + %d chunked mappings" % (len(mapping), len(chunked_map)))
    print("Restart tool (or re-run) to pick up.")


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
    """Top-level only: field_num -> raw *PolicyProto bytes."""
    pdf = _parse_raw(pd_bytes)
    _, pv_bytes = _get_field(pdf, 4)
    return {f: v for f, w, v in _parse_raw(pv_bytes)} if pv_bytes else {}

def _cs_locs_from_pd(pd_bytes):
    """All policies including chunked: (chunk, field) -> raw bytes."""
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
    """Return (pv_raw list, err) for live CloudPolicySettings fields."""
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
    """loc is field num (int, top-level) or (chunk, field). Returns (raw_bytes, err)."""
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
    # nested: CloudPolicySettings.subProto{chunk} then inner field
    sub_f = _subproto_cs_field(chunk)
    sub_bytes = next((v for f, w, v in pv_raw if f == sub_f and w == 2), None)
    if sub_bytes is None:
        return None, None  # unset
    sub_raw = _parse_raw(sub_bytes)
    raw = next((v for f, w, v in sub_raw if f == fnum), None)
    return raw, None

def _encode_pol_value(value, recommended=False):
    """Encode a Python value as *PolicyProto bytes (field2 payload wrapper)."""
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
    """Apply {(chunk, field): bytes|None} onto CloudPolicySettings field list. Returns new bytes."""
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
        if f == 2:  # the `value` field on every *PolicyProto message
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
    """Heuristic: bool policies vs int/string enums."""
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
    # bools on the wire are 0/1
    if kind == 'int' and val in (0, 1) and _looks_bool(name):
        return (_BGREEN + "true" + _RESET) if val else (_RED + "false" + _RESET)
    if kind == 'int':
        return _bold(str(val))
    if kind == 'string':
        # string enums
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

def _browse():
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

        rows = []
        for n in visible:
            chunk, fnum = _PN2F[n]
            raw, err = _cs_field((chunk, fnum))
            if err == "no_live_policy":
                _cls()
                print(_RED + "No live user policy found. Sign in first." + _RESET)
                _cls_end()
                try:
                    input("\n%sPress Enter to continue...%s" % (_DIM, _RESET))
                except EOFError:
                    pass
                return
            kind, val = _dec_pol(raw)
            rows.append((n, chunk, fnum, kind, val))

        _cls()
        title = "Policies  %d / %d" % (sel + 1, len(filtered))
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
        print(_DIM + "\n  [up/down] move  [enter] toggle/edit  [/] search  [x] unset  "
                     "[a] apply  [id+enter] jump  [q] back" + _RESET)
        _cls_end()

        key = _key()
        if key == 'UP':
            sel -= 1
            digit_buf = ""
        elif key == 'DOWN':
            sel += 1
            digit_buf = ""
        elif key in ('q', 'Q'):
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
            if kind != 'unset':
                with _quiet():
                    cmd_inject(argparse.Namespace(
                        profile=None, also_active=False, set=None, unset=[name],
                        force=True, new_key=False))
        elif key in ('a', 'A'):
            _cls()
            _cls_end()
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
                with _quiet():
                    cmd_inject(argparse.Namespace(
                        profile=None, also_active=False,
                        set=["%s=%s" % (name, 0 if val else 1)],
                        unset=None, force=True, new_key=False))
            else:
                _cls()
                default = "" if kind == 'unset' else str(val)
                print("Editing %s:" % _bold(name))
                _cls_end()
                new_val = _eline("> ", default)
                if not new_val:
                    new_val = default
                if new_val:
                    with _quiet():
                        cmd_inject(argparse.Namespace(
                            profile=None, also_active=False,
                            set=["%s=%s" % (name, new_val)],
                            unset=None, force=True, new_key=False))



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
    """Unset everything on the live blob, then force the values that actually turn things back on."""
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
    while True:
        inj_on = INJECT_STATE_FILE.exists()
        extra = [
            "%s%s policies known%s" % (_DIM, len(_PN2F), _RESET),
            "%sMade By: Aro_Moon / Nmsjayden%s" % (_DIM, _RESET),
        ]
        resignin_needed = False
        if inj_on:
            try:
                inj_state = json.loads(INJECT_STATE_FILE.read_text())
            except Exception:
                inj_state = {}
            resignin_needed = _need_resignin(inj_state)
            if resignin_needed:
                extra.append('%sPolicies not synced, press "%s%sApply Now%s%s" to sync.%s'
                             % (_YELLOW, _RESET, _BGREEN, _RESET, _YELLOW, _RESET))

        items = [
            ("Browse and edit policies", "browse", _BCYAN),
            ("Presets", "presets", _BMAGENTA),
        ]
        if resignin_needed:
            items.append(("Apply now", "apply_live", _BGREEN))
        items += [
            ("Fetch fresh policy", "fetch_fresh", _BBLUE),
            ("Refresh policy mapping (advanced)", "refresh_mapping", _DIM),
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
                continue  # _browse paces its own screen, skip the pause below
            elif action == "presets":
                _preset_menu()
                continue  # _preset_menu paces its own screen too
            elif action == "apply_live":
                if not _try_apply():
                    continue  # already paused after signing out
            elif action == "fetch_fresh":
                if inj_on:
                    try:
                        state = json.loads(INJECT_STATE_FILE.read_text())
                    except Exception:
                        state = {}
                    overrides = state.get("overrides", {})
                    print("This throws away current edits and pulls real policy from DM.")
                    if overrides:
                        print("Currently changed (%s):" % len(overrides))
                        for name, val in overrides.items():
                            shown = "unset" if val == "UNSET" else val
                            print("  %s%s = %s%s" % (_DIM, name, shown, _RESET))
                        print()
                    _cls_end()
                    confirm = input("Replace with normal policy? [y/N]: ").strip().lower()
                    if confirm != "y":
                        print("Cancelled.")
                        raise SystemExit
                else:
                    print("%sLocal policy file already normal.%s\n" % (_DIM, _RESET))
                _do_fetch()
            elif action == "refresh_mapping":
                cmd_refresh_mapping(argparse.Namespace())
            elif action == "status":
                cmd_status(argparse.Namespace())
        except SystemExit:
            pass  # a subcommand called sys.exit() on an error; stay in the menu

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
        description="DM policy fetch, inspect, and local-override tool")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="Fetch fresh policy from DM server")

    p_dump = sub.add_parser("dump", help="Decode and display a policy snapshot")
    p_dump.add_argument("snapshot", nargs="?", help="Path or label fragment (default: latest)")

    p_diff = sub.add_parser("diff", help="Diff two snapshots")
    p_diff.add_argument("a")
    p_diff.add_argument("b")

    p_edit = sub.add_parser("edit", help="Build/update a local-override profile")
    p_edit.add_argument("--name", default="default",
                        help="Profile name (default: 'default')")
    p_edit.add_argument("--set", action="append", metavar="KEY=VALUE",
                        help="Set a policy value (repeatable; value parsed as JSON)")
    p_edit.add_argument("--unset", action="append", metavar="KEY",
                        help="Remove a policy from the profile")

    p_local = sub.add_parser("local", help="Manage active local-override files")
    local_sub = p_local.add_subparsers(dest="local_cmd", required=True)
    local_sub.add_parser("list", help="List active overrides")
    p_la = local_sub.add_parser("apply", help="Apply a profile to managed/")
    p_la.add_argument("name")
    p_lr = local_sub.add_parser("remove", help="Remove a profile from managed/")
    p_lr.add_argument("name")
    local_sub.add_parser("clear", help="Remove all local overrides")

    sub.add_parser("profiles", help="List saved profiles")

    sub.add_parser("status", help="Show current policy mode")

    p_inject = sub.add_parser(
        "inject",
        help="Inject local overrides into the DM policy blob (makes local the source of truth)")
    p_inject.add_argument("--profile", metavar="NAME",
                          help="Apply this named profile (default: use active managed/ files)")
    p_inject.add_argument("--also-active", action="store_true",
                          help="With --profile, also include active managed/ overrides")
    p_inject.add_argument("--set", action="append", metavar="KEY=VALUE",
                          help="Additional override (repeatable; value parsed as JSON)")
    p_inject.add_argument("--unset", action="append", metavar="NAME",
                          help="Revert a policy to unset/default instead of setting it (repeatable)")
    p_inject.add_argument("--force", action="store_true",
                          help="Re-inject even if injection is already active")
    p_inject.add_argument("--new-key", action="store_true",
                          help="Generate a fresh key pair (discards the existing inject key)")

    sub.add_parser("eject",
                   help="Restore original DM key and policy blob (undo inject)")

    sub.add_parser("sign-out",
                   help="End the session right now (session_manager StopSession over D-Bus)")

    sub.add_parser("apply",
                   help="Push the current injection live, no sign-out needed")

    sub.add_parser("restart-chrome",
                   help="Restart just the Chrome browser process, same session, no sign-in")

    p_list = sub.add_parser("list", help="Search known Chrome policy names")
    p_list.add_argument("filter", nargs="?", help="Substring filter (case-insensitive)")

    p_get = sub.add_parser("get", help="Show a policy's current value on the live blob")
    p_get.add_argument("name")

    p_toggle = sub.add_parser("toggle", help="Flip a boolean policy and inject it")
    p_toggle.add_argument("name")

    p_unset = sub.add_parser("unset",
                             help="Revert a policy field to unset (Chrome's built-in default)")
    p_unset.add_argument("name")

    sub.add_parser("refresh-mapping",
                   help="Re-download the policy name<->field-number map from Chromium source")

    p_verify = sub.add_parser("verify-mapping",
                              help="Check the name<->field map against a chrome://policy export (or naming heuristics if none given)")
    p_verify.add_argument("--export", help="Path to a chrome://policy 'Export to JSON' file")

    p_fix = sub.add_parser("fix-mapping",
                           help="Manually correct one field's mapping (saved to the override file, wins over refresh-mapping)")
    p_fix.add_argument("field", type=int)
    p_fix.add_argument("name")

    p_block = sub.add_parser("dm-block",
                             help="Block/unblock outbound access to the DM server. Used "
                             "automatically by `apply`")
    block_sub = p_block.add_subparsers(dest="block_cmd", required=True)
    block_sub.add_parser("start", help="Block the DM server's current addresses")
    block_sub.add_parser("stop", help="Remove the block")
    block_sub.add_parser("status", help="Show whether it's currently blocked")

    sub.add_parser("interactive", help="Menu-driven interactive mode")

    if not sys.argv[1:] and not sys.stdin.isatty():
        print("No subcommand given and stdin isn't a terminal. Pass a "
              "subcommand (e.g. status), or save the script first.", file=sys.stderr)
        sys.exit(1)
    argv = sys.argv[1:] if sys.argv[1:] else ["interactive"]
    args = ap.parse_args(argv)

    if   args.cmd == "fetch":    cmd_fetch(args)
    elif args.cmd == "dump":     cmd_dump(args)
    elif args.cmd == "edit":     cmd_edit(args)
    elif args.cmd == "profiles": cmd_profiles(args)
    elif args.cmd == "status":   cmd_status(args)
    elif args.cmd == "inject":   cmd_inject(args)
    elif args.cmd == "eject":    cmd_eject(args)
    elif args.cmd == "sign-out": cmd_sign_out(args)
    elif args.cmd == "apply":    cmd_apply(args)
    elif args.cmd == "restart-chrome": cmd_restart_chrome(args)
    elif args.cmd == "list":     cmd_list_policies(args)
    elif args.cmd == "get":      cmd_get(args)
    elif args.cmd == "toggle":   cmd_toggle(args)
    elif args.cmd == "unset":    cmd_unset(args)
    elif args.cmd == "refresh-mapping": cmd_refresh_mapping(args)
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
    main()

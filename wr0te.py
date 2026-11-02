#!/usr/bin/env python3
"""wr0te — ambient writing tracker.

Watches a folder of text files and records every version it sees, so you can
look back at how much you wrote, where, and when.

Data model (all plain files, safe to sync between machines):

    <DATA>/logs/<machine>/<YYYY-MM>.jsonl   append-only observations, one JSON per line
    <DATA>/logs/<machine>/goal.json         last day this machine sent the daily-goal notification
    <DATA>/snapshots/<ab>/<sha1>.gz         gzipped copy of every distinct file version

Each machine only ever appends to its own log folder, so Dropbox never sees a
write conflict. Reports merge every machine's logs; a version is identified by
its content hash, so a change seen by both machines is only counted once.

Per-machine scratch state (never synced) and config.json live in
~/Library/Application Support/wr0te.

Commands:
    wr0te.py scan        look for changes (what launchd runs every 5 minutes)
    wr0te.py today       terminal summary of today / recent days
    wr0te.py report      build the HTML report (and daily.csv); --open to view
    wr0te.py install [FOLDER]
                         watch FOLDER (saved to config.json), install + start the launchd agent
    wr0te.py uninstall   stop + remove the launchd agent and menu bar app
    wr0te.py status      show agent status and last scans
"""

import csv
import datetime as dt
import difflib
import gzip
import hashlib
import html
import json
import os
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict

# ---------------------------------------------------------------- settings ---

LOCAL = os.path.expanduser(os.environ.get("WR0TE_LOCAL", "~/Library/Application Support/wr0te"))
CONFIG = os.path.join(LOCAL, "config.json")
SETTINGS = {  # name: (default, type); config.json may set any of these
    "root": (None, str),              # the folder of writing to watch
    "data": (None, str),              # logs + snapshots; default: wr0te-data next to root
    "daily_goal": (2000, int),        # notify once a day when written words reach this (0 = off)
    "day_start_hour": (4, int),       # writing at 2am counts toward the previous day
    "hide_folders": ([], list),       # leading folders left off paths in reports
    "split_folders": ([], list),      # top-level folders whose subfolders are each a project
    "menu_bar": (True, bool),         # show today's words in the menu bar
}


def read_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            user = json.load(f)
    except FileNotFoundError:
        user = {}
    except (OSError, ValueError) as e:
        sys.exit("wr0te: can't read %s: %s" % (CONFIG, e))
    if not isinstance(user, dict):
        sys.exit("wr0te: %s should be a JSON object" % CONFIG)
    cfg = {k: d for k, (d, _) in SETTINGS.items()}
    for k, v in user.items():
        ok = k in SETTINGS and (v is None or isinstance(v, SETTINGS[k][1]))
        if not ok or (isinstance(v, list) and not all(isinstance(x, str) for x in v)):
            sys.exit("wr0te: %s: bad setting %r" % (CONFIG, k))
        if v is not None:
            cfg[k] = v
    return cfg


def resolve_paths(cfg):
    """(root, data); the WR0TE_ROOT / WR0TE_DATA environment variables win over config.json."""
    root = os.environ.get("WR0TE_ROOT") or cfg["root"]
    if not root:
        return None, None
    root = os.path.abspath(os.path.expanduser(root))
    data = os.environ.get("WR0TE_DATA") or cfg["data"] or os.path.join(os.path.dirname(root), "wr0te-data")
    return root, os.path.abspath(os.path.expanduser(data))


_cfg = read_config()
ROOT, DATA = resolve_paths(_cfg)
DAILY_GOAL = _cfg["daily_goal"]
DAY_START_HOUR = _cfg["day_start_hour"]
HIDE_FOLDERS = _cfg["hide_folders"]
SPLIT_FOLDERS = _cfg["split_folders"]
MENU_BAR = _cfg["menu_bar"]

TEXT_EXT = {".md", ".markdown", ".mdown", ".txt", ".text", ".fountain"}
# Package-style folders (Pages, RTFD, Scrivener...) are opaque; never descend into them.
BUNDLE_EXT = (".pages", ".rtfd", ".textbundle", ".key", ".numbers", ".scriv", ".app")

SCAN_INTERVAL = 300          # seconds between launchd runs
TYPING_WPM = 90              # fast burst typing speed; anything quicker is a paste
SHINGLE = 8                  # tokens per fingerprint when checking "this text already existed"
SAMPLE = 4                   # index 1 in N fingerprints, to keep the archive index small
MOVED_MIN = 12               # matched runs shorter than this are coincidence, not moved text
RULES_VERSION = 3            # bump when classify() logic changes, so cached results are redone

LABEL = "com.wr0te.scan"
APP = os.path.expanduser("~/Applications/wr0te.app")
MENU_LABEL = "com.wr0te.menu"
MENU_APP = os.path.expanduser("~/Applications/wr0te-menu.app")
SF_DATALESS = 0x40000000     # Dropbox/iCloud "online-only" placeholder; reading it forces a download
MAX_FILE = 20 * 1024 * 1024  # skip anything bigger: not prose, and not worth syncing
HASH_RE = re.compile(r"[0-9a-f]{40}")

# ------------------------------------------------------------------ basics ---

CJK = "぀-ヿ㐀-䶿一-鿿豈-﫿가-힯"
TOKEN_RE = re.compile(r"[%s]|[^\s　-〿！-｠%s]+" % (CJK, CJK))
CJK_RE = re.compile(r"[%s]" % CJK)


def tokens(text):
    """Words, plus each CJK character as its own token. Bare markdown symbols don't count."""
    return [t for t in TOKEN_RE.findall(text) if any(c.isalnum() for c in t)]


def split_counts(toks):
    k = sum(1 for t in toks if len(t) == 1 and CJK_RE.match(t))
    return len(toks) - k, k


def decode(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def machine_name():
    name = os.environ.get("WR0TE_MACHINE")
    if not name:
        try:
            name = subprocess.check_output(["/usr/sbin/scutil", "--get", "LocalHostName"], text=True).strip()
        except (OSError, subprocess.CalledProcessError):
            name = socket.gethostname().split(".")[0]
    return re.sub(r"[^A-Za-z0-9_-]", "-", name)


def write_atomic(path, data):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")  # random name, O_EXCL: never follows a planted link
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def append_nofollow(path, text):
    """Append to a file, refusing to follow a symlink planted in its place."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(text)


def tilde(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def snapshot_path(h):
    return os.path.join(DATA, "snapshots", h[:2], h + ".gz")


def read_snapshot(h):
    if not HASH_RE.fullmatch(h or ""):
        return None
    try:
        with gzip.open(snapshot_path(h), "rb") as f:
            data = f.read(MAX_FILE + 1)  # bounded: a corrupt or hostile .gz can't eat memory
    except (OSError, EOFError, gzip.BadGzipFile):
        return None
    return decode(data) if len(data) <= MAX_FILE else None


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def writing_day(ts):
    return (dt.datetime.fromtimestamp(ts) - dt.timedelta(hours=DAY_START_HOUR)).date()


# -------------------------------------------------------------------- scan ---

def _walk_error(err):
    if isinstance(err, PermissionError):
        raise err  # a denied folder must not look like an empty one


def walk_text_files(root):
    for dirpath, dirs, names in os.walk(root, onerror=_walk_error):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not d.lower().endswith(BUNDLE_EXT)]
        for n in names:
            if not n.startswith(".") and os.path.splitext(n)[1].lower() in TEXT_EXT:
                yield os.path.join(dirpath, n)


def scan(quiet=False):
    machine = machine_name()
    state_file = os.path.join(LOCAL, "state.json")
    state = load_json(state_file, None)
    baseline = state is None or state.get("root") != ROOT
    if baseline:
        state = {"root": ROOT, "files": {}}
    last_scan = None if baseline else state.get("last_scan")
    files = state["files"]
    now = time.time()

    if not os.path.isdir(ROOT):
        log_line("scan skipped: %s not available" % ROOT)
        return 0

    events, seen = [], set()
    try:
        found = list(walk_text_files(ROOT))
    except PermissionError as e:
        log_line("scan: NO PERMISSION to read %s (%s) — open %s once and allow access to Dropbox"
                 % (ROOT, e.filename, APP))
        raise SystemExit(2)
    for full in found:
        try:
            st = os.lstat(full)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        rel = os.path.relpath(full, ROOT)
        seen.add(rel)
        if st.st_flags & SF_DATALESS or st.st_size > MAX_FILE:
            continue  # online-only (we'll catch it once it's local) or too big to be prose
        sig = [st.st_mtime_ns, st.st_size]
        prev = files.get(rel)
        if prev and prev[:2] == sig:
            continue
        try:
            fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW)  # it may have become a symlink since lstat
            with os.fdopen(fd, "rb") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    continue
                data = f.read(MAX_FILE + 1)
        except OSError:
            continue
        if len(data) > MAX_FILE:
            continue
        h = hashlib.sha1(data).hexdigest()
        files[rel] = sig + [h]
        if prev and prev[2] == h:
            continue  # touched but unchanged
        if not os.path.exists(snapshot_path(h)):
            write_atomic(snapshot_path(h), gzip.compress(data, mtime=0))
        w, k = split_counts(tokens(decode(data)))
        ev = {"e": "v", "p": rel, "h": h, "m": round(st.st_mtime, 3), "w": w, "k": k,
              "s": round(now), "by": machine}
        if baseline:
            ev["base"] = 1
        elif last_scan:
            ev["ps"] = round(last_scan)  # when this machine last looked; bounds the typing time
        events.append(ev)

    gone = [rel for rel in files if rel not in seen]
    # If the folder suddenly looks mostly empty (unmounted, mid-resync), hold off on logging mass
    # deletions; if they're still gone an hour later, they were really deleted or moved.
    if len(gone) > 20 and len(gone) > 0.25 * len(files) and now - state.setdefault("missing_since", now) < 3600:
        log_line("scan: %d files missing, not recording deletions yet" % len(gone))
    else:
        state.pop("missing_since", None)
        for rel in gone:
            events.append({"e": "d", "p": rel, "m": round(now, 3), "s": round(now), "by": machine})
            del files[rel]

    if events:
        month = dt.datetime.fromtimestamp(now).strftime("%Y-%m")
        logp = os.path.join(DATA, "logs", machine, month + ".jsonl")
        append_nofollow(logp, "".join(json.dumps(ev, ensure_ascii=False) + "\n" for ev in events))
    state["last_scan"] = now
    write_atomic(state_file, json.dumps(state).encode())
    msg = "scan: %d files, %d events%s (%.2fs)" % (
        len(seen), len(events), " [baseline]" if baseline else "", time.time() - now)
    log_line(msg)
    if not quiet:
        print(msg)
    return len(events)


def log_line(msg):
    os.makedirs(LOCAL, exist_ok=True)
    p = os.path.join(LOCAL, "scan.log")
    line = "%s %s\n" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        if os.path.getsize(p) > 512 * 1024:  # keep it small
            with open(p) as f:
                keep = f.readlines()[-2000:]
            with open(p, "w") as f:
                f.writelines(keep)
    except OSError:
        pass
    with open(p, "a") as f:
        f.write(line)


# ---------------------------------------------------------------- analysis ---

def load_events():
    evs = []
    logs = os.path.join(DATA, "logs")
    for machine in sorted(os.listdir(logs)) if os.path.isdir(logs) else []:
        d = os.path.join(logs, machine)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(".jsonl"):
                continue  # Dropbox "conflicted copy" logs end in .jsonl too: read them, repeats are harmless
            path = os.path.join(d, name)
            try:
                with open(path, encoding="utf-8") as f:
                    lines = f.readlines()
            except OSError as e:
                # Dropbox hasn't downloaded it yet, and launchd jobs can't make it (EDEADLK).
                # A report without another machine's history would be wrong, so don't build one.
                raise LogUnavailable("%s/%s (%s)" % (machine, name, e))
            for line in lines:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue  # partially synced last line
                if valid_event(ev):
                    evs.append(ev)
    return evs


class LogUnavailable(Exception):
    """A synced log can't be read right now (usually still online-only in Dropbox)."""


def _num(x, lo, hi):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and lo <= x <= hi  # also rejects NaN


def valid_event(ev):
    """Logs arrive from other machines via Dropbox; accept only well-formed events."""
    if not isinstance(ev, dict) or ev.get("e") not in ("v", "d"):
        return False
    if not isinstance(ev.get("p"), str) or not ev["p"] or not isinstance(ev.get("by", ""), str):
        return False
    if not (_num(ev.get("m"), 0, 4e9) and _num(ev.get("s"), 0, 4e9)):
        return False
    if "ps" in ev and not _num(ev["ps"], 0, 4e9):
        return False
    if ev["e"] == "v":
        if not isinstance(ev.get("h"), str) or not HASH_RE.fullmatch(ev["h"]):
            return False
        for k in ("w", "k"):
            if not (isinstance(ev.get(k), int) and not isinstance(ev[k], bool) and 0 <= ev[k] < 10 ** 8):
                return False
    return True


def diff_chunks(a, b):
    """Inserted and removed runs of tokens between two texts, in document order."""
    al, bl = a.splitlines(), b.splitlines()
    ins, rem = [], []
    if len(al) + len(bl) > 40000:  # enormous files: skip the line diff, go straight to the fallback
        opcodes = [("replace", 0, len(al), 0, len(bl))]
    else:
        opcodes = difflib.SequenceMatcher(None, al, bl, autojunk=False).get_opcodes()
    for op, i1, i2, j1, j2 in opcodes:
        if op == "equal":
            continue
        ta = tokens("\n".join(al[i1:i2]))
        tb = tokens("\n".join(bl[j1:j2]))
        if not ta or not tb:
            if ta:
                rem.append(ta)
            if tb:
                ins.append(tb)
        elif len(ta) + len(tb) < 4000:
            tm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
            # A few common words ("the", "and") matching by chance mustn't split a pasted
            # block into typeable pieces: join inserts across short matches.
            block = []
            for op2, x1, x2, y1, y2 in tm.get_opcodes():
                if op2 == "equal":
                    if x2 - x1 >= SHINGLE and block:
                        ins.append(block)
                        block = []
                    continue
                if x2 > x1:
                    rem.append(ta[x1:x2])
                if y2 > y1:
                    block = block + tb[y1:y2]
            if block:
                ins.append(block)
        else:  # huge rewrite: keep the tokens with no counterpart, in order (fast, close enough)
            for src, other, out in ((tb, ta, ins), (ta, tb, rem)):
                avail = Counter(other)
                extra = []
                for t in src:
                    if avail[t]:
                        avail[t] -= 1
                    else:
                        extra.append(t)
                if extra:
                    out.append(extra)
    return ins, rem


NORM_RE = re.compile(r"\W+")


def fingerprints(toks):
    """[(position, fp)] for a sample of the SHINGLE-token runs in toks."""
    n = [NORM_RE.sub("", t.lower()) for t in toks]
    out = []
    for i in range(len(n) - SHINGLE + 1):
        d = hashlib.blake2b("\x1f".join(n[i:i + SHINGLE]).encode(), digest_size=8).digest()
        v = int.from_bytes(d, "big", signed=True)
        if v % SAMPLE == 0:
            out.append((i, v))
    return out


def coverage(length, positions):
    """Mark tokens covered by matched shingles; drop matched runs too short to mean anything."""
    cov = [False] * length
    for i in positions:
        for j in range(i, min(i + SHINGLE, length)):
            cov[j] = True
    # Sampling leaves small holes inside a matched passage; fill them.
    last = None
    for j in range(length):
        if cov[j]:
            if last is not None and 0 < j - last - 1 <= SHINGLE * SAMPLE // 2:
                for x in range(last + 1, j):
                    cov[x] = True
            last = j
    j = 0
    while j < length:
        if cov[j]:
            k = j
            while k < length and cov[k]:
                k += 1
            if k - j < MOVED_MIN:
                for x in range(j, k):
                    cov[x] = False
            j = k
        else:
            j += 1
    # Only 1 in SAMPLE fingerprints is indexed, so a matched passage's first and last few tokens
    # go unseen; widen each passage by the expected gap.
    edge = SAMPLE - 1
    grown = list(cov)
    for flag, s, e in runs(cov):
        if flag:
            for x in range(max(0, s - edge), min(length, e + edge)):
                grown[x] = True
    return grown


def runs(flags):
    """[(flag, start, end)] for consecutive equal flags."""
    out, j = [], 0
    while j < len(flags):
        k = j
        while k < len(flags) and flags[k] == flags[j]:
            k += 1
        out.append((flags[j], j, k))
        j = k
    return out


class Archive:
    """Local SQLite store: fingerprints of everything ever written (with the earliest time each
    appeared) plus each version-to-version result. Rebuildable from snapshots; never synced."""

    def __init__(self):
        os.makedirs(LOCAL, exist_ok=True)
        self.db = sqlite3.connect(os.path.join(LOCAL, "archive.db"))
        self.db.execute("CREATE TABLE IF NOT EXISTS fp (h INTEGER PRIMARY KEY, t REAL) WITHOUT ROWID")
        self.db.execute("CREATE TABLE IF NOT EXISTS result (k TEXT PRIMARY KEY, v TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        # Cached results depend on the rules; if any rule changed, reclassify all history.
        index_rules = json.dumps([SHINGLE, SAMPLE])
        rules = json.dumps([RULES_VERSION, SHINGLE, SAMPLE, MOVED_MIN, TYPING_WPM, SCAN_INTERVAL])
        meta = dict(self.db.execute("SELECT k, v FROM meta"))
        if meta.get("index_rules") != index_rules:
            self.db.execute("DELETE FROM fp")
            self.db.execute("DELETE FROM result")
        elif meta.get("rules") != rules:
            self.db.execute("DELETE FROM result")
        self.db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                            [("index_rules", index_rules), ("rules", rules)])
        self.db.commit()
        self.results = dict(self.db.execute("SELECT k, v FROM result"))

    def found_before(self, fps, t):
        """Positions whose fingerprint already existed somewhere before time t."""
        if not fps:
            return []
        known = {}
        hs = list({h for _, h in fps})
        for i in range(0, len(hs), 500):
            part = hs[i:i + 500]
            q = "SELECT h, t FROM fp WHERE h IN (%s)" % ",".join("?" * len(part))
            known.update(self.db.execute(q, part))
        return [i for i, h in fps if h in known and known[h] < t]

    def add(self, fps, t):
        self.db.executemany("INSERT INTO fp VALUES (?, ?) ON CONFLICT(h) DO UPDATE SET t = min(t, excluded.t)",
                            [(h, t) for _, h in fps])

    def save_result(self, key, value):
        self.results[key] = json.dumps(value)
        self.db.execute("INSERT OR REPLACE INTO result VALUES (?, ?)", (key, self.results[key]))

    def close(self):
        self.db.commit()
        self.db.close()


def typing_window(prev, obs):
    """Seconds in which the new text could have been typed: since the later of the previous
    version and the last time a scanner looked at the file and saw the old version."""
    best = None
    for ev in obs:
        ps = ev.get("ps")
        if ps is None or ps > ev["m"]:
            continue  # unknown, or that machine hadn't synced the edit yet when it last looked
        w = ev["m"] - max(prev["m"] if prev else 0, ps)
        best = w if best is None else min(best, w)
    if best is None:
        best = obs[0]["m"] - prev["m"] if prev else SCAN_INTERVAL
    return max(best, 0)


def classify(archive, prev, ev, obs, kind):
    """Split one version-to-version change into written / moved / pasted / removed."""
    key = "%s>%s" % (prev["h"] if prev else "", ev["h"])
    a = read_snapshot(prev["h"]) if prev else ""
    b = read_snapshot(ev["h"])
    if a is None or b is None:
        # Snapshot hasn't synced to this machine yet: estimate from counts, store nothing.
        dw = ev["w"] - (prev["w"] if prev else 0)
        dk = ev["k"] - (prev["k"] if prev else 0)
        return {"aw": max(dw, 0), "ak": max(dk, 0), "rw": max(-dw, 0), "rk": max(-dk, 0), "mv": 0, "pa": 0}
    ins, rem = diff_chunks(a, b)
    r = {"aw": 0, "ak": 0, "rw": 0, "rk": 0, "mv": 0, "pa": 0}
    # At least one full scan interval: autosave timing makes shorter windows unreliable.
    allowance = TYPING_WPM * max(typing_window(prev, obs), SCAN_INTERVAL) / 60
    new_fps, typed = [], []
    for chunk in ins:
        fps = fingerprints(chunk)
        new_fps += fps
        if kind == "baseline":
            continue
        cov = coverage(len(chunk), archive.found_before(fps, ev["m"]))
        for moved, s, e in runs(cov):
            if moved:
                r["mv"] += e - s
            elif e - s > allowance:
                r["pa"] += e - s   # one unbroken block, faster than anyone types
            else:
                typed.append(chunk[s:e])
    # Separate blocks can't add up to more than could be typed either; the biggest are the pastes.
    typed.sort(key=len)
    total = sum(map(len, typed))
    while total > allowance:
        big = typed.pop()
        r["pa"] += len(big)
        total -= len(big)
    for toks in typed:
        w, k = split_counts(toks)
        r["aw"] += w
        r["ak"] += k
    # Text cut from one place and put back in another isn't "removed".
    inserted = {h for _, h in new_fps}
    for chunk in rem:
        fps = [(i, h) for i, h in fingerprints(chunk) if h in inserted]
        cov = coverage(len(chunk), [i for i, _ in fps])
        for moved, s, e in runs(cov):
            if not moved:
                w, k = split_counts(chunk[s:e])
                r["rw"] += w
                r["rk"] += k
    archive.add(new_fps, ev["m"])
    archive.save_result(key, r)
    return r


def build_changes(evs):
    """Merge all machines' observations into one list of real changes."""
    by_path = defaultdict(list)
    first_seen = {}  # hash -> (earliest mtime it existed anywhere, path)
    for ev in evs:
        by_path[ev["p"]].append(ev)
        if ev["e"] == "v":
            if ev["h"] not in first_seen or ev["m"] < first_seen[ev["h"]][0]:
                first_seen[ev["h"]] = (ev["m"], ev["p"])

    # 1. Each file's chain of distinct versions.
    steps = []  # [prev_ev, ev, observations-of-ev, kind]
    for path, lst in by_path.items():
        lst.sort(key=lambda e: (e["m"], e["s"]))
        prev, cur = None, None
        for ev in lst:
            if ev["e"] == "d":
                prev = cur = None
                continue
            if cur and cur[1]["h"] == ev["h"]:
                cur[2].append(ev)  # same version seen again (other machine, or rescan)
                continue
            kind = "edit"
            if prev is None:
                fm, fp = first_seen[ev["h"]]
                if ev.get("base") and not any(e is not ev and e["e"] == "v" and e["m"] < ev["m"] for e in lst):
                    kind = "baseline"
                elif fp != path and fm <= ev["m"]:
                    kind = "copy"  # moved, renamed or duplicated from an existing file
                else:
                    kind = "new"
            cur = [prev, ev, [ev], kind]
            steps.append(cur)
            prev = ev

    # 2. Classify in time order, so "already existed" only ever looks backwards.
    steps.sort(key=lambda s: s[1]["m"])
    archive = Archive()
    changes = []
    zero = {"aw": 0, "ak": 0, "rw": 0, "rk": 0, "mv": 0, "pa": 0}
    try:
        for prev, ev, obs, kind in steps:
            key = "%s>%s" % (prev["h"] if prev else "", ev["h"])
            if kind == "copy":
                r = zero  # identical text already indexed under its original path
            elif key in archive.results:
                r = json.loads(archive.results[key])
            else:
                r = classify(archive, prev, ev, obs, kind)
            if kind == "baseline":
                r = zero
            c = {"path": ev["p"], "m": ev["m"], "kind": kind, "by": ev.get("by", "?"),
                 "w": ev["w"], "k": ev["k"]}
            c.update(r)
            changes.append(c)
    finally:
        archive.close()
    return changes


def shown(path):
    """Path as displayed, without a leading folder from hide_folders."""
    for d in HIDE_FOLDERS:
        if path.startswith(d + os.sep):
            return path[len(d) + 1:]
    return path


def project_of(path):
    parts = shown(path).split(os.sep)[:-1]
    if not parts:
        return "(top level)"
    if len(parts) >= 2 and parts[0] in SPLIT_FOLDERS:
        return parts[0] + " / " + parts[1]
    return parts[0]


def counted(c):
    return c["kind"] in ("new", "edit")


def written(c):
    return c["aw"] + c["ak"] if counted(c) else 0


def removed(c):
    return c["rw"] + c["rk"] if counted(c) else 0


def daily(changes):
    days = defaultdict(lambda: {"added": 0, "removed": 0, "words": 0, "cjk": 0, "moved": 0, "pasted": 0,
                                "files": set(), "slots": set()})
    for c in changes:
        if not counted(c):
            continue
        d = days[writing_day(c["m"])]
        d["added"] += written(c)
        d["removed"] += removed(c)
        d["words"] += c["aw"]
        d["cjk"] += c["ak"]
        d["moved"] += c["mv"]
        d["pasted"] += c["pa"]
        if written(c) or removed(c):
            d["files"].add(c["path"])
            d["slots"].add(int(c["m"] // SCAN_INTERVAL))
    return days


def streak(days, today):
    n, d = 0, today
    if days.get(d, {}).get("added", 0) == 0:
        d -= dt.timedelta(days=1)  # today not started yet doesn't break the streak
    while days.get(d, {}).get("added", 0) > 0:
        n += 1
        d -= dt.timedelta(days=1)
    return n


# ---------------------------------------------------------------- terminal ---

def cmd_today(ndays=7):
    changes = build_changes(load_events())
    days = daily(changes)
    today = writing_day(time.time())
    print("%-12s %8s %8s %6s  %s" % ("day", "+words", "-words", "files", "~active"))
    for i in range(ndays - 1, -1, -1):
        d = today - dt.timedelta(days=i)
        x = days.get(d)
        if not x:
            print("%-12s %8s" % (d.strftime("%a %m-%d"), "·"))
            continue
        print("%-12s %8s %8s %6d  %s" % (d.strftime("%a %m-%d"), "{:,}".format(x["added"]),
                                         "{:,}".format(x["removed"]), len(x["files"]),
                                         fmt_minutes(len(x["slots"]) * SCAN_INTERVAL // 60)))
    print("streak: %d days" % streak(days, today))
    todays = [c for c in changes if writing_day(c["m"]) == today and written(c) + removed(c)]
    if todays:
        print("\ntoday:")
        per = defaultdict(lambda: [0, 0])
        for c in todays:
            per[c["path"]][0] += written(c)
            per[c["path"]][1] += removed(c)
        for p, (a, r) in sorted(per.items(), key=lambda kv: -kv[1][0]):
            print("  +%-6d -%-6d %s" % (a, r, shown(p)))


def fmt_minutes(m):
    return "%dh%02dm" % (m // 60, m % 60) if m >= 60 else "%dm" % m


# ------------------------------------------------------------------ report ---

def nice_step(maxv, n=4):
    if maxv <= 0:
        return 1
    raw = maxv / n
    mag = 10 ** (len(str(int(raw))) - 1)
    for s in (1, 2, 2.5, 5, 10):
        if s * mag >= raw:
            return int(s * mag) or 1
    return int(10 * mag)


def fmt_tick(v):
    if abs(v) >= 10000:
        return "%dK" % (v // 1000)
    return "{:,}".format(v)


def bar_path(x, y0, w, h, up=True, r=4):
    """Column with rounded data-end, square at the baseline."""
    r = min(r, w / 2, abs(h))
    if h <= 0:
        return ""
    if up:
        top = y0 - h
        return ("M%.1f,%.1f V%.1f Q%.1f,%.1f %.1f,%.1f H%.1f Q%.1f,%.1f %.1f,%.1f V%.1f Z"
                % (x, y0, top + r, x, top, x + r, top, x + w - r, x + w, top, x + w, top + r, y0))
    bot = y0 + h
    return ("M%.1f,%.1f V%.1f Q%.1f,%.1f %.1f,%.1f H%.1f Q%.1f,%.1f %.1f,%.1f V%.1f Z"
            % (x, y0, bot - r, x, bot, x + r, bot, x + w - r, x + w, bot, x + w, bot - r, y0))


def esc(s):
    return html.escape(str(s), quote=True)


def plural(n, word):
    return "{:,} {}{}".format(n, word, "" if n == 1 else "s")


def active(x):
    """Minutes with a saved change, rounded to scan windows."""
    return len(x["slots"]) * SCAN_INTERVAL // 60


def text_units(s):
    """Rough rendered width in Latin letters: CJK and other wide characters count two."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def clip(s, units):
    if text_units(s) <= units:
        return s
    out, n = "", 0
    for ch in s:
        n += text_units(ch)
        if n > units - 1:
            break
        out += ch
    return out + "…"


def longest_streak(days):
    best = run = 0
    prev = None
    for d in sorted(d for d, x in days.items() if x["added"] > 0):
        run = run + 1 if prev and d - prev == dt.timedelta(days=1) else 1
        best, prev = max(best, run), d
    return best


def day_tip(d, x, today):
    head = ("Today · " if d == today else "") + d.strftime("%a %b %-d, %Y")
    if not x or not (x["added"] or x["removed"] or x["moved"] or x["pasted"]):
        return head + "\nNo writing"
    parts = [head, plural(x["added"], "word") + " written"]
    if x["removed"]:
        parts.append("{:,} removed".format(x["removed"]))
    if x["cjk"]:
        parts.append("incl. {:,} CJK characters".format(x["cjk"]))
    if x["moved"] or x["pasted"]:
        parts.append("Not counted: {:,} moved · {:,} pasted".format(x["moved"], x["pasted"]))
    parts.append("%s · about %s" % (plural(len(x["files"]), "file"), fmt_minutes(active(x))))
    return "\n".join(parts)


def scroller(svg, min_width, end=False):
    """Charts keep a readable size in narrow windows and scroll sideways instead of shrinking."""
    return '<div class="scroll"%s><div style="min-width:%dpx">%s</div></div>' % (
        " data-end" if end else "", min_width, svg)


def svg_daily(days, today, n=90):
    W, H, L, R, T, B = 960, 260, 48, 8, 14, 28
    pw, ph = W - L - R, H - T - B
    seq = [today - dt.timedelta(days=i) for i in range(n - 1, -1, -1)]
    adds = [days.get(d, {}).get("added", 0) for d in seq]
    rems = [days.get(d, {}).get("removed", 0) for d in seq]
    peak = max(adds + [DAILY_GOAL, 1])
    step = nice_step(peak)
    top = step * -(-peak // step)
    # Removed words hang below the line, scaled like the rest but never taking more than a quarter
    # of the height; the tooltip and table have the true value.
    rmax = max(rems)
    below = min(0.25, rmax / (top + rmax)) if rmax else 0
    scale = ph * (1 - below) / top
    y0 = T + top * scale
    floor = ph * below / scale
    slot = pw / n
    bw = min(24, slot - 2)
    best = max(range(n), key=lambda i: adds[i])
    summary = "Words written per day, last %d days. Best day: %s on %s. Today: %s." % (
        n, "{:,}".format(adds[best]), seq[best].strftime("%b %-d"), "{:,}".format(adds[-1]))
    if DAILY_GOAL:
        summary += " Goal: {:,}.".format(DAILY_GOAL)
    out = ['<svg viewBox="0 0 %d %d" class="chart" role="img" aria-label="%s">' % (W, H, esc(summary))]
    for v in range(0, top + 1, step):
        y = y0 - v * scale
        out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="grid"/>' % (L, W - R, y, y))
        out.append('<text x="%d" y="%.1f" class="tick" text-anchor="end">%s</text>' % (L - 8, y + 4, fmt_tick(v)))
    if rmax:
        out.append('<text x="%d" y="%.1f" class="tick" text-anchor="end">−%s</text>'
                   % (L - 8, y0 + floor * scale, fmt_tick(int(floor))))
    for i, d in enumerate(seq):
        x = L + i * slot + (slot - bw) / 2
        a, r = adds[i], min(rems[i], floor)
        cls = "s1 today" if d == today else "s1"
        if a:
            out.append('<path d="%s" class="%s"/>' % (bar_path(x, y0 - 1, bw, max(2, a * scale - 1), True), cls))
        if r:
            out.append('<path d="%s" class="s2"/>' % bar_path(x, y0 + 1, bw, max(2, r * scale - 1), False))
        if d == today:
            out.append('<circle cx="%.1f" cy="%d" r="3" class="today-dot"/>' % (x + bw / 2, T + ph + 9))
        if d.day == 1 or i == 0:
            out.append('<text x="%.1f" y="%d" class="tick">%s</text>' % (L + i * slot, H - 4, d.strftime("%b %-d")))
        out.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" class="hit" data-tip="%s"/>'
                   % (L + i * slot, T, slot, ph, esc(day_tip(d, days.get(d), today))))
    if DAILY_GOAL:
        gy = y0 - DAILY_GOAL * scale
        out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="goal"/>' % (L, W - R, gy, gy))
        out.append('<text x="%d" y="%.1f" class="goal-l">goal {:,}</text>'.format(DAILY_GOAL) % (L + 6, gy - 5))
    out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="axis"/>' % (L, W - R, y0, y0))
    out.append("</svg>")
    return "\n".join(out)


def svg_calendar(days, today):
    cell, gap = 12, 3
    start = today - dt.timedelta(days=364)
    start -= dt.timedelta(days=(start.weekday() + 1) % 7)  # back to Sunday
    vals = sorted(x["added"] for d, x in days.items() if d >= start and x["added"] > 0)

    def q(p):
        return vals[min(len(vals) - 1, int(p * len(vals)))] if vals else 0
    cuts = [q(0.2), q(0.4), q(0.6), q(0.8)]
    weeks = ((today - start).days // 7) + 1
    W, H = 30 + weeks * (cell + gap) + 12, 20 + 7 * (cell + gap)  # room for a month label in the last week
    written_days = sum(1 for d, x in days.items() if d >= start and x["added"])
    label = "Words written per day over the last year: %s with writing, best day %s words." % (
        plural(written_days, "day"), "{:,}".format(vals[-1] if vals else 0))
    out = ['<svg viewBox="0 0 %d %d" class="chart cal" role="img" aria-label="%s">' % (W, H, esc(label))]
    for i, lab in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        out.append('<text x="0" y="%d" class="tick">%s</text>' % (20 + i * (cell + gap) + 10, lab))
    d, lastm = start, None
    while d <= today:
        wk, wd = (d - start).days // 7, (d.weekday() + 1) % 7
        x, y = 30 + wk * (cell + gap), 20 + wd * (cell + gap)
        if d.month != lastm and wd == 0:
            if wk > 0 or (d + dt.timedelta(days=14)).month == d.month:  # skip a cramped first label
                out.append('<text x="%d" y="12" class="tick">%s</text>' % (x, d.strftime("%b")))
            lastm = d.month
        a = days.get(d, {}).get("added", 0)
        lvl = 0 if a == 0 else 1 + sum(1 for c in cuts if a > c)
        out.append('<rect x="%d" y="%d" width="%d" height="%d" rx="2.5" class="h%d%s" data-tip="%s"/>'
                   % (x, y, cell, cell, lvl, " today" if d == today else "", esc(day_tip(d, days.get(d), today))))
        d += dt.timedelta(days=1)
    out.append("</svg>")
    ranges = ["No writing", "1–{:,} words".format(cuts[0])]
    ranges += ["{:,}–{:,} words".format(lo + 1, hi) for lo, hi in zip(cuts, cuts[1:])]
    ranges.append("More than {:,} words".format(cuts[3]))
    legend = ('<div class="scale" aria-hidden="true"><span>0</span>' +
              "".join('<i class="h%d" data-tip="%s"></i>' % (i, esc(r)) for i, r in enumerate(ranges)) +
              "<span>{:,}</span></div>".format(vals[-1] if vals else 0))
    return scroller("\n".join(out), W, end=True) + legend


def svg_hbars(rows, label):
    """rows: [(name, value, tip)] single series horizontal bars."""
    if not rows:
        return '<p class="empty">Nothing in the last 30 days.</p>'
    names = [clip(name, 36) for name, _, _ in rows]
    rh, W = 30, 960
    L = int(min(300, max(90, 16 + 7.4 * max(text_units(n) for n in names))))
    H = rh * len(rows) + 4
    mx = max(v for _, v, _ in rows) or 1
    aria = "%s: %s." % (label, "; ".join("%s {:,}".format(v) % name for name, v, _ in rows))
    out = ['<svg viewBox="0 0 %d %d" class="chart" role="img" aria-label="%s">' % (W, H, esc(aria))]
    for i, ((_, v, tip), short) in enumerate(zip(rows, names)):
        y = i * rh + 4
        w = (W - L - 80) * v / mx
        out.append('<text x="%d" y="%d" class="lab" text-anchor="end">%s</text>' % (L - 12, y + 15, esc(short)))
        if w >= 1:
            out.append('<path d="M%d,%d H%.1f Q%.1f,%d %.1f,%d V%d Q%.1f,%d %.1f,%d H%d Z" class="s1"/>'
                       % (L, y + 3, L + w - 4, L + w, y + 3, L + w, y + 7, y + 15, L + w, y + 19, L + w - 4, y + 19, L))
        out.append('<text x="%.1f" y="%d" class="val">%s</text>' % (L + w + 8, y + 15, "{:,}".format(v)))
        out.append('<rect x="0" y="%d" width="%d" height="%d" class="hit" data-tip="%s"/>' % (y - 2, W, rh, esc(tip)))
    out.append("</svg>")
    return scroller("\n".join(out), 600)


def hour_totals(changes, today, n=90):
    """Words written in each clock hour over the last n days, in writing-day order (from DAY_START_HOUR)."""
    since = today - dt.timedelta(days=n)
    hours = [0] * 24
    for c in changes:
        if writing_day(c["m"]) > since:
            hours[dt.datetime.fromtimestamp(c["m"]).hour] += written(c)
    return [((DAY_START_HOUR + i) % 24, hours[(DAY_START_HOUR + i) % 24]) for i in range(24)]


def svg_hours(hours, n=90):
    W, H, L, R, T, B = 960, 170, 48, 8, 12, 26
    pw, ph = W - L - R, H - T - B
    peak = max([v for _, v in hours] + [1])
    step = nice_step(peak, 3)
    top = step * -(-peak // step)
    scale = ph / top
    slot = pw / 24
    bw = min(24, slot - 2)
    y0 = T + ph
    busiest = max(hours, key=lambda hv: hv[1])
    label = "Words written by hour of day, last %d days. Most in the %02d:00 hour: %s." % (
        n, busiest[0], "{:,}".format(busiest[1]))
    out = ['<svg viewBox="0 0 %d %d" class="chart" role="img" aria-label="%s">' % (W, H, esc(label))]
    for v in range(0, top + 1, step):
        y = y0 - v * scale
        out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="grid"/>' % (L, W - R, y, y))
        out.append('<text x="%d" y="%.1f" class="tick" text-anchor="end">%s</text>' % (L - 8, y + 4, fmt_tick(v)))
    for i, (h, v) in enumerate(hours):
        x = L + i * slot + (slot - bw) / 2
        if v:
            out.append('<path d="%s" class="s1"/>' % bar_path(x, y0, bw, max(2, v * scale), True))
        if i % 3 == 0:
            out.append('<text x="%.1f" y="%d" class="tick" text-anchor="middle">%02d:00</text>'
                       % (L + i * slot + slot / 2, H - 6, h))
        out.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" class="hit" data-tip="%s"/>'
                   % (L + i * slot, T, slot, ph, esc("%02d:00–%02d:59\n%s written, last %d days"
                                                     % (h, h, plural(v, "word"), n))))
    out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="axis"/>' % (L, W - R, y0, y0))
    out.append("</svg>")
    return scroller("\n".join(out), 760)


def sessions(changes, gap=1800):
    """Saves of one file no more than `gap` seconds apart, merged into one writing session."""
    out, cur = [], {}
    for c in changes:  # in time order
        if c["kind"] == "copy":
            out.append({"path": c["path"], "start": c["m"], "end": c["m"], "w": 0, "r": 0, "mv": 0, "pa": 0,
                        "note": "copied from another file"})
            continue
        if not counted(c) or not (written(c) or removed(c) or c["mv"] or c["pa"]):
            continue
        s = cur.get(c["path"])
        if not s or c["m"] - s["end"] > gap:
            s = cur[c["path"]] = {"path": c["path"], "start": c["m"], "end": c["m"], "w": 0, "r": 0, "mv": 0,
                                  "pa": 0, "note": "new file" if c["kind"] == "new" else ""}
            out.append(s)
        s["end"] = c["m"]
        s["w"] += written(c)
        s["r"] += removed(c)
        s["mv"] += c["mv"]
        s["pa"] += c["pa"]
    for s in out:
        bits = [s["note"]] if s["note"] else []
        if s["mv"]:
            bits.append("{:,} moved".format(s["mv"]))
        if s["pa"]:
            bits.append("{:,} pasted".format(s["pa"]))
        s["note"] = " · ".join(bits)
    return out


def table(caption, cols, rows, opt=()):
    """cols: [(label, kind)], kind "n" (number), "p" (path), "t" (time) or ""; rows hold escaped cells.
    Columns listed in opt are hidden in narrow windows."""
    def cls(i):
        c = [k for k in (cols[i][1], "opt" if i in opt else "") if k]
        return ' class="%s"' % " ".join(c) if c else ""
    h = ['<table><caption class="sr">%s</caption><thead><tr>' % esc(caption)]
    h += ['<th scope="col"%s>%s</th>' % (cls(i), esc(c)) for i, (c, _) in enumerate(cols)]
    h.append("</tr></thead><tbody>")
    for r in rows:
        h.append("<tr>" + "".join("<td%s>%s</td>" % (cls(i), v) for i, v in enumerate(r)) + "</tr>")
    h.append("</tbody></table>")
    return "".join(h)


def num(v):
    return "{:,}".format(v) if v else '<span class="none">—</span>'


CSS = """
:root{color-scheme:light dark;--page:#f6f3ee;--surface:#fffdf9;--ink:#211d19;--ink2:#58524a;--muted:#6b645b;
--rule:#e3ddd3;--grid:#ebe5dc;--axis:#c9c1b4;--s1:#c2562f;--s2:#8f877b;--focus:#c2562f;
--h0:#e4ddd1;--h1:#ebb293;--h2:#e3946f;--h3:#cc6943;--h4:#a84826;--h5:#743016}
@media (prefers-color-scheme:dark){:root{--page:#14120f;--surface:#1c1915;--ink:#f1ece4;--ink2:#c7bfb3;
--muted:#a39a8d;--rule:#2d2924;--grid:#28241f;--axis:#3e3830;--s1:#e47c55;--s2:#80776b;--focus:#e47c55;
--h0:#2b2722;--h1:#5c301d;--h2:#7a3a22;--h3:#b2522e;--h4:#de7a52;--h5:#f4b494}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--page);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
-webkit-font-smoothing:antialiased}
::selection{background:var(--h1);color:var(--ink)}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:3px}
a{color:inherit;text-decoration-color:var(--axis);text-underline-offset:3px}
a:hover{text-decoration-color:var(--s1)}
main{max-width:1000px;margin:0 auto;padding:40px 20px 72px}
header{margin-bottom:24px}
h1{font-size:24px;line-height:1.2;font-weight:650;letter-spacing:-.01em;margin:0}
h2{font-size:18px;line-height:1.3;font-weight:600;margin:0 0 4px}
.sub{color:var(--ink2);font-size:14px;margin:4px 0 0;max-width:68ch}
.n,.num,.stats dd,td.n{font-variant-numeric:tabular-nums}
.now{background:var(--surface);border:1px solid var(--rule);border-radius:16px;padding:28px 28px 24px}
.big{margin:0;font-weight:500;font-size:20px;color:var(--ink2);line-height:1.1}
.big .num{display:block;font-size:64px;font-weight:650;letter-spacing:-.02em;color:var(--ink);margin-bottom:6px}
.big.quiet{font-size:30px;font-weight:600;color:var(--ink)}
.goalbar{margin:20px 0 6px;height:6px;border-radius:3px;background:var(--h0);overflow:hidden;max-width:480px}
.goalbar i{display:block;height:100%;background:var(--s1);border-radius:3px}
.goaltext{color:var(--ink2);font-size:14px;margin:0}
.pieces{margin:16px 0 0;font-size:16px}
.detail{color:var(--ink2);font-size:14px;margin:6px 0 0}
abbr{text-decoration:underline dotted var(--axis);text-underline-offset:3px;cursor:help}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:20px;margin:28px 0 0;
padding-top:20px;border-top:1px solid var(--rule)}
.stats div{margin:0}.stats dt{color:var(--ink2);font-size:13px}
.stats dd{margin:2px 0 0;font-size:24px;font-weight:600;letter-spacing:-.01em}
.stats small{display:block;color:var(--muted);font-size:13px;font-weight:400;letter-spacing:0;margin-top:2px}
section.block{margin-top:48px;padding-top:28px;border-top:1px solid var(--rule)}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;color:var(--ink2);font-size:13px;margin-top:12px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.legend i.dash{height:0;border-top:2px dashed var(--ink2);border-radius:0;vertical-align:3px;width:14px}
.legend i.dot{border-radius:50%;background:var(--ink)}
.scroll{overflow-x:auto;margin-top:14px;scrollbar-width:thin;scrollbar-color:var(--axis) transparent}
.chart{width:100%;height:auto;display:block}
.chart text{font-size:11px}
.tick{fill:var(--muted)}.lab{fill:var(--ink2);font-size:12.5px!important}
.val{fill:var(--ink);font-size:12.5px!important;font-variant-numeric:tabular-nums}
.grid{stroke:var(--grid);stroke-width:1}.axis{stroke:var(--axis);stroke-width:1}
.goal{stroke:var(--ink2);stroke-width:1.25;stroke-dasharray:4 4;opacity:.75}
.goal-l{fill:var(--ink2);font-size:11px;paint-order:stroke;stroke:var(--page);stroke-width:3px}
.s1{fill:var(--s1)}.s2{fill:var(--s2)}
.chart .s1.today{stroke:var(--ink);stroke-width:1.5}.today-dot{fill:var(--ink)}
.hit{fill:transparent}.hit:hover{fill:var(--ink);fill-opacity:.07}
.h0{fill:var(--h0)}.h1{fill:var(--h1)}.h2{fill:var(--h2)}.h3{fill:var(--h3)}.h4{fill:var(--h4)}.h5{fill:var(--h5)}
.cal rect:hover{stroke:var(--ink);stroke-width:1}.cal rect.today{stroke:var(--ink);stroke-width:1.5}
.scale{display:flex;gap:4px;align-items:center;justify-content:flex-end;color:var(--muted);font-size:12px;margin-top:8px}
.scale span{margin:0 4px;font-variant-numeric:tabular-nums}
.scale i{width:12px;height:12px;border-radius:2.5px;display:inline-block}
.scale i.h0{background:var(--h0)}.scale i.h1{background:var(--h1)}.scale i.h2{background:var(--h2)}
.scale i.h3{background:var(--h3)}.scale i.h4{background:var(--h4)}.scale i.h5{background:var(--h5)}
table{width:100%;border-collapse:collapse;font-size:14px;margin-top:14px}
th{text-align:left;color:var(--ink2);font-weight:500;font-size:13px;border-bottom:1px solid var(--rule);padding:6px 12px 6px 0}
td{border-bottom:1px solid var(--grid);padding:8px 12px 8px 0;vertical-align:top}
th:last-child,td:last-child{padding-right:0}
th.n,td.n{text-align:right;white-space:nowrap}
td.p{overflow-wrap:anywhere}
td.t{white-space:nowrap;color:var(--ink2)}
td.note{color:var(--ink2)}
.none{color:var(--muted)}
.empty{color:var(--ink2);margin:14px 0 0}
details{margin-top:14px}
details summary{cursor:pointer;color:var(--ink2);font-size:14px;width:max-content}
details summary:hover{color:var(--ink)}
.sr{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
footer{margin-top:56px;padding-top:20px;border-top:1px solid var(--rule);color:var(--ink2);font-size:13px}
footer p{max-width:68ch;margin:0 0 6px}
#tip{position:fixed;pointer-events:none;background:var(--surface);color:var(--ink);border:1px solid var(--rule);
border-radius:10px;padding:9px 11px;font-size:13px;line-height:1.45;white-space:pre-line;
box-shadow:0 2px 4px rgba(0,0,0,.08);display:none;z-index:9;max-width:300px}
#tip b{font-weight:600;display:block;margin-bottom:2px}
@media (max-width:640px){
main{padding:28px 16px 56px}
.now{padding:22px 18px 18px;border-radius:14px}
.big .num{font-size:52px}
.stats{grid-template-columns:1fr 1fr;gap:16px}
.opt{display:none}
section.block{margin-top:40px}
}
"""

JS = """
const tip=document.getElementById('tip');
function show(t,cx,cy){
  const [h,...rest]=t.dataset.tip.split('\\n');
  tip.textContent='';const b=document.createElement('b');b.textContent=h;tip.appendChild(b);
  if(rest.length){const s=document.createElement('div');s.textContent=rest.join('\\n');tip.appendChild(s)}
  tip.style.display='block';
  const r=tip.getBoundingClientRect();
  let x=cx+14,y=cy+14;
  if(x+r.width>innerWidth-8)x=cx-r.width-14;
  if(y+r.height>innerHeight-8)y=cy-r.height-14;
  tip.style.left=Math.max(8,x)+'px';tip.style.top=Math.max(8,y)+'px';
}
function target(e){return e.target.closest&&e.target.closest('[data-tip]')}
document.addEventListener('mousemove',e=>{const t=target(e);if(t)show(t,e.clientX,e.clientY);else tip.style.display='none'});
document.addEventListener('click',e=>{const t=target(e);if(t)show(t,e.clientX,e.clientY);else tip.style.display='none'});
addEventListener('scroll',()=>{tip.style.display='none'},{passive:true});
document.querySelectorAll('.scroll[data-end]').forEach(s=>{s.scrollLeft=s.scrollWidth});
"""


def cmd_report(open_it=False, quiet=False):
    changes = build_changes(load_events())
    days = daily(changes)
    now = time.time()
    today = writing_day(now)
    empty_day = {"added": 0, "removed": 0, "files": set(), "slots": set(), "cjk": 0, "moved": 0, "pasted": 0}
    t = days.get(today, empty_day)

    def total(since_days):
        lo = today - dt.timedelta(days=since_days - 1)
        return sum(x["added"] for d, x in days.items() if d >= lo)

    year_start = dt.date(today.year, 1, 1)
    ytd = sum(x["added"] for d, x in days.items() if d >= year_start)
    days_30 = sum(1 for d, x in days.items() if d > today - dt.timedelta(days=30) and x["added"])

    # projects & files, last 30 days
    lo = today - dt.timedelta(days=29)
    proj, fil = defaultdict(lambda: [0, 0, set()]), defaultdict(lambda: [0, 0, 0, 0])
    for c in changes:
        if writing_day(c["m"]) < lo or c["kind"] not in ("new", "edit"):
            continue
        p = proj[project_of(c["path"])]
        p[0] += written(c); p[1] += removed(c); p[2].add(c["path"])
        f = fil[c["path"]]
        f[0] += written(c); f[1] += removed(c); f[2] = max(f[2], c["m"]); f[3] = c["w"] + c["k"]
    prow = sorted(proj.items(), key=lambda kv: -kv[1][0])[:12]
    prow = [(k, v[0], "%s\n%s written · {:,} removed\n%s, last 30 days".format(v[1])
             % (k, plural(v[0], "word"), plural(len(v[2]), "file"))) for k, v in prow if v[0]]
    frow = [kv for kv in sorted(fil.items(), key=lambda kv: -kv[1][0]) if kv[1][0] or kv[1][1]][:25]

    # what was worked on today, biggest first
    per = defaultdict(int)
    for c in changes:
        if writing_day(c["m"]) == today and written(c):
            per[c["path"]] += written(c)
    pieces = [os.path.splitext(os.path.basename(p))[0] for p, _ in sorted(per.items(), key=lambda kv: -kv[1])]

    state = load_json(os.path.join(LOCAL, "state.json"), None)
    machines = sorted({c["by"] for c in changes})
    meta = [esc(tilde(ROOT))]
    if isinstance(state, dict) and state.get("files"):
        meta.append(plural(len(state["files"]), "file"))  # there now, not ever
    if machines:
        meta.append(esc(", ".join(machines)))
    meta.append("updated " + dt.datetime.fromtimestamp(now).strftime("%b %-d, %H:%M"))

    h = ['<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width,initial-scale=1">',
         "<title>%s</title><style>%s</style></head>"
         % ("%s today · wr0te" % plural(t["added"], "word") if t["added"] else "wr0te", CSS),
         "<body><main>",
         '<header><h1>wr0te</h1><p class="sub">%s</p></header>' % " · ".join(meta)]

    # ---- today
    h.append('<section class="now" aria-label="Today">')
    if t["added"]:
        h.append('<h2 class="big"><span class="num">%s</span>%s today</h2>'
                 % ("{:,}".format(t["added"]), "word" if t["added"] == 1 else "words"))
    else:
        yesterday = days.get(today - dt.timedelta(days=1), {}).get("added", 0)
        h.append('<h2 class="big quiet">No words yet today</h2>')
        h.append('<p class="detail">Your day started at %d:00.%s</p>' % (
            DAY_START_HOUR, " Yesterday: %s." % plural(yesterday, "word") if yesterday else ""))
    if DAILY_GOAL:
        pct = min(100, 100 * t["added"] // DAILY_GOAL)
        h.append('<div class="goalbar" role="progressbar" aria-label="Daily goal" aria-valuemin="0" '
                 'aria-valuemax="%d" aria-valuenow="%d"><i style="width:%d%%"></i></div>'
                 % (DAILY_GOAL, min(t["added"], DAILY_GOAL), pct))
        h.append('<p class="goaltext">%s</p>' % (
            "Goal of {:,} reached".format(DAILY_GOAL) if t["added"] >= DAILY_GOAL
            else "{:,} to go · goal {:,}".format(DAILY_GOAL - t["added"], DAILY_GOAL)))
    if pieces:
        names = ", ".join("<b>%s</b>" % esc(n) for n in pieces[:3])
        more = " and %d more" % (len(pieces) - 3) if len(pieces) > 3 else ""
        h.append('<p class="pieces">Worked on %s%s</p>' % (names, more))
    if t["added"] or t["removed"]:
        bits = [plural(len(t["files"]), "file"),
                'about %s <abbr title="Counted in %d-minute windows with a saved change">active</abbr>'
                % (fmt_minutes(active(t)), SCAN_INTERVAL // 60)]
        if t["removed"]:
            bits.append("{:,} removed".format(t["removed"]))
        if t["moved"] or t["pasted"]:
            bits.append("not counted: {:,} moved, {:,} pasted".format(t["moved"], t["pasted"]))
        h.append('<p class="detail">%s</p>' % " · ".join(bits))
    cur, best = streak(days, today), longest_streak(days)
    h.append('<dl class="stats">')
    for lab, val in (("Last 7 days", "{:,}".format(total(7))), ("Last 30 days", "{:,}".format(total(30))),
                     ("This year", "{:,}".format(ytd)),
                     ("Days written", "%d of 30<small>streak %d · longest %d</small>" % (days_30, cur, best))):
        h.append("<div><dt>%s</dt><dd>%s</dd></div>" % (lab, val))
    h.append("</dl></section>")

    # ---- last 90 days
    h.append('<section class="block" id="days"><h2>Last 90 days</h2>'
             '<p class="sub">Words written each day. Removed words hang below the line. '
             'Moved, pasted and copied text isn\'t counted.</p>')
    h.append('<div class="legend" aria-hidden="true"><span><i style="background:var(--s1)"></i>Written</span>'
             '<span><i style="background:var(--s2)"></i>Removed</span>' +
             ('<span><i class="dash"></i>Goal</span>' if DAILY_GOAL else "") +
             '<span><i class="dot"></i>Today</span></div>')
    h.append(scroller(svg_daily(days, today), 760, end=True))
    rows = []
    for i in range(90):
        d = today - dt.timedelta(days=i)
        x = days.get(d, empty_day)
        rows.append([("Today" if d == today else d.strftime("%a %b %-d")), num(x["added"]), num(x["removed"]),
                     num(len(x["files"])), fmt_minutes(active(x)) if active(x) else num(0)])
    h.append("<details><summary>Show as table</summary>%s</details></section>" % table(
        "Words per day, last 90 days, newest first",
        [("Day", "t"), ("Written", "n"), ("Removed", "n"), ("Files", "n"), ("Active", "n")], rows, opt=(3, 4)))

    # ---- year
    h.append('<section class="block" id="year"><h2>The last year</h2><p class="sub">Words written each day. '
             'Darker means more.</p>')
    h.append(svg_calendar(days, today))
    rows, y, m = [], today.year, today.month
    for _ in range(12):
        md = [x for d, x in days.items() if d.year == y and d.month == m]
        rows.append([dt.date(y, m, 1).strftime("%B %Y"), num(sum(x["added"] for x in md)),
                     num(sum(1 for x in md if x["added"]))])
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    h.append("<details><summary>Show by month</summary>%s</details></section>" % table(
        "Words per month, last 12 months", [("Month", ""), ("Written", "n"), ("Days written", "n")], rows))

    # ---- projects
    h.append('<section class="block" id="projects"><h2>Projects, last 30 days</h2>'
             '<p class="sub">Words written, by folder.</p>')
    h.append(svg_hbars(prow, "Words written per project, last 30 days") + "</section>")

    # ---- hours
    hours = hour_totals(changes, today)
    h.append('<section class="block" id="hours"><h2>When you write</h2><p class="sub">Words written by hour, '
             'last 90 days. Your day starts at %d:00.</p>' % DAY_START_HOUR)
    h.append(svg_hours(hours))
    h.append("<details><summary>Show as table</summary>%s</details></section>" % table(
        "Words written by hour of day, last 90 days", [("Hour", "t"), ("Written", "n")],
        [["%02d:00–%02d:59" % (hr, hr), num(v)] for hr, v in hours]))

    # ---- files
    h.append('<section class="block" id="files"><h2>Files, last 30 days</h2>')
    if frow:
        h.append(table("Files written in, last 30 days",
                       [("File", "p"), ("Written", "n"), ("Removed", "n"), ("Words now", "n"), ("Last edited", "n")],
                       [[esc(shown(p)), num(a), num(r), num(length), dt.datetime.fromtimestamp(m).strftime("%b %-d, %H:%M")]
                        for p, (a, r, m, length) in frow], opt=(2, 3)))
    else:
        h.append('<p class="empty">Nothing in the last 30 days.</p>')
    h.append("</section>")

    # ---- sessions
    recent = sorted(sessions(changes), key=lambda s: -s["end"])[:15]
    h.append('<section class="block" id="sessions"><h2>Recent sessions</h2>'
             '<p class="sub">Saves to one file less than 30 minutes apart.</p>')
    if recent:
        notes = any(s["note"] for s in recent)
        cols = [("When", "t"), ("File", "p"), ("Written", "n"), ("Removed", "n")] + ([("Note", "note")] if notes else [])
        rows = []
        for s in recent:
            a, b = dt.datetime.fromtimestamp(s["start"]), dt.datetime.fromtimestamp(s["end"])
            when = a.strftime("%b %-d, %H:%M") + ("–" + b.strftime("%H:%M") if b.strftime("%H:%M") != a.strftime("%H:%M") else "")
            row = [when, esc(shown(s["path"])), num(s["w"]), num(s["r"])]
            if notes:
                row.append(esc(s["note"]))
            rows.append(row)
        h.append(table("Recent writing sessions, newest first", cols, rows, opt=(3, 4)))
    else:
        h.append('<p class="empty">No sessions yet. They appear after your first saved change.</p>')
    h.append("</section>")

    h.append('<footer><p>Words are counted as words; Chinese, Japanese and Korean characters count one each. '
             'Text moved from elsewhere in your writing, pasted blocks and copied files aren\'t counted.</p>'
             '<p>Every day is in <a href="daily.csv">daily.csv</a>, next to this report.</p></footer>')
    h.append('<div id="tip" aria-hidden="true"></div><script>%s</script></main></body></html>' % JS)

    out = os.path.join(LOCAL, "report.html")
    write_atomic(out, "\n".join(h).encode())
    with open(os.path.join(LOCAL, "daily.csv"), "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["day", "written", "removed", "cjk_written", "moved", "pasted", "files", "active_minutes"])
        for d in sorted(days):
            x = days[d]
            wr.writerow([d.isoformat(), x["added"], x["removed"], x["cjk"], x["moved"], x["pasted"], len(x["files"]),
                         active(x)])
    write_atomic(os.path.join(LOCAL, "today.json"), json.dumps({  # what the menu bar shows
        "day": today.isoformat(), "day_start_hour": DAY_START_HOUR, "written": t["added"],
        "removed": t["removed"], "files": len(t["files"]), "active_minutes": active(t),
        "streak": streak(days, today), "goal": DAILY_GOAL, "updated": now}).encode())
    if not quiet:
        print(out)
    if open_it:
        subprocess.call(["/usr/bin/open", out])
    return days


# ----------------------------------------------------------------- launchd ---

def plist_path(label=LABEL):
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % label)


def build_bundle(app, ident, compile_cmd, build_id, ui_key):
    """Build an ad-hoc signed .app around one executable, unless build_id says it's already current."""
    name = os.path.splitext(os.path.basename(app))[0]
    exe = os.path.join(app, "Contents", "MacOS", name)
    id_file = os.path.join(app, "Contents", "Resources", "build-id")
    try:
        with open(id_file) as f:
            if f.read().strip() == build_id and os.path.exists(exe):
                return exe, False
    except OSError:
        pass
    os.makedirs(os.path.dirname(exe), exist_ok=True)
    info = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>{ident}</string>
  <key>CFBundleName</key><string>{name}</string>
  <key>CFBundleExecutable</key><string>{name}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>{ui_key}</key><true/>
</dict></plist>
""".format(ident=ident, name=name, ui_key=ui_key)
    write_atomic(os.path.join(app, "Contents", "Info.plist"), info.encode())
    subprocess.check_call(compile_cmd + ["-o", exe])
    write_atomic(id_file, build_id.encode())
    subprocess.check_call(["/usr/bin/codesign", "--force", "-s", "-", "--identifier", ident, app])
    return exe, True


def build_app(script):
    """Compile launcher.c into ~/Applications/wr0te.app.

    macOS ties the Dropbox permission to the exact binary, so only rebuild when the launcher
    source or script path changed; otherwise every reinstall would silently revoke access."""
    src = os.path.join(os.path.dirname(script), "launcher.c")
    with open(src, "rb") as f:
        build_id = hashlib.sha256(f.read() + script.encode()).hexdigest()
    return build_bundle(APP, "com.wr0te.agent", ["/usr/bin/clang", "-O2", "-DSCRIPT=%s" % json.dumps(script), src],
                        build_id, "LSBackgroundOnly")


def build_menu_app(script):
    """Compile menubar.swift into ~/Applications/wr0te-menu.app. It reads only files in LOCAL and
    asks wr0te.app for scans, so it needs no permissions of its own and can be rebuilt freely."""
    src = os.path.join(os.path.dirname(script), "menubar.swift")
    with open(src, "rb") as f:
        build_id = hashlib.sha256(f.read()).hexdigest()
    return build_bundle(MENU_APP, "com.wr0te.menu", ["/usr/bin/swiftc", "-O", src], build_id, "LSUIElement")[0]


def load_agent(label, body):
    """Write ~/Library/LaunchAgents/<label>.plist and (re)start it."""
    plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>%s</string>
%s</dict></plist>
""" % (label, body)
    unload_agent(label)
    write_atomic(plist_path(label), plist.encode())
    subprocess.check_call(["/bin/launchctl", "bootstrap", "gui/%d" % os.getuid(), plist_path(label)])


def unload_agent(label):
    subprocess.call(["/bin/launchctl", "bootout", "gui/%d" % os.getuid(), plist_path(label)],
                    stderr=subprocess.DEVNULL)
    if os.path.exists(plist_path(label)):
        os.remove(plist_path(label))


def cmd_install(folder=None):
    global ROOT, DATA
    if folder:
        folder = os.path.abspath(os.path.expanduser(folder))
        if not os.path.isdir(folder):
            sys.exit("wr0te: %s is not a folder" % folder)
        cfg = load_json(CONFIG, {})  # already checked by read_config()
        cfg["root"] = tilde(folder)
        write_atomic(CONFIG, (json.dumps(cfg, indent=2, ensure_ascii=False) + "\n").encode())
        ROOT, DATA = resolve_paths(read_config())
    if not ROOT:
        sys.exit("usage: wr0te.py install FOLDER   (the folder of writing to watch)")
    script = os.path.abspath(__file__)
    exe, rebuilt = build_app(script)
    env = "".join("<key>%s</key><string>%s</string>" % (k, html.escape(os.environ[k]))
                  for k in ("WR0TE_ROOT", "WR0TE_DATA", "WR0TE_MACHINE") if k in os.environ)
    os.makedirs(LOCAL, exist_ok=True)
    load_agent(LABEL, """  <key>ProgramArguments</key><array><string>{exe}</string></array>
  <key>EnvironmentVariables</key><dict>{env}</dict>
  <key>StartInterval</key><integer>{interval}</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>10</integer>
  <key>StandardOutPath</key><string>{local}/agent.log</string>
  <key>StandardErrorPath</key><string>{local}/agent.log</string>
""".format(exe=html.escape(exe), env=env, interval=SCAN_INTERVAL, local=html.escape(LOCAL)))
    print("installed %s — scanning %s every %d min" % (LABEL, ROOT, SCAN_INTERVAL // 60))
    if MENU_BAR:
        # Quitting from the menu exits cleanly, which keeps it quit until the next login.
        load_agent(MENU_LABEL, """  <key>ProgramArguments</key><array><string>{exe}</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>ProcessType</key><string>Interactive</string>
""".format(exe=html.escape(build_menu_app(script))))
        print("installed %s — today's words in the menu bar" % MENU_LABEL)
    else:
        unload_agent(MENU_LABEL)
    if rebuilt:
        print("Launcher (re)built. Run `open %s` once and click Allow if macOS asks about Dropbox;" % APP)
        print("then check `python3 wr0te.py status`.")


def cmd_uninstall():
    for label in (LABEL, MENU_LABEL):
        unload_agent(label)
        print("removed", label)


def cmd_status():
    print("machine:  ", machine_name())
    print("config:   ", tilde(CONFIG))
    print("root:     ", tilde(ROOT) if ROOT else "(not set: run `wr0te.py install FOLDER`)")
    print("data:     ", tilde(DATA) if DATA else "—")
    r = subprocess.run(["/bin/launchctl", "print", "gui/%d/%s" % (os.getuid(), LABEL)], capture_output=True, text=True)
    if r.returncode == 0:
        st = re.search(r"state = (.+)", r.stdout)
        ex = re.search(r"last exit code = (.+)", r.stdout)
        print("agent:     loaded (%s, last exit %s)" % (st.group(1) if st else "?", ex.group(1) if ex else "?"))
    else:
        print("agent:     not installed")
    try:
        with open(os.path.join(LOCAL, "scan.log")) as f:
            print("recent scans:\n  " + "  ".join(f.readlines()[-5:]))
    except OSError:
        pass


def cmd_auto():
    """What launchd runs: scan, and refresh the local report if anything changed."""
    try:
        n = scan(quiet=True)
        report = os.path.join(LOCAL, "report.html")
        stale = not os.path.exists(report) or writing_day(os.path.getmtime(report)) != writing_day(time.time())
        try:
            os.remove(os.path.join(LOCAL, "report-request"))  # left by the menu bar's Open Report
            requested = True
        except FileNotFoundError:
            requested = False
        if n or stale or requested or newer_logs_than(report):
            check_goal(cmd_report(quiet=True))
    except LogUnavailable as e:  # the report is left as it was and retried next run
        log_line("waiting for log to sync: %s" % e)
    except Exception as e:  # keep the agent alive; the log has the reason
        log_line("error: %r" % (e,))
        raise


def idle_seconds():
    """Seconds since the last keyboard/trackpad input on this Mac."""
    try:
        out = subprocess.check_output(["/usr/sbin/ioreg", "-c", "IOHIDSystem", "-d", "4"], text=True)
        return int(re.search(r'"HIDIdleTime" = (\d+)', out).group(1)) / 1e9
    except (OSError, subprocess.CalledProcessError, AttributeError):
        return 0


def notify(title, message):
    # Text goes in as an argument, never spliced into the AppleScript source.
    subprocess.call(["/usr/bin/osascript", "-e", "on run argv",
                     "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                     "-e", "end run", title, message])


def goal_notification(n):
    goal = "%gk" % (DAILY_GOAL / 1000) if DAILY_GOAL % 100 == 0 else "{:,}".format(DAILY_GOAL)
    notify("Congrats! %s hit!!!" % goal, "{:,} words today.".format(n))


def check_goal(days):
    """Notify once per writing day when the goal is reached, on the Mac being used."""
    if not DAILY_GOAL:
        return
    day = writing_day(time.time())
    n, today = days.get(day, {}).get("added", 0), day.isoformat()
    if n < DAILY_GOAL or goal_notified(today):
        return
    if idle_seconds() > 600:
        return  # the other Mac is the one being written on; it will notify
    goal_notification(n)
    write_atomic(os.path.join(DATA, "logs", machine_name(), "goal.json"),
                 json.dumps({"day": today, "words": n}).encode())
    log_line("goal reached: %d words" % n)


def goal_notified(day):
    """True if any machine already sent this day's notification (the records sync via Dropbox)."""
    logs = os.path.join(DATA, "logs")
    for machine in os.listdir(logs) if os.path.isdir(logs) else []:
        rec = load_json(os.path.join(logs, machine, "goal.json"), {})
        if isinstance(rec, dict) and rec.get("day") == day:
            return True
    return False


def newer_logs_than(path):
    """True if another machine's log changed since the report was built."""
    try:
        t = os.path.getmtime(path)
    except OSError:
        return True
    logs = os.path.join(DATA, "logs")
    for dirpath, _, names in os.walk(logs):
        for n in names:
            if n.endswith(".jsonl") and os.path.getmtime(os.path.join(dirpath, n)) > t:
                return True
    return False


def main(argv):
    os.umask(0o077)  # snapshots and state are private writing: owner-only files
    cmd = argv[1] if len(argv) > 1 else "today"
    if cmd in ("scan", "auto", "today", "report", "open") and not ROOT:
        msg = "no folder to watch yet: run `python3 wr0te.py install FOLDER`"
        if cmd == "auto":
            log_line(msg)
        print("wr0te: " + msg, file=sys.stderr)
        return 1
    if cmd == "scan":
        scan()
    elif cmd == "auto":
        cmd_auto()
    elif cmd == "today":
        cmd_today(int(argv[2]) if len(argv) > 2 else 7)
    elif cmd == "report":
        cmd_report(open_it="--open" in argv)
    elif cmd == "open":
        cmd_report(open_it=True)
    elif cmd == "notify-test":
        goal_notification(DAILY_GOAL + 137)  # preview of what the real one looks like
    elif cmd == "install":
        cmd_install(argv[2] if len(argv) > 2 else None)
    elif cmd == "uninstall":
        cmd_uninstall()
    elif cmd == "status":
        cmd_status()
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

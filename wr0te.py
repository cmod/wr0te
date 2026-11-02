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


def day_tip(d, x):
    if not x or not (x["added"] or x["removed"] or x["moved"] or x["pasted"]):
        return "%s — no writing" % d.strftime("%a %b %-d, %Y")
    parts = ["%s" % d.strftime("%a %b %-d, %Y"),
             "+{:,} / −{:,} words".format(x["added"], x["removed"])]
    if x["cjk"]:
        parts.append("incl. {:,} CJK characters".format(x["cjk"]))
    if x["moved"] or x["pasted"]:
        parts.append("not counted: {:,} moved · {:,} pasted".format(x["moved"], x["pasted"]))
    parts.append("%d file%s · ~%s active" % (len(x["files"]), "" if len(x["files"]) == 1 else "s",
                                              fmt_minutes(len(x["slots"]) * SCAN_INTERVAL // 60)))
    return "\n".join(parts)


def svg_daily(days, today, n=90):
    W, H, L, R, T, B = 960, 260, 48, 8, 12, 24
    pw, ph = W - L - R, H - T - B
    seq = [today - dt.timedelta(days=i) for i in range(n - 1, -1, -1)]
    adds = [days.get(d, {}).get("added", 0) for d in seq]
    rems = [days.get(d, {}).get("removed", 0) for d in seq]
    step = nice_step(max(adds + [1]))
    top = step * max(1, -(-max(adds + [1]) // step))
    bstep = step
    bot = bstep * (-(-max(rems + [0]) // bstep)) if max(rems + [0]) else 0
    bot = min(bot, top)  # deletions are clipped at the same scale; tooltip has the true value
    scale = ph / (top + bot)
    y0 = T + top * scale
    slot = pw / n
    bw = min(24, slot - 2)
    out = ['<svg viewBox="0 0 %d %d" class="chart" role="img" aria-label="Words added and removed per day, last %d days">' % (W, H, n)]
    v = 0
    while v <= top:
        y = y0 - v * scale
        out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="grid"/>' % (L, W - R, y, y))
        out.append('<text x="%d" y="%.1f" class="tick" text-anchor="end">%s</text>' % (L - 6, y + 4, fmt_tick(v)))
        v += step
    if bot:
        y = y0 + bot * scale
        out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="grid"/>' % (L, W - R, y, y))
        out.append('<text x="%d" y="%.1f" class="tick" text-anchor="end">−%s</text>' % (L - 6, y + 4, fmt_tick(bot)))
    for i, d in enumerate(seq):
        x = L + i * slot + (slot - bw) / 2
        a, r = adds[i], min(rems[i], bot)
        if a:
            out.append('<path d="%s" class="s1"/>' % bar_path(x, y0 - 1, bw, a * scale - 1, True))
        if r:
            out.append('<path d="%s" class="s2"/>' % bar_path(x, y0 + 1, bw, r * scale - 1, False))
        if d.day == 1 or i == 0:
            out.append('<text x="%.1f" y="%d" class="tick">%s</text>' % (L + i * slot, H - 6, d.strftime("%b %-d")))
        out.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" class="hit" data-tip="%s"/>'
                   % (L + i * slot, T, slot, ph, esc(day_tip(d, days.get(d)))))
    out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="axis"/>' % (L, W - R, y0, y0))
    out.append("</svg>")
    return "\n".join(out)


def svg_calendar(days, today):
    cell, gap = 12, 2
    start = today - dt.timedelta(days=364)
    start -= dt.timedelta(days=(start.weekday() + 1) % 7)  # back to Sunday
    vals = sorted(x["added"] for d, x in days.items() if d >= start and x["added"] > 0)
    def q(p):
        return vals[min(len(vals) - 1, int(p * len(vals)))] if vals else 0
    cuts = [q(0.2), q(0.4), q(0.6), q(0.8)]
    weeks = ((today - start).days // 7) + 1
    W, H = 30 + weeks * (cell + gap) + 12, 20 + 7 * (cell + gap)  # room for a month label in the last week
    out = ['<svg viewBox="0 0 %d %d" class="chart cal" role="img" aria-label="Calendar of words written over the last year">' % (W, H)]
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
        out.append('<rect x="%d" y="%d" width="%d" height="%d" rx="2" class="h%d" data-tip="%s"/>'
                   % (x, y, cell, cell, lvl, esc(day_tip(d, days.get(d)))))
        d += dt.timedelta(days=1)
    out.append("</svg>")
    legend = ('<div class="scale"><span>less</span>' +
              "".join('<i class="h%d"></i>' % i for i in range(6)) + "<span>more</span></div>")
    return "\n".join(out) + legend


def svg_hbars(rows, label):
    """rows: [(name, value, tip)] single series horizontal bars."""
    if not rows:
        return '<p class="muted">Nothing yet.</p>'
    rh, L, W = 26, 260, 960
    H = rh * len(rows) + 4
    mx = max(v for _, v, _ in rows) or 1
    out = ['<svg viewBox="0 0 %d %d" class="chart" role="img" aria-label="%s">' % (W, H, esc(label))]
    for i, (name, v, tip) in enumerate(rows):
        y = i * rh + 4
        w = (W - L - 80) * v / mx
        short = name if len(name) <= 38 else name[:36] + "…"
        out.append('<text x="%d" y="%d" class="lab" text-anchor="end">%s</text>' % (L - 10, y + 14, esc(short)))
        if w >= 1:
            out.append('<path d="M%d,%d H%.1f Q%.1f,%d %.1f,%d V%d Q%.1f,%d %.1f,%d H%d Z" class="s1"/>'
                       % (L, y + 2, L + w - 4, L + w, y + 2, L + w, y + 6, y + 14, L + w, y + 18, L + w - 4, y + 18, L))
        out.append('<text x="%.1f" y="%d" class="val">%s</text>' % (L + w + 6, y + 14, "{:,}".format(v)))
        out.append('<rect x="0" y="%d" width="%d" height="%d" class="hit" data-tip="%s"/>' % (y - 2, W, rh, esc(tip)))
    out.append("</svg>")
    return "\n".join(out)


def svg_hours(changes, today, n=90):
    W, H, L, R, T, B = 960, 160, 48, 8, 12, 24
    pw, ph = W - L - R, H - T - B
    since = today - dt.timedelta(days=n)
    hours = [0] * 24
    for c in changes:
        if writing_day(c["m"]) > since:
            hours[dt.datetime.fromtimestamp(c["m"]).hour] += written(c)
    step = nice_step(max(hours + [1]), 3)
    top = step * max(1, -(-max(hours + [1]) // step))
    scale = ph / top
    slot = pw / 24
    bw = min(24, slot - 2)
    y0 = T + ph
    out = ['<svg viewBox="0 0 %d %d" class="chart" role="img" aria-label="Words written by hour of day">' % (W, H)]
    v = 0
    while v <= top:
        y = y0 - v * scale
        out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="grid"/>' % (L, W - R, y, y))
        out.append('<text x="%d" y="%.1f" class="tick" text-anchor="end">%s</text>' % (L - 6, y + 4, fmt_tick(v)))
        v += step
    for h in range(24):
        x = L + h * slot + (slot - bw) / 2
        if hours[h]:
            out.append('<path d="%s" class="s1"/>' % bar_path(x, y0, bw, hours[h] * scale, True))
        if h % 3 == 0:
            out.append('<text x="%.1f" y="%d" class="tick" text-anchor="middle">%02d:00</text>' % (L + h * slot + slot / 2, H - 6, h))
        out.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" class="hit" data-tip="%s"/>'
                   % (L + h * slot, T, slot, ph, esc("%02d:00–%02d:59\n+{:,} words (last %d days)".format(hours[h]) % (h, h, n))))
    out.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="axis"/>' % (L, W - R, y0, y0))
    out.append("</svg>")
    return "\n".join(out)


CSS = """
.viz-root{color-scheme:light;--surface:#fcfcfb;--page:#f9f9f7;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--s1:#2a78d6;--s2:#eb6834;
--h0:#efeee9;--h1:#cde2fb;--h2:#9ec5f4;--h3:#5598e7;--h4:#256abf;--h5:#104281}
@media (prefers-color-scheme:dark){.viz-root{color-scheme:dark;--surface:#1a1a19;--page:#0d0d0d;--ink:#f0efec;
--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;
--h0:#262624;--h1:#104281;--h2:#1c5cab;--h3:#2a78d6;--h4:#5598e7;--h5:#9ec5f4}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1000px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:15px;font-weight:600;margin:0 0 4px}
h2{font-size:14px;font-weight:600;margin:0 0 2px}
.sub,.muted{color:var(--ink2);font-size:13px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:18px 20px;margin:16px 0}
.hero{font-size:56px;font-weight:600;line-height:1.1;margin:6px 0 2px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:16px}
.tile .l{color:var(--ink2);font-size:13px}.tile .v{font-size:22px;font-weight:600}
.chart{width:100%;height:auto;display:block;margin-top:12px}
.chart text{font-size:11px}
.tick{fill:var(--muted);font-variant-numeric:tabular-nums}.lab{fill:var(--ink2);font-size:12px!important}
.val{fill:var(--ink);font-size:12px!important;font-variant-numeric:tabular-nums}
.grid{stroke:var(--grid);stroke-width:1}.axis{stroke:var(--axis);stroke-width:1}
.s1{fill:var(--s1)}.s2{fill:var(--s2)}.hit{fill:transparent}.hit:hover{fill:var(--ink);fill-opacity:.04}
.h0{fill:var(--h0)}.h1{fill:var(--h1)}.h2{fill:var(--h2)}.h3{fill:var(--h3)}.h4{fill:var(--h4)}.h5{fill:var(--h5)}
.cal rect:hover{stroke:var(--ink);stroke-width:1}
.scale{display:flex;gap:3px;align-items:center;justify-content:flex-end;color:var(--muted);font-size:11px;margin-top:6px}
.scale i{width:12px;height:12px;border-radius:2px;display:inline-block}
.scale i.h0{background:var(--h0)}.scale i.h1{background:var(--h1)}.scale i.h2{background:var(--h2)}
.scale i.h3{background:var(--h3)}.scale i.h4{background:var(--h4)}.scale i.h5{background:var(--h5)}
.legend{display:flex;gap:16px;color:var(--ink2);font-size:12px;margin-top:8px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px}
th{text-align:left;color:var(--ink2);font-weight:500;border-bottom:1px solid var(--grid);padding:6px 8px}
td{border-bottom:1px solid var(--grid);padding:6px 8px;vertical-align:top}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.p{word-break:break-word}
details summary{cursor:pointer;color:var(--ink2);font-size:13px;margin-top:10px}
#tip{position:fixed;pointer-events:none;background:var(--surface);color:var(--ink);border:1px solid var(--border);
border-radius:8px;padding:8px 10px;font-size:12px;white-space:pre-line;box-shadow:0 4px 16px rgba(0,0,0,.12);display:none;z-index:9;max-width:320px}
#tip b{font-weight:600}
"""

JS = """
const tip=document.getElementById('tip');
document.addEventListener('mousemove',e=>{
  const t=e.target.closest&&e.target.closest('[data-tip]');
  if(!t){tip.style.display='none';return}
  const [h,...rest]=t.dataset.tip.split('\\n');
  tip.innerHTML='<b></b>';tip.firstChild.textContent=h;
  if(rest.length){const s=document.createElement('div');s.textContent=rest.join('\\n');tip.appendChild(s)}
  tip.style.display='block';
  const r=tip.getBoundingClientRect();
  let x=e.clientX+14,y=e.clientY+14;
  if(x+r.width>innerWidth-8)x=e.clientX-r.width-14;
  if(y+r.height>innerHeight-8)y=e.clientY-r.height-14;
  tip.style.left=x+'px';tip.style.top=y+'px';
});
"""


def cmd_report(open_it=False, quiet=False):
    changes = build_changes(load_events())
    days = daily(changes)
    now = time.time()
    today = writing_day(now)

    def total(since_days):
        lo = today - dt.timedelta(days=since_days - 1)
        return sum(x["added"] for d, x in days.items() if d >= lo)

    t = days.get(today, {"added": 0, "removed": 0, "files": set(), "slots": set(), "cjk": 0, "moved": 0, "pasted": 0})
    year_start = dt.date(today.year, 1, 1)
    ytd = sum(x["added"] for d, x in days.items() if d >= year_start)
    active_days_30 = sum(1 for d, x in days.items() if d > today - dt.timedelta(days=30) and x["added"])

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
    prow = [(k, v[0], "%s\n+{:,} / −{:,} words · %d file%s (last 30 days)".format(v[0], v[1])
             % (k, len(v[2]), "" if len(v[2]) == 1 else "s")) for k, v in prow if v[0]]
    frow = sorted(fil.items(), key=lambda kv: -kv[1][0])[:25]

    recent = [c for c in changes if c["kind"] == "copy" or (counted(c) and (written(c) or removed(c) or c["mv"] or c["pa"]))][-60:][::-1]
    n_files = len(load_json(os.path.join(LOCAL, "state.json"), {}).get("files", {}))  # there now, not ever
    machines = sorted({c["by"] for c in changes})

    h = ['<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width,initial-scale=1">',
         "<title>wr0te — writing</title><style>%s</style></head>" % CSS,
         '<body class="viz-root"><main>',
         '<h1>Writing</h1><div class="sub">%s · %s tracked files · machines: %s · updated %s</div>'
         % (esc(tilde(ROOT)), "{:,}".format(n_files), esc(", ".join(machines) or "—"),
            dt.datetime.fromtimestamp(now).strftime("%b %-d, %H:%M")),
         '<section class="card"><div class="muted">Words written today</div>',
         '<div class="hero">%s</div>' % "{:,}".format(t["added"]),
         '<div class="muted">−{:,} removed · {} file{} · ~{} active{}</div>'.format(
             t["removed"], len(t["files"]), "" if len(t["files"]) == 1 else "s",
             fmt_minutes(len(t["slots"]) * SCAN_INTERVAL // 60),
             " · not counted: {:,} moved, {:,} pasted".format(t["moved"], t["pasted"]) if t["moved"] or t["pasted"] else ""),
         '<div class="tiles">']
    for lab, val in (("Last 7 days", "{:,}".format(total(7))), ("Last 30 days", "{:,}".format(total(30))),
                     ("This year", "{:,}".format(ytd)), ("Current streak", "%d day%s" % (streak(days, today), "" if streak(days, today) == 1 else "s")),
                     ("Writing days, last 30", "%d" % active_days_30)):
        h.append('<div class="tile"><div class="l">%s</div><div class="v">%s</div></div>' % (lab, val))
    h.append("</div></section>")

    h.append('<section class="card"><h2>Daily words, last 90 days</h2>'
             '<div class="sub">Added above the line, removed below. Text moved from elsewhere in your writing, pasted blocks and copied files aren\'t counted.</div>')
    h.append('<div class="legend"><span><i style="background:var(--s1)"></i>Added</span>'
             '<span><i style="background:var(--s2)"></i>Removed</span></div>')
    h.append(svg_daily(days, today))
    h.append("<details><summary>Show as table</summary><table><tr><th>Day</th><th class=n>Added</th>"
             "<th class=n>Removed</th><th class=n>Files</th><th class=n>~Active</th></tr>")
    for i in range(90):
        d = today - dt.timedelta(days=i)
        x = days.get(d)
        if x and (x["added"] or x["removed"]):
            h.append("<tr><td>%s</td><td class=n>{:,}</td><td class=n>{:,}</td><td class=n>%d</td><td class=n>%s</td></tr>".format(
                x["added"], x["removed"]) % (d.strftime("%a %b %-d"), len(x["files"]), fmt_minutes(len(x["slots"]) * SCAN_INTERVAL // 60)))
    h.append("</table></details></section>")

    h.append('<section class="card"><h2>The last year</h2><div class="sub">Words added per day</div>')
    h.append(svg_calendar(days, today) + "</section>")

    h.append('<section class="card"><h2>Projects, last 30 days</h2><div class="sub">Words added, by folder</div>')
    h.append(svg_hbars(prow, "Words added per project, last 30 days") + "</section>")

    h.append('<section class="card"><h2>When you write</h2><div class="sub">Words added by hour of day, last 90 days</div>')
    h.append(svg_hours(changes, today) + "</section>")

    h.append('<section class="card"><h2>Files, last 30 days</h2><table><tr><th>File</th><th class=n>Added</th>'
             '<th class=n>Removed</th><th class=n>Length</th><th class=n>Last touched</th></tr>')
    for p, (a, r, m, length) in frow:
        h.append("<tr><td class=p>%s</td><td class=n>{:,}</td><td class=n>{:,}</td><td class=n>{:,}</td><td class=n>%s</td></tr>"
                 .format(a, r, length) % (esc(shown(p)), dt.datetime.fromtimestamp(m).strftime("%b %-d %H:%M")))
    h.append("</table></section>")

    h.append('<section class="card"><h2>Recent changes</h2><table><tr><th>When</th><th>File</th><th class=n>+</th>'
             '<th class=n>−</th><th>Note</th></tr>')
    def note(c):
        if c["kind"] == "copy":
            return "file moved or copied"
        bits = ["new file"] if c["kind"] == "new" else []
        if c["mv"]:
            bits.append("{:,} moved".format(c["mv"]))
        if c["pa"]:
            bits.append("{:,} pasted".format(c["pa"]))
        return " · ".join(bits)
    for c in recent:
        h.append("<tr><td class=n style='text-align:left'>%s</td><td class=p>%s</td><td class=n>{:,}</td><td class=n>{:,}</td><td class=muted>%s</td></tr>"
                 .format(written(c), removed(c))
                 % (dt.datetime.fromtimestamp(c["m"]).strftime("%b %-d %H:%M"), esc(shown(c["path"])), esc(note(c))))
    h.append("</table></section>")
    h.append('<p class="muted">Counts are words; Japanese/Chinese/Korean characters count one each. '
             'Active time is approximate: the number of %d-minute windows with a saved change. '
             'Day boundary is %d:00.</p>' % (SCAN_INTERVAL // 60, DAY_START_HOUR))
    h.append('<div id="tip"></div><script>%s</script></main></body></html>' % JS)

    out = os.path.join(LOCAL, "report.html")
    write_atomic(out, "\n".join(h).encode())
    with open(os.path.join(LOCAL, "daily.csv"), "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["day", "written", "removed", "cjk_written", "moved", "pasted", "files", "active_minutes"])
        for d in sorted(days):
            x = days[d]
            wr.writerow([d.isoformat(), x["added"], x["removed"], x["cjk"], x["moved"], x["pasted"], len(x["files"]),
                         len(x["slots"]) * SCAN_INTERVAL // 60])
    write_atomic(os.path.join(LOCAL, "today.json"), json.dumps({  # what the menu bar shows
        "day": today.isoformat(), "day_start_hour": DAY_START_HOUR, "written": t["added"],
        "removed": t["removed"], "files": len(t["files"]), "active_minutes": len(t["slots"]) * SCAN_INTERVAL // 60,
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

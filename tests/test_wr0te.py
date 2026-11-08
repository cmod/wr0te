"""Tests for wr0te. Run from the repo: python3 -m unittest

Everything happens in temporary folders; nothing touches your real settings, data or launchd.
"""

import gzip
import hashlib
import json
import os
import random
import shutil
import sys
import subprocess
import tempfile
import time
import unittest
from collections import defaultdict

# Settings load when wr0te is imported, so point it at an empty place first.
os.environ["WR0TE_LOCAL"] = tempfile.mkdtemp(prefix="wr0te-test-")
for _k in ("WR0TE_ROOT", "WR0TE_DATA", "WR0TE_MACHINE"):
    os.environ.pop(_k, None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wr0te as w  # noqa: E402

COMMON = "the a of and to in is it that was for on with as at by this but from".split()


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wr0te-test-")
        self.saved = {k: getattr(w, k) for k in ("LOCAL", "CONFIG", "FOLDERS", "DATA", "HIDE_FOLDERS", "SHOW_STREAK", "DONT_COUNT")}
        w.LOCAL = os.path.join(self.tmp, "local")
        w.CONFIG = os.path.join(w.LOCAL, "config.json")
        self.root = os.path.join(self.tmp, "writing")
        w.FOLDERS = [("", self.root)]
        w.DATA = os.path.join(self.tmp, "wr0te-data")
        os.makedirs(self.root)
        os.environ["WR0TE_MACHINE"] = "test"
        self.rng = random.Random(1)
        self.vocab = ["w%d" % i for i in range(5000)]

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(w, k, v)
        os.environ.pop("WR0TE_MACHINE", None)
        shutil.rmtree(self.tmp)

    def prose(self, n):
        """n words of made-up text, with real-looking common words mixed in."""
        return " ".join(self.rng.choice(COMMON) if self.rng.random() < .45 else self.rng.choice(self.vocab)
                        for _ in range(n)) + "."

    def version(self, text, m):
        data = text.encode()
        h = hashlib.sha1(data).hexdigest()
        os.makedirs(os.path.dirname(w.snapshot_path(h)), exist_ok=True)
        with open(w.snapshot_path(h), "wb") as f:
            f.write(gzip.compress(data))
        words, cjk = w.split_counts(w.tokens(text))
        return {"e": "v", "h": h, "m": m, "w": words, "k": cjk, "ps": m - 300}

    def classify(self, old, new, m=10000.0, kind="edit"):
        """Classify one change made within a single 5-minute scan."""
        prev = self.version(old, m - 300) if old is not None else None
        ev = self.version(new, m)
        archive = w.Archive()
        try:
            return w.classify(archive, prev, ev, [ev], kind)
        finally:
            archive.close()


class Counting(Case):
    def test_tokens(self):
        toks = w.tokens("Hello, world! ## **  日本語")
        self.assertEqual(toks, ["Hello,", "world!", "日", "本", "語"])
        self.assertEqual(w.split_counts(toks), (2, 3))

    def test_typed_text_is_written(self):
        paras = [self.prose(80) for _ in range(5)]
        new = list(paras)
        new[2] += " " + self.prose(300)
        r = self.classify("\n\n".join(paras), "\n\n".join(new))
        self.assertEqual((r["aw"], r["pa"], r["mv"]), (300, 0, 0))

    def test_scattered_edits_are_written(self):
        paras = [self.prose(80) for _ in range(5)]
        toks = paras[3].split()
        for i in range(0, len(toks), 4):
            toks[i] = "zz%d" % i
        new = list(paras)
        new[3] = " ".join(toks)
        r = self.classify("\n\n".join(paras), "\n\n".join(new))
        self.assertEqual((r["aw"], r["pa"]), (20, 0))
        self.assertEqual(r["rw"], 20)

    def test_one_big_paste(self):
        old = "\n\n".join(self.prose(80) for _ in range(5))
        r = self.classify(old, old + "\n\n" + self.prose(1000))
        self.assertEqual((r["aw"], r["pa"]), (0, 1000))

    def test_small_pastes_add_up(self):
        paras = [self.prose(80) for _ in range(6)]
        new = list(paras)
        for at in (1, 3, 5):
            new.insert(at, self.prose(400))
        r = self.classify("\n\n".join(paras), "\n\n".join(new))
        self.assertLessEqual(r["aw"], 450)  # no more than could be typed in 5 minutes
        self.assertEqual(r["aw"] + r["pa"], 3 * 400)

    def test_paste_over_a_paragraph_stays_whole(self):
        paras = [self.prose(80) for _ in range(5)]
        new = list(paras)
        new[2] = self.prose(1000)  # shares common words with the old paragraph
        r = self.classify("\n\n".join(paras), "\n\n".join(new))
        self.assertEqual(r["aw"], 0)
        self.assertGreater(r["pa"], 900)

    def test_text_from_another_file_is_moved(self):
        passage = self.prose(200)
        self.classify(None, passage, m=5000.0, kind="new")  # written somewhere else first
        paras = [self.prose(80) for _ in range(3)]
        r = self.classify("\n\n".join(paras), "\n\n".join(paras + [passage]))
        self.assertEqual(r["aw"], 0)
        self.assertGreater(r["mv"], 190)


class Scanning(Case):
    def touch(self, rel, ahead=1):
        t = time.time() + ahead  # saved after the last scan
        os.utime(os.path.join(self.root, rel), (t, t))

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        self.touch(rel)

    def test_finds_text_files_in_subfolders_only(self):
        for rel in ("a.md", "deep/er/b.org", "c.txt", "d.pdf", "deep/.#b.org", "deep/#b.org#",
                    "deep/b.org_archive", ".hidden/e.md", "Draft.scriv/f.txt"):
            self.write(rel, "x")
        found = sorted(os.path.relpath(p, self.root) for p in w.walk_text_files(self.root))
        self.assertEqual(found, ["a.md", "c.txt", "deep/er/b.org"])

    def test_scan_counts_new_writing_not_copies(self):
        self.write("Novel/ch1.md", "# Chapter one\n")
        w.scan(quiet=True)  # baseline: counts nothing
        self.write("Novel/ch1.md", "# Chapter one\n\n" + self.prose(59) + "\n")
        w.scan(quiet=True)
        shutil.copy(os.path.join(self.root, "Novel/ch1.md"), os.path.join(self.root, "Novel/ch1 copy.md"))
        self.touch("Novel/ch1 copy.md", ahead=2)
        w.scan(quiet=True)
        changes = w.build_changes(w.load_events())
        self.assertEqual(sorted(c["kind"] for c in changes), ["baseline", "copy", "edit"])
        today = w.writing_day(time.time())
        self.assertEqual(w.daily(changes)[today]["added"], 59)

        w.cmd_report(quiet=True)
        with open(os.path.join(w.LOCAL, "today.json")) as f:
            self.assertEqual(json.load(f)["written"], 59)

    def test_also_watch_folders(self):
        extra = os.path.join(self.tmp, "letters")
        os.makedirs(os.path.join(extra, "spring"))
        with open(os.path.join(extra, "spring", "001.md"), "w") as f:
            f.write(self.prose(40))
        self.write("Novel/ch1.md", "# Chapter one\n")
        w.scan(quiet=True)
        w.FOLDERS = [("", self.root), ("zine", extra)]  # added later: what's there is a baseline
        w.scan(quiet=True)
        with open(os.path.join(extra, "spring", "002.md"), "w") as f:
            f.write(self.prose(30))
        w.scan(quiet=True)
        changes = w.build_changes(w.load_events())
        kinds = {c["path"]: c["kind"] for c in changes}
        self.assertEqual(kinds["zine/spring/001.md"], "baseline")
        self.assertEqual(w.daily(changes)[w.writing_day(time.time())]["added"], 30)

        shutil.rmtree(extra)  # not on this Mac: skipped, not deleted
        w.scan(quiet=True)
        self.assertFalse([e for e in w.load_events() if e["e"] == "d"])
        w.FOLDERS = [("", self.root)]  # dropped from settings: forgotten, not deleted
        w.scan(quiet=True)
        self.assertFalse([e for e in w.load_events() if e["e"] == "d"])
        self.assertEqual(list(w.load_json(os.path.join(w.LOCAL, "state.json"), {})["files"]), ["Novel/ch1.md"])

    def test_copying_a_slowly_revised_file_is_moved(self):
        doc = self.prose(400).split()
        self.write("Book/aside.md", " ".join(doc))
        w.scan(quiet=True)
        for i in range(0, 400, 6):  # revised a word at a time, scan after scan
            doc[i] = "rev%d" % i
            self.write("Book/aside.md", " ".join(doc))
            w.scan(quiet=True)
        self.write("Book/main.md", "# Main\n")
        w.scan(quiet=True)
        self.write("Book/main.md", "# Main\n\n" + " ".join(doc) + "\n")
        w.scan(quiet=True)
        last = [c for c in w.build_changes(w.load_events()) if c["path"] == "Book/main.md"][-1]
        self.assertGreater(last["mv"], 380)
        self.assertLess(last["aw"] + last["pa"], 20)

    def test_unreadable_snapshot_counts_nothing_until_it_arrives(self):
        self.write("a.md", "# A\n")
        w.scan(quiet=True)
        self.write("a.md", "# A\n\n" + self.prose(900))
        w.scan(quiet=True)
        h = w.load_events()[-1]["h"]
        os.rename(w.snapshot_path(h), w.snapshot_path(h) + ".away")
        w.cmd_report(quiet=True)
        with open(os.path.join(w.LOCAL, "today.json")) as f:
            self.assertEqual(json.load(f)["written"], 0)
        self.assertTrue(os.path.exists(os.path.join(w.LOCAL, "report-pending")))
        os.rename(w.snapshot_path(h) + ".away", w.snapshot_path(h))
        w.cmd_report(quiet=True)
        self.assertFalse(os.path.exists(os.path.join(w.LOCAL, "report-pending")))

    def test_newly_watched_file_type_starts_as_baseline(self):
        old = self.prose(120)
        self.write("notes/old.org", old)  # there before wr0te watched .org files
        self.write("ch1.md", "# One\n")
        saved = w.TEXT_EXT
        w.TEXT_EXT = w.FIRST_TEXT_EXT
        try:
            w.scan(quiet=True)
        finally:
            w.TEXT_EXT = saved
        state_file = os.path.join(w.LOCAL, "state.json")
        state = w.load_json(state_file, None)
        del state["ext"]  # as written by a version that didn't record it
        w.write_atomic(state_file, json.dumps(state).encode())
        w.scan(quiet=True)
        self.write("notes/old.org", old + "\n" + self.prose(25))
        w.scan(quiet=True)
        changes = w.build_changes(w.load_events())
        self.assertEqual([c["kind"] for c in changes if c["path"] == "notes/old.org"], ["baseline", "edit"])
        self.assertEqual(w.daily(changes)[w.writing_day(time.time())]["added"], 25)

    def test_streak_can_be_hidden(self):
        self.write("a.md", "# A\n")
        w.scan(quiet=True)
        for show in (True, False):
            w.SHOW_STREAK = show
            w.cmd_report(quiet=True)
            with open(os.path.join(w.LOCAL, "report.html")) as f:
                page = f.read()
            self.assertEqual("longest" in page, show)
            self.assertEqual("Days written" in page, show)

    def test_old_state_carries_over(self):
        extra = os.path.join(self.tmp, "zine")
        os.makedirs(extra)
        with open(os.path.join(extra, "1.md"), "w") as f:
            f.write(self.prose(40))
        self.write("a.md", self.prose(40))
        w.FOLDERS = [("", self.root), ("zine", extra)]
        w.scan(quiet=True)
        state_file = os.path.join(w.LOCAL, "state.json")
        state = w.load_json(state_file, None)
        del state["folders"]  # as older versions kept it
        state.update(root=self.root, also=["zine"])
        w.write_atomic(state_file, json.dumps(state).encode())
        self.assertEqual(w.scan(quiet=True), 0)  # nothing re-baselined, nothing deleted
        self.assertEqual(w.load_json(state_file, None)["folders"], {"": self.root, "zine/": extra})

    def test_moving_a_folder_starts_it_over(self):
        self.write("a.md", self.prose(40))
        w.scan(quiet=True)
        moved = os.path.join(self.tmp, "elsewhere")
        shutil.copytree(self.root, moved)
        w.FOLDERS = [("", moved)]
        w.scan(quiet=True)
        evs = w.load_events()
        self.assertFalse([e for e in evs if e["e"] == "d"])
        self.assertEqual([e.get("base") for e in evs], [1, 1])

    def test_dont_count_folders(self):
        w.DONT_COUNT = ["*backup*", "Book/Misc"]
        self.write("Book/ch1.md", "# One\n")
        self.write("Book/Misc/notes.md", "# Notes\n")
        w.scan(quiet=True)
        notes = self.prose(60)
        self.write("Book/Misc/notes.md", "# Notes\n\n" + notes + "\n")
        w.scan(quiet=True)
        self.write("Book/ch1.md", "# One\n\n" + notes + "\n")  # the note, moved into the book
        self.touch("Book/ch1.md", ahead=2)  # saved after the note, not in the same instant
        w.scan(quiet=True)
        changes = w.build_changes(w.load_events())
        day = w.daily(changes)[w.writing_day(time.time())]
        self.assertLess(day["added"], 5)  # a word or two at the edges of a move can slip through
        self.assertEqual(day["notes"], 60)
        self.assertGreater([c for c in changes if c["path"] == "Book/ch1.md"][-1]["mv"], 50)

    def test_dont_count_matching(self):
        w.DONT_COUNT = ["*backup*", "misc", "Essays/Drafts/old"]
        for path, expect in (("Book/Backup — Pre 3rd Person/a.md", True), ("Backups, misc/a.md", True),
                             ("Book/MISC/a.md", True), ("Book/miscellany/a.md", False), ("misc.md", False),
                             ("Essays/Drafts/old/a.md", True), ("Essays/Drafts/old/x/a.md", True),
                             ("Essays/old/a.md", False), ("Book/ch1.md", False)):
            self.assertEqual(w.dont_count(path), expect, path)

    def test_auto_leaves_a_watch_list_for_the_launcher(self):
        self.write("a.md", "# A\n")
        w.cmd_auto()
        with open(os.path.join(w.LOCAL, "watch")) as f:
            rules = [line.split(" ", 1) for line in f.read().splitlines()]
        got = defaultdict(list)
        for k, v in rules:
            got[k].append(v)
        self.assertEqual(got["folder"], [os.path.realpath(self.root)])
        self.assertIn(".md", got["ext"])
        self.assertEqual(got["any"], [os.path.realpath(os.path.join(w.DATA, "logs"))])
        self.assertEqual(got["ignore"], [os.path.join(got["any"][0], "test")])
        due = int(got["due"][0])
        self.assertTrue(time.time() < due <= time.time() + 3 * 3600 + 1)

    def test_mass_disappearance_waits_an_hour(self):
        for i in range(30):
            self.write("f%d.md" % i, "file %d\n" % i)
        w.scan(quiet=True)
        for i in range(30):
            os.remove(os.path.join(self.root, "f%d.md" % i))
        w.scan(quiet=True)
        self.assertFalse([e for e in w.load_events() if e["e"] == "d"])  # maybe just unmounted

        state_file = os.path.join(w.LOCAL, "state.json")
        state = w.load_json(state_file, None)
        state["missing_since"] -= 3601
        w.write_atomic(state_file, json.dumps(state).encode())
        w.scan(quiet=True)
        self.assertEqual(len([e for e in w.load_events() if e["e"] == "d"]), 30)

    def test_open_report_request_rebuilds(self):
        self.write("a.md", "hello\n")
        w.scan(quiet=True)
        w.cmd_report(quiet=True)
        report = os.path.join(w.LOCAL, "report.html")
        later = time.time() + 60
        os.utime(report, (later, later))  # current, so only the request can trigger a rebuild
        os.remove(os.path.join(w.LOCAL, "today.json"))
        open(os.path.join(w.LOCAL, "report-request"), "w").close()
        w.cmd_auto()
        self.assertTrue(os.path.exists(os.path.join(w.LOCAL, "today.json")))
        self.assertFalse(os.path.exists(os.path.join(w.LOCAL, "report-request")))


class Goal(Case):
    def test_one_notification_across_macs(self):
        sent = []
        saved = w.notify, w.idle_seconds
        w.notify = lambda title, message: sent.append(os.environ["WR0TE_MACHINE"])
        w.idle_seconds = lambda: 0
        try:
            days = {w.writing_day(time.time()): {"added": w.DAILY_GOAL + 1}}
            for machine in ("mac-a", "mac-b", "mac-a"):
                os.environ["WR0TE_MACHINE"] = machine
                w.check_goal(days)
        finally:
            w.notify, w.idle_seconds = saved
        self.assertEqual(sent, ["mac-a"])

    def test_notification_goes_through_the_menu_bar_app(self):
        calls = []
        saved = w.menu_bar_running, w.subprocess.call, w.MENU_BAR
        w.subprocess.call = lambda args, **kw: calls.append(args[0])
        try:
            w.MENU_BAR, w.menu_bar_running = True, lambda: True
            w.notify("Goal", "2,019 words today.")
            with open(os.path.join(w.LOCAL, "notify.json")) as f:
                self.assertEqual(json.load(f)["body"], "2,019 words today.")
            self.assertEqual(calls, [])

            w.menu_bar_running = lambda: False  # quit, or turned off: fall back to osascript
            w.notify("Goal", "2,019 words today.")
            self.assertEqual(calls, ["/usr/bin/osascript"])
        finally:
            w.menu_bar_running, w.subprocess.call, w.MENU_BAR = saved


@unittest.skipUnless(sys.platform == "darwin" and os.path.exists("/usr/bin/clang"), "macOS launcher")
class Launcher(unittest.TestCase):
    """The real launcher.c, with a stand-in for wr0te.py that only notes each time it's started."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wr0te-launcher-"))
        self.local = os.path.join(self.tmp, "Library", "Application Support", "wr0te")
        self.writing = os.path.join(self.tmp, "writing")
        os.makedirs(self.local)
        os.makedirs(self.writing)
        stub = os.path.join(self.tmp, "stub.py")
        with open(stub, "w") as f:
            f.write("import os\nopen(os.path.join(os.environ['HOME'], 'runs'), 'a').write('run\\n')\n")
        src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "launcher.c")
        self.exe = os.path.join(self.tmp, "launcher")
        subprocess.check_call(["/usr/bin/clang", "-O2", "-DSCRIPT=%s" % json.dumps(stub),
                               "-framework", "CoreServices", src, "-o", self.exe])
        with open(os.path.join(self.local, "watch"), "w") as f:
            f.write("due 9999999999\next .md\nfolder %s\n" % self.writing)
        self.doc = os.path.join(self.writing, "a.md")
        with open(self.doc, "w") as f:
            f.write("# A\n")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def runs(self):
        try:
            with open(os.path.join(self.tmp, "runs")) as f:
                return len(f.readlines())
        except OSError:
            return 0

    def started_python(self, patience=5.0):
        """Run the launcher (again, while the change reaches the journal) until it starts wr0te.py."""
        before, end = self.runs(), time.time() + patience
        while True:
            subprocess.check_call([self.exe], env={"HOME": self.tmp})
            if self.runs() > before or time.time() > end:
                return self.runs() > before
            time.sleep(0.25)

    def settle(self):
        """Run until a run finds nothing: a change can reach the journal after the run that
        scanned it, which costs one extra run (never a missed one). Slow machines show it most."""
        end = time.time() + 10
        while self.started_python(patience=0) and time.time() < end:
            time.sleep(0.5)

    def test_runs_only_when_text_changed(self):
        self.assertTrue(self.started_python())                  # first run: no saved position
        self.settle()
        self.assertFalse(self.started_python(patience=0))       # nothing changed
        with open(self.doc, "a") as f:
            f.write(" ")                                         # one space, in place
        self.assertTrue(self.started_python())
        self.settle()
        tmp = self.doc + ".tmp"                                  # one space, saved atomically
        with open(self.doc) as f, open(tmp, "w") as g:
            g.write(f.read() + " ")
        os.replace(tmp, self.doc)
        self.assertTrue(self.started_python())
        self.settle()
        with open(os.path.join(self.writing, "photo.png"), "w") as f:
            f.write("x")                                         # not text
        time.sleep(2)
        self.assertFalse(self.started_python(patience=0))
        open(os.path.join(self.local, "report-request"), "w").close()  # the menu bar asked
        self.assertTrue(self.started_python(patience=0))


class Settings(Case):
    def write_config(self, cfg):
        os.makedirs(w.LOCAL, exist_ok=True)
        with open(w.CONFIG, "w") as f:
            f.write(cfg if isinstance(cfg, str) else json.dumps(cfg))

    def test_defaults_without_config(self):
        cfg = w.read_config()
        self.assertIsNone(cfg["root"])
        self.assertEqual(w.resolve_paths(cfg), ([], None))

    def test_data_defaults_next_to_root(self):
        self.write_config({"root": "~/Writing/", "daily_goal": 500})
        cfg = w.read_config()
        home = os.path.expanduser("~")
        self.assertEqual(w.resolve_paths(cfg), ([("", home + "/Writing")], home + "/wr0te-data"))
        self.assertEqual(cfg["daily_goal"], 500)

    def test_bad_config_stops(self):
        for bad in ('{"root": 5}', '{"roots": "x"}', '{"hide_folders": [1]}', '{"also_watch": {"a/b": "x"}}', '{"also_watch": ["x"]}', "{nope", "[]"):
            self.write_config(bad)
            with self.assertRaises(SystemExit, msg=bad):
                w.read_config()

    def test_display_folders(self):
        w.HIDE_FOLDERS = ["Projects"]
        self.assertEqual(w.shown("Projects/Novel/ch1.md"), "Novel/ch1.md")
        self.assertEqual(w.project_of("Projects/Novels/Harbor/ch1.md"), "Novels/Harbor")
        self.assertEqual(w.project_of("Journals/2026/jan.md"), "Journals/2026")
        self.assertEqual(w.project_of("notes.md"), "(top level)")
        labels = w.project_labels(["Novels/Harbor", "Essays/Drafts", "Novel/Drafts", "Journals/2026"])
        self.assertEqual(labels, {"Novels/Harbor": "Harbor", "Essays/Drafts": "Essays/Drafts",
                                  "Novel/Drafts": "Novel/Drafts", "Journals/2026": "2026"})

    def test_folder_list(self):
        self.write_config({"folders": [{"path": "~/Writing"}, {"name": "zine", "path": "~/Zine"}]})
        home = os.path.expanduser("~")
        self.assertEqual(w.resolve_paths(w.read_config()),
                         ([("", home + "/Writing"), ("zine", home + "/Zine")], home + "/wr0te-data"))
        for bad in ([{"path": "~/a"}, {"path": "~/b"}], [{"name": "x", "path": "~/a"}, {"name": "x", "path": "~/b"}],
                    [{"name": ".x", "path": "~/a"}], [{"path": "~/a", "extra": 1}], ["~/a"], [{"name": "x"}]):
            self.write_config({"folders": bad})
            with self.assertRaises(SystemExit, msg=bad):
                w.read_config()

    def test_retired_setting_is_ignored(self):
        self.write_config({"root": "~/Writing", "split_folders": ["Journals"]})
        self.assertEqual(w.read_config()["root"], "~/Writing")


class SyncedData(Case):
    def test_rejects_malformed_events(self):
        good = {"e": "v", "p": "a.md", "h": "0" * 40, "m": 1.0, "s": 1, "w": 3, "k": 0}
        self.assertTrue(w.valid_event(good))
        for change in ({"h": "../../etc"}, {"w": -1}, {"m": float("nan")}, {"e": "x"}, {"p": ""}, {"k": True}):
            self.assertFalse(w.valid_event(dict(good, **change)), change)


if __name__ == "__main__":
    unittest.main()

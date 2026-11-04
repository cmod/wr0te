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
import tempfile
import time
import unittest

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
        self.saved = {k: getattr(w, k) for k in ("LOCAL", "CONFIG", "ROOT", "DATA", "HIDE_FOLDERS")}
        w.LOCAL = os.path.join(self.tmp, "local")
        w.CONFIG = os.path.join(w.LOCAL, "config.json")
        w.ROOT = os.path.join(self.tmp, "writing")
        w.DATA = os.path.join(self.tmp, "wr0te-data")
        os.makedirs(w.ROOT)
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
        os.utime(os.path.join(w.ROOT, rel), (t, t))

    def write(self, rel, text):
        path = os.path.join(w.ROOT, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        self.touch(rel)

    def test_scan_counts_new_writing_not_copies(self):
        self.write("Novel/ch1.md", "# Chapter one\n")
        w.scan(quiet=True)  # baseline: counts nothing
        self.write("Novel/ch1.md", "# Chapter one\n\n" + self.prose(59) + "\n")
        w.scan(quiet=True)
        shutil.copy(os.path.join(w.ROOT, "Novel/ch1.md"), os.path.join(w.ROOT, "Novel/ch1 copy.md"))
        self.touch("Novel/ch1 copy.md", ahead=2)
        w.scan(quiet=True)
        changes = w.build_changes(w.load_events())
        self.assertEqual(sorted(c["kind"] for c in changes), ["baseline", "copy", "edit"])
        today = w.writing_day(time.time())
        self.assertEqual(w.daily(changes)[today]["added"], 59)

        w.cmd_report(quiet=True)
        with open(os.path.join(w.LOCAL, "today.json")) as f:
            self.assertEqual(json.load(f)["written"], 59)

    def test_mass_disappearance_waits_an_hour(self):
        for i in range(30):
            self.write("f%d.md" % i, "file %d\n" % i)
        w.scan(quiet=True)
        for i in range(30):
            os.remove(os.path.join(w.ROOT, "f%d.md" % i))
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


class Settings(Case):
    def write_config(self, cfg):
        os.makedirs(w.LOCAL, exist_ok=True)
        with open(w.CONFIG, "w") as f:
            f.write(cfg if isinstance(cfg, str) else json.dumps(cfg))

    def test_defaults_without_config(self):
        cfg = w.read_config()
        self.assertIsNone(cfg["root"])
        self.assertEqual(w.resolve_paths(cfg), (None, None))

    def test_data_defaults_next_to_root(self):
        self.write_config({"root": "~/Writing/", "daily_goal": 500})
        cfg = w.read_config()
        home = os.path.expanduser("~")
        self.assertEqual(w.resolve_paths(cfg), (home + "/Writing", home + "/wr0te-data"))
        self.assertEqual(cfg["daily_goal"], 500)

    def test_bad_config_stops(self):
        for bad in ('{"root": 5}', '{"roots": "x"}', '{"hide_folders": [1]}', "{nope", "[]"):
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

// Tiny launcher so the macOS privacy permission (access to Dropbox) can be granted to
// "wr0te.app" alone rather than to every Python script. `wr0te.py install` compiles this
// with SCRIPT set to the path of wr0te.py.
//
// Whatever this spawns inherits the app's permission, so it is deliberately rigid:
// fixed interpreter, isolated mode (-I: no PYTHON* env vars, no user site-packages, no
// script-directory imports), a minimal environment, and no arguments from the caller.
//
// It also decides whether there's anything to do. Each run of wr0te.py leaves a "watch" file
// listing the folders to watch; the launcher asks FSEvents (the kernel's journal of file
// changes) whether any text file in them changed since the last run, and if nothing did, quits
// without starting Python. That makes most runs cost a few milliseconds. Anything unexpected
// (no watch file, journal trouble, a timeout) means: run wr0te.py, as if this check didn't exist.
#include <CoreServices/CoreServices.h>
#include <dispatch/dispatch.h>
#include <fcntl.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define CLT_PYTHON "/Library/Developer/CommandLineTools/usr/bin/python3"
#define MAX_RULES 64
#define MAX_PATH_LEN 1024

typedef struct {
    char folder[MAX_RULES][MAX_PATH_LEN]; int nfolder;  // text files here count, hidden ones don't
    char any[MAX_RULES][MAX_PATH_LEN]; int nany;        // anything here counts (other Macs' logs)
    char ignore[MAX_RULES][MAX_PATH_LEN]; int nignore;  // nothing here counts (this Mac's own log)
    char ext[MAX_RULES][16]; int next;
    long long due;                                      // run regardless from this time on
    int changed;
    dispatch_semaphore_t done;
} Watch;

static char local[MAX_PATH_LEN];

static void local_path(char *out, const char *name) {
    snprintf(out, MAX_PATH_LEN, "%s/%s", local, name);
}

static int under(const char *path, const char *root) {
    size_t n = strlen(root);
    return strncmp(path, root, n) == 0 && (path[n] == '/' || path[n] == '\0');
}

static int counts(Watch *w, const char *path, FSEventStreamEventFlags flags) {
    for (int i = 0; i < w->nignore; i++)
        if (under(path, w->ignore[i])) return 0;
    for (int i = 0; i < w->nany; i++)
        if (under(path, w->any[i])) return 1;
    for (int i = 0; i < w->nfolder; i++) {
        if (!under(path, w->folder[i])) continue;
        if (strstr(path + strlen(w->folder[i]), "/.")) return 0;  // hidden, like the scan
        if (flags & kFSEventStreamEventFlagItemIsDir) return 1;   // a folder moved or removed
        const char *dot = strrchr(path, '.');
        if (!dot || strchr(dot, '/')) return 0;
        for (int k = 0; k < w->next; k++)
            if (strcasecmp(dot, w->ext[k]) == 0) return 1;
        return 0;
    }
    return 0;
}

static void on_events(ConstFSEventStreamRef s, void *info, size_t n, void *paths,
                      const FSEventStreamEventFlags flags[], const FSEventStreamEventId ids[]) {
    Watch *w = info;
    char **p = paths;
    const FSEventStreamEventFlags lost = kFSEventStreamEventFlagMustScanSubDirs |
        kFSEventStreamEventFlagUserDropped | kFSEventStreamEventFlagKernelDropped |
        kFSEventStreamEventFlagEventIdsWrapped | kFSEventStreamEventFlagRootChanged |
        kFSEventStreamEventFlagMount | kFSEventStreamEventFlagUnmount;
    for (size_t i = 0; i < n; i++) {
        if (flags[i] & kFSEventStreamEventFlagHistoryDone) {
            dispatch_semaphore_signal(w->done);
        } else if ((flags[i] & lost) || counts(w, p[i], flags[i])) {
            w->changed = 1;
        }
    }
}

static int read_watch(Watch *w) {
    char path[MAX_PATH_LEN], line[MAX_PATH_LEN + 16];
    local_path(path, "watch");
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    w->due = 0;
    while (fgets(line, sizeof line, f)) {
        line[strcspn(line, "\n")] = '\0';
        char *v = strchr(line, ' ');
        if (!v) continue;
        *v++ = '\0';
        if (!strcmp(line, "due")) w->due = atoll(v);
        else if (!strcmp(line, "folder") && w->nfolder < MAX_RULES) strlcpy(w->folder[w->nfolder++], v, MAX_PATH_LEN);
        else if (!strcmp(line, "any") && w->nany < MAX_RULES) strlcpy(w->any[w->nany++], v, MAX_PATH_LEN);
        else if (!strcmp(line, "ignore") && w->nignore < MAX_RULES) strlcpy(w->ignore[w->nignore++], v, MAX_PATH_LEN);
        else if (!strcmp(line, "ext") && w->next < MAX_RULES) strlcpy(w->ext[w->next++], v, 16);
    }
    fclose(f);
    return w->nfolder > 0;
}

static long long mtime_of(const char *name) {
    char path[MAX_PATH_LEN];
    struct stat st;
    local_path(path, name);
    return stat(path, &st) == 0 ? (long long)st.st_mtimespec.tv_sec : -1;
}

static void write_id(FSEventStreamEventId id) {
    char path[MAX_PATH_LEN], tmp[MAX_PATH_LEN + 8];
    local_path(path, "fsevents-id");
    snprintf(tmp, sizeof tmp, "%s.tmp", path);
    FILE *f = fopen(tmp, "w");
    if (!f) return;
    fprintf(f, "%llu\n", (unsigned long long)id);
    if (fclose(f) == 0) rename(tmp, path);
}

static int read_id(FSEventStreamEventId *id) {
    char path[MAX_PATH_LEN];
    local_path(path, "fsevents-id");
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    unsigned long long v = 0;
    int ok = fscanf(f, "%llu", &v) == 1 && v > 0;
    fclose(f);
    *id = v;
    return ok;
}

// The scan would find nothing: no text file in the watched folders changed since `since`.
static int nothing_changed(Watch *w, FSEventStreamEventId since) {
    CFMutableArrayRef roots = CFArrayCreateMutable(NULL, 0, &kCFTypeArrayCallBacks);
    for (int i = 0; i < w->nfolder + w->nany; i++) {
        const char *r = i < w->nfolder ? w->folder[i] : w->any[i - w->nfolder];
        CFStringRef s = CFStringCreateWithCString(NULL, r, kCFStringEncodingUTF8);
        if (s) { CFArrayAppendValue(roots, s); CFRelease(s); }
    }
    FSEventStreamContext ctx = {0, w, NULL, NULL, NULL};
    FSEventStreamRef stream = FSEventStreamCreate(NULL, on_events, &ctx, roots, since, 0,
                                                  kFSEventStreamCreateFlagFileEvents);
    CFRelease(roots);
    if (!stream) return 0;
    w->done = dispatch_semaphore_create(0);
    dispatch_queue_t q = dispatch_queue_create("wr0te.fsevents", NULL);
    FSEventStreamSetDispatchQueue(stream, q);
    int finished = FSEventStreamStart(stream) &&
        dispatch_semaphore_wait(w->done, dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC)) == 0;
    FSEventStreamStop(stream);
    FSEventStreamInvalidate(stream);
    FSEventStreamRelease(stream);
    dispatch_sync(q, ^{});  // let a callback in flight finish before reading `changed`
    return finished && !w->changed;
}

static int run_python(void) {
    const char *python = access(CLT_PYTHON, X_OK) == 0 ? CLT_PYTHON : "/usr/bin/python3";
    char *args[] = {(char *)python, "-I", SCRIPT, "auto", NULL};

    static char envbuf[8][1024];
    char *env[9];
    int n = 0;
    snprintf(envbuf[n], sizeof envbuf[n], "PATH=/usr/bin:/bin:/usr/sbin:/sbin");
    env[n] = envbuf[n]; n++;
    snprintf(envbuf[n], sizeof envbuf[n], "LANG=en_US.UTF-8");
    env[n] = envbuf[n]; n++;
    const char *keep[] = {"HOME", "WR0TE_ROOT", "WR0TE_DATA", "WR0TE_MACHINE"};
    for (size_t i = 0; i < sizeof keep / sizeof *keep; i++) {
        const char *v = getenv(keep[i]);
        if (v && strlen(v) < 900) {
            snprintf(envbuf[n], sizeof envbuf[n], "%s=%s", keep[i], v);
            env[n] = envbuf[n]; n++;
        }
    }
    env[n] = NULL;

    pid_t pid;
    int st;
    if (posix_spawn(&pid, python, NULL, NULL, args, env) != 0) {
        perror("wr0te: spawn python3");
        return 1;
    }
    waitpid(pid, &st, 0);
    return WIFEXITED(st) ? WEXITSTATUS(st) : 1;
}

int main(void) {
    const char *home = getenv("HOME");
    if (!home || strlen(home) > 900) return run_python();
    snprintf(local, sizeof local, "%s/Library/Application Support/wr0te", home);

    FSEventStreamEventId now_id = FSEventsGetCurrentEventId();
    static Watch w;
    FSEventStreamEventId since;
    long long watch_time = mtime_of("watch");
    if (mtime_of("report-request") < 0                      // the menu bar didn't ask for a run
        && mtime_of("config.json") <= watch_time            // settings unchanged since the watch list
        && read_watch(&w) && time(NULL) < w.due              // nothing scheduled, like a new day
        && read_id(&since) && nothing_changed(&w, since)) {
        // Counts as having looked: wr0te.py would have found every file as it was.
        char looked[MAX_PATH_LEN];
        local_path(looked, "looked");
        int fd = open(looked, O_WRONLY | O_CREAT | O_NOFOLLOW, 0600);
        if (fd >= 0) { futimes(fd, NULL); close(fd); }
        // The position stays where the last real run left it: an edit that happened just now
        // may not be in the journal yet, and must still be ahead of it next time.
        return 0;
    }
    int status = run_python();
    if (status == 0) write_id(now_id);  // changes during the run are picked up next time
    return status;
}

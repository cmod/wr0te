// Tiny launcher so the macOS privacy permission (access to Dropbox) can be granted to
// "wr0te.app" alone rather than to every Python script. `wr0te.py install` compiles this
// with SCRIPT set to the path of wr0te.py.
//
// Whatever this spawns inherits the app's permission, so it is deliberately rigid:
// fixed interpreter, isolated mode (-I: no PYTHON* env vars, no user site-packages, no
// script-directory imports), a minimal environment, and no arguments from the caller.
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#define CLT_PYTHON "/Library/Developer/CommandLineTools/usr/bin/python3"

int main(void) {
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

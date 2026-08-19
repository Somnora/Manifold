/* The sidecar path that never moves.
 *
 * Every MCP client on this machine is registered against
 * /Applications/Manifold.app/Contents/MacOS/manifold-backend, and Tauri's
 * sidecar API spawns the same path. The real backend now lives one level
 * over, in Contents/Resources/backend/ (PyInstaller onedir: the binary
 * plus its _internal tree), because a onefile binary re-extracts 87MB on
 * every spawn and macOS re-assesses every fresh extraction - the measured
 * cause of multi-second handshake tails (see DECISIONS.md, phase 106).
 *
 * So this program stands at the old path and exec()s the real one. exec,
 * not spawn: the pid the client (or Tauri) holds IS the backend, stdio
 * passes through untouched, and a kill lands on the real process instead
 * of a wrapper. argv[0] is rewritten to the real path so the backend's
 * own sys.executable-relative logic (doctor --handshake respawns itself)
 * resolves inside the bundle, not back through this shim.
 *
 * macOS only. On Windows the onedir binary needs no shim: the msi lays
 * the exe and _internal side by side in the install root (see
 * stage-sidecar.sh).
 */
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char *argv[]) {
    /* Resolve our own real location; the registered path may be reached
     * through a symlink and relative-to-cwd would break a Finder launch. */
    char raw[PATH_MAX];
    uint32_t size = sizeof(raw);
    if (_NSGetExecutablePath(raw, &size) != 0) {
        fprintf(stderr, "manifold-backend shim: executable path exceeds %d\n",
                PATH_MAX);
        return 127;
    }
    char self[PATH_MAX];
    if (realpath(raw, self) == NULL) {
        fprintf(stderr, "manifold-backend shim: realpath failed for %s\n", raw);
        return 127;
    }

    /* Contents/MacOS/<self> -> Contents/Resources/backend/manifold-backend */
    char *slash = strrchr(self, '/');
    if (slash == NULL) {
        fprintf(stderr, "manifold-backend shim: no directory in %s\n", self);
        return 127;
    }
    *slash = '\0';
    char target[PATH_MAX];
    int n = snprintf(target, sizeof(target),
                     "%s/../Resources/backend/manifold-backend", self);
    if (n < 0 || (size_t)n >= sizeof(target)) {
        fprintf(stderr, "manifold-backend shim: target path exceeds %d\n",
                PATH_MAX);
        return 127;
    }

    argv[0] = target;
    execv(target, argv);
    /* Only reached when exec failed - a broken bundle, say it plainly. */
    perror(target);
    return 127;
}

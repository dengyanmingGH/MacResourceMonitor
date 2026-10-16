/*
 * MacResourceMonitor 启动器
 * ============================
 * 为什么需要这个 C 启动器，而不是 shell 脚本？
 *
 * macOS 26 的状态栏项使用 scene 机制，由 ControlCenter 托管
 * (com.apple.controlcenter.statusitems)。当 .app 的主可执行文件用
 * exec() 直接替换成 Python 进程时，macOS 无法把该进程与应用包
 * 身份关联，ControlCenter 会拒绝创建状态栏场景，表现为：
 *
 *     [AppKit:StatusBar] scene activation failed:
 *     BSServiceConnectionErrorDomain Code=3 "XPC error"
 *
 * 结果是应用进程在运行，但菜单栏看不到任何图标。
 *
 * 解决方案：fork() 出子进程运行 Python，父进程（即本启动器，
 * 也是 .app 的 CFBundleExecutable）保持存活并等待子进程。
 * 这样 macOS 就能把 Python 子进程归属于本应用包，状态栏
 * 场景即可正常激活。
 *
 * Python 解释器自适应：
 *   优先使用 build.sh 探测并写入 Resources/interpreter 的路径；
 *   若其不存在或执行失败，依次回退到常见安装路径。
 *
 * 编译：
 *   cc -O2 -o MacResourceMonitor launcher.c
 */

#include <unistd.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <sys/wait.h>
#include <sys/stat.h>
#include <mach-o/dyld.h>

static char g_script[4096];

/* 尝试用指定解释器启动；execl 成功则不返回 */
static void try_exec(const char *py) {
    execl(py, "python", g_script, (char *)NULL);
}

static int usable(const char *path) {
    struct stat st;
    return path && path[0] && stat(path, &st) == 0 && S_ISREG(st.st_mode)
           && access(path, X_OK) == 0;
}

int main(int argc, char *argv[]) {
    char exe[4096];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0) return 1;

    /* exe = .../MacResourceMonitor.app/Contents/MacOS/MacResourceMonitor */
    char *slash = strrchr(exe, '/');
    if (!slash) return 1;
    *slash = '\0';                                  /* .../Contents/MacOS */

    char res[4096];
    snprintf(res, sizeof(res), "%s/../Resources", exe);

    char pypath[8192];
    snprintf(pypath, sizeof(pypath), "PYTHONPATH=%s/lib", res);
    putenv(pypath);

    putenv("PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin");
    putenv("MRM_ISMC=/opt/homebrew/bin/iSMC");

    snprintf(g_script, sizeof(g_script), "%s/system_monitor.py", res);

    /* build.sh 探测到的解释器路径（可能不存在） */
    char chosen[4096];
    chosen[0] = '\0';
    char ipath[4096];
    snprintf(ipath, sizeof(ipath), "%s/interpreter", res);
    FILE *f = fopen(ipath, "r");
    if (f) {
        if (fgets(chosen, sizeof(chosen), f)) {
            char *nl = strpbrk(chosen, "\r\n");
            if (nl) *nl = '\0';
        }
        fclose(f);
    }

    pid_t pid = fork();
    if (pid == 0) {
        /* 子进程：运行 Python 监控程序，日志写 /tmp */
        freopen("/tmp/macresourcemonitor.log", "a", stdout);
        freopen("/tmp/macresourcemonitor.log", "a", stderr);

        if (usable(chosen))
            try_exec(chosen);
        try_exec("/opt/homebrew/bin/python3.10");
        try_exec("/opt/homebrew/bin/python3.11");
        try_exec("/opt/homebrew/bin/python3.12");
        try_exec("/opt/homebrew/bin/python3.13");
        try_exec("/opt/homebrew/bin/python3");
        try_exec("/usr/bin/python3");
        _exit(127);
    }
    if (pid < 0) return 1;

    /* 父进程：保持应用身份存活，等待子进程结束 */
    int status = 0;
    waitpid(pid, &status, 0);
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}

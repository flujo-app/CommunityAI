/* Restricted clone3 birth primitive. The child never returns into Python.
 * All allocations and Python conversions happen before clone3; the child uses
 * only native async-signal-safe operations until execve or _exit. */
#define _GNU_SOURCE
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/magic.h>
#include <linux/sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#if defined(__x86_64__) && !defined(SYS_close_range)
#define SYS_close_range 436
#endif
#if !defined(SYS_clone3) || !defined(SYS_pidfd_send_signal) || !defined(SYS_close_range)
#error "Linux headers with clone3, pidfd_send_signal and close_range are required"
#endif

/* Ubuntu 20.04 headers predate clone3's cgroup field. The kernel UAPI layout
 * is fixed; use it directly so build-host headers do not set the ABI floor. */
#ifndef CLONE_INTO_CGROUP
#define CLONE_INTO_CGROUP 0x200000000ULL
#endif
#ifndef CLONE_CLEAR_SIGHAND
#define CLONE_CLEAR_SIGHAND 0x100000000ULL
#endif
struct restricted_clone_args {
    uint64_t flags, pidfd, child_tid, parent_tid, exit_signal;
    uint64_t stack, stack_size, tls, set_tid, set_tid_size, cgroup;
};
_Static_assert(sizeof(struct restricted_clone_args) == 88, "clone3 cgroup ABI size");

typedef struct {
    PyObject_HEAD
    pid_t pid;
    int pidfd;
    int output;
    int gate;
    int status;
    dev_t device;
    ino_t inode;
} Child;

static PyObject *failure(void) {
    PyErr_SetString(PyExc_RuntimeError, "Linux cgroup process creation is unavailable");
    return NULL;
}

static void close_fd(int *fd) {
    if (*fd >= 0) {
        /* Never retry close after EINTR: the descriptor may already be closed. */
        close(*fd);
        *fd = -1;
    }
}

static void child_dealloc(Child *self) {
    close_fd(&self->gate);  /* A never-resumed child sees EOF and exits. */
    close_fd(&self->status);
    close_fd(&self->output);
    close_fd(&self->pidfd);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *child_pid(Child *self, void *unused) {
    return PyLong_FromLong(self->pid);
}

static PyObject *child_identity(Child *self, void *unused) {
    return Py_BuildValue("(KK)", (unsigned long long)self->device, (unsigned long long)self->inode);
}

static PyObject *child_descriptors(Child *self, PyObject *unused) {
    return Py_BuildValue("(iii)", self->pidfd, self->gate, self->status);
}

static PyObject *child_output(Child *self, PyObject *unused) {
    if (self->output < 0) return failure();
    PyObject *result = PyLong_FromLong(self->output);
    if (result != NULL) self->output = -1;
    return result;
}

static PyObject *child_close_control(Child *self, PyObject *unused) {
    close_fd(&self->gate);
    close_fd(&self->status);
    Py_RETURN_NONE;
}

static PyObject *child_signal(Child *self, PyObject *argument) {
    long signum = PyLong_AsLong(argument);
    if (PyErr_Occurred()) return NULL;
    if (self->pidfd < 0 || (signum != SIGTERM && signum != SIGKILL)) return failure();
    if (syscall(SYS_pidfd_send_signal, self->pidfd, signum, NULL, 0) < 0 && errno != ESRCH) return failure();
    Py_RETURN_NONE;
}

static PyObject *child_poll(Child *self, PyObject *unused) {
    siginfo_t info;
    memset(&info, 0, sizeof(info));
    if (self->pidfd < 0) return failure();
    /* P_PIDFD is Linux idtype 3, even on libc headers predating its enum. */
    if (waitid((idtype_t)3, (id_t)self->pidfd, &info, WEXITED | WNOHANG) < 0) return failure();
    if (info.si_pid == 0) Py_RETURN_NONE;
    if (info.si_code == CLD_EXITED) return PyLong_FromLong(info.si_status);
    if (info.si_code == CLD_KILLED || info.si_code == CLD_DUMPED) return PyLong_FromLong(-info.si_status);
    return failure();
}

static PyGetSetDef child_getset[] = {
    {"pid", (getter)child_pid, NULL, NULL, NULL},
    {"cgroup_identity", (getter)child_identity, NULL, NULL, NULL},
    {NULL}
};

static PyMethodDef child_methods[] = {
    {"descriptors", (PyCFunction)child_descriptors, METH_NOARGS, NULL},
    {"take_stdout", (PyCFunction)child_output, METH_NOARGS, NULL},
    {"close_control", (PyCFunction)child_close_control, METH_NOARGS, NULL},
    {"send_signal", (PyCFunction)child_signal, METH_O, NULL},
    {"poll", (PyCFunction)child_poll, METH_NOARGS, NULL},
    {NULL}
};

static PyTypeObject ChildType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "drift.node._linux_cgroup_spawn.Child",
    .tp_basicsize = sizeof(Child),
    .tp_dealloc = (destructor)child_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_methods = child_methods,
    .tp_getset = child_getset,
};

static void child_fail(int status) {
    char marker = 'E';
    while (write(status, &marker, 1) < 0 && errno == EINTR) {}
    _exit(127);
}

static void execute_child(int input, int output, int error, int gate, int status,
                          char **argv, char **envp, const char *cwd, int session) {
    /* Parent chose gate/status >= 10. Stdio sources are consumed before any
     * fixed control descriptor is overwritten. No authority FD survives. */
    if (dup2(input, 0) < 0 || dup2(output, 1) < 0 || dup2(error, 2) < 0) child_fail(status);
    if (dup2(gate, 3) < 0 || dup2(status, 4) < 0) child_fail(status);
    if (fcntl(3, F_SETFD, FD_CLOEXEC) < 0 || fcntl(4, F_SETFD, FD_CLOEXEC) < 0) child_fail(4);
    if (syscall(SYS_close_range, 5U, UINT_MAX, 0) < 0) child_fail(4);
    /* Closing inherited flock descriptors must never issue LOCK_UN: their
     * shared open-file description still belongs to the live parent owner. */
    struct sigaction action;
    memset(&action, 0, sizeof(action));
    action.sa_handler = SIG_DFL;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGPIPE, &action, NULL) < 0 || sigaction(SIGCHLD, &action, NULL) < 0) child_fail(4);
#ifdef SIGXFZ
    if (sigaction(SIGXFZ, &action, NULL) < 0) child_fail(4);
#endif
    if (sigaction(SIGXFSZ, &action, NULL) < 0) child_fail(4);
    sigset_t empty;
    sigemptyset(&empty);
    if (sigprocmask(SIG_SETMASK, &empty, NULL) < 0) child_fail(4);
    if (session && setsid() < 0) child_fail(4);
    if (cwd != NULL && chdir(cwd) < 0) child_fail(4);
    char marker = 'R';
    ssize_t count;
    do { count = write(4, &marker, 1); } while (count < 0 && errno == EINTR);
    if (count != 1) _exit(127);
    do { count = read(3, &marker, 1); } while (count < 0 && errno == EINTR);
    if (count != 1 || marker != 'G') _exit(126);
    close(3);
    execve(argv[0], argv, envp);
    child_fail(4);
}

static char **string_vector(PyObject *value, int environment) {
    if (!PyTuple_Check(value)) return NULL;
    Py_ssize_t size = PyTuple_GET_SIZE(value);
    if (size < (environment ? 0 : 1) || size > 65536) return NULL;
    char **result = calloc((size_t)size + 1, sizeof(char *));
    if (result == NULL) { PyErr_NoMemory(); return NULL; }
    size_t total = 0;
    for (Py_ssize_t index = 0; index < size; index++) {
        PyObject *item = PyTuple_GET_ITEM(value, index);
        Py_ssize_t length;
        if (!PyUnicode_Check(item)) goto fail;
        const char *text = PyUnicode_AsUTF8AndSize(item, &length);
        if (text == NULL || (size_t)length != strlen(text)) goto fail;
        total += (size_t)length + 1;
        if (total > 1024 * 1024) goto fail;
        if (environment && (strchr(text, '=') == NULL || text[0] == '=')) goto fail;
        result[index] = (char *)text;
    }
    if (!environment && result[0][0] != '/') goto fail;
    return result;
fail:
    free(result);
    return NULL;
}

static int high_fd(int fd) {
    int result = fcntl(fd, F_DUPFD_CLOEXEC, 10);
    close(fd);
    return result;
}

static PyObject *spawn(PyObject *module, PyObject *arguments) {
    int borrowed, session, borrowed_input = -1;
    PyObject *argv_object, *env_object, *cwd_object, *input_object = NULL;
    if (!PyArg_ParseTuple(arguments, "iOOOp|O", &borrowed, &argv_object, &env_object, &cwd_object, &session, &input_object)) return NULL;
    if (input_object != NULL) {
        if (!PyLong_CheckExact(input_object)) return failure();
        long value = PyLong_AsLong(input_object);
        if (PyErr_Occurred() || value < 3 || value > INT_MAX) {
            PyErr_Clear();
            return failure();
        }
        borrowed_input = (int)value;
    }
    char **argv = string_vector(argv_object, 0), **envp = NULL;
    if (argv != NULL) envp = string_vector(env_object, 1);
    const char *cwd = NULL;
    if (cwd_object != Py_None) {
        Py_ssize_t length;
        if (!PyUnicode_Check(cwd_object)) goto invalid;
        cwd = PyUnicode_AsUTF8AndSize(cwd_object, &length);
        if (cwd == NULL || cwd[0] != '/' || strlen(cwd) != (size_t)length) goto invalid;
    }
    if (argv == NULL || envp == NULL) goto invalid;
    Child *child = PyObject_New(Child, &ChildType);
    if (child == NULL) { free(argv); free(envp); return NULL; }
    child->pid = 0;
    child->pidfd = child->output = child->gate = child->status = -1;
    int cgroup = -1, input = -1, error = -1, output[2] = {-1, -1}, gate[2] = {-1, -1}, status[2] = {-1, -1};
    int pidfd = -1;
    struct stat info;
    struct statfs filesystem;
    cgroup = fcntl(borrowed, F_DUPFD_CLOEXEC, 10);
    if (cgroup < 0 || fstat(cgroup, &info) < 0 || !S_ISDIR(info.st_mode) || fstatfs(cgroup, &filesystem) < 0 || filesystem.f_type != CGROUP2_SUPER_MAGIC) goto cleanup;
    child->device = info.st_dev;
    child->inode = info.st_ino;
    if (borrowed_input == -1) {
        input = open("/dev/null", O_RDONLY | O_CLOEXEC);
        if (input < 0) goto cleanup;
        input = high_fd(input);
    } else {
        /* Optional bounded protocol input. Borrow only a read-pipe end; never
         * pass caller authority files or a writable descriptor into the child. */
        if (borrowed_input < 3) goto cleanup;
        input = fcntl(borrowed_input, F_DUPFD_CLOEXEC, 10);
        struct stat input_info;
        int flags = input < 0 ? -1 : fcntl(input, F_GETFL);
        if (input < 0 || flags < 0 || (flags & O_ACCMODE) != O_RDONLY || (flags & (O_PATH | O_NONBLOCK)) ||
            fstat(input, &input_info) < 0 || !S_ISFIFO(input_info.st_mode)) goto cleanup;
        error = open("/dev/null", O_WRONLY | O_CLOEXEC);
        if (error < 0) goto cleanup;
        error = high_fd(error);
        if (error < 0) goto cleanup;
    }
    if (input < 0 || pipe2(output, O_CLOEXEC) < 0 || pipe2(gate, O_CLOEXEC) < 0 || pipe2(status, O_CLOEXEC) < 0) goto cleanup;
    output[1] = high_fd(output[1]);
    gate[0] = high_fd(gate[0]);
    status[1] = high_fd(status[1]);
    if (output[1] < 0 || gate[0] < 0 || status[1] < 0) goto cleanup;
    struct restricted_clone_args args;
    memset(&args, 0, sizeof(args));
    args.flags = CLONE_INTO_CGROUP | CLONE_PIDFD | CLONE_CLEAR_SIGHAND;
    args.pidfd = (uintptr_t)&pidfd;
    args.cgroup = (uint64_t)cgroup;
    args.exit_signal = SIGCHLD;
    long pid = syscall(SYS_clone3, &args, sizeof(args));
    if (pid == 0) execute_child(input, output[1], error < 0 ? output[1] : error, gate[0], status[1], argv, envp, cwd, session);
    if (pid < 0) goto cleanup;
    child->pid = (pid_t)pid;
    child->pidfd = pidfd; pidfd = -1;
    child->output = output[0]; output[0] = -1;
    child->gate = gate[1]; gate[1] = -1;
    child->status = status[0]; status[0] = -1;
cleanup:
    close_fd(&cgroup); close_fd(&input); close_fd(&error); close_fd(&pidfd);
    for (int i = 0; i < 2; i++) { close_fd(&output[i]); close_fd(&gate[i]); close_fd(&status[i]); }
    free(argv); free(envp);
    if (child->pid <= 0) { Py_DECREF(child); return failure(); }
    return (PyObject *)child;
invalid:
    free(argv); free(envp);
    PyErr_Clear();
    return failure();
}

static PyObject *validate(PyObject *module, PyObject *unused) {
    /* These probes cannot close an allocatable FD, signal a process, reap a
     * child or create one. Fail before admission if seccomp/kernel support is
     * insufficient for inherited-authority closure or direct-child control. */
    if (syscall(SYS_close_range, UINT_MAX, UINT_MAX, 0) < 0) return failure();
    if (syscall(SYS_pidfd_send_signal, INT_MAX, 0, NULL, 0) != -1 || errno != EBADF) return failure();
    siginfo_t info;
    memset(&info, 0, sizeof(info));
    if (waitid((idtype_t)3, (id_t)INT_MAX, &info, WEXITED | WNOHANG) != -1 || errno != EBADF) return failure();
    struct restricted_clone_args args;
    int pidfd = -1;
    memset(&args, 0, sizeof(args));
    args.flags = CLONE_INTO_CGROUP | CLONE_PIDFD | CLONE_CLEAR_SIGHAND;
    args.pidfd = (uintptr_t)&pidfd;
    args.cgroup = INT_MAX;  /* Outside allocatable FD range; no width overflow. */
    args.exit_signal = SIGCHLD;
    long result = syscall(SYS_clone3, &args, sizeof(args));
    if (result == 0) _exit(127);  /* Defensive only: Linux rejects the FD. */
    if (result > 0) {
        if (pidfd >= 0) syscall(SYS_pidfd_send_signal, pidfd, SIGKILL, NULL, 0);
        close_fd(&pidfd);
        return failure();
    }
    if (errno != EBADF) return failure();
    Py_RETURN_NONE;
}

static PyMethodDef methods[] = {
    {"spawn", spawn, METH_VARARGS, NULL},
    {"validate", validate, METH_NOARGS, NULL},
    {NULL}
};
static struct PyModuleDef definition = {PyModuleDef_HEAD_INIT, "_linux_cgroup_spawn", NULL, -1, methods};
PyMODINIT_FUNC PyInit__linux_cgroup_spawn(void) {
    if (PyType_Ready(&ChildType) < 0) return NULL;
    return PyModule_Create(&definition);
}

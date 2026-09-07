import subprocess
import threading
import sys
import os
from .log import log
from .platform import IS_WINDOWS
from . import signal
from .network_cost import should_limit_network_usage, get_network_cost

__all__ = [
    "run_command",
    "uninterruptible_wait",
]

_running_subprocess = None
_subprocess_io_threads = []


def run_command(
    command: list[str],
    stdout: list[str] = None,
    stderr: list[str] = None,
    network_heavy: bool = False,
) -> None:
    kwargs = {}
    if IS_WINDOWS:
        # NOTE: a Windows process group is only a TARGETING mechanism for
        # GenerateConsoleCtrlEvent -- unlike a POSIX foreground process group,
        # it does not filter delivery. Keyboard events still go to every
        # process attached to the console. What this flag actually buys:
        #   * pid becomes a group id that CTRL_BREAK_EVENT can target
        #   * an implicit SetConsoleCtrlHandler(NULL, TRUE), whose only effect
        #     is that Ctrl+C handlers are not invoked. The event is still
        #     delivered; the child could re-enable it with (NULL, FALSE).
        # Ctrl+BREAK has no such opt-out and ALWAYS invokes handlers, and
        # CTRL_CLOSE_EVENT still fans out too. So a console-sharing child has
        # two unavoidable extra delivery paths.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        # setsid(): new session, new process group, no controlling terminal.
        # Terminal-generated SIGINT/SIGQUIT/SIGHUP can no longer reach it.
        # Being a group leader also lets killpg() sweep up its descendants.
        kwargs["start_new_session"] = True

    global _running_subprocess
    _running_subprocess = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )

    print_lock = threading.Lock()

    def print_output(pipe, file, output_list):
        try:
            for line in iter(pipe.readline, ""):
                if output_list is None:
                    with print_lock:
                        log(line, file=file)
                else:
                    output_list.append(line)
        finally:
            pipe.close()

    global _subprocess_io_threads
    _subprocess_io_threads = [
        threading.Thread(
            target=print_output,
            args=(_running_subprocess.stdout, sys.stdout, stdout),
        ),
        threading.Thread(
            target=print_output,
            args=(_running_subprocess.stderr, sys.stderr, stderr),
        ),
    ]
    for t in _subprocess_io_threads:
        t.start()

    network_limiting = False  # To prevent repeatedly sending termination signals
    while (rc := _running_subprocess.poll()) is None:
        if signal.stop.is_set():
            # this will send a second termination signal for broadcasted signals (mostly relevant on Windows)
            _killpg(_running_subprocess)
            raise signal.StopRequested()
        if network_heavy and not network_limiting and should_limit_network_usage():
            log("Killing network-heavy subprocess due to switching to network-limiting mode. Current network cost:", sys.stderr)
            log(get_network_cost(), sys.stderr)
            network_limiting = True
            # Been thinking about suspending the process instead of killing it, but networking is not very amenable to
            # long suspensions. Moreover, suspension could cause the lock of the remote repository to be held for an extended period,
            # which could prevent other clients from accessing it.
            _killpg(_running_subprocess)
            # We should not raise an exception here because we want cleanup to proceed normally here, in comparison to
            # the stop request, where the cleanup is handled by the script cleanup routine.
        signal.stop.wait(0.1)
    _running_subprocess = None
    for t in _subprocess_io_threads:
        t.join()
    _subprocess_io_threads = []
    if rc != 0:
        raise subprocess.CalledProcessError(rc, command)


def _killpg(p: subprocess.Popen) -> None:
    try:
        if IS_WINDOWS:
            # Works because the child was created with CREATE_NEW_PROCESS_GROUP
            # (pid == process group id) and still shares our console.
            # CTRL_C_EVENT would silently no-op against a specific group.
            os.kill(p.pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
    except OSError:
        pass


def uninterruptible_wait() -> None:
    global _running_subprocess
    if _running_subprocess is not None:
        _running_subprocess.wait()
    _running_subprocess = None
    global _subprocess_io_threads
    for t in _subprocess_io_threads:
        t.join()
    _subprocess_io_threads = []

"""First process inside a proxy-mode jail. Stdlib only: it runs with
``python -I -S`` and sees nothing of the platform's packages.

Usage: jail_init.py <egress-socket> <port> -- <command...>

The jail has its own network namespace with only loopback, so it listens on
127.0.0.1:<port> and relays every connection to the worker's egress proxy
through the bound unix socket. Then it runs the command, forwards
termination signals to it and exits with its status.
"""

import contextlib
import os
import signal
import socket
import subprocess
import sys
import threading

BUFFER = 65536


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(BUFFER)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            dst.shutdown(socket.SHUT_WR)


def _relay(client: socket.socket, target: str) -> None:
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        upstream.connect(target)
    except OSError:
        client.close()
        upstream.close()
        return
    back = threading.Thread(target=_pipe, args=(upstream, client), daemon=True)
    back.start()
    _pipe(client, upstream)
    back.join()
    client.close()
    upstream.close()


def _serve(listener: socket.socket, target: str) -> None:
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        threading.Thread(target=_relay, args=(client, target), daemon=True).start()


def main() -> int:
    argv = sys.argv[1:]
    if len(argv) < 4 or argv[2] != "--":
        print("usage: jail_init.py <socket> <port> -- <command...>", file=sys.stderr)
        return 2
    target, port, command = argv[0], int(argv[1]), argv[3:]
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(128)
    threading.Thread(target=_serve, args=(listener, target), daemon=True).start()

    child = subprocess.Popen(command)  # noqa: S603 - the CLI, chosen by the worker

    def forward(signum: int, _frame: object) -> None:
        with contextlib.suppress(ProcessLookupError):
            child.send_signal(signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, forward)
    code = child.wait()
    return 128 - code if code < 0 else code


if __name__ == "__main__":
    os._exit(main())

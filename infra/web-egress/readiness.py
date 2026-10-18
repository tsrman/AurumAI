#!/usr/bin/env python3
"""Serve, check, or wait for post-policy web-egress readiness."""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time


TOKEN = b"WEB_EGRESS_READY_V1\n"


def probe(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5) as connection:
            return connection.recv(len(TOKEN)) == TOKEN
    except OSError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--serve", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--wait", action="store_true")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if args.port < 1 or args.port > 65535:
        parser.error("--port must be between 1 and 65535")

    if args.serve:
        if args.command:
            parser.error("--serve does not accept a command")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", args.port))
            listener.listen(8)
            while True:
                connection, _ = listener.accept()
                with connection:
                    connection.sendall(TOKEN)

    if args.check:
        if args.command:
            parser.error("--check does not accept a command")
        raise SystemExit(0 if probe(args.port) else 1)

    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("--wait requires a command after --")
    while not probe(args.port):
        time.sleep(0.1)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()

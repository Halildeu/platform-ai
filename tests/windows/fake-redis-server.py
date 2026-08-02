"""Minimal RESP server for Windows provisioning contract tests."""

from __future__ import annotations

import argparse
import socket
import threading
from typing import BinaryIO


def read_command(reader: BinaryIO) -> list[bytes] | None:
    marker = reader.readline()
    if not marker:
        return None
    if not marker.startswith(b"*"):
        return []
    count = int(marker[1:-2])
    parts: list[bytes] = []
    for _ in range(count):
        length = int(reader.readline()[1:-2])
        parts.append(reader.read(length))
        reader.read(2)
    return parts


def handle(connection: socket.socket, username: bytes, password: bytes) -> None:
    authenticated = False
    with connection:
        with connection.makefile("rb") as reader:
            while True:
                command = read_command(reader)
                if command is None:
                    return
                name = command[0].upper() if command else b""
                if name == b"AUTH":
                    authenticated = command[1:] == [username, password]
                    if authenticated:
                        connection.sendall(b"+OK\r\n")
                    else:
                        connection.sendall(b"-WRONGPASS invalid credentials\r\n")
                elif name == b"CLIENT":
                    connection.sendall(b"+OK\r\n")
                elif not authenticated:
                    connection.sendall(b"-NOAUTH Authentication required\r\n")
                elif name == b"PING":
                    connection.sendall(b"+PONG\r\n")
                elif name == b"SELECT":
                    connection.sendall(b"+OK\r\n")
                else:
                    connection.sendall(b"-ERR unsupported test command\r\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    args = parser.parse_args()
    username = args.username.encode("utf-8")
    password = args.password.encode("utf-8")
    with socket.create_server(("127.0.0.1", args.port), reuse_port=False) as server:
        while True:
            connection, _ = server.accept()
            threading.Thread(
                target=handle,
                args=(connection, username, password),
                daemon=True,
            ).start()


if __name__ == "__main__":
    main()

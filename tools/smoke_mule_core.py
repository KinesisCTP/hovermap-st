"""Fail-fast compatibility smoke test against the real pinned Mule core."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from hovermap_ros2_api.mule_config import (
    MuleSettings,
    PERSISTENT_PUBLICATIONS,
    VOLATILE_PUBLICATIONS,
    build_core_config,
)


def _close_client(client) -> None:
    """Close resources exposed by the pinned core, which has no public close."""

    for stream_name in ("_zsub_stream", "_zrouter_stream"):
        stream = getattr(client, stream_name, None)
        if stream is not None:
            stream.close(linger=0)
    for socket in getattr(client, "_zpub_socks", {}).values():
        socket.close(linger=0)
    connection = getattr(getattr(client, "_database", None), "_connection", None)
    if connection is not None:
        connection.close()


def _smoke_client(core) -> None:
    """Instantiate the real client on loopback and immediately release it."""

    import zmq
    from tornado.ioloop import IOLoop

    settings = MuleSettings(
        node_name="hvm_ros2_ci_smoke",
        ip_prefix="127.0.0.0",
        ip_netmask="255.0.0.0",
        min_port=55000,
        max_port=60000,
        initial_wait=0.0,
    )
    config = build_core_config(core, settings)
    if hasattr(config, "_replace") and hasattr(config, "database_memory"):
        config = config._replace(database_memory=True)
    loop = IOLoop()
    loop.make_current()
    context = zmq.Context()
    client = None
    original_directory = Path.cwd()
    try:
        with tempfile.TemporaryDirectory(prefix="hovermap-mule-smoke-") as temp:
            try:
                os.chdir(temp)
                client = core.Client.__new__(core.Client)
                core.Client.__init__(
                    client,
                    time.time,
                    config,
                    loop,
                    context,
                    uuid.uuid4().bytes,
                    list(VOLATILE_PUBLICATIONS),
                    list(PERSISTENT_PUBLICATIONS),
                )
                if list(client.get_peers()):
                    raise RuntimeError("new Mule client unexpectedly has peers")
            finally:
                try:
                    if client is not None:
                        _close_client(client)
                finally:
                    client = None
                    os.chdir(original_directory)
    finally:
        os.chdir(original_directory)
        loop.close(all_fds=True)
        IOLoop.clear_current()
        context.destroy(linger=0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-root", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--instantiate-client", action="store_true")
    arguments = parser.parse_args()
    expected_root = Path(arguments.expected_root).resolve()
    actual_commit = subprocess.run(
        ["git", "-C", str(expected_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if actual_commit != arguments.expected_commit:
        raise RuntimeError(
            f"Mule checkout is {actual_commit}, expected {arguments.expected_commit}"
        )

    import mule_bridge

    module_path = Path(mule_bridge.__file__).resolve()
    try:
        module_path.relative_to(expected_root)
    except ValueError as exc:
        raise RuntimeError(
            f"imported mule_bridge from {module_path}, not pinned root {expected_root}"
        ) from exc
    config = build_core_config(mule_bridge, MuleSettings())
    if config.swarm_name != "hvm":
        raise RuntimeError("real Mule Config did not preserve swarm_name")
    if sorted(config.bridge.volatile.flows) != ["configuration"]:
        raise RuntimeError("real Mule Config has an unexpected volatile flow shape")
    if len(config.bridge.volatile.publications) != 4:
        raise RuntimeError("real Mule Config did not accept four volatile publications")
    if len(config.bridge.persistent.publications) != 1:
        raise RuntimeError("real Mule Config did not accept the static TF publication")
    if arguments.instantiate_client:
        _smoke_client(mule_bridge)
    version = getattr(mule_bridge, "__version__", "unknown")
    mode = "config+client" if arguments.instantiate_client else "config"
    print(
        f"real Mule core smoke passed: mode={mode} version={version} "
        f"commit={actual_commit} module={module_path}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"real Mule core smoke failed: {exc}", file=sys.stderr)
        raise

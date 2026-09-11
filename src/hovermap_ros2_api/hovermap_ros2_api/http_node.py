"""ROS 2 topic facade for the Hovermap HTTP control API."""

from concurrent.futures import Future
from functools import partial
from pathlib import Path
import queue
import threading
from typing import Callable

import rclpy
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Bool, Empty, String

from hovermap_ros2_msgs.msg import HovermapStatus, ScanInformation, ScanInformationList

from .http_client import (
    HovermapError,
    HovermapHttpClient,
    validate_scan_name,
    validate_scan_prefix,
)
from .worker_pool import BoundedDaemonWorkerPool, WorkQueueFull


IP_PREFIX_TO_ADDRESS = {
    "192.168.2.0": "192.168.2.115",
    "192.168.3.0": "192.168.3.115",
    "10.9.0.0": "10.9.0.1",
}


class HttpInterfaceNode(Node):
    """Preserve the ROS 1 control/status topics while using rclpy."""

    def __init__(self) -> None:
        super().__init__("http_interface")
        callback_group = ReentrantCallbackGroup()
        control_callback_group = MutuallyExclusiveCallbackGroup()
        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=5,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )

        self.declare_parameter("hovermap_address", "")
        self.declare_parameter("ip_prefix", "10.9.0.0")
        self.declare_parameter("download_directory", "~/hovermap_downloads")
        self.declare_parameter("request_timeout", 5.0)
        self.declare_parameter("download_timeout", 450.0)
        self.declare_parameter("status_poll_interval", 1.0)
        self.declare_parameter("http_workers", 3)
        self.declare_parameter("max_pending_requests", 16)
        self.declare_parameter("max_pending_controls", 8)
        address = self.get_parameter("hovermap_address").value
        ip_prefix = self.get_parameter("ip_prefix").value
        base_url = _resolve_base_url(address, ip_prefix)

        self._download_directory = Path(
            self.get_parameter("download_directory").value
        ).expanduser()
        self._client = HovermapHttpClient(
            base_url,
            request_timeout=float(self.get_parameter("request_timeout").value),
            download_timeout=float(self.get_parameter("download_timeout").value),
            warning_sink=self.get_logger().warning,
        )
        worker_count = int(self.get_parameter("http_workers").value)
        max_pending = int(self.get_parameter("max_pending_requests").value)
        self._workers = BoundedDaemonWorkerPool(
            workers=worker_count,
            max_pending=max_pending,
            name="hovermap-http",
        )
        self._control_workers = BoundedDaemonWorkerPool(
            workers=1,
            max_pending=int(self.get_parameter("max_pending_controls").value),
            name="hovermap-control",
        )
        self._completions = queue.SimpleQueue()
        self._status_lock = threading.Lock()
        self._download_state_lock = threading.Lock()
        self._download_pending = False

        self._scan_names_pub = self.create_publisher(
            ScanInformationList, "/cortex/scan_names_response", qos
        )
        self._status_pub = self.create_publisher(
            HovermapStatus, "/cortex/hovermap_status", qos
        )
        self._download_pub = self.create_publisher(
            Bool, "/cortex/scan_download_successful", qos
        )

        self.create_subscription(
            Empty,
            "/cortex/start_scan",
            self._on_start_scan,
            qos,
            callback_group=control_callback_group,
        )
        self.create_subscription(
            Empty,
            "/cortex/stop_scan",
            self._on_stop_scan,
            qos,
            callback_group=control_callback_group,
        )
        self.create_subscription(
            Empty,
            "/cortex/scan_names_request",
            self._on_scan_names_request,
            qos,
            callback_group=callback_group,
        )
        self.create_subscription(
            String,
            "/cortex/download_scan",
            self._on_download_scan,
            qos,
            callback_group=callback_group,
        )
        self.create_subscription(
            String,
            "/cortex/set_scan_prefix",
            self._on_set_scan_prefix,
            qos,
            callback_group=control_callback_group,
        )

        interval = float(self.get_parameter("status_poll_interval").value)
        if interval <= 0:
            raise ValueError("status_poll_interval must be positive")
        self.create_timer(interval, self._poll_status, callback_group=callback_group)
        self.create_timer(0.05, self._drain_completions, callback_group=callback_group)
        self.get_logger().info(f"Hovermap HTTP target: {base_url}")

    def _submit(
        self,
        work: Callable,
        done: Callable[[Future], None],
        *,
        pool: BoundedDaemonWorkerPool | None = None,
    ) -> bool:
        selected_pool = self._workers if pool is None else pool
        try:
            future = selected_pool.submit(work)
        except (RuntimeError, WorkQueueFull) as exc:
            self.get_logger().error(f"Rejecting Hovermap HTTP work: {exc}")
            return False
        future.add_done_callback(lambda completed: self._completions.put((done, completed)))
        return True

    def _on_start_scan(self, _message: Empty) -> None:
        self._submit(
            self._client.start_scan,
            partial(self._finish_control, "start scan"),
            pool=self._control_workers,
        )

    def _on_stop_scan(self, _message: Empty) -> None:
        self._submit(
            self._client.stop_scan,
            partial(self._finish_control, "stop scan"),
            pool=self._control_workers,
        )

    def _on_set_scan_prefix(self, message: String) -> None:
        try:
            validate_scan_prefix(message.data)
        except ValueError as exc:
            self.get_logger().error(f"Rejecting scan prefix {message.data!r}: {exc}")
            return
        self._submit(
            partial(self._client.set_scan_prefix, message.data),
            partial(self._finish_control, "set scan prefix"),
            pool=self._control_workers,
        )

    def _on_scan_names_request(self, _message: Empty) -> None:
        self._submit(self._client.list_scans, self._finish_scan_names)

    def _on_download_scan(self, message: String) -> None:
        try:
            validate_scan_name(message.data)
        except ValueError as exc:
            self.get_logger().error(f"Rejecting scan download {message.data!r}: {exc}")
            self._download_pub.publish(Bool(data=False))
            return
        with self._download_state_lock:
            if self._download_pending:
                self.get_logger().error(
                    "Rejecting scan download: another download is queued or running"
                )
                self._download_pub.publish(Bool(data=False))
                return
            self._download_pending = True
        accepted = self._submit(
            partial(
                self._client.download_scan,
                message.data,
                self._download_directory,
            ),
            partial(self._finish_download, message.data),
        )
        if not accepted:
            with self._download_state_lock:
                self._download_pending = False
            self._download_pub.publish(Bool(data=False))

    def _poll_status(self) -> None:
        if not self._status_lock.acquire(blocking=False):
            return
        if not self._submit(self._client.get_status, self._finish_status):
            self._status_lock.release()

    def _drain_completions(self) -> None:
        for _ in range(64):
            try:
                done, future = self._completions.get_nowait()
            except queue.Empty:
                return
            done(future)

    def _finish_control(self, operation: str, future: Future) -> None:
        try:
            future.result()
        except (HovermapError, OSError, ValueError) as exc:
            self.get_logger().error(f"Hovermap {operation} failed: {exc}")
        except Exception as exc:  # Defensive boundary around the worker thread.
            self.get_logger().error(f"Unexpected Hovermap {operation} failure: {exc}")

    def _finish_scan_names(self, future: Future) -> None:
        try:
            scans = future.result()
        except (HovermapError, OSError, ValueError) as exc:
            self.get_logger().error(f"Hovermap scan listing failed: {exc}")
            return
        except Exception as exc:
            self.get_logger().error(f"Unexpected Hovermap scan listing failure: {exc}")
            return
        response = ScanInformationList()
        response.scans = [
            ScanInformation(name=scan.name, size=scan.size) for scan in scans
        ]
        self._scan_names_pub.publish(response)

    def _finish_download(self, scan_name: str, future: Future) -> None:
        successful = False
        try:
            result = future.result()
            self.get_logger().info(
                f"Downloaded {scan_name!r}: {result.size} bytes to {result.path}"
            )
            successful = True
        except (HovermapError, OSError, ValueError) as exc:
            self.get_logger().error(f"Hovermap download {scan_name!r} failed: {exc}")
        except Exception as exc:
            self.get_logger().error(
                f"Unexpected Hovermap download {scan_name!r} failure: {exc}"
            )
        finally:
            with self._download_state_lock:
                self._download_pending = False
        self._download_pub.publish(Bool(data=successful))

    def _finish_status(self, future: Future) -> None:
        try:
            status = future.result()
            message = HovermapStatus()
            message.scan_prefix = status.scan_prefix
            message.current_scan_name = status.current_scan_name
            message.free_space = status.free_space
            message.scan_running = status.scan_running
            self._status_pub.publish(message)
        except (HovermapError, OSError, ValueError) as exc:
            self.get_logger().error(f"Hovermap status poll failed: {exc}")
        except Exception as exc:
            self.get_logger().error(f"Unexpected Hovermap status failure: {exc}")
        finally:
            self._status_lock.release()

    def destroy_node(self) -> bool:
        self._client.cancel()
        self._control_workers.shutdown(cancel_pending=True, wait=False)
        self._workers.shutdown(cancel_pending=True, wait=False)
        return super().destroy_node()


def _resolve_base_url(address, ip_prefix) -> str:
    if not isinstance(address, str) or not isinstance(ip_prefix, str):
        raise ValueError("hovermap_address and ip_prefix parameters must be strings")
    if address:
        return address if "://" in address else f"http://{address}"
    try:
        return f"http://{IP_PREFIX_TO_ADDRESS[ip_prefix]}"
    except KeyError as exc:
        supported = ", ".join(sorted(IP_PREFIX_TO_ADDRESS))
        raise ValueError(
            f"unknown ip_prefix {ip_prefix!r}; set hovermap_address explicitly or use {supported}"
        ) from exc


def main(args=None) -> None:
    rclpy.init(args=args)
    node = HttpInterfaceNode()
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(node)
    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()

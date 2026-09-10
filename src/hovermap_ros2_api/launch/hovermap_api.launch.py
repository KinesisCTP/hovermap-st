"""Launch the native ROS 2 Hovermap Mule and HTTP adapters."""

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, EmitEvent, RegisterEventHandler
from launch.conditions import IfCondition
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
import os


def generate_launch_description():
    default_parameters = os.path.join(
        get_package_share_directory("hovermap_ros2_api"),
        "config",
        "hovermap.yaml",
    )
    parameters_file = LaunchConfiguration("params_file")
    ip_prefix = LaunchConfiguration("ip_prefix")
    hovermap_address = LaunchConfiguration("hovermap_address")
    download_directory = LaunchConfiguration("download_directory")
    shutdown_on_mule_exit = LaunchConfiguration("shutdown_on_mule_exit")
    shutdown_on_http_exit = LaunchConfiguration("shutdown_on_http_exit")

    mule_node = Node(
        package="hovermap_ros2_api",
        executable="mule_adapter",
        namespace="cortex",
        output="screen",
        parameters=[parameters_file, {"ip_prefix": ip_prefix}],
    )
    http_node = Node(
        package="hovermap_ros2_api",
        executable="http_interface",
        namespace="cortex",
        output="screen",
        parameters=[
            parameters_file,
            {
                "ip_prefix": ip_prefix,
                "hovermap_address": hovermap_address,
                "download_directory": download_directory,
            },
        ],
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument("params_file", default_value=default_parameters),
            DeclareLaunchArgument("ip_prefix", default_value="10.9.0.0"),
            DeclareLaunchArgument("hovermap_address", default_value=""),
            DeclareLaunchArgument(
                "download_directory", default_value="~/hovermap_downloads"
            ),
            DeclareLaunchArgument(
                "shutdown_on_mule_exit",
                default_value="true",
                description="Shut down all API nodes if the Mule adapter exits",
            ),
            DeclareLaunchArgument(
                "shutdown_on_http_exit",
                default_value="true",
                description="Shut down all API nodes if the HTTP adapter exits",
            ),
            RegisterEventHandler(
                OnProcessExit(
                    target_action=mule_node,
                    on_exit=[
                        EmitEvent(
                            event=Shutdown(reason="Mule adapter exited")
                        )
                    ],
                ),
                condition=IfCondition(shutdown_on_mule_exit),
            ),
            RegisterEventHandler(
                OnProcessExit(
                    target_action=http_node,
                    on_exit=[
                        EmitEvent(
                            event=Shutdown(reason="HTTP adapter exited")
                        )
                    ],
                ),
                condition=IfCondition(shutdown_on_http_exit),
            ),
            mule_node,
            http_node,
        ]
    )

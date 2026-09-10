cmake_minimum_required(VERSION 3.8)
project(hovermap_ros2_msgs)

find_package(ament_cmake REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(rosidl_default_generators REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/HovermapStatus.msg"
  "msg/ScanInformation.msg"
  "msg/ScanInformationList.msg"
  "msg/LogIndex.msg"
  "msg/Manifest.msg"
  "msg/PeerStatus.msg"
  "msg/TopicStatus.msg"
  "msg/BridgeStatus.msg"
  "srv/SetOverlay.srv"
  DEPENDENCIES builtin_interfaces
)

ament_export_dependencies(rosidl_default_runtime)
ament_package()

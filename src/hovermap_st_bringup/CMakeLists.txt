cmake_minimum_required(VERSION 3.0.2)
project(hovermap_st_bringup)

find_package(catkin REQUIRED)

catkin_package()

catkin_install_python(
  PROGRAMS scripts/tf_relay
  DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)
install(
  DIRECTORY launch rviz
  DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION}
)

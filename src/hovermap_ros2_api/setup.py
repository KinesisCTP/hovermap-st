from glob import glob
from setuptools import find_packages, setup


PACKAGE_NAME = "hovermap_ros2_api"


setup(
    name=PACKAGE_NAME,
    version="0.1.0",
    packages=find_packages(exclude=("test",)),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{PACKAGE_NAME}"]),
        (f"share/{PACKAGE_NAME}", ["package.xml", "README.md"]),
        (f"share/{PACKAGE_NAME}/launch", glob("launch/*.launch.py")),
        (f"share/{PACKAGE_NAME}/config", glob("config/*.yaml")),
        (f"share/{PACKAGE_NAME}/rviz", glob("rviz/*.rviz")),
    ],
    install_requires=["setuptools"],
    tests_require=["pytest"],
    zip_safe=True,
    maintainer="KINESIS Core Technology Platforms",
    maintainer_email="kinesis@nyu.edu",
    description="Native ROS 2 Jazzy control and Mule adapter for Hovermap.",
    license="Proprietary",
    entry_points={
        "console_scripts": [
            "http_interface = hovermap_ros2_api.http_node:main",
            "mule_adapter = hovermap_ros2_api.mule_adapter:main",
            "configure_perception = hovermap_ros2_api.configure_perception:main",
        ],
    },
)

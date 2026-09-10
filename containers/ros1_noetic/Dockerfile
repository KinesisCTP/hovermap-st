FROM osrf/ros:noetic-desktop-full

ARG USERNAME=ros
ARG USER_UID=1000
ARG USER_GID=1000
ARG VIDEO_GID=44
ARG RENDER_GID=109

SHELL ["/bin/bash", "-c"]

COPY requirements-ros1.txt /tmp/hovermap-requirements.txt

RUN user_group="$(getent group ${USER_GID} | cut -d: -f1)" \
    && if [ -z "${user_group}" ]; then groupadd --gid ${USER_GID} ${USERNAME}; user_group=${USERNAME}; fi \
    && video_group="$(getent group ${VIDEO_GID} | cut -d: -f1)" \
    && if [ -z "${video_group}" ]; then groupadd --gid ${VIDEO_GID} video-host; video_group=video-host; fi \
    && render_group="$(getent group ${RENDER_GID} | cut -d: -f1)" \
    && if [ -z "${render_group}" ]; then groupadd --gid ${RENDER_GID} render-host; render_group=render-host; fi \
    && useradd --uid ${USER_UID} --gid "${user_group}" -m ${USERNAME} \
    && usermod --append --groups "${video_group},${render_group}" ${USERNAME} \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        bash-completion \
        build-essential \
        ca-certificates \
        git \
        iproute2 \
        iputils-ping \
        net-tools \
        python3-catkin-tools \
        python3-dev \
        python3-nose \
        python3-pip \
        python3-rosdep \
        python3-vcstool \
        sudo \
        tmux \
        unzip \
        vim \
    && python3 -m pip install --no-cache-dir -r /tmp/hovermap-requirements.txt \
    && rm -f /tmp/hovermap-requirements.txt \
    && echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/${USERNAME} \
    && chmod 0440 /etc/sudoers.d/${USERNAME} \
    && rm -rf /var/lib/apt/lists/*

USER ${USERNAME}
WORKDIR /home/${USERNAME}/hovermap-st_ws

RUN echo 'source /opt/ros/noetic/setup.bash' >> /home/${USERNAME}/.bashrc \
    && echo 'if [ -n "$ROS_WORKSPACE" ] && [ -f "$ROS_WORKSPACE/devel/setup.bash" ]; then source "$ROS_WORKSPACE/devel/setup.bash"; elif [ -f "$HOME/hovermap-st_ws/devel/setup.bash" ]; then source "$HOME/hovermap-st_ws/devel/setup.bash"; fi' >> /home/${USERNAME}/.bashrc

CMD ["bash"]

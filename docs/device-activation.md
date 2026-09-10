# Hovermap API feature activation

Use this runbook only with the feature package supplied specifically for the
target Hovermap. Keep the package unmodified, outside Git, and on controlled
Kinesis storage.

The MENA3D instructions received on 2026-09-09 point to Emesent's
[Hovermap feature-upgrade guide](https://knowledge.emesent.com/docs/hovermap-feature-upgrade)
and the [Hovermap ROS API](https://github.com/Emesent/hovermap_ros_api). The
current feature guide lists Hovermap ST-X under **What you will need**, while
the Kinesis unit is a Hovermap ST. Treat that as a vendor-documentation
discrepancy: the Web UI must identify the package as a features update and
pass its integrity check. Stop and ask MENA3D/Emesent if it does not.

## Before touching the device

- Confirm the feature package came through the approved MENA3D/Emesent handoff
  and is intended for the exact target unit.
- Preserve the original filename and bytes; do not extract, rename, edit, or
  commit the package.
- Offload any needed scans and provide stable power for at least 30 minutes.
- Do not interrupt power during installation.

## Install the feature package

1. Put the unmodified feature package on a USB flash drive and attach it to
   the powered-on Hovermap.
2. Wait until the status LEDs return to slow, pulsing Emesent blue.
3. In Commander, open the top-left menu and select **Payload UI**. The direct
   Web UI at `http://hover.map` is also documented by Emesent.
4. Select **Upgrade Firmware**.
5. Advance past the expected **no BSP found** and **no upgrade image found**
   pages.
6. Confirm the wizard reports that a **features update package** was found.
   If it reports a firmware image, no package, a different unit, or any other
   unexpected result, stop without installing.
7. Select **Next** and allow the 2-to-3-minute integrity check to finish.
8. At **Ready to start upgrade**, remove the USB flash drive first, then select
   **Install**.
9. Leave power connected while Hovermap installs and restarts. Emesent states
   that the process takes 20 to 30 minutes. Wait for the slow, pulsing blue
   status indication.

## Enable and verify the external API

1. Reconnect to the Hovermap Web UI.
2. Enable **Publish external API messages**. If that control is absent, stop:
   Emesent says an updated entitlement is required.
3. For Wi-Fi operation, leave **Use Wi-Fi for external API** enabled. For the
   ST Fischer or USB Ethernet profiles, disable it.
4. Power-cycle Hovermap after changing the API transport selection.
5. Connect the client to the selected interface, configure the static `/24`
   address from the main README, and run the network preflight.
6. Start the ROS client and complete the read-only topic checks before issuing
   any start, stop, prefix, download, or perception command.

Record the installed feature-upgrade identifier, Cortex version, selected
transport, and acceptance result in the controlled equipment record. Do not
record or publish the feature image itself.

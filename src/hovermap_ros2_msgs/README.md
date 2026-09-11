# hovermap_ros2_msgs

ROS 2 equivalents of the public status interfaces used by the Hovermap ROS 1
API. `HovermapStatus`, `ScanInformation`, and `ScanInformationList` preserve
the original field-level interface. The bridge messages expose the Mule
transport's peer, manifest, and per-topic counters without depending on ROS 1.

These interfaces are KINESIS-authored ROS 2 definitions. They do not contain
the Mule transport implementation and remain proprietary; no open-source
license is granted. Public availability does not change those rights. See
[`THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md).

The original generic Mule message names are deliberately clarified in ROS 2:
`Status` is `BridgeStatus`, `Peer` is `PeerStatus`, and `Topic` is
`TopicStatus`. Original fields remain structurally equivalent. `BridgeStatus`
adds aggregate adapter drop/decode counters, while `TopicStatus` adds volatile
and persistent publication-drop counters. `Manifest`, `LogIndex`, and
`SetOverlay` retain their original names and field structures.

"""ROS-independent aggregation for transient-local static TF publication."""

from .ros1_wire import TFMessage


class StaticTransformCache:
    """Retain the latest static transform for every child frame."""

    def __init__(self, max_transforms: int = 4096) -> None:
        if max_transforms <= 0:
            raise ValueError("max_transforms must be positive")
        self._max_transforms = max_transforms
        self._by_child_frame = {}

    def update(self, message: TFMessage) -> TFMessage:
        """Merge a sample and return the complete aggregate."""

        if not isinstance(message, TFMessage):
            raise TypeError("message must be TFMessage")
        candidate = self._by_child_frame.copy()
        for transform in message.transforms:
            child_frame = transform.child_frame_id
            if not child_frame:
                raise ValueError("static transform child_frame_id must be non-empty")
            candidate[child_frame] = transform
            if len(candidate) > self._max_transforms:
                raise ValueError(
                    f"static transform cache exceeds limit {self._max_transforms}"
                )
        self._by_child_frame = candidate
        return TFMessage(tuple(self._by_child_frame.values()))

    def __len__(self) -> int:
        return len(self._by_child_frame)

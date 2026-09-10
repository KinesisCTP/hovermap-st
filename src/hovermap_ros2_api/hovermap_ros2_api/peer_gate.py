"""ROS-independent readiness predicate for guarded Mule commands."""

from collections.abc import Iterable


def peer_requirement_satisfied(
    peer_names: Iterable[str], required_peer_name: str = ""
) -> bool:
    """Return whether a live Mule peer satisfies the requested identity gate."""

    if not isinstance(required_peer_name, str):
        raise TypeError("required_peer_name must be a string")
    names = {
        name
        for name in peer_names
        if isinstance(name, str) and name
    }
    return required_peer_name in names if required_peer_name else bool(names)

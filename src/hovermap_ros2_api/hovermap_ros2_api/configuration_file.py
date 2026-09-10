"""Safe loading for explicitly selected Hovermap perception configuration."""

from pathlib import Path


def read_configuration(path, max_bytes: int = 1024 * 1024) -> str:
    """Read a non-empty, bounded UTF-8 configuration file."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    config_path = Path(path)
    if not config_path.is_file():
        raise ValueError(f"configuration path is not a file: {config_path}")
    try:
        with config_path.open("rb") as source:
            raw = source.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError(f"configuration file exceeds limit of {max_bytes} bytes")
        value = raw.decode("utf-8", "strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read UTF-8 configuration {config_path}: {exc}") from exc
    if not value.strip():
        raise ValueError("configuration file is empty")
    return value

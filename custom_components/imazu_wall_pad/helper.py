"""Helper methods for common Wall Pad integration operations."""


def format_host(host: str) -> str:
    """Format the host address string for entry into dev reg."""
    return host.replace(".", "_")


def host_to_last(host: str) -> str:
    """Format the host simple address string for entry into dev reg."""
    return host.split(".")[3]


def device_to_id(device: str) -> str:
    """Convert device path to ID."""
    # /dev/ttyUSB0 -> ttyUSB0
    return device.split("/")[-1]


def connection_to_id(connection_type: str, host: str | None, device: str | None) -> str:
    """Get connection identifier based on connection type."""
    if connection_type == "serial" and device:
        return device_to_id(device)
    if host:
        return host_to_last(host)
    return "unknown"

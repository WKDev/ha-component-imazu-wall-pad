"""Constants for the Imazu Wall Pad integration."""

from homeassistant.const import Platform

DOMAIN = "imazu_wall_pad"
BRAND_NAME = "imazu"
MANUFACTURER = "Hyundai HT"
MODEL = "WP-IMAZU"
SW_VERSION = "1.0"

DEFAULT_PORT = 8899

# Connection type constants
CONF_CONNECTION_TYPE = "connection_type"
CONF_DEVICE = "device"
CONNECTION_TCP = "tcp"
CONNECTION_SERIAL = "serial"
DEFAULT_DEVICE = "/dev/ttyUSB0"
DEFAULT_BAUDRATE = 9600

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.LIGHT,
    Platform.SWITCH,
    Platform.FAN,
    Platform.CLIMATE,
]
PACKET = "packet"

ATTR_DEVICE = "device"
ATTR_ROOM_ID = "room_id"
ATTR_SUB_ID = "sub_id"

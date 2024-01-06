from importlib.metadata import distribution
from logging import getLogger

_module = __name__.split(".")[0]

logger = getLogger(_module)
dist = distribution(_module)

# Default values
APPLICATION = "Mosaic"
CONNECTOR_HOST = "0.0.0.0"
PORT = 80

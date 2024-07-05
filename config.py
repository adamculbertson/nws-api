import logging
import os

import yaml

default_config_file = os.path.expanduser("~/.config/forecast.yml")
manual_logging = False

DEFAULT_ADDRESS = "0.0.0.0"
DEFAULT_PORT = 8080

DEFAULTS = {
    "server": {
        "address": "0.0.0.0",  # IP address / hostname to bind to (all by default)
        "port": 8080,  # Port to accept connections on
        "key": None  # API key, which must be sent via the Authorization HTTP header
    },
    # Global forecast settings
    # Location can be left blank
    "locations": []  # Locations to monitor the forecast for. For more information, see the example below.
}

"""
# Example location:
{
    "lat": None,
    "lon": None,
    "office": None # Force the NWS Office to use for the Hazardous Weather Outlook
}
"""


class ConfigError(Exception):
    pass


def setup_logging(path: str):
    file_handler = logging.FileHandler(path, 'a')
    formatter = logging.Formatter("%(asctime)s - %(levelname)s: %(message)s")
    file_handler.setFormatter(formatter)

    log = logging.getLogger()
    for hdlr in log.handlers[:]:  # Remove the existing file handlers only
        if isinstance(hdlr, logging.FileHandler):
            log.removeHandler(hdlr)

    log.addHandler(file_handler)  # Add the new file handler to the list of handlers


def set_log_level(level: str):
    if level.lower() == "debug":
        level = logging.DEBUG
    elif level.lower() == "info":
        level = logging.INFO
    elif level.lower() == "warning":
        level = logging.WARNING
    elif level.lower() == "error":
        level = logging.ERROR
    elif level.lower() == "critical":
        level = logging.CRITICAL
    else:
        logging.error(f"Unknown logging level '{level}'. Defaulting to 'INFO'")
        level = logging.INFO

    logging.getLogger().setLevel(level)


def load(file_path: str = default_config_file, data: dict = None) -> dict:
    """
    Load the configuration YAML from the specified config file. If no file was specified, then ~/.config/forecast.yml is
     used
    :param data: Loads the configuration parameters from the provided dictionary instead of the file.
    :param file_path: String path to the configuration YAML file
    :return: Dictionary of configuration parameters
    """
    config = DEFAULTS
    if data is None:
        if not os.path.exists(file_path):
            with open(file_path, "wt") as f:
                yaml.dump(DEFAULTS, f)

        else:
            with open(file_path, "rt") as f:
                config = yaml.safe_load(f)
    else:
        config = data

    if "server" in config:
        if "address" not in config['server']:
            config['server']['address'] = DEFAULT_ADDRESS
        if config['server']['address'] is None:
            config['server']['address'] = DEFAULT_ADDRESS

        if "port" not in config['server']:
            config['server']['port'] = DEFAULT_PORT
        if config['server']['port'] is None:
            config['server']['port'] = DEFAULT_PORT

        if "key" not in config['server']:
            config['server']['key'] = None
    else:
        config['server'] = {"address": DEFAULT_ADDRESS, "port": DEFAULT_PORT, "key": None}

    if "logging" in config:
        # Any logging option in the environment takes precedence over configuration options.
        # If any were set via command line, then manual_logging becomes true. Command line options override all other
        # options
        if not manual_logging:
            if "log_path" in config:
                setup_logging(config['logging']['log_path'])
                logging.debug(f"Using log path {config['logging']['log_path']} from config")
            if "log_level" in config:
                logging.debug(f"Setting log level to {config['logging']['log_level']} from config")
                set_log_level(config['logging']['log_level'])

    if "LOG_PATH" in os.environ and not manual_logging:
        logging.debug(f"Setting log path to {os.environ['LOG_PATH']} from environment")
        setup_logging(os.environ['LOG_PATH'])

    if "LOG_LEVEL" in os.environ and not manual_logging:
        logging.debug(f"Setting log level to {os.environ['LOG_LEVEL']} from environment")
        set_log_level(os.environ['LOG_LEVEL'])

    return config

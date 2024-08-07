import logging
import os
import copy

import yaml

manual_logging = False

DEFAULT_CONFIG_FILE = os.path.expanduser("~/.config/forecast.yml")

DEFAULTS = {
    "server": {
        "address": "0.0.0.0",  # IP address / hostname to bind to (all by default)
        "port": 8080,  # Port to accept connections on
        "keys": []  # List of dictionaries containing tokens and their permissions
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

# Example key item (for an admin):
{
    "name": "Admin",
    "admin": true,
    "token": "apiTokenHere"
}

# Example key item (for a read-only user):
{
    "name": "Read-Only user",
    "admin": false,
    "readOnly": true
    "token": "apiTokenHere"
}

# Example key item (for an alert only user):
{
    "name": "Alert-only user",
    "admin": false,
    "alertOnly": true,
    "token": "apiTokenHere"
}

Admin users inherently have ALL permissions.
Read-only users can request forecast information (due to the nature of sending data, it is odd to consider them 
  "read-only" since they POST data)
Alert only users can ONLY send a POST request to the alert endpoint and nothing else.
"""


# Custom Config class that will return the default value of an option if it is not present in the current configuration
# The DEFAULTS dictionary is basically read-only with this, as any changes will be added to the current config instead
# keys(), values(), items(), __cmp__(), __contains__(), and __iter__() will use the combined dictionaries
class Config(dict):
    config_path: str
    __config: dict

    def __init__(self, config_path: str = DEFAULT_CONFIG_FILE, data: dict = None, log_level: int = logging.INFO) -> None:
        super().__init__()

        if data is None:
            data = {}

        self.config_path = config_path
        self.log_level = log_level
        self.__config = data

        if not data:
            self.load()

    def __repr__(self):
        return repr(self.__config)

    def __setitem__(self, key, item):
        self.__config[key] = item

    def __len__(self):
        return len(self.__config)

    def __delitem__(self, key):
        del self.__config[key]

    def __getitem__(self, key):
        # Try to first get the requested item form the configuration dictionary
        # If not found, ignore the KeyError and try from the defaults dictionary
        try:
            return self.__config[key]
        except KeyError:
            pass

        # Let this raise the KeyError if it wasn't found
        return DEFAULTS[key]

    def __contains__(self, item):
        # Create a full copy of the default dictionary and add the user's config  to the dictionary
        # Combine the two dictionaries with update()
        items = copy.deepcopy(DEFAULTS)
        items.update(self.__config)
        return item in items

    def __iter__(self):
        items = copy.deepcopy(DEFAULTS)
        items.update(self.__config)
        return iter(items)

    def clear(self):
        return self.__config.clear()

    def copy(self):
        return self.__config.copy()

    def update(self, __m, **kwargs):
        return self.__config.update(__m, **kwargs)

    def keys(self):
        items = copy.deepcopy(DEFAULTS)
        items.update(self.__config)
        return items.keys()

    def values(self):
        items = copy.deepcopy(DEFAULTS)
        items.update(self.__config)
        return items.values()

    def items(self):
        items = copy.deepcopy(DEFAULTS)
        items.update(self.__config)
        return items.items()

    def pop(self, __key):
        return self.__config.pop(__key)

    def load(self):
        """
        Loads the configuration options from the configuration YAML file specified in the config_path.
        """
        if self.config_path is not None:
            with open(self.config_path, "rt") as f:
                self.__config = yaml.safe_load(f)

        if self.config_path is None and not self.__config:
            raise ConfigError("No configuration provided. "
                              "Please add a configuration file or pass the configuration parameters.")

    def save(self):
        """
        Saves the configuration options from the config dictionary to the YAML file specified in the config_path.
        """
        with open(self.config_path, "wt") as f:
            # Save only the user's config, not the defaults
            yaml.dump(self.__config, f)


class ConfigError(Exception):
    pass


def setup_file_logging(path: str) -> None:
    """
    Sets up logging so that it only logs to the specified file, and not stdout/stderr.
    :param path: Location of the log file
    """
    file_handler = logging.FileHandler(path, 'a')
    formatter = logging.Formatter("%(asctime)s - %(levelname)s: %(message)s")
    file_handler.setFormatter(formatter)

    log = logging.getLogger()
    for handler in log.handlers[:]:  # Remove the existing file handlers only
        if isinstance(handler, logging.FileHandler):
            log.removeHandler(handler)

    log.addHandler(file_handler)  # Add the new file handler to the list of handlers


def set_log_level(level: str) -> None:
    """
    Get the log level form the string provided and set the log level.
    :param level: Log level string, with the possible values: CRITICAL, FATAL, ERROR, WARN/WARNING, INFO, or DEBUG.
    """
    level = level.upper()
    try:
        logging.getLogger().setLevel(level)
    except ValueError:
        logging.getLogger().setLevel("INFO")


def load(config_path: str = DEFAULT_CONFIG_FILE, data: dict = None) -> Config:
    """
    Load the configuration YAML from the specified config file. If no file was specified, then DEFAULT_CONFIG_FILE is
     used
    :param data: Loads the configuration parameters from the provided dictionary instead of the file.
    :param config_path: String path to the configuration YAML file
    :return: Dictionary of configuration parameters
    """
    config = Config(config_path=config_path, data=data)

    if "logging" in config:
        # Any logging option in the environment takes precedence over configuration options.
        # If any were set via command line, then manual_logging becomes true. Command line options override all other
        # options
        if not manual_logging:
            if "log_path" in config:
                setup_file_logging(config['logging']['log_path'])
                logging.debug(f"Using log path {config['logging']['log_path']} from config")
            if "log_level" in config:
                set_log_level(config['logging']['log_level'])
                logging.debug(f"Setting log level to {config['logging']['log_level']} from config")

    if "LOG_PATH" in os.environ and not manual_logging:
        logging.debug(f"Setting log path to {os.environ['LOG_PATH']} from environment")
        setup_file_logging(os.environ['LOG_PATH'])

    if "LOG_LEVEL" in os.environ and not manual_logging:
        logging.debug(f"Setting log level to {os.environ['LOG_LEVEL']} from environment")
        set_log_level(os.environ['LOG_LEVEL'])

    config.log_level = logging.getLogger().level

    return config

import logging
import os
import copy

import yaml
import uvicorn.logging

manual_logging = False

DEFAULT_CONFIG_FILE = os.path.expanduser("~/.config/forecast.yml")
FORMAT: str = "%(levelprefix)s [%(name)s] [%(threadName)s]: %(message)s"  # Logging formatter

DEFAULTS = {
    "server": {
        "address": "0.0.0.0",  # IP address / hostname to bind to (all by default)
        "port": 8080,  # Port to accept connections on
        "alerts_file": "alerts.yml",  # Path (relative to the config path) for handling alerts
        "users": []  # List of dictionaries containing tokens and their permissions
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

# Example user item (for an admin):
{
    "name": "Admin",
    "admin": true,
    "token": "apiTokenHere"
}

# Example user item (for a read-only user):
{
    "name": "Read-Only user",
    "admin": false,
    "readOnly": true
    "token": "apiTokenHere"
}

# Example user item (for an alert only user):
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
    __extra: dict  # List of other configuration options that may be in other files

    def __init__(self, config_path: str = DEFAULT_CONFIG_FILE, data: dict = None, log_level: int = logging.INFO) -> None:
        super().__init__()

        if data is None:
            data = {}

        self.config_path = config_path
        self.log_level = log_level
        self.__config = data
        self.__extra = {}

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
        # This doesn't really seem to work for anything outside the first key in the dictionary
        # Better to use get_value() instead
        # Nested keys are all part of a standard dict instead
        # Try to first get the requested item form the configuration dictionary
        # If not found, ignore the KeyError and try from the defaults dictionary
        try:
            return self.__config[key]
        except KeyError:
            pass

        try:
            return self.__extra[key]
        except KeyError:
            pass

        # Let this raise the KeyError if it wasn't found
        return DEFAULTS[key]

    def __contains__(self, item):
        # Create a full copy of the default dictionary and add the user's config  to the dictionary
        # Combine the two dictionaries with update()
        items = copy.deepcopy(DEFAULTS)
        items.update(self.__config)
        items.update(self.__extra)
        return item in items

    def __iter__(self):
        items = copy.deepcopy(DEFAULTS)
        items.update(self.__config)
        items.update(self.__extra)
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
        items.update(self.__extra)
        return items.keys()

    def values(self):
        items = copy.deepcopy(DEFAULTS)
        items.update(self.__config)
        items.update(self.__extra)
        return items.values()

    def items(self):
        items = copy.deepcopy(DEFAULTS)
        items.update(self.__config)
        items.update(self.__extra)
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

    def add_extra(self, name: str, path: str = None, data: dict = None) -> bool:
        """
        Add extra configuration options that are stored in a different file. Path OR data must be specified.
        :param name: Unique name to use for the extra config options.
        :param path: Optional path to the extra YAML file.
        :param data: Optional dictionary of the extra configuration options.
        :return:
        """
        if path is None and data is None:
            logging.error("Cannot add extra to config. Need a path or data")
            return False

        if path is not None:
            config_path = os.path.split(self.config_path)[0]
            alerts_path = os.path.join(config_path, path)
            if os.path.exists(alerts_path):
                with open(alerts_path, "rt") as f:
                    data = yaml.safe_load(f)
            else:
                logging.warning(f"Could not load extra configuration: {path} (not found)")
                return False

        # If only one element in the dictionary, and the key is the name, reassign the dictionary to the name
        # This prevents redundant config options, such as alerts.alerts
        if len(data) == 1 and name in data:
            data = data[name]

        self.__extra[name] = data
        return True

    def get_value(self, name) -> object | dict | list | str | int | float | None:
        """
        Retrieves a configuration parameter in dot notation.
        :param name: Name of the parameter to retrieve from in dot notation. Example: server.address for config['server']['address']
        :return: The requested value or None if not found
        """

        # No . in the name is simple, just try to get it from the config, extra, or defaults
        # Instead of throwing a KeyError if nothing is found, return None
        if "." not in name:
            try:
                return self.__config[name]
            except KeyError:
                pass

            try:
                return self.__extra[name]
            except KeyError:
                pass

            try:
                return DEFAULTS[name]
            except KeyError:
                return None

        config = self.__config
        extra = self.__extra
        defaults = DEFAULTS

        # Divide the name up into the various parts and loop through them
        # Try to obtain the value from all three sections (config, extra, and defaults) until the end
        # This way, if a result wasn't found in one, it will keep searching the rest
        # Once a KeyError is thrown for one, that one will no longer be searched and set to None
        parts = name.split(".")
        for part in parts:
            if config is not None:
                try:
                    config = config[part]
                except KeyError:
                    config = None

            if extra is not None:
                try:
                    extra = extra[part]
                except KeyError:
                    extra = None

            if defaults is not None:
                try:
                    defaults = defaults[part]
                except KeyError:
                    defaults = None

        # Now return whichever one was found, starting first with the config, then extra, then defaults
        if config is not None:
            return config

        if extra is not None:
            return extra

        if defaults is not None:
            return defaults

        # If nothing at all was found, return None
        return None


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
    console_logging = True  # If true, set the formatting at the end. Sets to False when a log path is specified

    if "logging" in config:
        # Any logging option in the environment takes precedence over configuration options.
        # If any were set via command line, then manual_logging becomes true. Command line options override all other
        # options
        if not manual_logging:
            if "log_path" in config:
                setup_file_logging(config['logging']['log_path'])
                console_logging = False
                logging.debug(f"Using log path {config['logging']['log_path']} from config")
            if "log_level" in config:
                set_log_level(config['logging']['log_level'])
                logging.debug(f"Setting log level to {config['logging']['log_level']} from config")

    if "LOG_PATH" in os.environ and not manual_logging:
        logging.debug(f"Setting log path to {os.environ['LOG_PATH']} from environment")
        console_logging = False
        setup_file_logging(os.environ['LOG_PATH'])

    if "LOG_LEVEL" in os.environ and not manual_logging:
        logging.debug(f"Setting log level to {os.environ['LOG_LEVEL']} from environment")
        set_log_level(os.environ['LOG_LEVEL'])

    # Only set the formatter if we're not logging to a file
    if console_logging:
        # Set the root logger to use the formatting of uvicorn
        logger = logging.getLogger()
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.getLogger().level)
        formatter = uvicorn.logging.DefaultFormatter(FORMAT)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    config.log_level = logging.getLogger().level

    alert_path = str(config.get_value("server.alerts_file"))
    if alert_path is not None:
        config.add_extra("alerts", path=alert_path)

    return config

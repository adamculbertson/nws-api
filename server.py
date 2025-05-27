import logging
import time
import uuid
from enum import Enum

from fastapi import FastAPI, APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel

import requests
from requests.exceptions import ConnectionError

import forecast
from config import Config, ConfigError, load

# Define the severity level of each of the alert types
# See alert_types.txt in the examples folder for what each item stands for
severity_warn = ["AVW", "BHW", "BWW", "BZW", "CDW", "CEM", "CFW", "CHW", "CWW", "DBW", "DEW", "DSW", "EAN", "EQW",
                 "EVI", "EWW", "FCW", "FFW", "FLW", "FRW", "FSW", "FZW", "HMW", "HUW", "HWW", "IBW", "IFW", "LAE",
                 "LEW", "LSW", "NUW", "RHW", "SMW", "SPW", "SSW", "SVR", "TOR", "TRW", "TSW", "VOW", "WFW", "WSW",
                 "SQW"]

severity_watch = ["AVA", "CFA", "DBA", "EVA", "FFA", "FLA", "HUA", "HWA", "SSA", "SVA", "TOA", "TRA", "TSA", "WFA",
                  "WSA"]

severity_advisory = ["ADR", "CAE", "EAT", "FFS", "FLS", "HLS", "NIC", "NMN", "POS", "SPS", "SVS", "TOE"]

severity_test = ["NAT",  "NPT", "NST", "RMT", "RWT", "DMO"]

# When the client requests the weather information, the following payload is allowed:
"""
{
  "lat": 93.12,
  "lon": -35.76,
  "city": "Someplace",
  "state": "CA"
}
"""
# City and State may be the only things provided by the client
# If the server has not seen this combination before, which means the lat and lon are NOT in the cache, the server
# will respond with a 404 Not Found error

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# TODO: Check if a location is within the grid coordinates of the office. That may allow for less lookups.

# TODO: Cache this using redis maybe?
# Store the grid coordinates for a given city and state
# Format: locations[state][city] = (x, y)
locations = {}

# Store the GPS coordinates for a city, state, and location
# Format: coordinates[lat][lon] = {"city": city, "state": state}
coordinates = {}

# Store the weather information (forecast, hourly (if requested), and hazardous weather outlook
# Format: locations[office][x][y] = {"forecast": forecast, "hourly": hourly, "hwo": hwo, "time": timestamp}
weather_info = {}

# Store the NWS offices for a given city and state
# Format: offices[state][city] = office
offices = {}

# Office Locations
# Format: offices_locations[office] = {"city": city, "state": state}
offices_locations = {}

# Zone IDs
# Format: zone_ids[office][x][y] = zoneID
zone_ids = {}

verbosity = 1

# Client payload structure
# All items are listed as optional, but a pair must be specified
# For example: latitude and longitude OR city and state
class Payload(BaseModel):
    lat: float | str | None = None
    lon: float | str | None = None
    city: str | None = None
    state: str | None = None


# dsame3 webhook payload structure
class DsamePayload(BaseModel):
    ORG: str
    EEE: str
    TTTT: str
    JJJHHMM: str
    STATION: str
    TYPE: str
    LLLLLLLL: str
    COUNTRY: str
    LANG: str
    event: str
    type: str
    end: str
    start: str
    organization: str
    PSSCCC: str
    PSSCCC_list: list
    location: str
    date: str
    length: str
    seconds: int
    MESSAGE: str


# Enum for creating tokens
# Only readOnly or alertOnly are possible
class TokenType(str, Enum):
    readOnly = "readOnly"
    alertOnly = "alertOnly"


# Model for modifying tokens
class Token(BaseModel):
    name: str | None = None
    alertOnly: bool | None = None
    readOnly: bool | None = None

def convert_coordinates(lat: float|int|str, lon: float|int|str) -> tuple:
    """
    Convert the given latitude and longitude to a string, while also rounding down any floats.
    :param lat: Latitude as a float, integer, or string.
    :param lon: Longitude as a float, integer, or string.
    :return: Tuple containing the modified latitude and longitude
    """
    # If the latitude and longitude are provided as a float, round them to 2 decimal places and convert to a string
    if type(lat) is float:
        lat = str(round(lat, 2))
    if type(lon) is float:
        lon = str(round(lon, 2))

    # Convert the latitude and longitude to a string if they were provided as an integer
    # This helps make behavior more consistent.
    if type(lat) is int:
        lat = str(lat)
    if type(lon) is int:
        lon = str(lon)

    return lat, lon

def get_location_info(lat_lon: tuple) -> bool:
    """
    Call the point endpoint of the NWS API to obtain information for the provided coordinates.
    :param lat_lon: Tuple of latitude and longitude coordinates.
    :return: False if get_point() returns an error or True otherwise.
    """

    logging.debug(f"Calling get_location_info(lat_lon: {lat_lon})")
    lat, lon = lat_lon
    lat, lon = convert_coordinates(lat, lon)

    fc = forecast.Forecast()
    # Lookup point information
    if fc.get_point(lat_lon=lat_lon) < 0:
        return False

    # Lookup office information
    if fc.get_office_info(fc.office) < 0:
        return False

    # Create the dictionaries as needed

    # Determine if the state is not in the list of locations and create a dictionary for it if not
    if fc.state not in locations:
        locations[fc.state] = {}

    # Add the grid coordinates to the city and state combination
    locations[fc.state][fc.city] = fc.grid

    # Break up the latitude and longitude
    city_lat, city_lon = fc.city_lat_lon

    # Determine if the city latitude is in the list of coordinates and create a dictionary for it if not
    if city_lat not in coordinates:
        coordinates[city_lat] = {}

    # Determine if the city longitude is in the list of coordinates and create a dictionary for it if not
    if city_lon not in coordinates[city_lat]:
        coordinates[city_lat][city_lon] = {}

    # Repeat the same for the user-provided latitude and longitude values
    if lat not in coordinates:
        coordinates[lat] = {}

    if lon not in coordinates[lat]:
        coordinates[lat][lon] = {}

    # Check if the state exists in the list of offices and create a dictionary if not
    if fc.state not in offices:
        offices[fc.state] = {}

    # Check if the office's location is in the cache and create if needed
    if fc.office not in offices_locations:
        offices_locations[fc.office] = {}

    # Check if the office key exists
    if fc.office not in zone_ids:
        zone_ids[fc.office] = {}

    x, y = fc.grid

    # Check if the x coordinate key exists
    if x not in zone_ids[fc.office]:
        zone_ids[fc.office][x] = {}

    # End creating dictionaries

    # Start filling in the cache information
    # Latitude and longitude information for the city
    coordinates[city_lat][city_lon] = {"city": fc.city, "state": fc.state}
    # Latitude and longitude information that the user provided
    coordinates[lat][lon] = {"city": fc.city, "state": fc.state}
    # City and state for the office of the coordinates provided
    offices_locations[fc.office] = {"city": fc.office_city, "state": fc.office_state}
    # Assign the office to the given city and state for the user
    offices[fc.state][fc.city] = fc.office
    # Set the zone ID to the given grid coordinates
    zone_ids[fc.office][x][y] = fc.zone_id

    return True


def get_location_grid(lat_lon: tuple) -> tuple | None:
    """
    Retrieves the grid X and Y coordinates of the given latitude and longitude.
    :param lat_lon: Tuple containing latitude and longitude.
    :return: Tuple of X, Y coordinates if found. None if not found.
    """
    lat, lon = lat_lon
    lat, lon = convert_coordinates(lat, lon)

    try:
        info = coordinates[lat][lon]
        state = info['state']
        city = info['city']
        return locations[state][city]
    except KeyError:
        return None


def refresh_weather(grid_xy: tuple, office: str) -> dict | None:
    """
    Refreshes weather information by calling the appropriate NWS API endpoints.
    :param grid_xy: Tuple containing grid X, Y coordinates that can be obtained from the point API.
    :param office: NWS office to obtain data from.
    :return: Dictionary containing the hourly and regular forecasts, hazardous weather outlook, and update timestamp.
    """
    logging.debug(f"Calling refresh_weather(gridXY: {grid_xy}, office: {office})")
    fc = forecast.Forecast()
    hourly = fc.get_forecast_hourly(grid_xy=grid_xy, office=office)

    if hourly is None:
        return None

    regular = fc.get_forecast(grid_xy=grid_xy, office=office)

    if regular is None:
        return None

    try:
        x, y = grid_xy
        fc.office = office
        office_info = offices_locations[office]
        fc.office_city = office_info['city']
        fc.office_state = office_info['state']
        fc.zone_id = zone_ids[office][x][y]
    except KeyError:
        logging.error(f"Unable to locate information for {office} in the office location cache.")
        return None

    timestamp = int(time.time())

    data = {"hourly": hourly, "forecast": regular, "time": timestamp}

    x, y = grid_xy
    weather_info[office][x][y] = data
    return data

def get_hwo(payload_model: Payload, config: Config) -> list | None:
    """
    Get the Hazardous Weather Outlook for the given latitude and longitude
    :param config: Configuration options for the server
    :param payload_model: User-provided payload
    :return: Dictionary containing the HWO result
    """
    payload = payload_model.model_dump()
    if not "lat" or not "lon" in payload:
        return None

    lat, lon = convert_coordinates(payload['lat'], payload['lon'])

    fc = forecast.Forecast()
    fc.lat_lon = (lat, lon)
    start = int(time.time())
    hwo = fc.get_hwo() # TODO: Cache the HWO results similar to the forecast cache
    end = int(time.time())

    if verbosity > 3:
        logging.debug(f"fc.get_hwo() took {end - start} seconds")

    if len(hwo) > 0:
        # Check if the newest alert is similar to the ignore text
        # If BOTH 'day1' AND 'days2-7' contain the 'hwo.ignore_text' config string, then no HWO will be sent
        ignore_test = 0 # When 2, this HWO will be ignored
        ignore_text = config.get_value("hwo.ignore_text")

        if ignore_text in hwo[0]['day1']['content'].lower():
            ignore_test += 1

        if ignore_text in hwo[0]['days2-7']['content'].lower():
            ignore_test += 1

        # Ignore the HWO entry
        if ignore_test >= 2:
            logging.debug(f"Ignoring HWO entry due to '{ignore_text}' being present")
            hwo = []

    return hwo


def parse_payload(payload: dict) -> tuple | int | None:
    """
    Parses the user-provided JSON to obtain the location information and add it to the cache if not found.
    :param payload: Dictionary containing city, state, latitude, and longitude.
    :return: Tuple containing x and y coordinates, city, and state on success. None or -1 on failure.
    """
    logging.debug(f"Calling parse_payload(payload: {payload})")

    # Check if the city and state were provided and not None
    # If either is set to None, delete it
    # If they are both set to None, it still causes the first check to attempt to use them
    if "city" in payload and "state" in payload:
        if payload['city'] is None:
            del payload['city']
        if payload['state'] is None:
            del payload['state']

    # If the city and state are specified in the payload, try them first
    if "city" in payload and "state" in payload:
        # Check if the city and state's coordinates are in the cache
        # These are grid X and Y values
        try:
            location = locations[payload['state']][payload['city']]
        except KeyError:
            if "lat" not in payload or "lon" not in payload:
                # No coordinates were specified, and we do not have a way to look them up
                return None  # Causes a 404 error to be sent to the client

            # Coordinates were provided, so use them instead
            # iOS Shortcuts app sends the latitude and longitude as an integer
            payload_lat, payload_lon = convert_coordinates(payload['lat'], payload['lon'])

            # Try to get the grid X and Y coordinates from the cache first
            try:
                location = coordinates[payload_lat][payload_lon]
            except KeyError:
                # Not in the cache, so attempt to fetch the information from the API
                result = get_location_info((payload_lat, payload_lon))
                if result < 0:
                    return -1  # Returns a 400 error
                location = locations[payload['state']][payload['city']]

        city = payload['city']
        state = payload['state']
        x, y = location

    else:
        # Determine if the latitude AND longitude were specified by the client and send an error if not
        try:
            lat, lon = convert_coordinates(payload['lat'], payload['lon'])
        except KeyError:
            return -1  # Causes a 400 error to be sent to the client

        # Try to get the grid X and Y coordinates from the cache first
        location = get_location_grid((lat, lon))
        if location is None:
            # Nothing found in the cache, so retrieve the location information.
            result = get_location_info((lat, lon))
            # Still no results, so give up with a client error.
            if not result:
                return -1
            # Try one more time to get the grid coordinates.
            # If still not found, then return None to trigger a 404 Not Found.
            location = get_location_grid((lat, lon))
            if location is None:
                return None

        x, y = location
        city_state = coordinates[lat][lon]
        city = city_state['city']
        state = city_state['state']

    return x, y, city, state


def get_weather(payload_model: Payload, config: Config = None) -> dict | None:
    """
    Fetches the weather from the cache or calls the API to refresh the cache if necessary.
    :param config: Configuration information class
    :param payload_model: Model from user input that contains the latitude, longitude, city, and state of the request.
    :return: Dictionary of weather data or None on error.
    """
    payload = payload_model.model_dump()
    result = None
    try:
        result = parse_payload(payload)
        x, y, city, state = result
    except TypeError:
        # If None, then the location couldn't be found in the cache and it could not be determined
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Not found. Please try specifying coordinates instead"
            )

        # Any other value is a bad request
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid parameters"
        )

    office = offices[state][city]
    # Determine if the office dictionary exists and create it if not
    if office not in weather_info:
        weather_info[office] = {}

    # Determine if the x coordinate dictionary exists and create it if not
    if x not in weather_info[office]:
        weather_info[office][x] = {}

    try:
        weather = weather_info[office][x][y]
    except KeyError:
        weather = refresh_weather((x, y), office)
        if weather is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unable to obtain weather information for the coordinates {x}, {y}"
            )

    # Check if the forecast has been cached recently
    # If it was just created above, then the below check should fail and not be called
    now = int(time.time())

    if weather['time'] < now - config.get_value("server.cache_time") * 60:
        weather = refresh_weather((x, y), office)

        if weather is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unable to obtain weather information for the coordinates {x}, {y}"
            )

    return weather


class APIv1:
    app: FastAPI
    config: Config
    router: APIRouter
    version: str = "v1"

    def __init__(self, app: FastAPI, config: Config) -> None:
        self.app = app
        self.router = APIRouter()
        self.config = config

        # Check that the config file has a "server" section and that the API key was specified in it
        if self.config.get_value("server") is None:
            raise ConfigError("No server configuration options were provided in the configuration file")

        if self.config.get_value("server.users") is None or not self.config.get_value("server.users"):
            raise ConfigError("Please provide a list of users and keys in the 'server' section"
                              " of the configuration file.")

        # Define routers for the API
        # These are standard read-only methods (they can't change anything but add data to the cache)
        # Routes that only require read permissions
        self.router.add_api_route("/forecast/all", self.get_all_forecast_info, methods=["POST"],
                                  dependencies=[Depends(self.check_token_read)],
                                  description="Obtain all available forecast information from the NWS")

        self.router.add_api_route("/forecast/daily", self.get_forecast_info, methods=["POST"],
                                  dependencies=[Depends(self.check_token_read)],
                                  description="Obtain only the daily forecast information from the NWS")

        self.router.add_api_route("/forecast/hourly", self.get_hourly_forecast, methods=["POST"],
                                  dependencies=[Depends(self.check_token_read)],
                                  description="Obtain only the hourly forecast information from the NWS")

        self.router.add_api_route("/hwo", self.get_hazardous_weather_outlook, methods=["POST"],
                                  dependencies=[Depends(self.check_token_read)],
                                  description="Parse and obtain the Hazardous Weather Outlook from the NWS")

        self.router.add_api_route("/hwo/spotter", self.get_spotter_activation_statement, methods=["POST"],
                                  dependencies=[Depends(self.check_token_read)],
                                  description="Parse the Hazardous Weather Outlook and only obtain the Spotter "
                                              "Activation Statement")

        # Routers that only require alert permissions
        self.router.add_api_route("/alert", self.receive_dsame_alert, methods=["POST"],
                                  dependencies=[Depends(self.check_token_alert)],
                                  description="Receive an alert from dsame3")

        # Routers that require admin permissions
        # These can change server configuration options, so they will have a different token check
        self.router.add_api_route("/admin/cache", self.admin_get_cache, methods=["GET"],
                                  dependencies=[Depends(self.check_token_admin)],
                                  description="View the cached forecast data")

        self.router.add_api_route("/admin/cache/clear", self.admin_clear_cache, methods=["DELETE"],
                                  dependencies=[Depends(self.check_token_admin)],
                                  description="Clear ALL of the currently cached forecast data")

        self.router.add_api_route("/admin/token", self.admin_get_tokens, methods=["GET"],
                                  dependencies=[Depends(self.check_token_admin)],
                                  description="Get a list of non-admin tokens")

        self.router.add_api_route("/admin/token/delete/{token}", self.admin_delete_token, methods=["DELETE"],
                                  dependencies=[Depends(self.check_token_admin)],
                                  description="Delete the specified non-admin token")

        self.router.add_api_route("/admin/token/create/{token_type}", self.admin_create_token, methods=["PUT"],
                                  dependencies=[Depends(self.check_token_admin)],
                                  description="Create a read-only or alert-only token")

        self.router.add_api_route("/admin/token/modify/{token}", self.admin_modify_token, methods=["POST"],
                                  dependencies=[Depends(self.check_token_admin)],
                                  description="Modify the specified non-admin token")

        self.router.add_api_route("/admin/config/save", self.admin_save_config,
                                  methods=["POST", "GET", "HEAD", "PATCH"],
                                  dependencies=[Depends(self.check_token_admin)],
                                  description="Saves any modified configuration options "
                                              "(and users) to the configuration file.")

        self.app.include_router(self.router, prefix=f"/api/{self.version}/weather")

    # Protected endpoint example: https://testdriven.io/tips/6840e037-4b8f-4354-a9af-6863fb1c69eb/
    # Another API key example: https://timberry.dev/posts/fastapi-with-apikeys/

    def check_token_admin(self, token: str = Depends(oauth2_scheme)) -> None:
        # For endpoints that are only available to administrators
        if not self.is_admin(token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Forbidden"
            )

    def check_token_read(self, token: str = Depends(oauth2_scheme)) -> None:
        # For endpoints that are only available to those with read access
        if not self.has_read_permissions(token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Forbidden"
            )

    def check_token_alert(self, token: str = Depends(oauth2_scheme)) -> None:
        # For the alert endpoint
        if not self.has_alert_permissions(token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Forbidden"
            )

    def is_admin(self, token: str) -> bool:
        perms = self.get_token_permissions(token)

        if perms['admin']:
            return True

        return False

    def has_read_permissions(self, token: str) -> bool:
        perms = self.get_token_permissions(token)

        # Admin has permission, regardless of what the rest of their permissions state
        if perms['admin']:
            return True

        if perms['readOnly']:
            return True

        return False

    def has_alert_permissions(self, token: str) -> bool:
        perms = self.get_token_permissions(token)

        # Admin has permission, regardless of what the rest of their permissions state
        if perms['admin']:
            return True

        if perms['alertOnly']:
            return True

        return False

    def get_token_permissions(self, token: str) -> dict:
        # Start out with a complete denial of permissions
        # Any additional info in the token will also be returned
        # admin: All permissions
        # readOnly: Can only obtain forecast information (cannot POST alerts)
        # alertOnly: Can only POST alerts (cannot retrieve forecast information)
        result = {"admin": False, "readOnly": False, "alertOnly": False, "info": None}

        try:
            users = self.config['server']['users']
        except KeyError:
            # If the keys are not configured for whatever reason, deny all permissions
            logging.error("The users in the config file are not configured correctly")
            return result

        for test_user in users:
            if test_user['token'] == token:
                result['info'] = test_user
                if "admin" in test_user:
                    result['admin'] = test_user['admin']
                if "readOnly" in test_user:
                    result['readOnly'] = test_user['readOnly']
                if "alertOnly" in test_user:
                    result['alertOnly'] = test_user['alertOnly']

                break

        return result

    # BEGIN API CALLBACKS
    def admin_get_cache(self) -> dict:
        # /admin/cache
        return {"locations": locations, "coordinates": coordinates, "weather_info": weather_info,
                "offices": offices, "offices_locations": offices_locations}

    def admin_clear_cache(self) -> dict:
        global locations, coordinates, weather_info, offices, offices_locations

        locations = {}
        coordinates = {}
        weather_info = {}
        offices = {}
        offices_locations = {}

        return {"success": True}

    def admin_get_tokens(self) -> dict:
        result = []
        try:
            users = self.config['server']['users']
        except KeyError:
            # If the keys are not configured for whatever reason, deny all permissions
            logging.error("The users in the config file are not configured correctly")
            return {}

        admin_users = 0
        for user in users:
            # Don't display admin users
            # We will count them, however
            if self.is_admin(user['token']):
                admin_users += 1
                continue

            result.append(user)

        return {"admin_users": admin_users, "users": result}

    def admin_delete_token(self, token: str) -> dict:
        if self.is_admin(token):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden. Cannot remove admin tokens. Please see the configuration YAML file."
            )

        try:
            users = self.config['server']['users']
        except KeyError:
            # If the keys are not configured for whatever reason, deny all permissions
            logging.error("The users in the config file are not configured correctly")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="The users in the config file are not configured correctly"
            )

        for index, user in enumerate(users):
            if user['token'] == token:
                del self.config['server']['users'][index]
                return {"success": True}

        # If we made it to this point, then the provided token was invalid
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"The token {token} was not found"
        )

    def admin_create_token(self, token_type: TokenType) -> dict:
        user = {}
        if token_type is TokenType.readOnly:
            user['readOnly'] = True
        elif token_type is TokenType.alertOnly:
            user['alertOnly'] = True
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid token type: {token_type.value}"
            )

        user['token'] = str(uuid.uuid4())

        self.config['server']['users'].append(user)

        return user

    def admin_modify_token(self, token: str, payload: Token) -> dict:
        if self.is_admin(token):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden. Cannot remove admin tokens. Please see the configuration YAML file."
            )

        try:
            users = self.config['server']['users']
        except KeyError:
            # If the keys are not configured for whatever reason, deny all permissions
            logging.error("The users in the config file are not configured correctly")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="The users in the config file are not configured correctly"
            )
        found = None
        found_index = None
        for index, user in enumerate(users):
            if user['token'] == token:
                found = user
                found_index = index
                break

        if found is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"The token {token} was not found"
            )

        user = users[found_index]
        if payload.name is not None:
            user['name'] = payload.name

        if payload.readOnly is not None:
            user['readOnly'] = payload.readOnly

        if payload.alertOnly is not None:
            user['alertOnly'] = payload.alertOnly

        self.config['server']['users'][found_index] = user
        return {"success": True}

    def admin_save_config(self) -> dict:
        self.config.save()
        return {"success": True}

    def get_all_forecast_info(self, payload: Payload) -> dict:
        # /all
        return get_weather(payload, config=self.config)

    def get_forecast_info(self, payload: Payload) -> dict:
        # /forecast
        return get_weather(payload, config=self.config)['forecast']

    def get_hourly_forecast(self, payload: Payload) -> dict:
        # /hourly
        return get_weather(payload, config=self.config)['hourly']

    def get_hazardous_weather_outlook(self, payload: Payload) -> list | None:
        # /hwo
        start = int(time.time())
        hwo = get_hwo(payload, config=self.config)
        end = int(time.time())

        if verbosity > 3:
            logging.debug(f"get_hwo() took {end - start} seconds")

        return hwo

    def get_spotter_activation_statement(self, payload: Payload) -> list | None:
        # /spotter
        hwo = get_hwo(payload, config=self.config)['hwo']
        if hwo is None or not hwo:
            return None
        spotter = []
        for item in hwo:
            spotter.append(item['spotter'])

        return spotter

    def run_actions(self, actions: list, post: dict = None) -> int:
        action_counter = 0
        for action in actions:
            # Verify the type is set
            if "type" not in action:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="No type defined for the action. Check alerts configuration."
                )

            # Determine what type of action it is
            if action['type'] == "webhook":
                # Verify the data section exists in the config
                if "data" not in action:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="No data defined for the action. Check alerts configuration."
                    )

                # Verify that a webhook URL was specified
                if "url" not in action['data']:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="No URL defined for the webhook."
                    )

                # Determine the request method to use
                if "method" not in action['data']:
                    # A method wasn't specified, so default to GET
                    method = "get"
                elif not action['data']['method']:
                    # A method was provided, but not set, so default to GET
                    method = "get"
                else:
                    method = action['data']['method'].lower()

                headers = []

                if "headers" in action['data']:
                    headers = action['data']['headers']

                url = action['data']['url']
                error = None

                # If an error occurs while connecting, set the error value and set r to None
                # The error value will be sent to the user
                if method == "get":
                    try:
                        r = requests.get(url, headers=headers)
                    except ConnectionError as e:
                        error = e
                        r = None

                elif method == "post":
                    try:
                        if post is None:
                            r = requests.post(url, headers=headers)
                        else:
                            r = requests.post(url, headers=headers, json=post)
                    except ConnectionError as e:
                        error = e
                        r = None

                elif method == "put":
                    try:
                        r = requests.put(url, headers=headers)
                    except ConnectionError as e:
                        error = e
                        r = None

                # Define any other methods to support here

                else:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Unsupported method {method}"
                    )

                # An error occurred with the requests above, so send to the user
                if r is None:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=str(error)
                    )

                # If the webhook returns a non-200 status code, echo that status code back to the user
                if not r.ok:
                    raise HTTPException(
                        status_code=r.status_code,
                        detail=f"Webhook error. Webhook returned status code {r.status_code}"
                    )

                action_counter += 1

        return action_counter

    def receive_dsame_alert(self, payload: DsamePayload) -> dict:
        # /alert
        alert_type = payload.EEE
        same_list = payload.PSSCCC_list

        # Determine the severity of the alert
        # Assign a default severity of None if not found
        severity = None

        if alert_type in severity_warn:
            severity = "warning"
        elif alert_type in severity_watch:
            severity = "watch"
        elif alert_type in severity_advisory:
            severity = "advisory"
        elif alert_type in severity_test:
            severity = "test"

        # If the alert is of unknown severity, throw a 400 Bad Request
        if severity is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown alert type: {alert_type}"
            )

        # Obtain the alerts from the configuration and check if they have been configured
        alerts: dict = self.config.get_value("alerts")
        if alerts is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Alerts have not been configured"
            )

        action_count = 0  # Counter that is incremented each time an action is ran

        # Run actions for alerts based on severity
        if "severity" in alerts:
            if severity in alerts['severity']:
                # Make sure we don't have an empty array
                if alerts['severity'][severity]:
                    logging.debug(f"Running actions for severity {severity}")
                    action_count += self.run_actions(alerts['severity'][severity], post=payload.model_dump())

        # Run actions for alerts based on the type
        if "types" in alerts:
            if alert_type in alerts['types']:
                logging.debug(f"Running actions for alert type {alert_type}")
                action_count += self.run_actions(alerts['types'], post=payload.model_dump())

        # Run actions for alerts based on the SAME code
        if "same" in alerts:
            if alerts['same']:
                for same in same_list:
                    # If the current SAME code is not in the list for alerts, skip it
                    if same not in alerts['same']:
                        if verbosity > 2:
                            logging.debug(f"Skipping SAME code {same} as it is not in the config")
                        continue

                    entry = alerts['same'][same]
                    if "actions" in entry:
                        if entry['actions']:
                            logging.debug(f"Running 'actions' section for SAME code {same}")
                            action_count += self.run_actions(entry['actions'], post=payload.model_dump())

                    if "severity" in entry:
                        if severity in entry['severity']:
                            logging.debug(f"Running 'severity' section for SAME code {same}")
                            action_count += self.run_actions(entry['severity'][severity], post=payload.model_dump())

                    if "types" in entry:
                        if alert_type in entry['types']:
                            logging.debug(f"Running alert type '{alert_type}' section for SAME code {same}")
                            action_count += self.run_actions(entry['types'][alert_type], post=payload.model_dump())

        return {"actions": action_count}

    # END API CALLBACKS


if __name__ == "__main__":
    import uvicorn

    cfg = load()
    app = FastAPI()
    api = APIv1(app=app, config=cfg)

    address = str(cfg.get_value("server.address"))
    port = int(cfg.get_value("server.port"))
    uvicorn.run(app, host=address, port=port, log_level=cfg.log_level)

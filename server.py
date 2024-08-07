import logging
import time

from fastapi import FastAPI, APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel

import forecast
from config import Config, ConfigError, load

MAX_LEN = 128  # Maximum allowed length of a JSON POST payload
CACHE_TIME = 5  # Time to cache the forecast information, in minutes
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


class Payload(BaseModel):
    lat: float | str | None = None
    lon: float | str | None = None
    city: str | None = None
    state: str | None = None


def get_location_info(lat_lon: tuple) -> bool:
    """
    Call the point endpoint of the NWS API to obtain information for the provided coordinates.
    :param lat_lon: Tuple of latitude and longitude coordinates.
    :return: False if get_point() returns an error or True otherwise.
    """

    logging.debug(f"Calling get_location_info(lat_lon: {lat_lon})")
    lat, lon = lat_lon
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

    return True


def get_location_grid(lat_lon: tuple) -> tuple | None:
    """
    Retrieves the grid X and Y coordinates of the given latitude and longitude.
    :param lat_lon: Tuple containing latitude and longitude.
    :return: Tuple of X, Y coordinates if found. None if not found.
    """
    lat, lon = lat_lon

    # Convert the latitude and longitude to a string if they were provided as an integer
    # This helps make behavior more consistent.
    if type(lat) is int:
        lat = str(lat)
    if type(lon) is int:
        lon = str(lon)

    try:
        info = coordinates[lat][lon]
        state = info['state']
        city = info['city']
        return locations[state][city]
    except KeyError:
        return None


def refresh_weather(gridXY: tuple, office: str) -> dict | None:
    """
    Refreshes weather information by calling the appropriate NWS API endpoints.
    :param gridXY: Tuple containing grid X, Y coordinates that can be obtained from the point API.
    :param office: NWS office to obtain data from.
    :return: Dictionary containing the hourly and regular forecasts, hazardous weather outlook, and update timestamp.
    """
    logging.debug(f"Calling refresh_weather(gridXY: {gridXY}, office: {office})")
    fc = forecast.Forecast()
    hourly = fc.get_forecast_hourly(gridXY=gridXY, office=office)

    if hourly is None:
        return None

    regular = fc.get_forecast(gridXY=gridXY, office=office)

    if regular is None:
        return None

    try:
        fc.office = office
        office_info = offices_locations[office]
        fc.office_city = office_info['city']
        fc.office_state = office_info['state']
    except KeyError:
        logging.error(f"Unable to locate information for {office} in the office location cache.")
        return None

    hwo = fc.get_hwo()
    timestamp = int(time.time())

    data = {"hourly": hourly, "forecast": regular, "hwo": hwo, "time": timestamp}

    x, y = gridXY
    weather_info[office][x][y] = data
    return data


def parse_payload(payload: dict) -> tuple | int | None:
    """
    Parses the user-provided JSON to obtain the location information and add it to the cache if not found.
    :param payload: Dictionary containing city, state, latitude, and longitude.
    :return: Tuple containing x and y coordinates, city, and state on success. None or -1 on failure.
    """
    logging.debug(f"Calling parse_payload(payload: {payload})")
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
            payload_lat = str(payload['lat'])
            payload_lon = str(payload['lon'])

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
            lat = str(payload['lat'])
            lon = str(payload['lon'])
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


def get_weather(payload_model: Payload) -> dict | None:
    """
    Fetches the weather from the cache or calls the API to refresh the cache if necessary.
    :param payload_model: Model from user input that contains the latitude, longitude, city, and state of the request.
    :return: Dictionary of weather data or None on error.
    """
    payload = payload_model.dict()
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
    # If it was just crated above, then the below check should fail and not be called
    now = int(time.time())

    if weather['time'] < now - CACHE_TIME * 60:
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

    def __init__(self, app: FastAPI, config: Config):
        self.app = app
        self.router = APIRouter()
        self.config = config

        # Check that the config file has a "server" section and that the API key was specified in it
        if "server" not in self.config:
            raise ConfigError("No server configuration options were provided in the configuration file")

        if "key" not in self.config['server']:
            raise ConfigError("Please provide a key in the 'server' section of the configuration file")

        if not self.config['server']['key']:
            raise ConfigError("Please provide a key in the 'server' section of the configuration file")

        # Define routers for the API
        # These are standard read-only methods (they can't change anything but add data to the cache)
        self.router.add_api_route("/all", self.get_all, methods=["POST"], dependencies=[Depends(self.check_token)],
                                  description="Obtain all available forecast information from the NWS")
        self.router.add_api_route("/forecast", self.get_forecast, methods=["POST"],
                                  dependencies=[Depends(self.check_token)],
                                  description="Obtain only the daily forecast information from the NWS")
        self.router.add_api_route("/hourly", self.get_hourly, methods=["POST"],
                                  dependencies=[Depends(self.check_token)],
                                  description="Obtain only the hourly forecast information from the NWS")
        self.router.add_api_route("/hwo", self.get_hwo, methods=["POST"], dependencies=[Depends(self.check_token)],
                                  description="Parse and obtain the Hazardous Weather Outlook from the NWS")
        self.router.add_api_route("/spotter", self.get_spotter, methods=["POST"],
                                  dependencies=[Depends(self.check_token)],
                                  description="Parse the Hazardous Weather Outlook and only obtain the Spotter "
                                              "Activation Statement")
        # TODO: Token that can ONLY send alerts
        self.router.add_api_route("/alert", self.receive_alert, methods=["POST"],
                                  dependencies=[Depends(self.check_token)], description="Receive an alert from dsame3")

        # Administrative methods
        # These can change server configuration options, so they will have a different token check
        # TODO: Add/remove read-only tokens, clear the cache, check the cache

        self.app.include_router(self.router, prefix=f"/api/{self.version}")

    def check_token(self, key: str = Depends(oauth2_scheme)):
        # Protected endpoint example: https://testdriven.io/tips/6840e037-4b8f-4354-a9af-6863fb1c69eb/
        # Another API key example: https://timberry.dev/posts/fastapi-with-apikeys/
        # TODO: Handle multiple keys
        if key != self.config['server']['key']:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Forbidden"
            )

    # BEGIN API CALLBACKS
    def get_all(self, payload: Payload):
        # /all
        return get_weather(payload)

    def get_forecast(self, payload: Payload):
        # /forecast
        return get_weather(payload)['forecast']

    def get_hourly(self, payload: Payload):
        # /hourly
        return get_weather(payload)['hourly']

    def get_hwo(self, payload: Payload):
        # /hwo
        return get_weather(payload)['hwo']

    def get_spotter(self, payload: Payload):
        # /spotter
        hwo = get_weather(payload)['hwo']
        spotter = []
        for item in hwo:
            spotter.append(item['spotter'])

        return spotter

    def receive_alert(self, payload: Payload):
        # /alert
        # TODO: Implement alerts
        return {"alert": "success", "payload": payload.dict()}

    # END API CALLBACKS


if __name__ == "__main__":
    import uvicorn

    cfg = load()
    app = FastAPI()
    api = APIv1(app=app, config=cfg)
    uvicorn.run(app, host=cfg['server']['address'], port=cfg['server']['port'], log_level=cfg.log_level)

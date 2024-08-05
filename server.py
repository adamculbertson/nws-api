import http.server
import json
import logging
import time

from fastapi import FastAPI

import forecast
from config import ConfigError, load

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
    fc = forecast.Forecast({})
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


class Server(http.server.HTTPServer):
    def __init__(self, server_address, handler_class, config):
        super().__init__(server_address, handler_class)
        # TODO: Implement browser cache-control
        self.hashes = {}  # Hashes for browser cache-control
        self.config = config

        # Check that the config file has a "server" section and that the API key was specified in it
        if "server" not in self.config:
            raise ConfigError("No server configuration options were provided in the configuration file")

        if "key" not in self.config['server']:
            raise ConfigError("Please provide a key in the 'server' section of the configuration file")

        if not self.config['server']['key']:
            raise ConfigError("Please provide a key in the 'server' section of the configuration file")

    def reload(self):
        self.hashes = {}


class RequestHandler(http.server.BaseHTTPRequestHandler):
    server: Server

    def log_message(self, format, *args) -> None:
        logging.info("%s - - [%s] %s\n" %
                     (self.address_string(),
                      self.log_date_time_string(),
                      format % args))

    def send_status_code(self, code: int, message: str = None):
        self.send_response(code)
        cl = 0  # Content-Length value

        status = json.dumps({"error": 400, "message": message})
        if message is not None:
            cl = len(status)

        cl = str(cl)
        self.send_header("Content-Length", cl)
        self.end_headers()
        if message is not None:
            self.wfile.write(status.encode("utf-8"))

    def send_no_content(self):
        self.send_status_code(204)

    def send_bad_request(self, message: str = None):
        self.send_status_code(400, message)

    def send_forbidden(self, message: str = None):
        self.send_status_code(403, message)

    def send_not_found(self, message: str = None):
        self.send_status_code(404, message)

    def send_not_modified(self, tag=None):
        self.send_response(304)
        self.send_header("Content-Length", "0")
        if tag is not None:
            self.send_header("ETag", tag)
        self.end_headers()

    def send_json(self, data, status=200):
        js = json.dumps(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(js)))
        self.end_headers()
        self.wfile.write(js.encode("utf-8"))

    def do_POST(self):
        # Check for and get the authorization token
        auth = self.headers.get("Authorization")

        # If the authorization token was not specified, send a forbidden
        # All API requests require the token
        if auth is None:
            self.send_forbidden(message="Missing token")
            return

        # Check for a properly formatted token
        # The token should be in the format "Bearer token"
        # "token" is the token data
        if not auth.startswith("Bearer"):
            self.send_bad_request(message="Invalid token format")
            return

        # Try to split the token by a space, to get "Bearer" and the token value
        # If the length of the split list is not 2, then it is improperly formatted
        tokens = auth.split(" ")
        if len(tokens) != 2:
            self.send_bad_request(message="Invalid token format")
            return

        # Finally, compare the token to that in the configuration data
        # Instead of sending a forbidden message, send bad request if the token does not match
        if tokens[1] != self.server.config['server']['key']:
            self.send_bad_request(message="Bad token")
            return

        #  Get the payload sent by the client
        ln = int(self.headers.get("Content-Length"))

        # Determine if the payload is of a safe length
        if ln > MAX_LEN:
            self.send_bad_request(message="Payload too large")
            return

        content = self.rfile.read(ln)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            self.send_bad_request(message="JSON decoding error")
            return

        # Parse the path into sections
        # In case we're running behind a reverse proxy, strip out the "api" part of the path.
        paths = self.path.split("/")
        paths.pop(0)
        if paths[0] == "api":
            paths.pop(0)

        # If the path is /weather remove that
        if len(paths) > 0:
            if paths[0] == "weather":
                paths.pop(0)

        # No path was specified, so send an invalid request.
        if len(paths) == 0:
            self.send_bad_request(message="No path specified")
            return

        # Don't bother sending a favicon. Just reply with not found.
        elif paths[0] == "favicon.ico":
            self.send_not_found()
            return

        elif paths[0] == "all":
            pass
            weather = self.get_weather(payload)
            self.send_json(weather)

        elif paths[0] == "forecast":
            weather = self.get_weather(payload)
            self.send_json(weather['forecast'])

        elif paths[0] == "hourly":
            weather = self.get_weather(payload)
            self.send_json(weather['hourly'])

        elif paths[0] == "hwo":
            weather = self.get_weather(payload)
            self.send_json(weather['hwo'])

        elif paths[0] == "spotter":
            weather = self.get_weather(payload)
            spotter = []
            for item in weather['hwo']:
                spotter.append(item['spotter'])
            self.send_json(spotter)

        # Any other request is considered invalid.
        else:
            self.send_bad_request(message="Invalid path")
            return

    def do_GET(self):
        # All endpoints are POST requests, so all GET requests are invalid
        self.send_bad_request()

    def get_weather(self, payload: dict) -> dict | None:
        """
        Fetches the weather from the cache or calls the API to refresh the cache if necessary.
        :param payload: Dictionary that contains the latitude, longitude, city, and state of the request.
        :return: Dictionary of weather data or None on error.
        """
        result = None
        try:
            result = parse_payload(payload)
            x, y, city, state = result
        except TypeError:
            # If None, then the location couldn't be found in the cache and it could not be determined
            if result is None:
                self.send_not_found(message="Not found. Please try specifying coordinates instead")
                return None

            # Any other value is a bad request
            self.send_bad_request(message="Invalid parameters")
            return None

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
                self.send_bad_request(message=f"Unable to obtain weather information for the coordinates {x}, {y}")
                return None

        # Check if the forecast has been cached recently
        # If it was just crated above, then the below check should fail and not be called
        now = int(time.time())

        if weather['time'] < now - CACHE_TIME * 60:
            weather = refresh_weather((x, y), office)

            if weather is None:
                self.send_bad_request(message=f"Unable to obtain weather information for the coordinates {x}, {y}")
                return None

        return weather


def setup(address, config):
    return Server(address, RequestHandler, config)


if __name__ == "__main__":
    cfg = load()
    s = Server((cfg['server']['address'], cfg['server']['port']), RequestHandler, cfg)
    s.serve_forever()

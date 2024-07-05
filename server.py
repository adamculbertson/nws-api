import http.server
import json
import os
import hashlib
import logging
import time
import datetime
import threading
import copy

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


class RequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args) -> None:
        logging.info("%s - - [%s] %s\n" %
                     (self.address_string(),
                      self.log_date_time_string(),
                      format % args))

    def send_no_content(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_bad_request(self):
        self.send_response(400)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_forbidden(self):
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_not_found(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

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
            self.send_forbidden()
            return

        # Check for a properly formatted token
        # The token should be in the format "Bearer token"
        # "token" is the token data
        if not auth.startswith("Bearer"):
            self.send_bad_request()
            return

        # Try to split the token by a space, to get "Bearer" and the token value
        # If the length of the split list is not 2, then it is improperly formatted
        tokens = auth.split(" ")
        if len(tokens) != 2:
            self.send_bad_request()
            return

        # Finally, compare the token to that in the configuration data
        # Instead of sending a forbidden message, send bad request if the token does not match
        if tokens[1] != self.server.config['server']['key']:
            print("bad token")
            self.send_bad_request()
            return

        #  Get the payload sent by the client
        ln = int(self.headers.get("Content-Length"))

        # Determine if the payload is of a safe length
        if ln > MAX_LEN:
            self.send_bad_request()
            return

        content = self.rfile.read(ln)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            self.send_bad_request()
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
            self.send_bad_request()
            return

        # Don't bother sending a favicon. Just reply with not found.
        elif paths[0] == "favicon.ico":
            self.send_not_found()
            return

        elif paths[0] == "all":
            weather = self.get_weather(payload)
            data = self.prepare_json(weather)
            self.send_json(data)

        elif paths[0] == "forecast":
            weather = self.get_weather(payload)
            data = self.prepare_json(weather)
            self.send_json(data['forecast'])

        elif paths[0] == "current":
            weather = self.get_weather(payload)
            data = self.prepare_json(weather)
            self.send_json(data['observations'])

        elif paths[0] == "hwo":
            weather = self.get_weather(payload)
            data = self.prepare_json(weather)
            self.send_json(data['hwo'])

        elif paths[0] == "spotter":
            weather = self.get_weather(payload)
            spotter = []
            for item in weather['hwo']:
                spotter.append(item['spotter'])
            self.send_json(spotter)

        # Any other request is considered invalid.
        else:
            self.send_bad_request()
            return

    def do_GET(self):
        self.send_bad_request()

    def get_weather(self, payload):
        try:
            lat, lon, city, state = self.parse_payload(payload)
            # iOS Shortcuts app sends the latitude and longitude as an integer
            lat = str(lat)
            lon = str(lon)
        except TypeError:
            result = self.parse_payload(payload)
            if result is None:
                self.send_not_found()
                return None
            self.send_bad_request()
            return None

        # Check if the forecast has been cached recently
        weather = None
        now = int(time.time())
        index = 0
        for entry in self.server.weather:
            test_lat = entry['forecast']['location']['coords']['lat']
            test_lon = entry['forecast']['location']['coords']['lon']

            if test_lat == lat and test_lon == lon:
                # If the entry is less than CACHE_TIME, then use that entry
                # Otherwise, don't modify weather and it will be updated later
                if now - entry['cached'] < CACHE_TIME * 60:
                    weather = copy.deepcopy(entry)  # Deep copy because we want to remove the cache information
                    del weather['cached']
                else:
                    # Stale cache, delete it
                    del self.server.weather[index]
                break
            index += 1

        # The weather is either too old in the cache or not cached if weather is None
        if weather is None:
            print(f"Fetching forecast information...: {lat}, {lon}")
            cfg = load(data={"forecast": {"lat": lat, "lon": lon, "unit": self.server.config['forecast']['unit'],
                                          "lang": self.server.config['forecast']['lang']}})
            fc = forecast.Forecast(cfg)
            fc.load()
            weather = fc.weather
            weather['cached'] = now  # Update cache timer
            self.server.weather.append(weather)

        # Check the location cache for these values, and update the cache if it wasn't found
        new_lat = weather['forecast']['location']['coords']['lat']
        new_lon = weather['forecast']['location']['coords']['lon']

        if city is not None and state is not None:
            if self.find_location_info(new_lat, new_lon) is None:
                location = {"lat": new_lat, "lon": new_lon, "city": city, "state": state}
                self.server.locations.append(location)

        return weather

    def parse_payload(self, payload):
        # If the city and state are specified in the payload, try them first
        city, state = None, None
        if "city" in payload and "state" in payload:
            # Check if the city and state's coordinates are in the cache
            coords = self.find_location_coords(payload['city'], payload['state'])
            if coords is None and "lat" not in payload and "lon" not in payload:
                # No coordinates in the cache, and the latitude and longitude were not specified
                return None  # Causes a 404 error to be sent to the client
            elif coords is None and "lat" in payload and "lon" in payload:
                # Coordinates were provided, so use them instead
                coords = payload

            city = payload['city']
            state = payload['state']
            lat = coords['lat']
            lon = coords['lon']

        else:
            # Determine if the latitude AND longitude were specified by the client and send an error if not
            try:
                lat = payload['lat']
                lon = payload['lon']
            except KeyError:
                return -1  # Causes a 400 error to be sent to the client

        return lat, lon, city, state

    def find_location_info(self, lat, lon):
        """
        Locates city and state information about the provided latitude and longitude from the cache
        :param lat: Latitude of the location
        :param lon: Longitude of the location
        :return: None if not found, or a dictionary containing the city, state, and coordinates
        """
        for location in self.server.locations:
            if str(location['lat']) == str(lat) and str(location['lon']) == str(lon):
                return location
        return None

    def find_location_coords(self, city, state):
        """
        Locates the coordinates for the given city and state from the cache
        :param city: City to search for
        :param state: State to search for
        :return: None if not found, or a dictionary containing the city, state, and coordinates
        """
        for location in self.server.locations:
            if location['city'] == city and location['state'] == state:
                return location
        return None

    def find_time(self, name, times):
        """
        Loops through a list of times and searches for the one that matches the given name
        :param name: Name to search for, which is usually a day of the week
        :param times: List of time formats to search through
        :return: None if not found, or the time that matches the given name
        """
        for i in range(len(times)):
            sname, value = times[i]
            if sname == name:
                return value
        return None

    def prepare_json(self, data):
        """
        Converts the dictionary from forecast.load() into a format better suited for JSON output
        :param data: Dictionary containing results from forecast.load()
        :return: Modified dictionary suitable for JSON output
        """
        result = {"date": data['product']['date'], "location": data['forecast']['location'],
                  "source": {"location": data['source']['location'], "url": data['source']['url']}, 'forecast': {}}

        times = data['forecast']['times']  # Used to get the values for the time slots, but is not included
        # in the final output

        for item in data['forecast']['parameters']:
            # The 'parameters' entry is removed completely, and is instead just under 'forecast'
            if item == "temperature":
                if "temperature" not in result['forecast']:
                    result['forecast']['temperature'] = {}

                # Change "minimum" to "low"
                if "minimum" in data['forecast']['parameters']['temperature']:
                    min = data['forecast']['parameters']['temperature']['minimum']
                    result['forecast']['temperature']['low'] = self.get_values_for_time(times, min)

                # Change "maximum" to "high"
                if "maximum" in data['forecast']['parameters']['temperature']:
                    max = data['forecast']['parameters']['temperature']['maximum']
                    result['forecast']['temperature']['high'] = self.get_values_for_time(times, max)

            elif item == "precip":
                precip = data['forecast']['parameters']['precip']
                result['forecast']['precip'] = self.get_values_for_time(times, precip)

            elif item == "weather":
                weather = data['forecast']['parameters']['weather']
                result['forecast']['weather'] = self.get_values_for_time(times, weather)

            elif item == "icons":
                icons = data['forecast']['parameters']['icons']
                result['forecast']['icons'] = self.get_values_for_time(times, icons)

            elif item == "worded":
                worded = data['forecast']['parameters']['worded']
                result['forecast']['worded'] = self.get_values_for_time(times, worded)

        result['observations'] = data['observations']  # Current observations is mostly the same
        # Remove point information that we do not need
        if "point" in result['observations']['location']:
            del result['observations']['location']['point']
        if "point" in result['location']:
            del result['location']['point']

        result['hwo'] = data['hwo']

        return result

    def get_values_for_time(self, times, values):
        """
        Gets the list of values and groups it based on the given time forma.
        :param times: Time format to use
        :param values: Dictionary of data containing the values to add the time data to
        :return: List of modified values
        """
        result = []

        for name in values['values']:
            time_value = self.find_time(name, times[values['times']])
            result.append({"value": values['values'][name], "time": time_value, "time_text": name})

        return result


class Server(http.server.HTTPServer):
    def __init__(self, server_address, handler_class, config):
        super().__init__(server_address, handler_class)
        self.hashes = {}  # Hashes for browser cache-control
        self.locations = []  # Cache information that contains City/State/County information and coordinates
        self.weather = []  # Cache information that holds recently retrieved weather forecasts
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


def setup(address, config):
    return Server(address, RequestHandler, config)


if __name__ == "__main__":
    cfg = load()
    s = Server((cfg['server']['address'], cfg['server']['port']), RequestHandler, cfg)
    s.serve_forever()
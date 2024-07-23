import logging
from datetime import datetime

import requests
from bs4 import BeautifulSoup

# URL to obtain the Hazardous Weather Outlook
# OFFICE will be converted to the appropriate NWS office to use
HWO = "https://forecast.weather.gov/wwamap/wwatxtget.php?cwa={OFFICE}&wwa=hazardous%20weather%20outlook"

# NWS API: https://api.weather.gov/openapi.json

BASE_URL = "https://api.weather.gov"
OFFICE_URL = BASE_URL + "/offices/{OFFICE}"
POINTS_URL = BASE_URL + "/points/{LAT},{LON}"
ADVISORIES_URL = BASE_URL + "/alerts/active/area/{STATE}"  # TODO
FORECAST_URL = BASE_URL + "/gridpoints/{OFFICE}/{X},{Y}/forecast"  # X and Y are grid coordinates obtained from points
FORECAST_URL_HOURLY = FORECAST_URL + "/hourly"

"""
Steps for retrieving forecast information
1. Get the office name or retrieve from cache. Call get_point((lat, lon)) to get this info.
2. If not cached, get the city and state for the NWS office via get_office_info()
3. Call get_hwo() or any other forecast item.
"""


class Forecast:
    def __init__(self, config: dict = None):
        if config is None:
            config = {}

        self.config = config
        self.lat_lon = ()  # Provided coordinates
        self.city_lat_lon = ()  # Coordinates of the city and state for the provided ones
        self.grid = ()  # Grid location of the city coordinates
        self.office = None  # National Weather Service office that is responsible for the grid location
        self.office_city = None  # City of the NWS office
        self.office_state = None  # State of the NWS office
        self.city = None
        self.state = None
        self.weather = {}

        # Determine if the office is in the configuration
        if "office" in config:
            if config['office'] is not None:
                self.office = config['office']

    def get_point(self, lat_lon: tuple = None) -> int:
        """
        Get the office, grid, and city/state information for a given point.
        :param lat_lon: Use the given latitude and longitude tuple instead of what is already stored.
        :return: 0 unless a latitude/longitude pair could not be obtained.
        """

        # TODO: Use GeoPandas or python-geojson for this and the forecasts?
        # This may allow for determining if a coordinate is in a given point or not

        # If a latitude/longitude tuple was specified, override the currently stored one
        if lat_lon is not None:
            self.lat_lon = lat_lon

        # No latitude/longitude pair stored or provided, so return -1 to indicate an error.
        if not self.lat_lon:
            return -1

        latitude, longitude = self.lat_lon

        # Generate the URL based on the latitude and longitude
        url = POINTS_URL.replace("{LAT}", latitude) \
            .replace("{LON}", longitude)

        r = requests.get(url)
        r.raise_for_status()

        data = r.json()

        # Get grid X/Y coordinates, office (cwa), and city/state
        self.office = data['properties']['cwa']
        self.grid = data['properties']['gridX'], data['properties']['gridY']
        self.city = data['properties']['relativeLocation']['properties']['city']
        self.state = data['properties']['relativeLocation']['properties']['state']

        # Seems the API returns the coordinates backwards? At least it does in my tests
        self.city_lat_lon = (data['properties']['relativeLocation']['geometry']['coordinates'][1],
                             data['properties']['relativeLocation']['geometry']['coordinates'][0])

        return 0

    def get_office_info(self, office: str = None) -> int:
        """
        Get the location of the NWS office specified.
        :param office: Use the provided office value instead of what was stored.
        :return: 0 unless the office value could not be obtained.
        """

        # Overrides the stored office value to the given value
        if office is not None:
            self.office = office

        # No office value was provided and one is not stored, so return an error
        if self.office is None:
            return -1

        # Generate the URL based on the office
        url = OFFICE_URL.replace("{OFFICE}", self.office)

        r = requests.get(url)
        r.raise_for_status()

        data = r.json()
        name = data['name']

        # The location is in the format of "City, State", so we split based on that
        city_state = name.split(", ")

        self.office_city = city_state[0].strip()
        self.office_state = city_state[1].strip()

        return 0

    def load(self):
        # Obtains the standard forecast and hazardous weather outlook
        self.weather['forecast'] = self.get_forecast()
        self.weather['hwo'] = self.get_hwo()

    def get_forecast(self, gridXY: tuple = None, office: str = None, hourly: bool = False) -> dict | None:
        """
        Get the forecast, either hourly or weekly, from the National Weather Service.
        :param gridXY: Optional tuple containing the grid X and Y coordinates to get the forecast for
        :param office: Optional string containing the office to obtain the forecast from.
        :param hourly: If true, fetch the hourly forecast instead.
        :return: Dictionary containing forecast information.
        """

        forecast = {}

        # If no "office" value was provided, try to get it from the already present value
        if office is None:
            office = self.office
            # If there is no currently present office value, attempt to call the point endpoint to get it
            if office is None:
                result = self.get_point()
                # If get_point() returns anything but 0, then we were not able to retrieve the office information
                if result < 0:
                    logging.error("Could not determine office information")
                    return None

                office = self.office

        # If no grid coordinates were provided, then try to get them from the already present value
        if gridXY is None and self.grid:
            x, y = self.grid

        elif gridXY is not None:
            x, y = gridXY

        else:
            # We still do not have the grid coordinates, so try calling get_point() to retrieve them
            result = self.get_point()
            if result < 0:
                logging.error("Could not determine grid coordinates")
                return None

            x, y = self.grid

        url = FORECAST_URL
        if hourly:
            url = FORECAST_URL_HOURLY

        # Format the URL with the office, x, and y parameters
        url = url.replace("{OFFICE}", office).replace("{X}", str(x)).replace("{Y}", str(y))
        r = requests.get(url)
        r.raise_for_status()

        data = r.json()

        # As of right now, the coordinates are not used, but may be in the future
        forecast['coordinates'] = data['geometry']['coordinates']

        # Get the date and time the forecast was generated and updated
        # Format is ISO 8601
        forecast['updated'] = data['properties']['updateTime']
        forecast['generated'] = data['properties']['generatedAt']
        forecast['forecast'] = []

        # Hourly and regular forecast all have the same information
        # Make that information a bit less verbose and organize it a little differently
        for period in data['properties']['periods']:
            info = {'period': period['name'], 'start': period['startTime'], 'end': period['endTime'],
                    'daytime': period['isDaytime'],
                    'temperature': {"value": period['temperature'], "unit": period['temperatureUnit']},
                    'precipitation': 0}
            if period['probabilityOfPrecipitation']['value'] is not None:
                info['precipitation'] = period['probabilityOfPrecipitation']['value']
            info['wind'] = {"speed": period['windSpeed'], "direction": period['windDirection']}
            info['short'] = period['shortForecast']
            info['detailed'] = period['detailedForecast']

            forecast['forecast'].append(info)

        return forecast

    def get_forecast_hourly(self, gridXY: tuple = None, office: str = None) -> dict | None:
        """
        Helper function that simply calls get_forecast with the hourly parameter.
        :param gridXY: Optional tuple containing the grid X and Y coordinates to get the forecast for
        :param office: Optional string containing the office to obtain the forecast from.
        :return: Dictionary containing forecast information.
        """
        return self.get_forecast(gridXY=gridXY, office=office, hourly=True)

    def get_hwo(self, include_all: bool = False) -> list | None:
        """
        Obtain the Hazardous Weather Outlook for the stored location information.
        :param include_all: If True, don't restrict the HWO to the provided office.
        :return: List of data from the HWO (multiple if include_all) or None if any information is missing.
        """
        # TODO: Use a tuple to hold the office, city, and state rather than instead?

        data = []
        # If the office has not already been specified, try to determine it from the coordinates
        if self.office is None:
            # If the latitude and longitude tuple is not empty, try to get the point information from the API
            # Otherwise, we need that information to continue
            if not self.lat_lon:
                logging.error("No latitude or longitude set. Please set it in the configuration file")
                return None

            # Try to get the point information
            # If it is still None, then we need more information
            self.get_point(self.lat_lon)
            if self.office is None:
                logging.error(f"Failed to get point information twice using lat/lon: {self.lat_lon}")
                return None

        # Get the URL using the office value
        url = HWO.replace("{OFFICE}", self.office)

        r = requests.get(url)
        r.raise_for_status()
        html = r.text
        soup = BeautifulSoup(html, "html.parser")
        items = soup.find_all("pre", string=True)

        for item in items:
            hwo = {}
            lc = 0  # Line counter, only used for the date/time parser
            mode = None  # Determines what we are parsing, for multi-line parsers
            buffer = ""  # Buffer to hold the data as it's being processed
            additional = ""  # Any additional data, such as the affected time for day one and values for days 2-7

            for line in item.text.splitlines():
                lc += 1
                lower = line.lower()  # Lowercase line for easier checking
                if line == "" or line == " ":
                    # Once on a blank line, indicate that county parsing is done (but only if line count is more than 4)
                    # Don't skip if done, because the mode check will handle continuing
                    if mode == "county" and lc > 4:
                        # TODO: Parse the counties list
                        hwo['counties'] = buffer.strip()
                        buffer = ""
                        # Once completed with the county parsing, set the mode to parsing the affected areas
                        mode = "affected-areas"

                    elif mode == "affected-areas":
                        hwo['affected'] = buffer.strip()
                        buffer = ""
                        mode = None
                        continue

                    elif mode == "spotter-activation":
                        if buffer != "":
                            hwo['spotter'] = buffer.strip()
                            buffer = ""
                            mode = None
                            continue

                    else:
                        continue

                if lc == 1:
                    # Skip the first line, which usually just states "Hazardous Weather Outlook"
                    continue

                elif lc == 2:
                    # Get the National Weather Service office
                    # The line starts with "National Weather Service " (space at the end), so get rid of that
                    line = line.replace("National Weather Service ", "")
                    # Only the city and state (no comma separation) are left, so separate them by removing the spaces
                    city_state = line.split(" ")
                    state = city_state.pop(-1)  # State is the last item in the list, so pop it to get it
                    # All that remains is the city
                    # If the city name is one that contains spaces, then there will be more than one item in the list
                    # Join the list together by spaces so that we get the proper city name
                    city = " ".join(city_state)

                    # Check if we've previously obtained the weather information to get the office that we are
                    # looking for
                    # Setting include_all to True will skip the check
                    if self.office_state is not None and self.office_city is not None:

                        if not include_all and self.office_state != state:
                            # State doesn't match, so end line parsing
                            break

                        if not include_all and self.office_city != city:
                            # City doesn't match, so end line parsing
                            break

                    hwo['state'] = state
                    hwo['city'] = city

                elif lc == 3:
                    # We need to strip out the timezone information, as %Z is not reliable
                    # To do this, we split the line by spaces
                    # Typical format of the NWS date: 700 PM EDT Fri May 10 2024
                    # We pop the value at index 2, then join the array with spaces

                    arr = line.split(" ")
                    arr.pop(2)  # Removes the timezone information
                    line = " ".join(arr)  # Re-joins the array as the original string
                    hwo['datetime'] = datetime.strptime(line, "%I%M %p %a %b %d %Y").isoformat()

                    mode = "county"  # Sets the mode to county parser, as that should be next

                elif lower.startswith(".day one"):
                    mode = "day-one"
                    # Remove periods and the DAY ONE text to get the time period
                    additional = line.replace(".DAY ONE...", "").replace(".", "")

                elif lower.startswith(".days two through seven"):
                    # Finish parsing day one before parsing the rest
                    if mode == "day-one":
                        if buffer != "":
                            hwo['day1'] = {"period": additional, "info": buffer}
                            buffer = ""

                    mode = "days-two-seven"
                    info = line.replace(".DAYS TWO THROUGH SEVEN...", "").replace(".", "")
                    # Example: Saturday through Thursday
                    # Remove the " through " and just get the start and end days
                    period = info.split(" through ")
                    additional = {"start": period[0], "end": period[1]}

                elif lower.startswith(".spotter information statement"):
                    # Finish parsing days two through seven before parsing the rest
                    if mode == "days-two-seven":
                        hwo['day27'] = {"period": additional, "info": buffer}
                        buffer = ""
                        additional = ""
                    mode = "spotter-activation"

                elif lower.startswith("general storm motion of the day:"):
                    mode = "storm-motion"

                elif line.startswith("$$"):
                    # Indicates the end of the HWO for the given location, so stop parsing the lines
                    if mode == "storm-motion":
                        hwo['motion'] = buffer.strip()
                    break

                elif line.startswith("&&"):
                    # Indicates the end of the HWO for the given location, so stop parsing the lines
                    if mode == "storm-motion":
                        hwo['motion'] = buffer.strip()
                    break

                elif mode == "county" or mode == "affected-areas" or mode == "spotter-activation":
                    buffer += line + " "

                elif mode == "day-one" or mode == "days-two-seven" or mode == "storm-motion":
                    buffer += line + "\n"

            if hwo:
                data.append(hwo)

        return data

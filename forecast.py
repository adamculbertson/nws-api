import logging
import re
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# Try to set the timezone from the environment
# Fallback to UTC if one is not set
try:
    TZ = os.environ['TZ']
except KeyError:
    TZ = "UTC"

# NWS API: https://api.weather.gov/openapi.json

# Define API endpoints
BASE_URL = "https://api.weather.gov"
OFFICE_URL = BASE_URL + "/offices/{OFFICE}"
POINTS_URL = BASE_URL + "/points/{LAT},{LON}"
ADVISORIES_URL = BASE_URL + "/alerts/active/area/{STATE}"  # TODO
FORECAST_URL = BASE_URL + "/gridpoints/{OFFICE}/{X},{Y}/forecast"  # X and Y are grid coordinates obtained from points
FORECAST_URL_HOURLY = FORECAST_URL + "/hourly"
HWO_URL = BASE_URL + "/products/types/HWO/locations/{OFFICE}"

# Replace any newlines that occur within a paragraph, while allowing normal newlines
# Compile the expression to make it slightly more efficient and faster
# Example: newlines like\nthis\n\n
# Output: newlines like this\n\n
replace_inline_newline = re.compile(r'(?<!\n)\n(?!\n)')

# Matches a section of a HWO
# Need to take into account the extra information in "DAY 1" and "DAYS 2 THROUGH 7"

section_pattern = re.compile(
    r'''
    ^\.(?P<dot_header>[A-Z0-9 \-]+?)\.\.\.(?P<dot_description>.*?)$    # .SECTION...Description
    | ^(?P<colon_header>[A-Z0-9 \- ]+):$                               # SECTION:
    | ^(?P<divider>&&|\$\$)$                                          # && or $$
    ''',
    re.MULTILINE | re.VERBOSE
)

# Matches the list of zones from an HWO entry
zone_line_pattern = re.compile(r'^[A-Z]{3}\d{3}.*\d{3}', re.MULTILINE)

"""
Steps for retrieving forecast information
1. Get the office name or retrieve from cache. Call get_point((lat, lon)) to get this info.
2. If not cached, get the city and state for the NWS office via get_office_info()
3. Call get_hwo() or any other forecast item.
"""

def hwo_parse_headers(text: str) -> dict | None:
    """
    Parses information from the headers of a Hazardous Weather Outlook
    :param text: Contents of the Hazardous Weather Outlook (HWO)
    :return: Dict with message_number, wmo_code, site_id, datetime_utc, and awips_id (or None if not found)
    """
    # Normalize line endings
    lines = text.strip().splitlines()

    if len(lines) < 3:
        return None  # Not enough header lines

    message_number = lines[0].strip()

    # Match WMO header (e.g., FLUS43 KLMK)
    wmo_match = re.match(r'^([A-Z]{4}\d{2})\s+([A-Z]{4})\s+(\d{6})$', lines[1].strip())
    if not wmo_match:
        # Nothing found, return None
        return None

    wmo_code, site_id, date_code = wmo_match.groups()

    # Parse AWIPS ID
    awips_id = lines[2].strip()

    return {
        "message_number": message_number,
        "wmo_code": wmo_code,
        "site_id": site_id,
        #"issued": datetime.strptime(date_code, "%y%m%d%H%M").replace(tzinfo=timezone.utc).isoformat(),
        "issued": date_code,
        "awips_id": awips_id,
    }

def hwo_get_zones(line: str) -> list[str]:
    """
    Retrieves a formatted list of zones from the zone line.
    :param line: Encoded line to parse for zone IDs.
    :return: List of zone IDs
    """
    result = []
    current_prefix = None
    parts = line.replace('\n', '').split('-')

    for part in parts:
        match = re.match(r'([A-Z]{3})?(\d{3})(?:>(\d{3}))?', part)
        if match:
            prefix, start, end = match.groups()
            if prefix:
                current_prefix = prefix
            if not current_prefix:
                continue
            start_num = int(start)
            end_num = int(end) if end else start_num
            for z in range(start_num, end_num + 1):
                result.append(f"{current_prefix}{z:03d}")
    return result

def hwo_parse(text: str, zone_search: str = None) -> dict | list | None:
    """
    Parses the contents of the Hazardous Weather Outlook.
    :param zone_search: If specified, ignores HWO entries that do not match the specified Zone ID
    :param text: Raw, unparsed, content.
    :return: List (or dict if only one entry) of found entries in the HWO or None if none were found.
    """
    text = text.strip().replace('\r\n', '\n')

    # $$ indicates the end of one block (HWO entry)
    raw_blocks = re.split(r'^\$\$(?:\s*\n.*)?$', text, flags=re.MULTILINE)

    blocks = []

    for block in raw_blocks:
        block = block.strip()
        if not block:
            continue

        sections = {}
        # Search for various patterns (sections, zone IDs, intro paragraph)
        matches = list(section_pattern.finditer(block))
        zone_line_match = zone_line_pattern.search(block)

        intro_match = re.search(
            r'(This Hazardous Weather Outlook is for.*?)\n\n(?=\.[A-Z0-9 \-]+\.\.\.)',
            text,
            re.DOTALL
        )

        sections['intro'] = intro_match.group(1).replace('\n', ' ').strip()

        if zone_line_match:
            zone_line = zone_line_match.group(0)
            sections['zones'] = hwo_get_zones(zone_line)

            if zone_search is not None:
                if zone_search not in sections['zones']:
                    continue

        for i, match in enumerate(matches):
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(block)
            section_content = replace_inline_newline.sub(' ', block[start:end].strip())

            if match.group('divider'):
                section_title = 'additional'
                sections[section_title] = section_content

            elif match.group("colon_header"):
                section_title = match.group("colon_header").lower()

                if "general storm motion of the day" in section_title:
                    section_title = "motion"

                sections[section_title] = section_content

            else:
                header = match.group('dot_header').strip()
                description = match.group('dot_description').strip()

                section_title = header.lower()

                # Check for the most common section titles and shorten their names
                if section_title.startswith("day one"):
                    section_title = "day1"
                elif section_title.startswith("days two through seven"):
                    section_title = "days2-7"
                elif section_title.startswith("spotter information statement"):
                    section_title = "spotter"

                if not description.strip():
                    # For items that do not contain a description
                    sections[section_title] = section_content
                else:
                    sections[section_title] = {
                        "description": description,
                        "content": section_content
                    }

                # For the days 2-7 outlook, parse the start and end days
                if section_title == "days2-7":
                    period = description.split(" through ")
                    sections[section_title]['period'] = {"start": period[0], "end": period[1]}

        blocks.append(sections)

    # Return just the dictionary if only one entry
    if len(blocks) > 1:
        return blocks
    return blocks[0]


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
        self.zone_id = None

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

        # Extract the Zone ID from the forecastZone element
        # The Zone ID is contained in the foreCast zone as a URL
        # Split the URL by / and get the last item (the zone ID)
        self.zone_id = data['properties']['forecastZone'].split("/")[-1]

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

    def get_hwo(self, today_only: bool = True) -> list | None:
        """
        Retrieves the Hazardous Weather Outlook (HWO) product from the NWS API.
        :param today_only: If True, only return entries from today
        :return: List of HWO entries or None if none were found
        """
        hwo_list = []
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

        url = HWO_URL.replace("{OFFICE}", self.office)

        r = requests.get(url)
        r.raise_for_status()
        js = r.json()

        # Loop through all the HWO entries to get the ID
        for item in js['@graph']:
            # Retrieve the actual entry with the @id parameter
            r = requests.get(item['@id'])
            r.raise_for_status()

            data = r.json()
            raw_hwo = data.pop('productText') # Remove the productText so that there are no duplicates

            # Parse the time information if we are skipping entries that aren't from today
            if today_only:
                # Parse the UTC time string
                utc_dt = datetime.fromisoformat(data['issuanceTime'])

                # Get the local timezone info
                local_tz = ZoneInfo(TZ)

                # Convert to local time
                local_dt = utc_dt.astimezone(local_tz)

                # Get today's date in local time
                today_local = datetime.now(local_tz).date()

                # Compare to the local time and skip if it is not from today
                if local_dt.date() != today_local:
                    logging.debug(f"Skipping product with issuanceTime of {data['issuanceTime']}. Local time is {today_local}")
                    continue

            hwo = hwo_parse(raw_hwo, zone_search=self.zone_id)

            if hwo:
                # If the HWO is a list, add the metadata to each entry
                # Otherwise, just add it to the dictionary
                if type(hwo) is list:
                    for i in range(len(hwo)):
                        hwo[i]['meta'] = data
                else:
                    hwo['meta'] = data

                logging.debug(f"{self.zone_id}  {len(hwo)}  {item['@id']}  {hwo}")
                hwo_list.append(hwo)

        return hwo_list
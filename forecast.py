import logging
import xml.etree.ElementTree as ET
from datetime import datetime

import requests
from bs4 import BeautifulSoup

import config

DWML_VERSION = "1.0"  # Version of DWML that we expect to parse

BASE_URL = "https://forecast.weather.gov/MapClick.php?lat={LAT}&lon={LON}&unit={UNIT}&lg={LANG}&FcstType=dwml"
# URL to obtain the Hazardous Weather Outlook
# NWS_LOCATION will be converted to the appropriate NWS office to use
HWO = "https://forecast.weather.gov/wwamap/wwatxtget.php?cwa={NWS_LOCATION}&wwa=hazardous%20weather%20outlook"

class WeatherParsingError(Exception):
    pass


class Forecast:
    def __init__(self, config):
        self.config = config
        self.weather = {}

    def load(self):
        data = {}
        url = self.get_forecast_url(self.config['forecast']['lat'], self.config['forecast']['lon'],
                                    self.config['forecast']['unit'], self.config['forecast']['lang'])
        r = requests.get(url)
        r.raise_for_status()

        # Parse the dwml data and make sure that the root tag is dwml
        root = ET.fromstring(r.text)
        if root.tag != "dwml":
            raise WeatherParsingError(f"Received invalid data from the NWS")

        # Check the version of DWML
        if "version" not in root.attrib:
            raise WeatherParsingError(f"No version information received from the NWS")

        if root.attrib['version'] != DWML_VERSION:
            raise WeatherParsingError(f"Unsupported DWML version: {root.attrib['version']}")

        for child in root:
            if child.tag == "head":
                data.update(self.parse_head(child))
            elif child.tag == "data":
                if "type" not in child.attrib:
                    raise WeatherParsingError(f"Unknown data type from the NWS")
                if child.attrib['type'] == "forecast":
                    data.update(self.parse_forecast(child))
                elif child.attrib['type'] == "current observations":
                    data.update(self.parse_observations(child))

        self.weather = data
        self.weather['hwo'] = self.get_hwo()

    def parse_head(self, head):
        data = {}
        for child in head:
            # Obtain information about the type of data being retrieved
            # The product tag will contain some information, but we won't really be parsing its attributes
            # There are some tags in the product that we are interested in, such as the creation date, refresh interval,
            # and the category of data
            if child.tag == "product":
                data['product'] = child.attrib
                for item in child:
                    if item.tag == "creation-date":
                        data['product']['date'] = item.text
                        if "refresh-frequency" in item.attrib:
                            data['product']['refresh'] = item.attrib['refresh-frequency']
                    elif item.tag == "category":
                        data['product']['category'] = item.text

            # The source tag will contain information about the NWS office that is providing the data
            # The tags are production-center, credit, and more-information
            # production-center contains the location of the NWS office
            # credit is a link to that office
            # and more information points to the forcast xml section of the nws website
            elif child.tag == "source":
                data['source'] = {}
                for item in child:
                    if item.tag == "production-center":
                        data['source']['location'] = item.text
                    elif item.tag == "credit":
                        data['source']['url'] = item.text
                        # Strip out the URL to get the office
                        data['source']['office'] = item.text.replace("https://www.weather.gov", "").replace("/", "")
                    elif item.tag == "more-information":
                        data['source']['info'] = item.text

        return data

    def parse_forecast(self, forecast):
        data = {"times": {}}
        for child in forecast:
            # Parse information provided for the specified location
            if child.tag == "location":
                data['location'] = {}
                for item in child:
                    # Point name, usually point1
                    if item.tag == "location-key":
                        data['location']['point'] = item.text

                    # Location the forecast is for
                    elif item.tag == "description":
                        data['location']['name'] = item.text

                    # Latitude and Longitude of the location
                    elif item.tag == "point":
                        if "latitude" not in item.attrib or "longitude" not in item.attrib:
                            raise WeatherParsingError(
                                f"Positional information missing from the NWS")
                        data['location']['coords'] = {"lat": item.attrib['latitude'], "lon": item.attrib['longitude']}

                    # City information for the location
                    elif item.tag == "city":
                        if "state" not in item.attrib:
                            raise WeatherParsingError(f"State missing from city from the NWS")

                        data['location']['city'] = item.text
                        data['location']['state'] = item.attrib['state']

                    elif item.tag == "height":
                        data['location']['height'] = item.text
                # End location parsing

            elif child.tag == "moreWeatherInformation":
                # moreWeatherInformation also provides an "applicable-location" attribute that refers to a point name
                # Ignoring this for now
                data['url'] = child.text

            elif child.tag == "time-layout":
                # Time layouts provide a name of the period and the date/time that the name starts on
                # They also have a key that is used to identify them for each of the forecast types
                # The different layouts are in the "times" dictionary

                key = None
                times = []

                for item in child:
                    if item.tag == "layout-key":
                        key = item.text
                    elif item.tag == "start-valid-time":
                        if "period-name" not in item.attrib:
                            raise WeatherParsingError(
                                f"Missing period name from  missing from the NWS")
                        name = item.attrib['period-name']
                        value = item.text

                        times.append((name, value))

                if key is None:
                    raise WeatherParsingError(f"Missing layout key from the NWS")
                data['times'][key] = times

            elif child.tag == "parameters":
                data['parameters'] = {}
                # Check that the location for the parameters matches the location specified in the location key
                if "applicable-location" not in child.attrib:
                    raise WeatherParsingError(f"Missing location info from the NWS")

                if child.attrib['applicable-location'] != data['location']['point']:
                    raise WeatherParsingError(f"Got invalid parameters from the NWS")

                for param in child:
                    if param.tag == "temperature":
                        if "temperature" not in data['parameters']:
                            data['parameters']['temperature'] = {}

                        if "type" not in param.attrib or "units" not in param.attrib or "time-layout" not in param.attrib:
                            raise WeatherParsingError(
                                f"Got invalid temperature parameters from the NWS")

                        temp_type = param.attrib['type']
                        unit = param.attrib['units']
                        layout_name = param.attrib['time-layout']
                        layout = data['times'][layout_name]

                        data['parameters']['temperature'][temp_type] = {"times": layout_name, "values": {}}
                        index = 0  # Used for tracking the time-layout values
                        for item in param:
                            if item.tag == "name":
                                data['parameters']['temperature'][temp_type]['name'] = item.text
                            elif item.tag == "value":
                                value = 0
                                if item.text is not None:
                                    value = int(item.text)

                                layout_name, _ = layout[index]
                                if unit == "Fahrenheit":
                                    symbol = "F"
                                elif unit == "Celsius":
                                    symbol = "C"
                                else:
                                    symbol = "K"

                                data['parameters']['temperature'][temp_type]['values'][
                                    layout_name] = f"{value}°{symbol}"
                                index += 1

                    elif param.tag == "probability-of-precipitation":
                        if "type" not in param.attrib or "units" not in param.attrib or "time-layout" not in param.attrib:
                            raise WeatherParsingError(
                                f"Got invalid precipitation parameters from the NWS")

                        unit = param.attrib['units']
                        layout_name = param.attrib['time-layout']
                        layout = data['times'][layout_name]

                        if unit != "percent":
                            raise WeatherParsingError(
                                f"Got invalid unit of {unit} from the NWS")

                        data['parameters']['precip'] = {"times": layout_name, "values": {}}
                        index = 0
                        for item in param:
                            if item.tag == "name":
                                data['parameters']['precip']['name'] = item.text
                            elif item.tag == "value":
                                value = 0
                                if item.text is not None:
                                    value = int(item.text)

                                layout_name, _ = layout[index]
                                data['parameters']['precip']['values'][layout_name] = f"{value}%"
                                index += 1

                    elif param.tag == "weather":
                        if "time-layout" not in param.attrib:
                            raise WeatherParsingError(
                                f"Got invalid weather parameters from the NWS")

                        layout_name = param.attrib['time-layout']
                        layout = data['times'][layout_name]

                        data['parameters']['weather'] = {"times": layout_name, "values": {}}
                        index = 0
                        for item in param:
                            if item.tag == "name":
                                data['parameters']['weather']['name'] = item.text
                            elif item.tag == "weather-conditions":
                                if "weather-summary" not in item.attrib:
                                    raise WeatherParsingError(
                                        f"Got invalid weather parameters from the NWS")

                                layout_name, _ = layout[index]
                                data['parameters']['weather']['values'][layout_name] = item.attrib['weather-summary']
                                index += 1

                    elif param.tag == "conditions-icon":
                        if "time-layout" not in param.attrib or "type" not in param.attrib:
                            raise WeatherParsingError(
                                f"Got invalid weather parameters from the NWS")

                        layout_name = param.attrib['time-layout']
                        layout = data['times'][layout_name]

                        data['parameters']['icons'] = {"times": layout_name, "values": {}}
                        index = 0
                        for item in param:
                            if item.tag == "name":
                                data['parameters']['icons']['name'] = item.text
                            elif item.tag == "icon-link":
                                layout_name, _ = layout[index]
                                data['parameters']['icons']['values'][layout_name] = item.text
                                index += 1

                    elif param.tag == "wordedForecast":
                        if "time-layout" not in param.attrib:
                            raise WeatherParsingError(
                                f"Got invalid forecast parameters from the NWS")

                        layout_name = param.attrib['time-layout']
                        layout = data['times'][layout_name]

                        data['parameters']['worded'] = {"times": layout_name, "values": {}}
                        index = 0
                        for item in param:
                            if item.tag == "name":
                                data['parameters']['worded']['name'] = item.text
                            elif item.tag == "text":
                                layout_name, _ = layout[index]
                                data['parameters']['worded']['values'][layout_name] = item.text
                                index += 1

            else:
                logging.debug(f"Ignoring unknown tag: {child.tag}")
                continue

        return {"forecast": data}

    def parse_observations(self, observations):
        data = {"temperature": {}, "conditions": {}, "wind": {}}

        for child in observations:
            if child.tag == "location":
                data['location'] = {}
                for item in child:
                    # Point name, usually point1
                    if item.tag == "location-key":
                        data['location']['point'] = item.text

                    # Location the forecast is for
                    elif item.tag == "area-description":
                        data['location']['name'] = item.text

                    # Latitude and Longitude of the location
                    elif item.tag == "point":
                        if "latitude" not in item.attrib or "longitude" not in item.attrib:
                            raise WeatherParsingError(
                                f"Positional information missing from the NWS")
                        data['location']['coords'] = {"lat": item.attrib['latitude'], "lon": item.attrib['longitude']}

                    elif item.tag == "height":
                        data['location']['height'] = item.text

            elif child.tag == "moreWeatherInformation":
                data['info'] = child.text

            elif child.tag == "time-layout":
                # Not sure if the time-coordinate changes or not from the NWS, so we'll just output a warning
                # if it's unexpected
                if "time-coordinate" not in child.attrib:
                    logging.warning("time-coordinate missing from time-layout")
                else:
                    if child.attrib['time-coordinate'] != 'local':
                        logging.warning(
                            f"unknown value for time coordinate: {child.attrib['time-coordinate']}")

                # The current observations time layout is normally just two items: the layout key and the start time
                # We won't bother looping through those values, so long as they match the expected ones
                if len(child) != 2:
                    raise WeatherParsingError(f"Unknown length for current time layout: {len(child)}")

                if child[0].tag != "layout-key" or child[1].tag != "start-valid-time":
                    raise WeatherParsingError(f"First tag: {child[0].tag}, expected: layout-key,"
                                              f" second tag: {child[1].tag}, expected: start-valid-time")

                # We want the time frame for the current conditions
                data['time'] = child[1].text

            elif child.tag == "parameters":
                if "applicable-location" not in child.attrib:
                    raise WeatherParsingError("Location information missing from parameters")
                if child.attrib['applicable-location'] != data['location']['point']:
                    raise WeatherParsingError("Location information does not match what was expected")

                for item in child:
                    if item.tag == "temperature":
                        if "type" not in item.attrib or "units" not in item.attrib:
                            raise WeatherParsingError("Missing temperature type or unit information")

                        if item.attrib['units'] == "Fahrenheit":
                            units = "F"
                        elif item.attrib['units'] == "Celsius":
                            units = "C"
                        else:
                            units = "K"  # Not sure that this will ever be displayed

                        # Perform some sanity checks on the item
                        # A length of 1 means more values are displayed, while we're assuming only 1 will be
                        if len(item) != 1:
                            raise WeatherParsingError(f"Incorrect length of temperature data. Got {len(item)},"
                                                      f" but expected 1")

                        # Check that the tag is 'value', which should always be the case unless we have some odd data
                        if item[0].tag != "value":
                            raise WeatherParsingError(f"Wrong tag for temperature values. Got {item[0].tag},"
                                                      f" expected value")

                        temp = item[0].text

                        if item.attrib['type'] == "apparent":
                            name = "apparent"
                        elif item.attrib['type'] == "dew point":
                            name = "dew"
                        else:
                            raise WeatherParsingError(f"Unknown type: {item.attrib['type']}")

                        value = f"{temp} °{units}"
                        data['temperature'][name] = value

                    elif item.tag == "humidity":
                        if "type" not in item.attrib:
                            raise WeatherParsingError("Missing humidity type")

                        if item.attrib['type'] != "relative":
                            raise WeatherParsingError(f"Unknown humidity type: {item.attrib['type']}")

                        # Perform some sanity checks on the item
                        # A length of 1 means more values are displayed, while we're assuming only 1 will be
                        if len(item) != 1:
                            raise WeatherParsingError(f"Incorrect length of humidity data. Got {len(item)},"
                                                      f" but expected 1")

                        # Check that the tag is 'value', which should always be the case unless we have some odd data
                        if item[0].tag != "value":
                            raise WeatherParsingError(f"Wrong tag for humidity values. Got {item[0].tag},"
                                                      f" expected value")

                        data['humidity'] = f"{item[0].text}%"

                    elif item.tag == "weather":
                        """
                        This section may need to be re-evaluated from time to time. Currently tested using the following data:
                            <weather time-layout="k-p1h-n1-1">
                            <name>Weather Type, Coverage, Intensity</name>
                            <weather-conditions weather-summary=" Fog/Mist"/>
                            <weather-conditions>
                            <value>
                            <visibility units="statute miles">6.00</visibility>
                            </value>
                            </weather-conditions>
                        """
                        for condition in item:
                            # skip the 'name' tag
                            if condition.tag == "name":
                                continue

                            elif condition.tag == "weather-conditions":
                                if "weather-summary" in condition.attrib:
                                    data['conditions']['summary'] = condition.attrib['weather-summary']
                                else:
                                    if len(condition) != 1:
                                        raise WeatherParsingError(f"More values than expected for the weather "
                                                                  f"conditions: {len(condition)}")
                                    if condition[0].tag != "value":
                                        raise WeatherParsingError(f"Expected value, got {condition[0].tag}"
                                                                  f" for weather conditions")

                                    if len(condition[0]) != 1:
                                        raise WeatherParsingError(f"More values than expected within the value "
                                                                  f"tag:{len(condition[0])}")

                                    value = condition[0][0]
                                    if value.tag != "visibility":
                                        raise WeatherParsingError(f"Expected visibility, but got {value.tag}")

                                    if "units" not in value.attrib:
                                        raise WeatherParsingError("Missing units for visibility data")

                                    if value.attrib['units'] != "statute miles":
                                        logging.warning(f"WARNING: Unknown visibility unit: {value.attrib['units']}")

                                    data['conditions']['visibility'] = value.text

                    elif item.tag == "conditions-icon":
                        # Assume only two items in this: name and icon-link
                        if len(item) != 2:
                            raise WeatherParsingError(f"Expected icon count of 2, got {len(item)}")
                        # Check to make sure that the two tags are name and icon-link, and ignore the name
                        if item[0].tag != "name" or item[1].tag != "icon-link":
                            raise WeatherParsingError("Missing information for the icons")

                        data['conditions']['icon'] = item[1].text

                    elif item.tag == "direction":
                        if "type" not in item.attrib or "units" not in item.attrib:
                            raise WeatherParsingError("Missing type or units from direction information")

                        if item.attrib['type'] == "wind":
                            if item.attrib['units'] != "degrees true":
                                logging.warning(f"Unknown wind unit: {item.attrib['units']}")

                            if len(item) != 1:
                                raise WeatherParsingError(f"Too many values for wind direction: {len(item)}")
                            if item[0].tag != "value":
                                raise WeatherParsingError(f"Expected value for wind direction, got {item[0].tag}")

                            data['wind']['direction'] = item[0].text
                        else:
                            logging.warning(f"WARNING: Ignoring unknown direction type: {item.attrib['type']}")
                            continue

                    elif item.tag == "wind-speed":
                        if "type" not in item.attrib or "units" not in item.attrib:
                            raise WeatherParsingError("Missing type or units from wind speed information")

                        if len(item) != 1:
                            raise WeatherParsingError(f"Extra data for wind speed information. "
                                                      f"Expected length 1, got {len(item)}")

                        value = item[0].text
                        # If no value, then set to None instead of "NA"
                        if value == "NA":
                            value = None

                        if item.attrib['type'] == "gust":
                            data['wind']['gust'] = value
                        elif item.attrib['type'] == "sustained":
                            data['wind']['speed'] = value
                        else:
                            logging.warning(f"Ignoring unknown wind speed type: {item.attrib['type']}")

                    elif item.tag == "pressure":
                        if "type" not in item.attrib or "units" not in item.attrib:
                            raise WeatherParsingError("Missing type or units from pressure information")

                        if item.attrib['units'] != "inches of mercury":
                            logging.warning(f"Ignoring unknown pressure unit: {item.attrib['units']}")
                            continue

                        if item.attrib['type'] != "barometer":
                            logging.warning(f"Ignoring unknown pressure type: {item.attrib['type']}")
                            continue

                        if len(item) != 1:
                            raise WeatherParsingError(f"Extra data for pressure information. "
                                                      f"Expected length 1, got {len(item)}")

                        data['pressure'] = item[0].text

            else:
                logging.info(f"Ignoring unknown tag: {child.tag}")
                continue

        return {"observations": data}

    def get_hwo(self, include_all=False):
        data = []
        url = None
        # Determine the source URL to use to get the Hazardous Weather Outlook
        if "hwo" in self.config:
            # Check for a URL option in the configuration settings and make sure it is not None
            if "url" in self.config['hwo']:
                if self.config['hwo']['url'] is not None:
                    # Set the custom URL as the URL to use for the HWO
                    url = self.config['hwo']['url']
            # Determine if an office parameter was specified AND that we haven't already specified a URL to use
            # The URL will override any other settings
            if "office" in self.config['hwo'] and url is None:
                if self.config['hwo']['office'] is not None:
                    url = HWO.replace("{NWS_LOCATION}", self.config['hwo']['office'])

        # If the URL still is not set, then determining the office depends on previously obtaining weather information
        # If we do not have that information, return None
        if not self.weather and url is None:
            return None

        if url is None:
            url = HWO.replace("{NWS_LOCATION}", self.weather['source']['office'])

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
                    if self.weather:
                        loc = self.weather['source']['location'].split(", ")
                        expected_state = loc[1].strip()
                        expected_city = loc[0].strip()

                        if not include_all and expected_state != state:
                            # State doesn't match, so end line parsing
                            break

                        if not include_all and expected_city != city:
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
                    line = " ".join(arr) # Re-joins the array as the original string
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

                elif line.startswith("$$"):
                    # Indicates the end of the HWO for the given location, so stop parsing the lines
                    break

                elif mode == "county" or mode == "affected-areas" or mode == "spotter-activation":
                    buffer += line + " "

                elif mode == "day-one" or mode == "days-two-seven":
                    buffer += line + "\n"
            if hwo:
                data.append(hwo)

        return data

    def get_forecast_url(self, lat, lon, unit, lang):        
        return BASE_URL.replace("{LAT}", lat) \
            .replace("{LON}", lon) \
            .replace("{UNIT}", unit) \
            .replace("{LANG}", lang)


if __name__ == "__main__":
    cfg = config.load()
    forecast = Forecast(cfg)
    forecast.load()

    import json
    with open("forecast.json", "wt") as f:
        json.dump(forecast.weather, f)
# nws-api
Fetch weather forecasts and the hazardous weather outlook from the National Weather Service

## FastAPI
The API makes use of [FastAPI](https://fastapi.tiangolo.com/). This will make the API *far* easier to implement in other products, and it will also be OpenAPI compatible.

## National Weather Service API Python Library
This is a Python library that makes use of the [API Web Service](https://www.weather.gov/documentation/services-web-api) from the National Weather Service. The current implementation is very limited, as it is based solely on my own use case.

In addition to the standard API service, the library can also parse the text for the Hazardous Weather Outlook that is published by the various offices of the National Weather Service. I use this to retrieve the Spotter Information Statement, for example.

There is an API server as well, that can be used for caching API queries instead of constantly hitting the API service endpoints.

If you would like to help add support for something, you can take a look at their [Open API Specification](https://api.weather.gov/openapi.json) and submit a pull request.

The code is not exactly clean, so comments to improve it (as well as pull requests) are more than welcome!

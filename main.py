if __name__ == "__main__":
    import os
    import sys
    import argparse
    import config

    from fastapi import FastAPI
    import uvicorn

    import forecast
    import server

    parser = argparse.ArgumentParser(description="Fetches weather data from the National Weather Service.")
    parser.add_argument("-L", "--logging-level", choices=["debug", "info", "warning", "error", "critical"],
                        help="Set the logging level to the provided value. For the least output, use error or critical")
    parser.add_argument("-l", "--log-file", help="Write logs to the specified file instead of to the console.")
    parser.add_argument("-v", "--verbose", help="Set the verbosity level of the DEBUG log level",
                        action="count", default=1)
    parser.add_argument("-c", "--config-file", action="store", default=config.DEFAULT_CONFIG_FILE,
                        help="Use the specified configuration YAML file instead of the default one.")
    parser.add_argument("--no-server", action="store_true", help="Prints the Hazardous Weather Outlook"
                                                                 " for the locations in the config and exits.")

    args = parser.parse_args()

    # Get the verbosity level from the command line or the environment (environment takes precedence)
    verbosity = args.verbose
    if "VERBOSITY" in os.environ:
        verbosity = os.environ['VERBOSITY']

    forecast.verbosity = verbosity

    # Set the logger to log to the specified file, which indicates that manual logging was specified.
    if args.log_file:
        config.manual_logging = True
        config.setup_file_logging(args.log_file)

    # Adjust the log level to the user specified level, which indicates that manual logging was specified.
    if args.logging_level:
        config.manual_logging = True
        config.set_log_level(args.logging_level)

    cfg = config.load(config_path=args.config_file)
    if args.no_server:
        # Check that one or more locations were specified in the config file and exit if not
        if cfg.get_value("locations") is None:
            sys.stderr.write("No location specified in the config file\n")
            sys.exit(1)

        locations: list = cfg.get_value("locations")
        if len(locations) < 1:
            sys.stderr.write("No location specified in the config file\n")
            sys.exit(1)

        forecasts = []
        for location in locations:
            fc = forecast.Forecast(cfg)
            fc.get_point((location['lat'], location['lon']))
            fc.get_office_info()
            fc.load()

            forecasts.append(fc.weather)

        import json
        with open("forecast.json", "wt") as f:
            json.dump(forecasts, f)
    else:
        server.verbosity = verbosity
        app = FastAPI()
        api = server.APIv1(app=app, config=cfg)

        address = cfg.get_value("server.address")
        port = cfg.get_value("server.port")
        uvicorn.run(app, host=address, port=port, log_level=cfg.log_level)

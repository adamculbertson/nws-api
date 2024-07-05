if __name__ == "__main__":
    import argparse
    import config
    from server import Server, RequestHandler

    parser = argparse.ArgumentParser(description="Fetches weather data from the National Weather Service.")
    parser.add_argument("-L", "--logging-level", choices=["debug", "info", "warning", "error", "critical"],
                        help="Set the logging level to the provided value. For the least output, use error or critical.")
    parser.add_argument("-O", "--log-file", help="Write logs to the specified file instead of to the console.")
    parser.add_argument("-c", "--config-file", action="store", default=config.default_config_file,
                        help="Use the specified configuration YAML file instead of the default one.")

    args = parser.parse_args()

    # Set the logger to log to the specified file, which indicates that manual logging was specified.
    if args.log_file:
        config.manual_logging = True
        config.setup_logging(args.log_file)

    # Adjust the log level to the user specified level, which indicates that manual logging was specified.
    if args.logging_level:
        config.manual_logging = True
        config.set_log_level(args.logging_level)

    cfg = config.load(file_path=args.config_file)
    s = Server((cfg['server']['address'], cfg['server']['port']), RequestHandler, cfg)
    s.serve_forever()

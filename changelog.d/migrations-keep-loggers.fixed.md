Running the database migrations no longer turns off the collector's own loggers in the same process: the Alembic environment's logging setup keeps every logger its configuration doesn't name.

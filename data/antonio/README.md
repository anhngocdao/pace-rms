# Public-data pilot inputs

`settings.json` holds every number that could have been tuned after seeing
the data. It was committed before `hotels.csv` was downloaded; the pilot
report prints the hash of that commit.

Source: Antonio, de Almeida and Nunes (2019), "Hotel booking demand datasets",
Data in Brief 22, CC BY 4.0. The CSV used is the TidyTuesday 2020-02-11 copy,
119,390 rows. Download (about 16 MB), never committed:

    curl -L -o data/antonio/hotels.csv https://raw.githubusercontent.com/rfordatascience/tidytuesday/main/data/2020/2020-02-11/hotels.csv

Convert:

    python3 -m tools.convert_antonio data/antonio/hotels.csv data/antonio data/antonio/settings.json

Outputs `h1-bookings.csv`, `h2-bookings.csv`, `h1-hotel.json`, `h2-hotel.json`
and `audit.md`, all ignored by git.

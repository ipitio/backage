# GitHub Packages Stats

## JSON Endpoint and Dataset

GitHub Packages doesn't provide an API endpoint for the download count, so it's scanned twice daily for every package in `pkg.txt`. The data is stored in `index.db`, a SQLite database that is then used to populate [`index.json`](./index.json) with the latest stats. See [JSON Endpoint](#json-endpoint) and [Database Schema](#database-schema) below for more information, including how to make badges for packages and versions.

Why all this? To make the following badges possible, of course. If we don't yet follow a package, you can either:

* open an issue or
* add it on a new line in `pkg.txt` on your own fork [here](https://github.com/ipitio/ghcr-pulls/edit/master/pkg.txt) and make a pull request.

[![users/container/arevindh/pihole-speedtest/pihole-speedtest/downloads](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.package%3D%3D%22pihole-speedtest%22)%5D.downloads&label=pihole-speedtest&color=117)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![users/container/arevindh/pihole-speedtest/pihole-speedtest/size](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.package%3D%3D%22pihole-speedtest%22)%5D.size&label=size&color=711)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![users/container/arevindh/pihole-speedtest/pihole-speedtest/latest](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner_type%3D%3D%22users%22%20%26%26%20%40.package_type%3D%3D%22container%22%20%26%26%20%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.package%3D%3D%22pihole-speedtest%22)%5D.version%5B%3F(%40.latest%3D%3Dtrue)%5D.name&label=latest&color=171)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest)

### JSON Endpoint

The JSON endpoint is refreshed every hour, for packages that haven't been scanned in the last 12 hours.

To make a badge, you can modify one of the badges above or generate one with something like [shields.io](https://shields.io/badges/dynamic-json-badge) and these parameters:

#### URL

```markdown
https://raw.githubusercontent.com/ipitio/ghcr-pulls/master/index.json
```

#### JSONPath

Just fill in the blanks to get what you want!

##### Package

You can get the general stats for a package...

```markdown
$[?(@.owner_type=="<TYPE>" && @.package_type=="<TYPE>" && @.owner=="<OWNER>" && @.repo=="<REPO>" && @.package=="<PACKAGE>")].<PROPERTY>
```

Available properties are:
|       Property        | Description                                         |
| :-------------------: | --------------------------------------------------- |
|     `owner_type`      | The type of owner (e.g. `users`)                    |
|    `package_type`     | The type of package (e.g. `container`)              |
|        `owner`        | The owner of the package                            |
|        `repo`         | The repository of the package                       |
|       `package`       | The package name                                    |
|       `version`       | An array of all versions (see below)                |
|      `versions`       | Formatted number of versions                        |
|    `raw_versions`     | Actual number of versions                           |
|        `size`         | Formatted size of the latest version                |
|      `raw_size`       | Actual size of the latest version, in bytes         |
|      `downloads`      | Formatted number of all downloads                   |
|   `downloads_month`   | Formatted number of all downloads in the last month |
|   `downloads_week`    | Formatted number of all downloads in the last week  |
|    `downloads_day`    | Formatted number of all downloads in the last day   |
|    `raw_downloads`    | Actual number of all downloads                      |
| `raw_downloads_month` | Actual number of all downloads in the last month    |
| `raw_downloads_week`  | Actual number of all downloads in the last week     |
|  `raw_downloads_day`  | Actual number of all downloads in the last day      |
|        `date`         | The most recent date the above stats were refreshed |

###### Version

...or the stats for a specific version.

```markdown
$[?(@.owner_type=="<TYPE>" && @.package_type=="<TYPE>" && @.owner=="<USER>" && @.repo=="<REPO>" && @.package=="<PACKAGE>"].version[?(@.name=="<VERSION>")].<PROPERTY>
```

Available properties are:

|       Property        | Description                                     |
| :-------------------: | ----------------------------------------------- |
|         `id`          | The version ID                                  |
|        `name`         | The version name                                |
|       `latest`        | Whether the version is the latest (e.g. `true`) |
|        `size`         | Formatted size of the version                   |
|      `raw_size`       | Actual size of the version, in bytes            |
|      `downloads`      | Formatted number of downloads                   |
|   `downloads_month`   | Formatted number of downloads in the last month |
|   `downloads_week`    | Formatted number of downloads in the last week  |
|    `downloads_day`    | Formatted number of downloads in the last day   |
|    `raw_downloads`    | Actual number of downloads                      |
| `raw_downloads_month` | Actual number of downloads in the last month    |
| `raw_downloads_week`  | Actual number of downloads in the last week     |
|  `raw_downloads_day`  | Actual number of downloads in the last day      |
|        `date`         | The date the version was updated                |

### Database Schema

The properties are generated from the following tables:

#### Packages Table

This table contains the latest stats for each package.

|      Column       |  Type   | Description                                     |
| :---------------: | :-----: | ----------------------------------------------- |
|   `owner_type`    |  TEXT   | The type of owner (e.g. `users`)                |
|  `package_type`   |  TEXT   | The type of package (e.g. `container`)          |
|      `owner`      |  TEXT   | The owner of the package                        |
|      `repo`       |  TEXT   | The repository of the package                   |
|     `package`     |  TEXT   | The package name                                |
|    `downloads`    | INTEGER | The total number of downloads                   |
| `downloads_month` | INTEGER | The total number of downloads in the last month |
| `downloads_week`  | INTEGER | The total number of downloads in the last week  |
|  `downloads_day`  | INTEGER | The total number of downloads in the last day   |
|      `size`       | INTEGER | The size of the latest version                  |
|      `date`       |  TEXT   | The most recent date the package was updated    |

#### Version Tables

|      Column       |  Type   | Description                                     |
| :---------------: | :-----: | ----------------------------------------------- |
|   `package_id`    | INTEGER | The ID of the package                           |
|      `name`       |  TEXT   | The version name                                |
|      `size`       | INTEGER | The size of the version                         |
|    `downloads`    | INTEGER | The total number of downloads                   |
| `downloads_month` | INTEGER | The total number of downloads in the last month |
| `downloads_week`  | INTEGER | The total number of downloads in the last week  |
|  `downloads_day`  | INTEGER | The total number of downloads in the last day   |
|      `date`       |  TEXT   | The date the version was updated                |

### TODO

Feel free to help with any of these:

* [ ] Make a GitHub Pages badge maker, or at least a JSONPath maker
* [ ] Get sizes for package types other than `container`
* [ ] Any other improvements or ideas you have

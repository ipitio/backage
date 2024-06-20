<div align="center">

# Badpack

## Badges for GitHub Packages

A [JSON Endpoint](#endpoint) backed by a [SQLite Dataset](#database) to supplement the API.

</div>

---

With the help of GitHub Actions, `update.sh` makes possible the following badges, and more! To make a badge, you can modify one below or generate one with something like [shields.io/json](https://shields.io/badges/dynamic-json-badge) and [these parameters](#url). If we don't yet follow a package, you can either:

* open an issue or
* add it on a new line in `pkg.txt` on your own fork [here](https://github.com/ipitio/ghcr-pulls/edit/master/pkg.txt) and make a pull request.

[![users/container/arevindh/pihole-speedtest/pihole-speedtest/downloads](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.package%3D%3D%22pihole-speedtest%22)%5D.downloads&label=pihole-speedtest)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![users/container/arevindh/pihole-speedtest/pihole-speedtest/downloads/month](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.package%3D%3D%22pihole-speedtest%22)%5D.downloads_month&label=month)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![users/container/arevindh/pihole-speedtest/pihole-speedtest/downloads/week](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.package%3D%3D%22pihole-speedtest%22)%5D.downloads_week&label=week)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![users/container/arevindh/pihole-speedtest/pihole-speedtest/downloads/day](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.package%3D%3D%22pihole-speedtest%22)%5D.downloads_day&label=day)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![users/container/arevindh/pihole-speedtest/pihole-speedtest/size](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.package%3D%3D%22pihole-speedtest%22)%5D.size&label=size&color=a0a)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![users/container/arevindh/pihole-speedtest/pihole-speedtest/versions](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.package%3D%3D%22pihole-speedtest%22)%5D.versions&label=versions&color=0a0)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![users/container/arevindh/pihole-speedtest/pihole-speedtest/releases](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.package%3D%3D%22pihole-speedtest%22)%5D.tagged&label=releases&color=0a0)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![users/container/arevindh/pihole-speedtest/pihole-speedtest/latest](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner_type%3D%3D%22users%22%20%26%26%20%40.package_type%3D%3D%22container%22%20%26%26%20%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.package%3D%3D%22pihole-speedtest%22)%5D.version%5B%3F(%40.tags.indexOf(%22latest%22)!%3D-1)%5D.tags%5B%3F(%40!%3D%22latest%22)%5D&label=latest&color=0a0)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![users/container/arevindh/pihole-speedtest/pihole-speedtest/newest](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.package%3D%3D%22pihole-speedtest%22)%5D.version%5B%3F(%40.newest%3D%3Dtrue)%5D.name&label=newest&color=0a0)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest)

### Endpoint

Refreshed daily, the endpoint is always up-to-date.

#### URL

```markdown
https://raw.githubusercontent.com/ipitio/ghcr-pulls/dev/index.json
```

#### JSONPath

Just fill in the blanks to get the properties you want. Or get creative and forge your own path!

##### Package

You can query a package for any of the following properties like so:

```markdown
$[?(@.owner_type == "<TYPE>"
 && @.package_type == "<TYPE>"
 && @.owner == "<OWNER>"
 && @.repo == "<REPO>"
 && @.package == "<PACKAGE>"
)].<PROPERTY>
```

<details>

<summary>Properties</summary>

|       Property        |     Type     | Description                                        |
| :-------------------: | :----------: | -------------------------------------------------- |
|     `owner_type`      |    string    | The type of owner (e.g. `users`)                   |
|    `package_type`     |    string    | The type of package (e.g. `container`)             |
|        `owner`        |    string    | The owner of the package                           |
|        `repo`         |    string    | The repository of the package                      |
|       `package`       |    string    | The package name                                   |
|        `date`         |    string    | The most recent date the package was refreshed     |
|        `size`         |    string    | Formatted size of the latest version               |
|      `versions`       |    string    | Formatted count of versions                        |
|       `tagged`        |    string    | Formatted count of tagged versions                 |
|      `downloads`      |    string    | Formatted count of all downloads                   |
|   `downloads_month`   |    string    | Formatted count of all downloads in the last month |
|   `downloads_week`    |    string    | Formatted count of all downloads in the last week  |
|    `downloads_day`    |    string    | Formatted count of all downloads in the last day   |
|      `raw_size`       |    number    | Size of the latest version, in bytes               |
|    `raw_versions`     |    number    | Count of versions                                  |
|     `raw_tagged`      |    number    | Count of tagged versions                           |
|    `raw_downloads`    |    number    | Count of all downloads                             |
| `raw_downloads_month` |    number    | Count of all downloads in the last month           |
| `raw_downloads_week`  |    number    | Count of all downloads in the last week            |
|  `raw_downloads_day`  |    number    | Count of all downloads in the last day             |
|       `version`       | object array | The versions of the package (see below)            |

</details>

##### Version

You can query a package version by replacing `<PROPERTY>` above with the following:

```markdown
version[?(@.<FILTER>)].<PROPERTY>
```

For example, to get the current `latest` tag(s):

```markdown
version[?(@.tags.indexOf("latest") != -1)]
.tags[?(@ != "latest")]
```

Or to get the number of downloads in the last month for a version by name:

```markdown
version[?(@.name=="<VERSION>")].downloads_month
```

<details>

<summary>Properties</summary>

|       Property        |     Type     | Description                                    |
| :-------------------: | :----------: | ---------------------------------------------- |
|         `id`          |    number    | The ID of the version                          |
|        `name`         |    string    | The version name                               |
|        `date`         |    string    | The most recent date the version was refreshed |
|       `newest`        |   boolean    | Whether the version is the latest              |
|        `size`         |    string    | Formatted size of the version                  |
|      `downloads`      |    string    | Formatted count of downloads                   |
|   `downloads_month`   |    string    | Formatted count of downloads in the last month |
|   `downloads_week`    |    string    | Formatted count of downloads in the last week  |
|    `downloads_day`    |    string    | Formatted number of downloads in the last day  |
|      `raw_size`       |    number    | Size of the version, in bytes                  |
|    `raw_downloads`    |    number    | Count of downloads                             |
| `raw_downloads_month` |    number    | Count of downloads in the last month           |
| `raw_downloads_week`  |    number    | Count of downloads in the last week            |
|  `raw_downloads_day`  |    number    | Count of downloads in the last day             |
|        `tags`         | string array | The tags of the version                        |

</details>

### Database

The properties are generated from the following tables:

#### Packages

The general stats for all packages.

<details>

<summary>Schema</summary>

|      Column       |  Type   | Description                                     |
| :---------------: | :-----: | ----------------------------------------------- |
|   `owner_type`    |  TEXT   | The type of owner (e.g. `users`)                |
|  `package_type`   |  TEXT   | The type of package (e.g. `container`)          |
|      `owner`      |  TEXT   | The owner of the package                        |
|      `repo`       |  TEXT   | The repository of the package                   |
|     `package`     |  TEXT   | The package name                                |
|      `date`       |  TEXT   | The most recent date the package was refreshed  |
|      `size`       | INTEGER | The size of the latest version                  |
|    `downloads`    | INTEGER | The total number of downloads                   |
| `downloads_month` | INTEGER | The total number of downloads in the last month |
| `downloads_week`  | INTEGER | The total number of downloads in the last week  |
|  `downloads_day`  | INTEGER | The total number of downloads in the last day   |

</details>

#### Versions

The stats for all versions of each package.

<details>

<summary>Schema</summary>

|      Column       |  Type   | Description                                     |
| :---------------: | :-----: | ----------------------------------------------- |
|       `id`        | INTEGER | The ID of the version                           |
|      `name`       |  TEXT   | The version name                                |
|      `date`       |  TEXT   | The most recent date the version was refreshed  |
|      `size`       | INTEGER | The size of the version                         |
|    `downloads`    | INTEGER | The total number of downloads                   |
| `downloads_month` | INTEGER | The total number of downloads in the last month |
| `downloads_week`  | INTEGER | The total number of downloads in the last week  |
|  `downloads_day`  | INTEGER | The total number of downloads in the last day   |
|      `tags`       |  TEXT   | The tags of the version (csv)                   |

</details>

### TODO

Feel free to help with any of these:

* [ ] Make a GitHub Pages badge/JSONPath maker
* [ ] Get sizes for package types other than `container`
* [ ] Any other improvements or ideas you have -- see this [discussion](https://github.com/ipitio/ghcr-pulls/discussions/9)

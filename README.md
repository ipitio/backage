<div align="center">

![logo](src/img/logo-b.png)

# backage

## SOTA Sprinkler System

It's all part and parcel.

</div>

---

The GitHub Packages API doesn't expose much metadata that other registries provide; this is the remedy. Four owner services are hosted and ran on GitHub: manual entry, automatic discovery, batch queuing, and batch processing. They query the GitHub API, scraping GitHub when needed, to produce a JSON endpoint served from GitHub and a SQLite database stored under GitHub Releases.

If you're here to make a badge, use something like [shields.io/json](https://shields.io/badges/dynamic-json-badge) with the endpoint parameters below. Here's what badges could look like for a few of the available parameters:

<div align="center">

[![package](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fbackage%2Fmaster%2Findex%2Farevindh%2Fpihole-speedtest%2Fpihole-speedtest.json&query=%24.package&logo=github&label=package&style=for-the-badge&color=black)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest)

[![downloads](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fbackage%2Fmaster%2Findex%2Farevindh%2Fpihole-speedtest%2Fpihole-speedtest.json&query=%24.downloads&logo=github&label=downloads)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![size](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fbackage%2Fmaster%2Findex%2Farevindh%2Fpihole-speedtest%2Fpihole-speedtest.json&query=%24.size&logo=github&label=size&color=indigo)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![latest](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fbackage%2Fmaster%2Findex%2Farevindh%2Fpihole-speedtest%2Fpihole-speedtest.json&query=%24.version%5B%3F(%40.latest%3D%3Dtrue)%5D.tags%5B%3F(%40!%3D%22latest%22)%5D&logo=github&label=latest&color=darkgreen)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest)

</div>

To add any owners post-haste, you can:

* open an issue, and/or
* add the case-sensitive name of each user or organization on a new line in `owners.txt` on your own fork [here](https://github.com/ipitio/backage/edit/master/owners.txt) and make a pull request. Please add just the name -- ids, repos, and packages will be retrieved automatically.

### Endpoint

The latest data in the continually updated database.

#### URL

Replace `<OWNER>/<REPO>/<PACKAGE>` with their respective values:

```markdown
https://ipitio.github.io/backage/<OWNER>/<REPO>/<PACKAGE>.json
```

#### JSONPath

Just fill in the blanks to get the properties you want. Or get creative and forge your own path! Here are some simple examples.

##### Package

You can query a package for its properties:

```js
$.<PROPERTY>
```

For instance, to get the size:

```js
$.size
```

<details>

<summary>Properties</summary>

|       Property        |     Type     | Description                                        |
| :-------------------: | :----------: | -------------------------------------------------- |
|      `owner_id`       |    number    | The ID of the owner                                |
|     `owner_type`      |    string    | The type of owner (e.g. `users`)                   |
|    `package_type`     |    string    | The type of package (e.g. `container`)             |
|        `owner`        |    string    | The owner of the package                           |
|        `repo`         |    string    | The repository of the package                      |
|       `package`       |    string    | The package name                                   |
|        `date`         |    string    | The most recent date the package was refreshed     |
|        `size`         |    string    | Formatted size of the latest version               |
|      `versions`       |    string    | Formatted count of versions tracked                |
|       `tagged`        |    string    | Formatted count of tagged versions tracked         |
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

```js
$.version[<FILTER>].<PROPERTY>
```

For example, to get the latest tag(s), we can find the newest version with tags (and exclude a possible `latest` from the list):

```js
$.version[?(@.latest == true)]
    .tags[?(@ != "latest")]
```

<details>

<summary>Properties</summary>

|       Property        |     Type     | Description                                    |
| :-------------------: | :----------: | ---------------------------------------------- |
|         `id`          |    number    | The ID of the version                          |
|        `name`         |    string    | The version name                               |
|        `date`         |    string    | The most recent date the version was refreshed |
|       `newest`        |   boolean    | Whether the version is the newest              |
|       `latest`        |   boolean    | Whether the version is the newest tagged       |
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

Up to the most recent 2GB (compressed) of data. It, along with any older data, can be found under the [latest release](https://github.com/ipitio/backage/releases/latest).

#### Packages

The general data for all packages.

<details>

<summary>Schema</summary>

|      Column       |  Type   | Description                                     |
| :---------------: | :-----: | ----------------------------------------------- |
|    `owner_id`     | INTEGER | The ID of the owner                             |
|   `owner_type`    |  TEXT   | The type of owner (e.g. `users`)                |
|  `package_type`   |  TEXT   | The type of package (e.g. `container`)          |
|      `owner`      |  TEXT   | The owner of the package                        |
|      `repo`       |  TEXT   | The repository of the package                   |
|     `package`     |  TEXT   | The package name                                |
|      `size`       | INTEGER | The size of the latest version                  |
|    `downloads`    | INTEGER | The total number of downloads                   |
| `downloads_month` | INTEGER | The total number of downloads in the last month |
| `downloads_week`  | INTEGER | The total number of downloads in the last week  |
|  `downloads_day`  | INTEGER | The total number of downloads in the last day   |
|      `date`       |  TEXT   | The most recent date the package was refreshed  |

</details>

#### Versions

The data for all tracked versions of each package.

<details>

<summary>Schema</summary>

|      Column       |  Type   | Description                                     |
| :---------------: | :-----: | ----------------------------------------------- |
|       `id`        | INTEGER | The ID of the version                           |
|      `name`       |  TEXT   | The version name                                |
|      `size`       | INTEGER | The size of the version                         |
|    `downloads`    | INTEGER | The total number of downloads                   |
| `downloads_month` | INTEGER | The total number of downloads in the last month |
| `downloads_week`  | INTEGER | The total number of downloads in the last week  |
|  `downloads_day`  | INTEGER | The total number of downloads in the last day   |
|      `date`       |  TEXT   | The most recent date the version was refreshed  |
|      `tags`       |  TEXT   | The tags of the version (csv)                   |

</details>

### TODO

* [ ] Get sizes for all package types

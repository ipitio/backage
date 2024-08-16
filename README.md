<div align="center">

![logo](src/img/logo-b.png)

# backage

## SOTA Sprinkler System

No API? No Problem!

</div>

---

The GitHub Packages API doesn't provide much metadata; this project aims to both fill that gap and provide a timeseries for further analysis. If you're here to make a badge, use something like [shields.io/json](https://shields.io/badges/dynamic-json-badge) with the endpoint. Here's what badges could look like for some of the available parameters:

[![package](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fbackage%2Fmaster%2Findex%2Farevindh%2Fpihole-speedtest%2Fpihole-speedtest.json&query=%24.package&logo=github&label=package&style=for-the-badge&color=black)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![type](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fbackage%2Fmaster%2Findex%2Farevindh%2Fpihole-speedtest%2Fpihole-speedtest.json&query=%24.package_type&logo=github&label=type&style=for-the-badge&color=black)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![watered](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fbackage%2Fmaster%2Findex%2Farevindh%2Fpihole-speedtest%2Fpihole-speedtest.json&query=%24.version%5B%3F(%40.newest%20%3D%3D%20true)%5D.date&logo=github&label=watered&style=for-the-badge&color=black)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest)

[![downloads/all](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fbackage%2Fmaster%2Findex%2Farevindh%2Fpihole-speedtest%2Fpihole-speedtest.json&query=%24.downloads&logo=github&label=downloads)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![downloads/month](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fbackage%2Fmaster%2Findex%2Farevindh%2Fpihole-speedtest%2Fpihole-speedtest.json&query=%24.downloads_month&logo=github&label=month)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![downloads/week](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fbackage%2Fmaster%2Findex%2Farevindh%2Fpihole-speedtest%2Fpihole-speedtest.json&query=%24.downloads_week&logo=github&label=week)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![downloads/day](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fbackage%2Fmaster%2Findex%2Farevindh%2Fpihole-speedtest%2Fpihole-speedtest.json&query=%24.downloads_day&logo=github&label=day)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![size](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fbackage%2Fmaster%2Findex%2Farevindh%2Fpihole-speedtest%2Fpihole-speedtest.json&query=%24.size&logo=github&label=size&color=indigo)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![releases](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fbackage%2Fmaster%2Findex%2Farevindh%2Fpihole-speedtest%2Fpihole-speedtest.json&query=%24.tagged&logo=github&label=releases&color=darkgreen)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![latest](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fbackage%2Fmaster%2Findex%2Farevindh%2Fpihole-speedtest%2Fpihole-speedtest.json&query=%24.version%5B%3F(%40.latest%3D%3Dtrue)%5D.tags%5B%3F(%40!%3D%22latest%22)%5D&logo=github&label=latest&color=darkgreen)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![versions](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fbackage%2Fmaster%2Findex%2Farevindh%2Fpihole-speedtest%2Fpihole-speedtest.json&query=%24.versions&logo=github&label=versions&color=darkgreen)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![newest](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fbackage%2Fmaster%2Findex%2Farevindh%2Fpihole-speedtest%2Fpihole-speedtest.json&query=%24.version%5B%3F(%40.newest%3D%3Dtrue)%5D.id&logo=github&label=newest&color=darkgreen)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest)

### Endpoint

If the database is a lawn we're sprinkling then the endpoint is what's been watered over the last 2π rad. To add any users or orgs post-haste, you can either:

* open an issue, or
* add the case-sensitive name of each one on a new line in `owners.txt` on your own fork [here](https://github.com/ipitio/backage/edit/master/owners.txt) and make a pull request. Please add just the name -- ids, repos, and packages will be retrieved automatically.

#### URL

Replace `<OWNER>/<REPO>/<PACKAGE>` with their respective values:

```markdown
https://raw.githubusercontent.com/ipitio/backage/master/index/<OWNER>/<REPO>/<PACKAGE>.json
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
|      `versions`       |    string    | Formatted count of versions scraped                |
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

Imagine an ever-growing lawn increasingly covered less than π/2 rad at a time. It can be found under the [latest release](https://github.com/ipitio/backage/releases/latest).

#### Packages

The general stats for all packages.

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

The stats for all versions of each package.

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

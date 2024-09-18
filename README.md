<div align="center">

[![logo](src/img/logo-b.png)](https://github.com/ipitio/backage)

# [backage](https://github.com/ipitio/backage)

**It's all part and parcel**

---

[![packages](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fgithub.com%2Fipitio%2Fbackage%2Fraw%2Findex%2F.json&query=%24.packages&logo=github&logoColor=959da5&label=packages&labelColor=333a41&color=2ebc4f)](https://github.com/ipitio/backage/tree/index) [![date](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fgithub.com%2Fipitio%2Fbackage%2Fraw%2Findex%2F.json&query=%24.date&logo=github&logoColor=959da5&label=refreshed&labelColor=333a41&color=2ebc4f)](https://github.com/ipitio/backage/releases/latest) [![pages-build-deployment](https://github.com/ipitio/backage/actions/workflows/pages/pages-build-deployment/badge.svg)](https://github.com/ipitio/backage/actions/workflows/pages/pages-build-deployment)

</div>

The GitHub Packages API doesn't expose much of the publicly-available metadata that other registries provide; this completely automated closed-loop system [is the solution](https://github.com/badges/shields/issues/5594#issuecomment-2157626147). **Just star this project to have GitHub serve a JSON endpoint for your packages and those of any organizations you're a part of!** A service ran by GitHub will pick up your name in the next few hours and scrape GitHub for the numbers, storing them in a SQLite database hosted on GitHub. If you'd like the service to ignore some or all of your packages, even after giving a star, simply add `owner[/repo[/package]]` on a new line in `optout.txt` on your own fork [here](https://github.com/ipitio/backage/edit/master/optout.txt) and make a pull request.

To enable the endpoint for any other users or organizations not yet [in the index](https://github.com/ipitio/backage/tree/index), add the case-sensitive name of each owner on a new line in `owners.txt` on your own fork [here](https://github.com/ipitio/backage/edit/master/owners.txt) and make a pull request. Please add just the name(s) -- ids, repos, and packages will be obtained automatically!

Use something like [shields.io/json](https://shields.io/badges/dynamic-json-badge) with the endpoint below to make badges. Here's what they could look like for a few of the available properties:

<div align="center">

[![package](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fipitio.github.io%2Fbackage%2Farevindh%2Fpihole-speedtest%2Fpihole-speedtest.json&query=%24.package&logo=github&label=package&style=for-the-badge&color=black)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest)

[![downloads](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fipitio.github.io%2Fbackage%2Farevindh%2Fpihole-speedtest%2Fpihole-speedtest.json&query=%24.downloads&logo=github&label=pulls)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![size](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fipitio.github.io%2Fbackage%2Farevindh%2Fpihole-speedtest%2Fpihole-speedtest.json&query=%24.size&logo=github&label=size&color=indigo)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![latest](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fipitio.github.io%2Fbackage%2Farevindh%2Fpihole-speedtest%2Fpihole-speedtest.json&query=%24.version%5B%3F(%40.latest%3D%3Dtrue)%5D.tags%5B%3F(%40!%3D%22latest%22)%5D&logo=github&label=latest&color=darkgreen)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest)

</div>

## Database

All of the data is available under the [latest release](https://github.com/ipitio/backage/releases/latest). There are two classes of tables:

<details>

<summary>Package</summary>

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

<details>

<summary>Version</summary>

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

## Endpoint

The latest additions to the continually updated database, with two classes of objects:

<details>

<summary>Package</summary>

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
|    `raw_versions`     |    number    | Count of versions tracked                          |
|     `raw_tagged`      |    number    | Count of tagged versions tracked                   |
|    `raw_downloads`    |    number    | Count of all downloads                             |
| `raw_downloads_month` |    number    | Count of all downloads in the last month           |
| `raw_downloads_week`  |    number    | Count of all downloads in the last week            |
|  `raw_downloads_day`  |    number    | Count of all downloads in the last day             |
|       `version`       | object array | The versions of the package (see below)            |

</details>

<details>

<summary>Version</summary>

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

### URL

Replace `OWNER/REPO/PACKAGE` with their respective values:

```py
https://ipitio.github.io/backage/OWNER/REPO/PACKAGE.json
```

### JSONPath

You can query a package for its properties, like size or version:

```js
$.PROPERTY
```

```js
$.size
```

Versions can be filtered in and tags out:

```js
$.version[FILTER].PROPERTY
```

```js
$.version[?(@.latest == true)].tags[?(@ != "latest")]
```

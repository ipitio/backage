<div align="center">

[![logo](logo-b.webp)](https://github.com/ipitio/backage)

# [backage](https://github.com/ipitio/backage)

**It's all part and parcel**

---

[![packages](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fgithub.com%2Fipitio%2Fbackage%2Fraw%2Findex%2F.json&query=%24.packages&logo=github&logoColor=959da5&label=packages&labelColor=333a41&color=grey)](https://github.com/ipitio/backage/tree/index) [![build](https://github.com/ipitio/backage/actions/workflows/publish.yml/badge.svg)](https://github.com/ipitio/backage/pkgs/container/backage) [![built](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fgithub.com%2Fipitio%2Fbackage%2Fraw%2Findex%2F.json&query=%24.date&logo=github&logoColor=959da5&label=built&labelColor=333a41&color=purple)](https://github.com/ipitio/backage/releases/latest)

</div>

Ever wish you could show npm, gem, mvn, Gradle, NuGet, or GHCR badges for GitHub Packages? Or just query for the download counts? This endpoint makes that possible, using only free GitHub resources; the API doesn't, and has never, exposed the public metadata that other registries provide.

## Getting Started

If this is [`ipitio/backage`](https://github.com/ipitio/backage), all you have to do is **star the repo to get your public packages added!** The service's circular priority queue will update the [closed-loop system](https://github.com/ipitio/backage/releases/latest) with them within the next few hours. Additionally watching and forking the repo, and following the owner, are ways to increase their priority. Yes, I know, but these are the graphs GitHub has available.

> [!WARNING]
> For this to work, your profile must be set to [public](https://github.com/ipitio/backage/issues/34#issuecomment-2968850773).

Otherwise, if this is a fork, you'd prefer an alternative method, or your packages weren't added to the [index](https://github.com/ipitio/backage/tree/index) after a day, enter the case-sensitive name of each missing user or organization on a new line at the top of `owners.txt` [here](https://github.com/ipitio/backage/edit/master/owners.txt) and make a pull request.

> [!TIP]
> You only need to add the name(s), IDs are fetched as needed.

New packages won't be added until *all* existing ones are refreshed; you should also create an independent instance that'll update faster and more frequently, up to hourly. This centralized repo will then serve as a backup for all subsets of packages not in `optout.txt`. Simply fork just the `master` branch, choose one of the following options, and use the [Alternative URL](#alternative-url) when it changes.

> [!IMPORTANT]
> Your own packages will be picked up automatically! If you need to edit `owners.txt`, do so after the first run.

<details>
<summary>With Actions</summary>

1. Enable Actions from its tab
2. Enable all disabled workflows

> [!CAUTION]
> This will use a lot of minutes!

</details>

<details>
<summary>Self-Host</summary>

This is an example for `systemd`; adapt it to your needs. `-d -1` will run the script indefinitely -- see the top of `bkg.sh` for more details about the options. When passed to `update.sh`, as below, they must come last.

```bash
echo "[Unit]
Description=Run Backage
After=network.target
StartLimitIntervalSec=0

[Service]
Type=simple
Restart=always
RestartSec=5
ExecStart=/usr/bin/sh -c '       \
  GITHUB_TOKEN=<your PAT>       ;\
  GITHUB_OWNER=<your username>  ;\
  GITHUB_REPO=backage     ;\
  GITHUB_BRANCH=master ;\
  docker run -v \$\${PWD}:/app --env-file <(env | grep GITHUB) ghcr.io/ipitio/backage:master bash src/test/update.sh backage -m 0 -d -1'

[Install]
WantedBy=multi-user.target
" | sudo tee /etc/systemd/system/bkg.service
sudo systemctl daemon-reload
sudo systemctl enable --now bkg
```

> [!TIP]
> The token is optional if you're already logged in with `gh`.

</details>

## The Endpoint

```prolog
https://ipitio.github.io/backage/OWNER/[REPO/[PACKAGE]].FORMAT
```

Once the packages you're interested in have been added, replace the parameters with their respective values, scoping to your parsing needs, then access the latest data however you want. The format can be either `json` or `xml`.

> [!TIP]
> Use something like [shields.io/json](https://shields.io/badges/dynamic-json-badge) or [shields.io/xml](https://shields.io/badges/dynamic-xml-badge) to make badges like [this one](https://github.com/badges/shields/issues/5594#issuecomment-2157626147). You'll need the latter to evaluate expressions, like filters ([issue](https://github.com/ipitio/backage/issues/23)).

### Available Properties

<details>

<summary>Package</summary>

|       Property        |     Type     | Description                                             |
| :-------------------: | :----------: | ------------------------------------------------------- |
|      `owner_id`       |    number    | The ID of the owner                                     |
|     `owner_type`      |    string    | The type of owner (e.g. `users`)                        |
|    `package_type`     |    string    | The type of package (e.g. `container`)                  |
|        `owner`        |    string    | The owner of the package                                |
|        `repo`         |    string    | The repository of the package                           |
|       `package`       |    string    | The package name                                        |
|        `date`         |    string    | The most recent date the package was refreshed          |
|        `size`         |    string    | Formatted size of the latest version                    |
|      `versions`       |    string    | Formatted count of all versions recently tracked        |
|       `tagged`        |    string    | Formatted count of all tagged versions recently tracked |
|     `owner_rank`      |    string    | Formatted rank by downloads within the owner            |
|      `repo_rank`      |    string    | Formatted rank by downloads within the repository       |
|      `downloads`      |    string    | Formatted count of all downloads                        |
|   `downloads_month`   |    string    | Formatted count of all downloads in the last month      |
|   `downloads_week`    |    string    | Formatted count of all downloads in the last week       |
|    `downloads_day`    |    string    | Formatted count of all downloads in the last day        |
|      `raw_size`       |    number    | Size of the latest version, in bytes                    |
|    `raw_versions`     |    number    | Count of versions ever tracked                          |
|     `raw_tagged`      |    number    | Count of tagged versions ever tracked                   |
|   `raw_owner_rank`    |    number    | Rank by downloads within the owner                      |
|    `raw_repo_rank`    |    number    | Rank by downloads within the repository                 |
|    `raw_downloads`    |    number    | Count of all downloads                                  |
| `raw_downloads_month` |    number    | Count of all downloads in the last month                |
| `raw_downloads_week`  |    number    | Count of all downloads in the last week                 |
|  `raw_downloads_day`  |    number    | Count of all downloads in the last day                  |
|       `version`       | object array | The versions of the package (see below)                 |

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

### Query Syntax

<details>

<summary>JSON</summary>

You can query a package for its properties, like size or version:

```jboss-cli
$.PROPERTY
```

```jboss-cli
$.size
```

Versions may be filtered in and tags out:

```jboss-cli
$.version[FILTER].PROPERTY
```

```jboss-cli
$.version[?(@.latest)].tags[?(@!="latest")]
```

As can packages in `owner[/repo]/.json` files:

```jboss-cli
$.[FILTER].PROPERTY
```

</details>

<details>

<summary>XML</summary>

You can query a package for its properties, like size or version:

```prolog
/xml/PROPERTY
```

```prolog
/xml/size
```

Versions can be filtered in and tags out:

```prolog
/xml/version[FILTER]/PROPERTY
```

```prolog
/xml/version[./latest[.="true"]]/tags[.!="latest"]
```

As can packages in `owner[/repo]/.xml` files:

```prolog
/xml/package[FILTER]/PROPERTY
```

</details>

### Alternative URL

```prolog
https://github.com/ipitio/backage/raw/index/OWNER/[REPO/[PACKAGE]].FORMAT
```

The endpoint is also available here! This will change to your fork once it updates.

## JSON2XML Proxy

```prolog
https://ipitio.github.io/backage?json=https://URL/ENCODED/JSON
```

Use your own external JSON with this proxy to convert it into XML. This doesn't currently work with Shields. Try it out in your browser:

**<https://ipitio.github.io/backage?json=https://raw.githubusercontent.com/ipitio/backage/index/.json>**

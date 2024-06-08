# ghcr pulls

## JSON Endpoint for GHCR Total Downloads

This is a free service that you can use to get the total number of downloads for a GitHub Container Registry (GHCR) image, since they don't provide one. If we don't yet follow the image you're interested in, simply open an issue or pull request to add it to `pkg.txt`.

If you'd like to use this to add a badge to your repo's README, you can use the following Markdown snippet. Be sure to replace `<USER>`, `<REPO>`, and `<IMAGE>` with the appropriate values.

```markdown
![ghcr pulls](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fmaster%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22<USER>%22%20%26%26%20%40.repo%3D%3D%22<REPO>%22%20%26%26%20%40.image%3D%3D%22<IMAGE>%22)%5D.pulls)
```

Example:

[![pihole-speedtest/pulls](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fmaster%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.image%3D%3D%22pihole-speedtest%22)%5D.pulls&label=pulls)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest)

You can generate this yourself [here](https://shields.io/badges/dynamic-json-badge) with whatever custom options you want and the following:

URL

```markdown
https://raw.githubusercontent.com/ipitio/ghcr-pulls/master/index.json
```

JSONPath

```markdown
$[?(@.owner=="<USER>" && @.repo=="<REPO>" && @.image=="<IMAGE>")].pulls
```

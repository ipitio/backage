# ghcr.io pulls

## JSON Endpoint for GHCR Badge

Makes the pull count badge possible for these ghcr.io packages:


[//]: # (new ones will be added here)

Do you wish that you could make a badge that shows the number of GHCR pulls, like you can for Docker Hub? Now you can! GHCR itself doesn't provide an API endpoint for the pull count, so this repo scrapes the data from Github Packages for every image in `pkg.txt` daily. If we don't yet follow the image you're interested in, open an issue or pull request to add it (or just make a fork).

### Custom Badges

To make a badge, you can modify one of above ones or generate one with something like [shields.io](https://shields.io/badges/dynamic-json-badge) and these parameters:

#### URL

```markdown
https://raw.githubusercontent.com/ipitio/ghcr-pulls/master/index.json
```

#### JSONPath

You can show either a pretty value like 12K or the raw number like 12345.

##### Pretty Count

```markdown
$[?(@.owner=="<USER>" && @.repo=="<REPO>" && @.image=="<IMAGE>")].pulls
```

##### Raw Count

```markdown
$[?(@.owner=="<USER>" && @.repo=="<REPO>" && @.image=="<IMAGE>")].raw_pulls
```

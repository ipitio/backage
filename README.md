<div align="center">

# Badpack

## Badges for GitHub Packages

A [JSON Endpoint](#endpoint) backed by a [SQLite Dataset](#database) to supplement the API.

</div>

---

With the help of GitHub Actions, `update.sh` makes the following badges possible. To make a badge, you can modify one of the badges below or generate one with something like [shields.io/json](https://shields.io/badges/dynamic-json-badge) and [these parameters](#url). If we don't yet follow a package, you can either:

* open an issue or
* add it on a new line in `pkg.txt` on your own fork [here](https://github.com/ipitio/ghcr-pulls/edit/master/pkg.txt) and make a pull request.

[![users/container/arevindh/pihole-speedtest/pihole-speedtest/downloads](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.package%3D%3D%22pihole-speedtest%22)%5D.downloads&label=pihole-speedtest-all)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![users/container/arevindh/pihole-speedtest/pihole-speedtest/downloads/month](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.package%3D%3D%22pihole-speedtest%22)%5D.downloads_month&label=month)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![users/container/arevindh/pihole-speedtest/pihole-speedtest/downloads/week](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.package%3D%3D%22pihole-speedtest%22)%5D.downloads_week&label=week)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![users/container/arevindh/pihole-speedtest/pihole-speedtest/downloads/day](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.package%3D%3D%22pihole-speedtest%22)%5D.downloads_day&label=day)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![users/container/arevindh/pihole-speedtest/pihole-speedtest/size](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.package%3D%3D%22pihole-speedtest%22)%5D.size&label=size&color=a0a)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![users/container/arevindh/pihole-speedtest/pihole-speedtest/releases](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.package%3D%3D%22pihole-speedtest%22)%5D.tagged&label=releases&color=0a0)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![users/container/arevindh/pihole-speedtest/pihole-speedtest/latest](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner_type%3D%3D%22users%22%20%26%26%20%40.package_type%3D%3D%22container%22%20%26%26%20%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.package%3D%3D%22pihole-speedtest%22)%5D.version%5B%3F(%40.tags.indexOf(%22latest%22)!%3D-1)%5D.tags%5B%3F(%40!%3D%22latest%22)%5D&label=latest&color=0a0)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![users/container/arevindh/pihole-speedtest/pihole-speedtest/versions](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.package%3D%3D%22pihole-speedtest%22)%5D.versions&label=versions&color=0a0)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![users/container/arevindh/pihole-speedtest/pihole-speedtest/newest](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22arevindh%22%20%26%26%20%40.repo%3D%3D%22pihole-speedtest%22%20%26%26%20%40.package%3D%3D%22pihole-speedtest%22)%5D.version%5B%3F(%40.newest%3D%3Dtrue)%5D.name&label=newest&color=0a0)](https://github.com/arevindh/pihole-speedtest/pkgs/container/pihole-speedtest) [![users/container/drakkan/sftpgo/sftpgo](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22drakkan%22%20%26%26%20%40.repo%3D%3D%22sftpgo%22%20%26%26%20%40.package%3D%3D%22sftpgo%22)%5D.downloads&label=sftpgo)](https://github.com/drakkan/sftpgo/pkgs/container/sftpgo) [![users/container/aquasecurity/trivy/trivy](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22aquasecurity%22%20%26%26%20%40.repo%3D%3D%22trivy%22%20%26%26%20%40.package%3D%3D%22trivy%22)%5D.downloads&label=trivy)](https://github.com/aquasecurity/trivy/pkgs/container/trivy) [![users/container/FlareSolverr/FlareSolverr/flaresolverr](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22FlareSolverr%22%20%26%26%20%40.repo%3D%3D%22FlareSolverr%22%20%26%26%20%40.package%3D%3D%22flaresolverr%22)%5D.downloads&label=flaresolverr)](https://github.com/FlareSolverr/FlareSolverr/pkgs/container/flaresolverr) [![users/container/Mailu/Mailu/clamav](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22Mailu%22%20%26%26%20%40.repo%3D%3D%22Mailu%22%20%26%26%20%40.package%3D%3D%22clamav%22)%5D.downloads&label=clamav)](https://github.com/Mailu/Mailu/pkgs/container/clamav) [![users/container/k3d-io/k3d/k3d-tools](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22k3d-io%22%20%26%26%20%40.repo%3D%3D%22k3d%22%20%26%26%20%40.package%3D%3D%22k3d-tools%22)%5D.downloads&label=k3d-tools)](https://github.com/k3d-io/k3d/pkgs/container/k3d-tools) [![users/container/gethomepage/homepage/homepage](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22gethomepage%22%20%26%26%20%40.repo%3D%3D%22homepage%22%20%26%26%20%40.package%3D%3D%22homepage%22)%5D.downloads&label=homepage)](https://github.com/gethomepage/homepage/pkgs/container/homepage) [![users/container/ajnart/homarr/homarr](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22ajnart%22%20%26%26%20%40.repo%3D%3D%22homarr%22%20%26%26%20%40.package%3D%3D%22homarr%22)%5D.downloads&label=homarr)](https://github.com/ajnart/homarr/pkgs/container/homarr) [![users/container/mealie-recipes/mealie/mealie](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22mealie-recipes%22%20%26%26%20%40.repo%3D%3D%22mealie%22%20%26%26%20%40.package%3D%3D%22mealie%22)%5D.downloads&label=mealie)](https://github.com/mealie-recipes/mealie/pkgs/container/mealie) [![users/container/containerd/containerd/busybox](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22containerd%22%20%26%26%20%40.repo%3D%3D%22containerd%22%20%26%26%20%40.package%3D%3D%22busybox%22)%5D.downloads&label=busybox)](https://github.com/containerd/containerd/pkgs/container/busybox) [![users/container/containrrr/watchtower/watchtower](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22containrrr%22%20%26%26%20%40.repo%3D%3D%22watchtower%22%20%26%26%20%40.package%3D%3D%22watchtower%22)%5D.downloads&label=watchtower)](https://github.com/containrrr/watchtower/pkgs/container/watchtower) [![users/container/coollabsio/coolify/coolify](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22coollabsio%22%20%26%26%20%40.repo%3D%3D%22coolify%22%20%26%26%20%40.package%3D%3D%22coolify%22)%5D.downloads&label=coolify)](https://github.com/coollabsio/coolify/pkgs/container/coolify) [![users/container/mastodon/mastodon/mastodon](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22mastodon%22%20%26%26%20%40.repo%3D%3D%22mastodon%22%20%26%26%20%40.package%3D%3D%22mastodon%22)%5D.downloads&label=mastodon)](https://github.com/mastodon/mastodon/pkgs/container/mastodon) [![users/container/authelia/authelia/authelia](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22authelia%22%20%26%26%20%40.repo%3D%3D%22authelia%22%20%26%26%20%40.package%3D%3D%22authelia%22)%5D.downloads&label=authelia)](https://github.com/authelia/authelia/pkgs/container/authelia) [![users/container/docker-mailserver/docker-mailserver/docker-mailserver](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22docker-mailserver%22%20%26%26%20%40.repo%3D%3D%22docker-mailserver%22%20%26%26%20%40.package%3D%3D%22docker-mailserver%22)%5D.downloads&label=docker-mailserver)](https://github.com/docker-mailserver/docker-mailserver/pkgs/container/docker-mailserver) [![users/container/amacneil/dbmate/dbmate](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22amacneil%22%20%26%26%20%40.repo%3D%3D%22dbmate%22%20%26%26%20%40.package%3D%3D%22dbmate%22)%5D.downloads&label=dbmate)](https://github.com/amacneil/dbmate/pkgs/container/dbmate) [![users/container/br3ndonland/inboard/inboard](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22br3ndonland%22%20%26%26%20%40.repo%3D%3D%22inboard%22%20%26%26%20%40.package%3D%3D%22inboard%22)%5D.downloads&label=inboard)](https://github.com/br3ndonland/inboard/pkgs/container/inboard) [![users/container/fission/fission/fission-bundle](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22fission%22%20%26%26%20%40.repo%3D%3D%22fission%22%20%26%26%20%40.package%3D%3D%22fission-bundle%22)%5D.downloads&label=fission-bundle)](https://github.com/fission/fission/pkgs/container/fission-bundle) [![users/container/anchore/syft/syft](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22anchore%22%20%26%26%20%40.repo%3D%3D%22syft%22%20%26%26%20%40.package%3D%3D%22syft%22)%5D.downloads&label=syft)](https://github.com/anchore/syft/pkgs/container/syft) [![users/container/Lissy93/dashy/dashy](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22Lissy93%22%20%26%26%20%40.repo%3D%3D%22dashy%22%20%26%26%20%40.package%3D%3D%22dashy%22)%5D.downloads&label=dashy)](https://github.com/Lissy93/dashy/pkgs/container/dashy) [![users/container/dani-garcia/vaultwarden/vaultwarden](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22dani-garcia%22%20%26%26%20%40.repo%3D%3D%22vaultwarden%22%20%26%26%20%40.package%3D%3D%22vaultwarden%22)%5D.downloads&label=vaultwarden)](https://github.com/dani-garcia/vaultwarden/pkgs/container/vaultwarden) [![users/container/ko-build/ko/ko](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22ko-build%22%20%26%26%20%40.repo%3D%3D%22ko%22%20%26%26%20%40.package%3D%3D%22ko%22)%5D.downloads&label=ko)](https://github.com/ko-build/ko/pkgs/container/ko) [![orgs/container/argoproj/argo-cd/argo-cd%252Fargocd](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22argoproj%22%20%26%26%20%40.repo%3D%3D%22argo-cd%22%20%26%26%20%40.package%3D%3D%22argo-cd%252Fargocd%22)%5D.downloads&label=argo-cd%2Fargocd)](https://github.com/argoproj/argo-cd/pkgs/container/argo-cd%252Fargocd) [![users/container/LizardByte/Sunshine/sunshine](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22LizardByte%22%20%26%26%20%40.repo%3D%3D%22Sunshine%22%20%26%26%20%40.package%3D%3D%22sunshine%22)%5D.downloads&label=sunshine)](https://github.com/LizardByte/Sunshine/pkgs/container/sunshine) [![users/container/nginx-proxy/nginx-proxy/nginx-proxy](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22nginx-proxy%22%20%26%26%20%40.repo%3D%3D%22nginx-proxy%22%20%26%26%20%40.package%3D%3D%22nginx-proxy%22)%5D.downloads&label=nginx-proxy)](https://github.com/nginx-proxy/nginx-proxy/pkgs/container/nginx-proxy) [![users/container/n8n-io/n8n/n8n](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22n8n-io%22%20%26%26%20%40.repo%3D%3D%22n8n%22%20%26%26%20%40.package%3D%3D%22n8n%22)%5D.downloads&label=n8n)](https://github.com/n8n-io/n8n/pkgs/container/n8n) [![users/container/labring/sealos/sealos-patch](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22labring%22%20%26%26%20%40.repo%3D%3D%22sealos%22%20%26%26%20%40.package%3D%3D%22sealos-patch%22)%5D.downloads&label=sealos-patch)](https://github.com/labring/sealos/pkgs/container/sealos-patch) [![users/container/hugomods/docker/hugo](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22hugomods%22%20%26%26%20%40.repo%3D%3D%22docker%22%20%26%26%20%40.package%3D%3D%22hugo%22)%5D.downloads&label=hugo)](https://github.com/hugomods/docker/pkgs/container/hugo) [![users/container/benbusby/whoogle-search/whoogle-search](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22benbusby%22%20%26%26%20%40.repo%3D%3D%22whoogle-search%22%20%26%26%20%40.package%3D%3D%22whoogle-search%22)%5D.downloads&label=whoogle-search)](https://github.com/benbusby/whoogle-search/pkgs/container/whoogle-search) [![users/container/anchore/grype/grype](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22anchore%22%20%26%26%20%40.repo%3D%3D%22grype%22%20%26%26%20%40.package%3D%3D%22grype%22)%5D.downloads&label=grype)](https://github.com/anchore/grype/pkgs/container/grype) [![users/container/gogs/gogs/gogs](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22gogs%22%20%26%26%20%40.repo%3D%3D%22gogs%22%20%26%26%20%40.package%3D%3D%22gogs%22)%5D.downloads&label=gogs)](https://github.com/gogs/gogs/pkgs/container/gogs) [![users/container/m1k1o/neko/neko%252Fxfce](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22m1k1o%22%20%26%26%20%40.repo%3D%3D%22neko%22%20%26%26%20%40.package%3D%3D%22neko%252Fxfce%22)%5D.downloads&label=neko%2Fxfce)](https://github.com/m1k1o/neko/pkgs/container/neko%252Fxfce) [![orgs/container/telekom-security/tpotce/conpot](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22telekom-security%22%20%26%26%20%40.repo%3D%3D%22tpotce%22%20%26%26%20%40.package%3D%3D%22conpot%22)%5D.downloads&label=conpot)](https://github.com/telekom-security/tpotce/pkgs/container/conpot) [![users/container/netdata/netdata/netdata](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22netdata%22%20%26%26%20%40.repo%3D%3D%22netdata%22%20%26%26%20%40.package%3D%3D%22netdata%22)%5D.downloads&label=netdata)](https://github.com/netdata/netdata/pkgs/container/netdata) [![orgs/container/screego/server/server](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22screego%22%20%26%26%20%40.repo%3D%3D%22server%22%20%26%26%20%40.package%3D%3D%22server%22)%5D.downloads&label=server)](https://github.com/screego/server/pkgs/container/server) [![users/container/nginx-proxy/docker-gen/docker-gen](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22nginx-proxy%22%20%26%26%20%40.repo%3D%3D%22docker-gen%22%20%26%26%20%40.package%3D%3D%22docker-gen%22)%5D.downloads&label=docker-gen)](https://github.com/nginx-proxy/docker-gen/pkgs/container/docker-gen) [![users/container/nginx-proxy/acme-companion/acme-companion](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22nginx-proxy%22%20%26%26%20%40.repo%3D%3D%22acme-companion%22%20%26%26%20%40.package%3D%3D%22acme-companion%22)%5D.downloads&label=acme-companion)](https://github.com/nginx-proxy/acme-companion/pkgs/container/acme-companion) [![users/container/eggplants/ghcr-badge/ghcr-badge](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22eggplants%22%20%26%26%20%40.repo%3D%3D%22ghcr-badge%22%20%26%26%20%40.package%3D%3D%22ghcr-badge%22)%5D.downloads&label=ghcr-badge)](https://github.com/eggplants/ghcr-badge/pkgs/container/ghcr-badge) [![users/container/home-assistant/operating-system/haos-builder](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22home-assistant%22%20%26%26%20%40.repo%3D%3D%22operating-system%22%20%26%26%20%40.package%3D%3D%22haos-builder%22)%5D.downloads&label=haos-builder)](https://github.com/home-assistant/operating-system/pkgs/container/haos-builder) [![users/container/dockur/windows/windows](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22dockur%22%20%26%26%20%40.repo%3D%3D%22windows%22%20%26%26%20%40.package%3D%3D%22windows%22)%5D.downloads&label=windows)](https://github.com/dockur/windows/pkgs/container/windows) [![users/container/dockur/lemmy/lemmy](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22dockur%22%20%26%26%20%40.repo%3D%3D%22lemmy%22%20%26%26%20%40.package%3D%3D%22lemmy%22)%5D.downloads&label=lemmy)](https://github.com/dockur/lemmy/pkgs/container/lemmy) [![users/container/dockur/lemmy-ui/lemmy-ui](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22dockur%22%20%26%26%20%40.repo%3D%3D%22lemmy-ui%22%20%26%26%20%40.package%3D%3D%22lemmy-ui%22)%5D.downloads&label=lemmy-ui)](https://github.com/dockur/lemmy-ui/pkgs/container/lemmy-ui) [![users/container/dobtc/btc-rpc-proxy/btc-rpc-proxy](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22dobtc%22%20%26%26%20%40.repo%3D%3D%22btc-rpc-proxy%22%20%26%26%20%40.package%3D%3D%22btc-rpc-proxy%22)%5D.downloads&label=btc-rpc-proxy)](https://github.com/dobtc/btc-rpc-proxy/pkgs/container/btc-rpc-proxy) [![users/container/browserless/browserless/firefox](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22browserless%22%20%26%26%20%40.repo%3D%3D%22browserless%22%20%26%26%20%40.package%3D%3D%22firefox%22)%5D.downloads&label=firefox)](https://github.com/browserless/browserless/pkgs/container/firefox) [![users/container/dockur/snort/snort](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22dockur%22%20%26%26%20%40.repo%3D%3D%22snort%22%20%26%26%20%40.package%3D%3D%22snort%22)%5D.downloads&label=snort)](https://github.com/dockur/snort/pkgs/container/snort) [![users/container/dockur/portainer-backup/portainer-backup](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22dockur%22%20%26%26%20%40.repo%3D%3D%22portainer-backup%22%20%26%26%20%40.package%3D%3D%22portainer-backup%22)%5D.downloads&label=portainer-backup)](https://github.com/dockur/portainer-backup/pkgs/container/portainer-backup) [![users/container/dobtc/bitcoin/bitcoin](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22dobtc%22%20%26%26%20%40.repo%3D%3D%22bitcoin%22%20%26%26%20%40.package%3D%3D%22bitcoin%22)%5D.downloads&label=bitcoin)](https://github.com/dobtc/bitcoin/pkgs/container/bitcoin) [![users/container/dockur/windows-arm/windows-arm](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22dockur%22%20%26%26%20%40.repo%3D%3D%22windows-arm%22%20%26%26%20%40.package%3D%3D%22windows-arm%22)%5D.downloads&label=windows-arm)](https://github.com/dockur/windows-arm/pkgs/container/windows-arm) [![users/container/dockur/tor/tor](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22dockur%22%20%26%26%20%40.repo%3D%3D%22tor%22%20%26%26%20%40.package%3D%3D%22tor%22)%5D.downloads&label=tor)](https://github.com/dockur/tor/pkgs/container/tor) [![users/container/dockur/strfry/strfry](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22dockur%22%20%26%26%20%40.repo%3D%3D%22strfry%22%20%26%26%20%40.package%3D%3D%22strfry%22)%5D.downloads&label=strfry)](https://github.com/dockur/strfry/pkgs/container/strfry) [![users/container/macbre/push-to-ghcr/push-to-ghcr](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22macbre%22%20%26%26%20%40.repo%3D%3D%22push-to-ghcr%22%20%26%26%20%40.package%3D%3D%22push-to-ghcr%22)%5D.downloads&label=push-to-ghcr)](https://github.com/macbre/push-to-ghcr/pkgs/container/push-to-ghcr) [![users/container/dockur/munin/munin](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22dockur%22%20%26%26%20%40.repo%3D%3D%22munin%22%20%26%26%20%40.package%3D%3D%22munin%22)%5D.downloads&label=munin)](https://github.com/dockur/munin/pkgs/container/munin) [![users/container/dockur/chrony/chrony](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22dockur%22%20%26%26%20%40.repo%3D%3D%22chrony%22%20%26%26%20%40.package%3D%3D%22chrony%22)%5D.downloads&label=chrony)](https://github.com/dockur/chrony/pkgs/container/chrony) [![users/container/dockur/samba/samba](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22dockur%22%20%26%26%20%40.repo%3D%3D%22samba%22%20%26%26%20%40.package%3D%3D%22samba%22)%5D.downloads&label=samba)](https://github.com/dockur/samba/pkgs/container/samba) [![users/container/dobtc/lndmanage/lndmanage](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22dobtc%22%20%26%26%20%40.repo%3D%3D%22lndmanage%22%20%26%26%20%40.package%3D%3D%22lndmanage%22)%5D.downloads&label=lndmanage)](https://github.com/dobtc/lndmanage/pkgs/container/lndmanage) [![users/container/dockur/statping/statping](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22dockur%22%20%26%26%20%40.repo%3D%3D%22statping%22%20%26%26%20%40.package%3D%3D%22statping%22)%5D.downloads&label=statping)](https://github.com/dockur/statping/pkgs/container/statping) [![users/container/dockur/dnsmasq/dnsmasq](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22dockur%22%20%26%26%20%40.repo%3D%3D%22dnsmasq%22%20%26%26%20%40.package%3D%3D%22dnsmasq%22)%5D.downloads&label=dnsmasq)](https://github.com/dockur/dnsmasq/pkgs/container/dnsmasq) [![users/container/dockur/macos/macos](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22dockur%22%20%26%26%20%40.repo%3D%3D%22macos%22%20%26%26%20%40.package%3D%3D%22macos%22)%5D.downloads&label=macos)](https://github.com/dockur/macos/pkgs/container/macos) [![users/container/emissary-ingress/emissary/emissary](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fdev%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22emissary-ingress%22%20%26%26%20%40.repo%3D%3D%22emissary%22%20%26%26%20%40.package%3D%3D%22emissary%22)%5D.downloads&label=emissary)](https://github.com/emissary-ingress/emissary/pkgs/container/emissary)

### Endpoint

Refreshed daily, the endpoint is always up-to-date.

#### URL

```markdown
https://raw.githubusercontent.com/ipitio/ghcr-pulls/master/index.json
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

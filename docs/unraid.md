# Installing MediaHub on Unraid

## 1. Add the container

1. **Docker → Add Container → Template repositories**: or copy
   [`unraid/my-mediahub.xml`](../unraid/my-mediahub.xml) to
   `/boot/config/plugins/dockerMan/templates-user/my-mediahub.xml` on the flash drive, then pick
   **MediaHub** from the template list.
2. Check the settings:
   - **Web UI port**: `8080` (change the host side if it's taken).
   - **Config**: `/mnt/user/appdata/mediahub`.
   - **Server name**: e.g. `Skynet` (your brother might use `Brother Server`).
   - **Network type**: see step 3 if you use InfiniDysk.
3. **Apply**, then open the Web UI and create the admin account.

> The image is `ghcr.io/fahmula/mediahub`, published with each release and tagged `X.Y.Z`, `X.Y`
> and `latest`.

## 2. Add your services

**Servers → Add service**, choose the type, enter the URL as reachable *from the MediaHub
container* and the API key, then **Test connection** before saving.

Container names (e.g. `http://sonarr:8989`) only resolve when both containers share a custom
Docker network; otherwise use the Unraid IP and published port (e.g. `http://192.168.1.10:8989`).

## 3. InfiniDysk: backend access

InfiniDysk only exposes its health checks, Prometheus metrics (cache statistics) and the live
stream feed on its **internal backend port 8080**, and the live feed only accepts its internal
key. To enable them:

1. Create a custom network once (Unraid terminal): `docker network create media`.
2. Set **Network type: `media`** on both the InfiniDysk and MediaHub containers.
3. In InfiniDysk's template, add a variable `FRONTEND_BACKEND_API_KEY` with a long random value
   (e.g. `openssl rand -hex 32`). Without pinning it, it changes on every restart.
4. In MediaHub's InfiniDysk service: **Backend URL** `http://infinidysk:8080`, **Internal API
   key** = that value.

Don't publish port 8080 of InfiniDysk to your LAN for this; the shared network is enough.

## 4. Remote access and peers

MediaHub is designed for a private network such as Tailscale. For HTTPS, `tailscale serve`
in front of MediaHub works well; then set **Secure cookies** to `true`.

To link your install with another one (read-only), on the server to be read go to
**Servers → Access tokens**, create a token and copy it (it's shown once). On the other server,
**Servers → Link a server**, enter the first server's Tailscale URL (e.g.
`https://mediahub.your-tailnet.ts.net`) and paste the token. **Test connection** checks the URL
and token before saving. Repeat in the other direction for both to see each other. Client IPs,
file paths and credentials never leave a server (notifications have paths removed too); revoking the token cuts access immediately.

### Install as an app

Over HTTPS (e.g. `tailscale serve`), MediaHub can be installed on a phone or desktop: Chrome or
Edge → **Install app** / **Add to Home screen**, Safari on iOS → **Share → Add to Home Screen**.
It then opens full screen with its own icon and shows an offline page when the server can't be
reached. Browsers only allow this over HTTPS (or `localhost`); over plain `http://` the site
still works, just as a normal tab or bookmark.

## 5. Backups

Back up `/mnt/user/appdata/mediahub` (the Appdata Backup plugin covers it). `secret.key` and
`mediahub.db` belong together: the key decrypts the stored API keys.

## Troubleshooting

- **"Another MediaHub process holds /config/mediahub.lock"**: two containers point at the same
  appdata folder. Each install needs its own.
- **"Unexpected redirect … check the base URL"**: the URL is missing a URL base (e.g.
  `/sonarr`) or points at a login page.
- **"Stored credentials can't be decrypted"**: `secret.key` was replaced or lost. Re-enter
  the API keys under Servers.

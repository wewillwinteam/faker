# Stage 1: the giddygiddyboy practice driver (Go, MIT, justnau/giddygiddyboy-mech-royale), built with its own pinned image
FROM golang:1.26.1-bookworm@sha256:ab3d6955bbc813a0f3fdf220c1d817dd89c0b3f283777db8ece4a32fe7858edd AS giddy
WORKDIR /src
COPY drivers/giddy/go.mod ./
COPY drivers/giddy/mech ./mech
COPY drivers/giddy/cmd/bot ./cmd/bot
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /mechbot ./cmd/bot

# Stage 2: the TTUN practice driver (TypeScript, MIT, varchaskukreja/TTUN), bundled to one file with pinned Bun 1.4.4
FROM oven/bun:1.4.4-slim@sha256:1846adf22496bd711036aae8be147e57bdebbaea2e5264e3e0b5af1db187d9a4 AS ttun
WORKDIR /src
COPY drivers/ttun/bot.ts drivers/ttun/map.ts drivers/ttun/mechfight.ts ./
RUN bun build /src/bot.ts --target=bun --outfile=/src/bot.js

# Final: our bot (python 3.13.18-slim, pinned) plus the driver binaries
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /bot
COPY mechfight.py bot.py nav.py params.json driver.py lance_bot.py lance_plus.py LICENSE-lance ./
COPY brain ./brain
COPY drivers/giddy/LICENSE ./drivers/giddy/LICENSE
COPY --from=giddy /mechbot ./drivers/giddy/mechbot
COPY drivers/ttun/LICENSE ./drivers/ttun/LICENSE
COPY --from=ttun /src/bot.js ./drivers/ttun/bot.js
COPY --from=ttun /usr/local/bin/bun /usr/local/bin/bun
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]

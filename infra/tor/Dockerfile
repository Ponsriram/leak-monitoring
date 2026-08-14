# Minimal Tor SOCKS proxy.
#
# Built rather than pulled so the torrc is visible and version-controlled — circuit lifetime
# and the number of SOCKS ports are tuning decisions worth reviewing, not defaults to inherit
# silently from someone else's image.

FROM alpine:3.21

RUN apk add --no-cache tor curl

COPY torrc /etc/tor/torrc

USER tor

EXPOSE 9050 9051 9052

# Tor is up once the SOCKS port accepts connections. Checking the port rather than making a
# real request avoids generating traffic just to answer a healthcheck.
HEALTHCHECK --interval=20s --timeout=10s --start-period=60s --retries=5 \
    CMD nc -z 127.0.0.1 9050 || exit 1

ENTRYPOINT ["tor", "-f", "/etc/tor/torrc"]

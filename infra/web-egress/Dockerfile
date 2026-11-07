FROM alpine:3.24.3

RUN apk add --no-cache iproute2 nftables python3

COPY rules.nft /etc/web-egress/rules.nft
COPY hermes-rules.nft /etc/web-egress/hermes-rules.nft
COPY hermes_proxy.py /usr/local/bin/hermes-egress-proxy
COPY bootstrap.nft /etc/web-egress/bootstrap.nft
COPY entrypoint.sh /usr/local/bin/web-egress-entrypoint
COPY readiness.py /usr/local/bin/web-egress-readiness
RUN chmod 0555 /usr/local/bin/web-egress-entrypoint \
    && chmod 0555 /usr/local/bin/web-egress-readiness \
    && chmod 0555 /usr/local/bin/hermes-egress-proxy \
    && chmod 0444 /etc/web-egress/bootstrap.nft /etc/web-egress/rules.nft \
       /etc/web-egress/hermes-rules.nft

HEALTHCHECK --interval=2s --timeout=2s --start-period=1s --retries=5 \
  CMD python3 /usr/local/bin/web-egress-readiness --check --port "$WEB_EGRESS_READY_PORT"

ENTRYPOINT ["/usr/local/bin/web-egress-entrypoint"]

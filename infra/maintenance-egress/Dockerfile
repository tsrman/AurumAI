FROM alpine:3.22.1
RUN apk add --no-cache python3 nftables
COPY guard.py /opt/guard.py
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=3 CMD python3 /opt/guard.py check
ENTRYPOINT ["python3", "/opt/guard.py"]

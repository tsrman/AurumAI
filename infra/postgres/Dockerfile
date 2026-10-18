# Keep the existing pgvector PostgreSQL16 data format and add controlled
# partition maintenance. pg_partman background worker is deliberately not loaded.
FROM pgvector/pgvector:pg16
COPY install-partman.sh /usr/local/bin/install-partman.sh
RUN sh /usr/local/bin/install-partman.sh
COPY partman.sql /docker-entrypoint-initdb.d/03-partman.sql

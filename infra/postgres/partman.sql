-- Applied to the business database before its partition migration.
CREATE SCHEMA IF NOT EXISTS partman;
CREATE EXTENSION IF NOT EXISTS pg_partman WITH SCHEMA partman VERSION '5.5.0';

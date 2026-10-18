-- Disposable phaseb-lifecycle-test only. Never a production role installer.
\set ON_ERROR_STOP on
BEGIN;
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='lifecycle_data_operator') THEN
    CREATE ROLE lifecycle_data_operator LOGIN PASSWORD 'lifecycle-data-operator-password';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='lifecycle_rag_operator') THEN
    CREATE ROLE lifecycle_rag_operator LOGIN PASSWORD 'lifecycle-rag-operator-password';
  END IF;
END $$;
GRANT aurum_ai_lifecycle TO lifecycle_data_operator;
GRANT rag_lifecycle TO lifecycle_rag_operator;
GRANT CONNECT ON DATABASE aurum_ai TO lifecycle_data_operator;
GRANT CONNECT ON DATABASE rag_platform TO lifecycle_rag_operator;
COMMIT;

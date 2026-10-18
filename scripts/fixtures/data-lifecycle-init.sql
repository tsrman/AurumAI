-- Disposable acceptance credentials; this file is mounted only by the standalone fixture.
CREATE ROLE rag_app LOGIN PASSWORD 'lifecycle-rag-password';
CREATE DATABASE rag_platform OWNER rag_app;
CREATE DATABASE rag_platform_test OWNER rag_app;

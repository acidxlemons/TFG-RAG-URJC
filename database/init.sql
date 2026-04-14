-- ============================================
-- database/init.sql
-- Initial schema for JARVIS RAG
-- ============================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================
-- USERS
-- ============================================

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    azure_id VARCHAR(255) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    name VARCHAR(255),
    role VARCHAR(50) DEFAULT 'user',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    preferences JSONB DEFAULT '{}'::jsonb,
    is_active BOOLEAN DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_users_azure_id ON users(azure_id);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active);

COMMENT ON TABLE users IS 'Application users from Azure AD';

-- ============================================
-- CONVERSATIONS
-- ============================================

CREATE TABLE IF NOT EXISTS conversations (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(500),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_archived BOOLEAN DEFAULT FALSE,
    summary TEXT,
    summary_generated_at TIMESTAMP,
    metadata JSONB DEFAULT '{}'::jsonb,
    tags TEXT[]
);

CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations(user_id);
CREATE INDEX IF NOT EXISTS idx_conversations_updated_at ON conversations(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversations_archived ON conversations(is_archived);

COMMENT ON TABLE conversations IS 'Chat conversations per user';

-- ============================================
-- MESSAGES
-- ============================================

CREATE TABLE IF NOT EXISTS messages (
    id SERIAL PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role VARCHAR(50) NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    sources_used JSONB DEFAULT '[]'::jsonb,
    retrieval_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    tokens_used INTEGER DEFAULT 0,
    processing_time_ms INTEGER,
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_role ON messages(role);
CREATE INDEX IF NOT EXISTS idx_messages_conversation_id_created_at ON messages(conversation_id, created_at);

COMMENT ON TABLE messages IS 'Individual messages in conversations';

-- ============================================
-- DOCUMENT REGISTRY
-- ============================================

CREATE TABLE IF NOT EXISTS indexed_documents (
    id SERIAL PRIMARY KEY,
    filename VARCHAR(500) NOT NULL,
    source_path TEXT NOT NULL,
    source_type VARCHAR(50) NOT NULL CHECK (source_type IN ('upload', 'sharepoint', 'scrape', 'web')),
    file_hash VARCHAR(64) UNIQUE NOT NULL,
    file_size BIGINT,
    mime_type VARCHAR(100),
    page_count INTEGER,
    chunk_count INTEGER,
    from_ocr BOOLEAN DEFAULT FALSE,
    indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    indexed_by INTEGER REFERENCES users(id),
    metadata JSONB DEFAULT '{}'::jsonb,
    status VARCHAR(50) DEFAULT 'indexed' CHECK (status IN ('processing', 'indexed', 'failed', 'deleted'))
);

CREATE INDEX IF NOT EXISTS idx_documents_filename ON indexed_documents(filename);
CREATE INDEX IF NOT EXISTS idx_documents_source_type ON indexed_documents(source_type);
CREATE INDEX IF NOT EXISTS idx_documents_indexed_at ON indexed_documents(indexed_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_status ON indexed_documents(status);

COMMENT ON TABLE indexed_documents IS 'Registry of indexed documents';

-- ============================================
-- INGESTION STATUS
-- ============================================

CREATE TABLE IF NOT EXISTS ingestion_status (
    filename VARCHAR(255) PRIMARY KEY,
    status VARCHAR(50) NOT NULL,
    message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ingestion_status_updated_at ON ingestion_status(updated_at DESC);

COMMENT ON TABLE ingestion_status IS 'Current document ingestion status';

-- ============================================
-- SHAREPOINT SYNC
-- ============================================

CREATE TABLE IF NOT EXISTS sharepoint_sync (
    id SERIAL PRIMARY KEY,
    site_id VARCHAR(255) NOT NULL,
    folder_path TEXT NOT NULL,
    delta_token TEXT,
    last_sync TIMESTAMP,
    subscription_id VARCHAR(255),
    subscription_expires TIMESTAMP,
    is_active BOOLEAN DEFAULT TRUE
);

COMMENT ON TABLE sharepoint_sync IS 'SharePoint synchronization state';

-- ============================================
-- AUDIT LOG
-- ============================================

CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    action VARCHAR(100) NOT NULL,
    resource_type VARCHAR(50),
    resource_id INTEGER,
    details JSONB DEFAULT '{}'::jsonb,
    ip_address INET,
    user_agent TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audit_user_id ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_log(created_at DESC);

COMMENT ON TABLE audit_log IS 'Audit log for user actions';

-- ============================================
-- FUNCTIONS AND TRIGGERS
-- ============================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS update_conversations_updated_at ON conversations;
CREATE TRIGGER update_conversations_updated_at
    BEFORE UPDATE ON conversations
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_ingestion_status_updated_at ON ingestion_status;
CREATE TRIGGER update_ingestion_status_updated_at
    BEFORE UPDATE ON ingestion_status
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- ============================================
-- VIEWS
-- ============================================

CREATE OR REPLACE VIEW conversation_stats AS
SELECT
    c.id,
    c.user_id,
    c.title,
    c.created_at,
    c.updated_at,
    COUNT(m.id) AS message_count,
    MAX(m.created_at) AS last_message_at,
    COALESCE(SUM(m.tokens_used), 0) AS total_tokens
FROM conversations c
LEFT JOIN messages m ON c.id = m.conversation_id
GROUP BY c.id;

CREATE OR REPLACE VIEW user_activity AS
SELECT
    u.id,
    u.name,
    u.email,
    COUNT(DISTINCT c.id) AS conversation_count,
    COUNT(m.id) AS message_count,
    MAX(m.created_at) AS last_activity
FROM users u
LEFT JOIN conversations c ON u.id = c.user_id
LEFT JOIN messages m ON c.id = m.conversation_id
GROUP BY u.id;

CREATE OR REPLACE VIEW documents_by_source AS
SELECT
    source_type,
    COUNT(*) AS document_count,
    COALESCE(SUM(chunk_count), 0) AS total_chunks,
    COALESCE(SUM(file_size), 0) AS total_size,
    COUNT(CASE WHEN from_ocr THEN 1 END) AS ocr_count
FROM indexed_documents
WHERE status = 'indexed'
GROUP BY source_type;

-- ============================================
-- DEFAULT DATA
-- ============================================

INSERT INTO users (azure_id, email, name, role)
VALUES ('admin-local', 'admin@localhost', 'Administrator', 'admin')
ON CONFLICT (azure_id) DO NOTHING;

-- ============================================
-- PERMISSIONS
-- ============================================

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO rag_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO rag_user;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO rag_user;

COMMENT ON DATABASE rag_system IS 'JARVIS RAG system database';

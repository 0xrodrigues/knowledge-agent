-- Knowledge Agent local graph schema

CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('business', 'technical')),
    confluence_page_id TEXT NOT NULL,
    confluence_url TEXT NOT NULL,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(product, type)
);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    product TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL,
    page_id INTEGER NOT NULL,
    UNIQUE(rule_id, page_id),
    FOREIGN KEY (rule_id) REFERENCES rules(rule_id),
    FOREIGN KEY (page_id) REFERENCES pages(id)
);

CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation TEXT NOT NULL,
    document_path TEXT,
    pages_affected TEXT,
    rules_affected TEXT,
    status TEXT NOT NULL,
    detail TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_pages_product_type ON pages(product, type);
CREATE INDEX IF NOT EXISTS idx_rules_product ON rules(product);
CREATE INDEX IF NOT EXISTS idx_dependencies_rule ON dependencies(rule_id);
CREATE INDEX IF NOT EXISTS idx_dependencies_page ON dependencies(page_id);

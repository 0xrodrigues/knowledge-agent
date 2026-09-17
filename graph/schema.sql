-- Knowledge Agent local graph schema

CREATE TABLE IF NOT EXISTS components (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    last_full_scan_commit TEXT,
    last_full_scan_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL UNIQUE,
    component_id INTEGER NOT NULL,
    description TEXT NOT NULL,
    condition TEXT DEFAULT '',
    category TEXT NOT NULL CHECK(category IN ('business', 'technical')),
    confidence TEXT NOT NULL CHECK(confidence IN ('high', 'medium', 'low')),
    origin TEXT NOT NULL CHECK(origin IN ('repo_doc', 'explicit_tag', 'inferred')),
    status TEXT NOT NULL CHECK(status IN ('doc_only', 'code_confirmed', 'code_contradicts_doc', 'code_only')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (component_id) REFERENCES components(id)
);

CREATE TABLE IF NOT EXISTS rule_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    line_range TEXT DEFAULT '',
    source_kind TEXT NOT NULL CHECK(source_kind IN ('doc', 'code')),
    content_hash TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    last_verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(rule_id, file_path),
    FOREIGN KEY (rule_id) REFERENCES rules(rule_id)
);

CREATE TABLE IF NOT EXISTS technical_refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component_id INTEGER NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('table', 'endpoint', 'microservice', 'class', 'method',
                                       'function', 'module', 'script', 'route', 'component',
                                       'constraint', 'sequence', 'policy', 'trigger')),
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    file_path TEXT,
    line_range TEXT DEFAULT '',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(component_id, type, name),
    FOREIGN KEY (component_id) REFERENCES components(id)
);

CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation TEXT NOT NULL,
    source_ref TEXT,
    components_affected TEXT,
    rules_affected TEXT,
    status TEXT NOT NULL,
    detail TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_rules_component ON rules(component_id);
CREATE INDEX IF NOT EXISTS idx_rule_sources_file ON rule_sources(file_path);
CREATE INDEX IF NOT EXISTS idx_rule_sources_rule ON rule_sources(rule_id);
CREATE INDEX IF NOT EXISTS idx_technical_refs_component ON technical_refs(component_id);

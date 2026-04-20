PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_user_id INTEGER NOT NULL UNIQUE,
    username TEXT,
    display_name TEXT,
    is_reachable INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS families (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by_user_id INTEGER NOT NULL,
    FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS family_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role_type TEXT NOT NULL CHECK(role_type IN ('parent', 'child')),
    is_admin INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (family_id, user_id),
    FOREIGN KEY (family_id) REFERENCES families(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS family_invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    role_type TEXT NOT NULL CHECK(role_type IN ('parent', 'child')),
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    accepted_at TEXT,
    UNIQUE (family_id, username),
    FOREIGN KEY (family_id) REFERENCES families(id) ON DELETE CASCADE,
    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (family_id, name),
    FOREIGN KEY (family_id) REFERENCES families(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS planned_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id INTEGER NOT NULL,
    group_id INTEGER,
    title TEXT NOT NULL,
    description TEXT,
    requires_comment INTEGER NOT NULL DEFAULT 0,
    effort_stars INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (family_id) REFERENCES families(id) ON DELETE CASCADE,
    FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE SET NULL,
    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS default_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    default_schedule_json TEXT,
    default_dependencies_json TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS task_dependency_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id INTEGER NOT NULL,
    parent_task_id INTEGER NOT NULL,
    child_task_id INTEGER NOT NULL,
    is_required INTEGER NOT NULL DEFAULT 1,
    delay_mode TEXT NOT NULL CHECK(delay_mode IN ('none', 'fixed', 'configurable')),
    default_delay_minutes INTEGER NOT NULL DEFAULT 0,
    UNIQUE (parent_task_id, child_task_id),
    FOREIGN KEY (family_id) REFERENCES families(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_task_id) REFERENCES planned_tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (child_task_id) REFERENCES planned_tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    day_of_week INTEGER NOT NULL CHECK(day_of_week BETWEEN 0 AND 6),
    time_hhmm TEXT NOT NULL,
    is_weekend_profile INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (task_id) REFERENCES planned_tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id INTEGER NOT NULL,
    planned_task_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('scheduled', 'pending', 'done', 'cancelled')),
    due_at TEXT,
    activated_at TEXT,
    created_by INTEGER,
    source_type TEXT NOT NULL CHECK(source_type IN ('manual', 'schedule', 'dependency')),
    source_ref_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (family_id) REFERENCES families(id) ON DELETE CASCADE,
    FOREIGN KEY (planned_task_id) REFERENCES planned_tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS task_completions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_instance_id INTEGER,
    family_id INTEGER NOT NULL,
    planned_task_id INTEGER NOT NULL,
    completed_by INTEGER NOT NULL,
    completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    comment_text TEXT,
    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    history_updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completion_mode TEXT NOT NULL CHECK(completion_mode IN ('current', 'manual')),
    FOREIGN KEY (task_instance_id) REFERENCES task_instances(id) ON DELETE SET NULL,
    FOREIGN KEY (family_id) REFERENCES families(id) ON DELETE CASCADE,
    FOREIGN KEY (planned_task_id) REFERENCES planned_tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (completed_by) REFERENCES users(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS undo_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    action_ref_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    is_reverted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (family_id) REFERENCES families(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS alice_user_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alice_user_id TEXT NOT NULL UNIQUE,
    family_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    linked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (family_id) REFERENCES families(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS alice_link_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    family_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (family_id) REFERENCES families(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS notification_quiet_hours (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    day_of_week INTEGER CHECK(day_of_week BETWEEN 0 AND 6),
    quiet_from TEXT NOT NULL,
    quiet_to TEXT NOT NULL,
    is_all_week INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (family_id) REFERENCES families(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_family_members_family_active_admin
    ON family_members(family_id, is_active, is_admin);

CREATE INDEX IF NOT EXISTS idx_planned_tasks_family_active
    ON planned_tasks(family_id, is_active);

CREATE INDEX IF NOT EXISTS idx_planned_tasks_family_sort_order
    ON planned_tasks(family_id, sort_order);

CREATE INDEX IF NOT EXISTS idx_groups_family_name
    ON groups(family_id, name);

CREATE INDEX IF NOT EXISTS idx_groups_family_sort_order
    ON groups(family_id, sort_order);

CREATE INDEX IF NOT EXISTS idx_planned_tasks_family_group_sort
    ON planned_tasks(family_id, group_id, sort_order);

CREATE INDEX IF NOT EXISTS idx_task_instances_family_status_activated
    ON task_instances(family_id, status, activated_at);

CREATE INDEX IF NOT EXISTS idx_task_instances_family_due
    ON task_instances(family_id, due_at);

CREATE INDEX IF NOT EXISTS idx_task_completions_family_completed_at
    ON task_completions(family_id, completed_at);

CREATE INDEX IF NOT EXISTS idx_alice_user_links_family_user
    ON alice_user_links(family_id, user_id);

CREATE INDEX IF NOT EXISTS idx_alice_link_codes_user_expires
    ON alice_link_codes(user_id, expires_at);

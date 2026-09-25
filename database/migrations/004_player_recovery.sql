BEGIN;

ALTER TABLE room_players ADD COLUMN pending_admin_action TEXT
    CHECK (pending_admin_action IN ('stand', 'remove'));

CREATE TABLE player_recovery_codes (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(id),
    player_id TEXT NOT NULL REFERENCES room_players(id),
    code_hash TEXT NOT NULL UNIQUE,
    expires_at REAL NOT NULL,
    consumed_at REAL,
    revoked_at REAL,
    created_at TEXT NOT NULL
);
CREATE INDEX player_recovery_player_idx
    ON player_recovery_codes(room_id, player_id, expires_at DESC);

CREATE TABLE room_admin_events (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(id),
    player_id TEXT REFERENCES room_players(id),
    action TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX room_admin_events_room_idx ON room_admin_events(room_id, created_at DESC);

PRAGMA user_version = 4;
COMMIT;

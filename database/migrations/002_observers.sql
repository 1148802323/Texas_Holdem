PRAGMA foreign_keys = OFF;
BEGIN;

CREATE TABLE room_players_new (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(id),
    nickname TEXT NOT NULL,
    nickname_key TEXT NOT NULL,
    seat INTEGER CHECK (seat >= 0),
    session_token_hash TEXT NOT NULL UNIQUE,
    stack INTEGER NOT NULL DEFAULT 0 CHECK (stack >= 0),
    total_buyin INTEGER NOT NULL DEFAULT 0 CHECK (total_buyin >= 0),
    total_cashout INTEGER NOT NULL DEFAULT 0 CHECK (total_cashout >= 0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    left_at TEXT
);

INSERT INTO room_players_new
    (id, room_id, nickname, nickname_key, seat, session_token_hash,
     stack, total_buyin, created_at)
SELECT id, room_id, nickname, nickname_key, seat, session_token_hash,
       stack, total_buyin, created_at FROM room_players;

DROP TABLE room_players;
ALTER TABLE room_players_new RENAME TO room_players;
CREATE UNIQUE INDEX active_room_nickname_idx
    ON room_players(room_id, nickname_key) WHERE is_active = 1;
CREATE UNIQUE INDEX active_room_seat_idx
    ON room_players(room_id, seat) WHERE is_active = 1 AND seat IS NOT NULL;

PRAGMA user_version = 2;
COMMIT;
PRAGMA foreign_keys = ON;

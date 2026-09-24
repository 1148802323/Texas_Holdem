BEGIN;

CREATE TABLE rooms (
    id TEXT PRIMARY KEY,
    small_blind INTEGER NOT NULL CHECK (small_blind > 0),
    big_blind INTEGER NOT NULL CHECK (big_blind > small_blind),
    max_players INTEGER NOT NULL CHECK (max_players BETWEEN 2 AND 9),
    max_buyin_stack INTEGER NOT NULL CHECK (max_buyin_stack > 0),
    nickname_policy TEXT NOT NULL DEFAULT 'free' CHECK (nickname_policy IN ('free', 'preset')),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_at TEXT NOT NULL
);

CREATE TABLE allowed_nicknames (
    room_id TEXT NOT NULL REFERENCES rooms(id),
    nickname TEXT NOT NULL,
    nickname_key TEXT NOT NULL,
    PRIMARY KEY (room_id, nickname_key)
);

CREATE TABLE room_players (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(id),
    nickname TEXT NOT NULL,
    nickname_key TEXT NOT NULL,
    seat INTEGER NOT NULL CHECK (seat >= 0),
    session_token_hash TEXT NOT NULL UNIQUE,
    stack INTEGER NOT NULL DEFAULT 0 CHECK (stack >= 0),
    total_buyin INTEGER NOT NULL DEFAULT 0 CHECK (total_buyin >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (room_id, nickname_key),
    UNIQUE (room_id, seat)
);

CREATE TABLE buyins (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(id),
    player_id TEXT NOT NULL REFERENCES room_players(id),
    request_id TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    stack_before INTEGER NOT NULL CHECK (stack_before >= 0),
    stack_after INTEGER NOT NULL CHECK (stack_after >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (room_id, request_id)
);

CREATE TABLE hands (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(id),
    hand_number INTEGER NOT NULL CHECK (hand_number > 0),
    start_request_id TEXT NOT NULL,
    button_seat INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'complete')),
    version INTEGER NOT NULL CHECK (version >= 0),
    private_snapshot TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (room_id, hand_number),
    UNIQUE (room_id, start_request_id)
);

CREATE INDEX hands_room_number_idx ON hands(room_id, hand_number DESC);

CREATE TABLE hand_players (
    hand_id TEXT NOT NULL REFERENCES hands(id),
    player_id TEXT NOT NULL REFERENCES room_players(id),
    seat INTEGER NOT NULL,
    starting_stack INTEGER NOT NULL CHECK (starting_stack >= 0),
    blind_paid INTEGER NOT NULL DEFAULT 0 CHECK (blind_paid >= 0),
    ending_stack INTEGER CHECK (ending_stack >= 0),
    payout INTEGER NOT NULL DEFAULT 0 CHECK (payout >= 0),
    refund INTEGER NOT NULL DEFAULT 0 CHECK (refund >= 0),
    PRIMARY KEY (hand_id, player_id),
    UNIQUE (hand_id, seat)
);

CREATE INDEX hand_players_player_idx ON hand_players(player_id, hand_id);

CREATE TABLE hand_actions (
    id TEXT PRIMARY KEY,
    hand_id TEXT NOT NULL REFERENCES hands(id),
    player_id TEXT NOT NULL REFERENCES room_players(id),
    request_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    street TEXT NOT NULL,
    action TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount >= 0),
    paid INTEGER NOT NULL CHECK (paid >= 0),
    to_call INTEGER NOT NULL CHECK (to_call >= 0),
    pot_after INTEGER NOT NULL CHECK (pot_after >= 0),
    version_after INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (hand_id, request_id),
    UNIQUE (hand_id, sequence)
);

PRAGMA user_version = 1;
COMMIT;

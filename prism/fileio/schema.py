USER_VERSION = 4
APPLICATION_ID = 2060242126


SCHEMA = [
    """
    CREATE TABLE items (
        id INTEGER PRIMARY KEY,
        type TEXT NOT NULL,
        x REAL DEFAULT 0,
        y REAL DEFAULT 0,
        z REAL DEFAULT 0,
        scale REAL DEFAULT 1,
        rotation REAL DEFAULT 0,
        flip INTEGER DEFAULT 1,
        data JSON
    )
    """,
    """
    CREATE TABLE sqlar (
        name TEXT PRIMARY KEY,
        item_id INTEGER NOT NULL UNIQUE,
        mode INT,
        mtime INT default current_timestamp,
        sz INT,
        data BLOB,
        FOREIGN KEY (item_id)
          REFERENCES items (id)
             ON DELETE CASCADE
             ON UPDATE NO ACTION
    )
    """,
    # One rich-text note per saved document. Images referenced from the note
    # live in the attachments table, so a .prism file stays self contained.
    """
    CREATE TABLE board_notes (
        id INTEGER PRIMARY KEY,
        board_id INTEGER NOT NULL DEFAULT 0,
        html TEXT NOT NULL DEFAULT '',
        updated_at INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE attachments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        board_id INTEGER NOT NULL DEFAULT 0,
        name TEXT NOT NULL,
        mime TEXT,
        blob BLOB,
        created_at INTEGER NOT NULL DEFAULT 0
    )
    """,
    # The mind map keeps its own tree instead of being derived from the note:
    # node layout, tags and collapsed state would be lost in a round trip.
    """
    CREATE TABLE board_mindmap (
        board_id INTEGER PRIMARY KEY,
        tree JSON,
        updated_at INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE workspace_pages (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        data JSON,
        sort_order INTEGER NOT NULL DEFAULT 0
    )
    """,
]


MIGRATIONS = {
    2: [
        "ALTER TABLE items ADD COLUMN data JSON",
        "UPDATE items SET data = json_object('filename', filename)",
    ],
    # 2 -> 3: document notes, their attachments and the mind map tree.
    3: [
        """
        CREATE TABLE IF NOT EXISTS board_notes (
            id INTEGER PRIMARY KEY,
            board_id INTEGER NOT NULL DEFAULT 0,
            html TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            board_id INTEGER NOT NULL DEFAULT 0,
            name TEXT NOT NULL,
            mime TEXT,
            blob BLOB,
            created_at INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS board_mindmap (
            board_id INTEGER PRIMARY KEY,
            tree JSON,
            updated_at INTEGER NOT NULL DEFAULT 0
        )
        """,
    ],
    # 3 -> 4: user-created document and mind-map pages in the workspace rail.
    4: [
        """
        CREATE TABLE IF NOT EXISTS workspace_pages (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            data JSON,
            sort_order INTEGER NOT NULL DEFAULT 0
        )
        """,
    ],
}

"""Read-only queue diagnostics. Does not print tokens or message contents."""
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config


def main():
    path = Path(config.DB_PATH)
    if not path.exists():
        raise SystemExit('База не найдена; бот ещё не создал её.')
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        version = conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(outbox)')}
        print('schema:', version)
        print('feature_chat:', config.CHAT_PEER_ID, 'moderation_chat:', config.MODERATION_CHAT_PEER_ID)
        rows = conn.execute('SELECT * FROM outbox ORDER BY id').fetchall()
        states = Counter(row['status'] if 'status' in columns else ('done' if row['done_at'] else 'pending') for row in rows)
        print('outbox:', dict(states))
        problems = [row for row in rows if (row['status'] != 'done' if 'status' in columns else not row['done_at'])]
        for row in problems[-30:]:
            payload = json.loads(row['payload'])
            peer = payload.get('peer_id', payload.get('params', {}).get('peer_id', 'unknown'))
            status = row['status'] if 'status' in columns else 'pending'
            print('id:', row['id'], 'kind:', row['kind'], 'chat:', peer,
                  'status:', status, 'attempts:', row['attempts'], 'error:', row['last_error'])
        inbox = conn.execute('SELECT COUNT(*) FROM inbox WHERE done_at = 0').fetchone()[0]
        print('pending inbox:', inbox)
    finally:
        conn.close()


if __name__ == '__main__':
    main()

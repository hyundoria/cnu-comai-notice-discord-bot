# migrate.py
import json, os
import db

db.init()

if os.path.exists("guild_channels.json"):
    with open("guild_channels.json", encoding="utf-8") as f:
        for gid, cid in json.load(f).items():
            db.upsert_guild_channel(int(gid), int(cid))
    print("✅ guild_channels migrated")

if os.path.exists("seen.json"):
    with open("seen.json", encoding="utf-8") as f:
        data = json.load(f)
        for cat, ids in data.items():
            db.mark_seen(cat, [{"id": i, "title": ""} for i in ids])
    print("✅ seen articles migrated")

"""Database migration script: add new tables and new fields"""
import sqlite3
import os
from pathlib import Path

# Database path
DB_PATH = Path(__file__).parent / "data" / "fire_detection.db"

def migrate():
    if not DB_PATH.exists():
        print(f"Database file does not exist: {DB_PATH}")
        return
    
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    # 1. Add new fields to the detection_models table
    print("Checking detection_models table fields...")
    
    # Check the default_threshold field
    cursor.execute("PRAGMA table_info(detection_models)")
    columns = [col[1] for col in cursor.fetchall()]
    
    if "default_threshold" not in columns:
        print("Adding default_threshold field...")
        cursor.execute("ALTER TABLE detection_models ADD COLUMN default_threshold FLOAT DEFAULT 0.25")
    
    if "class_names" not in columns:
        print("Adding class_names field...")
        cursor.execute("ALTER TABLE detection_models ADD COLUMN class_names TEXT DEFAULT '[]'")
    
    # 2. Add new fields to the tasks table
    print("Checking tasks table fields...")
    cursor.execute("PRAGMA table_info(tasks)")
    columns = [col[1] for col in cursor.fetchall()]
    
    if "detection_config" not in columns:
        print("Adding detection_config field...")
        cursor.execute("ALTER TABLE tasks ADD COLUMN detection_config TEXT DEFAULT '{}'")
    
    # 3. Create the detection_records table
    print("Creating detection_records table...")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS detection_records (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            class_name TEXT NOT NULL,
            confidence FLOAT NOT NULL,
            box TEXT DEFAULT '[]',
            detected_at DATETIME NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )
    """)
    
    # Create indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_detection_records_task_id ON detection_records(task_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_detection_records_detected_at ON detection_records(detected_at)")
    
    # 4. Create the detection_events table (event-driven detection records)
    print("Creating detection_events table...")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS detection_events (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            track_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            class_name TEXT NOT NULL,
            confidence FLOAT NOT NULL,
            box TEXT DEFAULT '[]',
            entered_at DATETIME NOT NULL,
            left_at DATETIME,
            duration_ms INTEGER DEFAULT 0,
            max_confidence FLOAT DEFAULT 0.0,
            avg_confidence FLOAT DEFAULT 0.0,
            update_count INTEGER DEFAULT 0,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )
    """)
    
    # Create indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_detection_events_task_id ON detection_events(task_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_detection_events_track_id ON detection_events(track_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_detection_events_event_type ON detection_events(event_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_detection_events_class_name ON detection_events(class_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_detection_events_entered_at ON detection_events(entered_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_detection_events_left_at ON detection_events(left_at)")
    
    conn.commit()
    conn.close()
    print("Database migration complete!")

if __name__ == "__main__":
    migrate()

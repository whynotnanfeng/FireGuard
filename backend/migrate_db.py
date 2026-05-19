"""数据库迁移脚本：添加新表和新字段"""
import sqlite3
import os
from pathlib import Path

# 数据库路径
DB_PATH = Path(__file__).parent / "data" / "fire_detection.db"

def migrate():
    if not DB_PATH.exists():
        print(f"数据库文件不存在: {DB_PATH}")
        return
    
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    # 1. 添加 detection_models 表的新字段
    print("检查 detection_models 表字段...")
    
    # 检查 default_threshold 字段
    cursor.execute("PRAGMA table_info(detection_models)")
    columns = [col[1] for col in cursor.fetchall()]
    
    if "default_threshold" not in columns:
        print("添加 default_threshold 字段...")
        cursor.execute("ALTER TABLE detection_models ADD COLUMN default_threshold FLOAT DEFAULT 0.25")
    
    if "class_names" not in columns:
        print("添加 class_names 字段...")
        cursor.execute("ALTER TABLE detection_models ADD COLUMN class_names TEXT DEFAULT '[]'")
    
    # 2. 添加 tasks 表的新字段
    print("检查 tasks 表字段...")
    cursor.execute("PRAGMA table_info(tasks)")
    columns = [col[1] for col in cursor.fetchall()]
    
    if "detection_config" not in columns:
        print("添加 detection_config 字段...")
        cursor.execute("ALTER TABLE tasks ADD COLUMN detection_config TEXT DEFAULT '{}'")
    
    # 3. 创建 detection_records 表
    print("创建 detection_records 表...")
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
    
    # 创建索引
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_detection_records_task_id ON detection_records(task_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_detection_records_detected_at ON detection_records(detected_at)")
    
    # 4. 创建 detection_events 表（事件驱动检测记录）
    print("创建 detection_events 表...")
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
    
    # 创建索引
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_detection_events_task_id ON detection_events(task_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_detection_events_track_id ON detection_events(track_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_detection_events_event_type ON detection_events(event_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_detection_events_class_name ON detection_events(class_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_detection_events_entered_at ON detection_events(entered_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_detection_events_left_at ON detection_events(left_at)")
    
    conn.commit()
    conn.close()
    print("数据库迁移完成！")

if __name__ == "__main__":
    migrate()

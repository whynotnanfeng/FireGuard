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
    
    conn.commit()
    conn.close()
    print("数据库迁移完成！")

if __name__ == "__main__":
    migrate()

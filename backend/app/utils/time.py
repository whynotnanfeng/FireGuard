from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))


def now_beijing() -> datetime:
    """返回北京时间 (UTC+8) 的 datetime 对象"""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)

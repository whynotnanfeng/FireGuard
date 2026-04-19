from sqlmodel import SQLModel, create_engine, Session
from app.config import config

# Ensure data directory exists
config.DATA_DIR.mkdir(exist_ok=True)
config.UPLOADS_DIR.mkdir(exist_ok=True)
config.MODELS_DIR.mkdir(exist_ok=True)
config.RESULTS_DIR.mkdir(exist_ok=True)

sqlite_url = f"sqlite:///{config.DB_PATH}"

engine = create_engine(
    sqlite_url,
    echo=False,
    connect_args={"check_same_thread": False},
)


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session

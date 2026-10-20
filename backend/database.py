import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

load_dotenv()

def _normalizar_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Falta la variable de entorno DATABASE_URL")

engine = create_engine(
    _normalizar_url(DATABASE_URL),
    poolclass=NullPool,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

_tablas_listas = False

def asegurar_tablas():
    """En Vercel (serverless) el lifespan de FastAPI no es confiable:
    creamos las tablas la primera vez que se pide una sesión."""
    global _tablas_listas
    if not _tablas_listas:
        init_db()
        _tablas_listas = True

def get_db():
    asegurar_tablas()
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def init_db():
    from backend import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
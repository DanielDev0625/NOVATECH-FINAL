import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

load_dotenv()


def _normalizar_url(url: str) -> str:
    # Neon/Heroku suelen entregar "postgres://"; SQLAlchemy requiere "postgresql://"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Falta la variable de entorno DATABASE_URL")

# NullPool: en Vercel (serverless) cada invocación abre/cierra su conexión;
# la agrupación la hace el pooler del proveedor (p. ej. Neon).
engine = create_engine(
    _normalizar_url(DATABASE_URL),
    poolclass=NullPool,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    """Dependencia de FastAPI: una sesión por petición."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    """Crea las tablas si no existen (procedimiento de inicialización)."""
    from backend import models  # noqa: F401  (registra los modelos)
    Base.metadata.create_all(bind=engine)
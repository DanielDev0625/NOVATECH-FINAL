import os
import re
from io import BytesIO
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import bcrypt
import jwt
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, selectinload
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend import automatizacion as auto
from backend import models as m
from backend import schemas as s
from backend.database import engine, get_db, init_db
from backend.models import ReglaNegocioError
import secrets


def _migrar_trazabilidad():
    """Agrega las columnas nuevas de etapas_produccion en bases que ya existían
    (create_all solo crea tablas nuevas). Corre una única vez: si la columna ya está, no hace nada."""
    with engine.begin() as conn:
        existe = conn.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'etapas_produccion' AND column_name = 'completada_en'")).first()
        if existe:
            return
        conn.execute(text("ALTER TABLE etapas_produccion ADD COLUMN IF NOT EXISTS "
                          "tipo VARCHAR(20) NOT NULL DEFAULT 'PRODUCCION'"))
        conn.execute(text("ALTER TABLE etapas_produccion ADD COLUMN IF NOT EXISTS fecha_limite TIMESTAMPTZ"))
        conn.execute(text("ALTER TABLE etapas_produccion ADD COLUMN IF NOT EXISTS completada_en TIMESTAMPTZ"))
        # Las etapas que ya existían eran registros de algo ya ocurrido
        conn.execute(text("UPDATE etapas_produccion SET completada_en = fecha"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Crea las tablas si no existen. Si la BD falla, el servidor sigue arriba
    # y /api/health informa el problema.
    try:
        init_db()
        _migrar_trazabilidad()
        app.state.db_ok = True
    except Exception as e:
        app.state.db_ok = False
        print(f"[NOVATECH] Error inicializando la base de datos: {e}")
    yield


app = FastAPI(title="NOVATECH", lifespan=lifespan)


@app.exception_handler(ReglaNegocioError)
async def regla_negocio_handler(request: Request, exc: ReglaNegocioError):
    return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})


@app.get("/api/health")
def health():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True, "base_de_datos": "conectada"}
    except Exception as e:
        print(f"[NOVATECH] Health check falló: {e}")
        return JSONResponse(
            status_code=503,
            content={"ok": False, "base_de_datos": "sin conexión"},
        )


# ── Los routers de cada módulo se incluirán AQUÍ, antes del montaje estático ──
# ───────────── Errores con formato JSON consistente ─────────────
@app.exception_handler(StarletteHTTPException)
async def http_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": exc.detail})


@app.exception_handler(RequestValidationError)
async def validacion_handler(request: Request, exc: RequestValidationError):
    campos = "; ".join(f"{'.'.join(str(x) for x in e['loc'][1:])}: {e['msg']}" for e in exc.errors())
    return JSONResponse(status_code=422, content={"ok": False, "error": f"Datos inválidos — {campos}"})


def _commit(db: Session):
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Conflicto: registro duplicado o relación inválida")


# ───────────── Seguridad ─────────────
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError("Falta la variable de entorno JWT_SECRET")
bearer = HTTPBearer(auto_error=False)


def _hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _crear_token(u: m.Usuario) -> str:
    minutos = int(os.getenv("JWT_EXPIRA_MINUTOS", "480"))
    exp = datetime.now(timezone.utc) + timedelta(minutes=minutos)
    return jwt.encode({"sub": str(u.id), "exp": exp}, JWT_SECRET, algorithm="HS256")


def usuario_actual(cred: HTTPAuthorizationCredentials | None = Depends(bearer),
                   db: Session = Depends(get_db)) -> m.Usuario:
    if not cred:
        raise HTTPException(401, "Falta iniciar sesión")
    try:
        data = jwt.decode(cred.credentials, JWT_SECRET, algorithms=["HS256"])
        u = db.get(m.Usuario, int(data["sub"]))
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, "Sesión inválida o vencida")
    if not u or not u.activo:
        raise HTTPException(401, "Usuario no autorizado")
    return u


def requiere(*roles: str):
    """El rol se lee de la base de datos, no del token."""
    def dep(u: m.Usuario = Depends(usuario_actual)) -> m.Usuario:
        if u.rol not in roles:
            raise HTTPException(403, "No tienes permisos para esta operación")
        return u
    return dep


auth = APIRouter(prefix="/api", tags=["auth"])


@auth.post("/auth/primer-admin", status_code=201)
def primer_admin(d: s.UsuarioCrear, db: Session = Depends(get_db)):
    """Solo funciona mientras no exista ningún usuario."""
    if db.scalar(select(m.Usuario.id).limit(1)):
        raise HTTPException(403, "Ya existe un administrador")
    u = m.Usuario(email=d.email, nombre=d.nombre, password_hash=_hash(d.password), rol="admin")
    db.add(u)
    _commit(db)
    return {"ok": True, "data": s.UsuarioOut.model_validate(u)}


@auth.post("/auth/login")
def login(d: s.LoginIn, db: Session = Depends(get_db)):
    u = db.scalar(select(m.Usuario).where(m.Usuario.email == d.email.strip().lower()))
    ok = u and u.activo and bcrypt.checkpw(d.password.encode(), u.password_hash.encode())
    if not ok:
        raise HTTPException(401, "Correo o contraseña incorrectos")
    return {"ok": True, "token": _crear_token(u), "data": s.UsuarioOut.model_validate(u)}


@auth.get("/auth/yo")
def yo(u: m.Usuario = Depends(usuario_actual)):
    return {"ok": True, "data": s.UsuarioOut.model_validate(u)}


@auth.post("/usuarios", status_code=201)
def crear_usuario(d: s.UsuarioCrear, db: Session = Depends(get_db), _=Depends(requiere("admin"))):
    u = m.Usuario(email=d.email, nombre=d.nombre, password_hash=_hash(d.password), rol=d.rol)
    db.add(u)
    _commit(db)
    return {"ok": True, "data": s.UsuarioOut.model_validate(u)}


# ───────────── Inventario ─────────────
inv = APIRouter(prefix="/api", tags=["inventario"])
LEE = Depends(usuario_actual)
ESCRIBE = Depends(requiere("admin", "operador"))
ADMIN = Depends(requiere("admin"))
OPUESTO = {m.ENTRADA: m.SALIDA, m.SALIDA: m.ENTRADA,
           m.AJUSTE_ENTRADA: m.AJUSTE_SALIDA, m.AJUSTE_SALIDA: m.AJUSTE_ENTRADA}


def _producto_bloqueado(db: Session, producto_id: int) -> m.Producto:
    """Bloquea la fila para que dos salidas simultáneas no pasen el mismo stock."""
    p = db.scalar(select(m.Producto).where(m.Producto.id == producto_id).with_for_update())
    if not p:
        raise HTTPException(404, "El producto no existe")
    return p


def _registrar(db, p, tipo, cantidad, ref, usuario, **extra) -> m.MovimientoInventario:
    stock = p.aplicar_movimiento(tipo, cantidad)  # valida reglas y actualiza el stock
    mov = m.MovimientoInventario(
        producto_id=p.id, tipo=tipo, cantidad=cantidad, stock_resultante=stock,
        referencia_operacion=ref, usuario_id=usuario.id, **extra)
    db.add(mov)
    return mov


@inv.get("/categorias")
def listar_categorias(db: Session = Depends(get_db), _=LEE):
    filas = db.scalars(select(m.Categoria).order_by(m.Categoria.nombre)).all()
    return {"ok": True, "data": [s.CategoriaOut.model_validate(c) for c in filas]}


@inv.post("/categorias", status_code=201)
def crear_categoria(d: s.CategoriaIn, db: Session = Depends(get_db), _=ESCRIBE):
    c = m.Categoria(nombre=d.nombre.strip())
    db.add(c)
    _commit(db)
    return {"ok": True, "data": s.CategoriaOut.model_validate(c)}


@inv.get("/productos")
def listar_productos(q: str | None = None, stock_bajo: bool = False, inactivos: bool = False,
                     db: Session = Depends(get_db), _=LEE):
    stmt = select(m.Producto).order_by(m.Producto.nombre)
    if not inactivos:
        stmt = stmt.where(m.Producto.activo.is_(True))
    if q:
        patron = f"%{q.strip()}%"
        stmt = stmt.where(m.Producto.nombre.ilike(patron) | m.Producto.codigo.ilike(patron))
    if stock_bajo:
        stmt = stmt.where(m.Producto._stock <= m.Producto.stock_minimo)
    return {"ok": True, "data": [s.ProductoOut.model_validate(p) for p in db.scalars(stmt)]}


@inv.post("/productos", status_code=201)
def crear_producto(d: s.ProductoIn, db: Session = Depends(get_db), u=ESCRIBE):
    if d.categoria_id and not db.get(m.Categoria, d.categoria_id):
        raise HTTPException(404, "La categoría no existe")
    if db.scalar(select(m.Producto.id).where(m.Producto.codigo == d.codigo)):
        raise HTTPException(409, "Ya existe un producto con ese código")
    p = m.Producto(**d.model_dump(exclude={"stock_inicial"}))
    db.add(p)
    db.flush()  # obtiene p.id; todo sigue en la misma transacción
    if d.stock_inicial > 0:
        _registrar(db, p, m.ENTRADA, d.stock_inicial, f"inicial-{p.id}", u,
                   observacion="Existencia inicial")
    _commit(db)
    return {"ok": True, "data": s.ProductoOut.model_validate(p)}


@inv.put("/productos/{producto_id}")
def editar_producto(producto_id: int, d: s.ProductoEditar,
                    db: Session = Depends(get_db), _=ESCRIBE):
    p = db.get(m.Producto, producto_id)
    if not p:
        raise HTTPException(404, "El producto no existe")
    cambios = d.model_dump(exclude_unset=True)  # el stock NO se edita aquí
    if cambios.get("categoria_id") and not db.get(m.Categoria, cambios["categoria_id"]):
        raise HTTPException(404, "La categoría no existe")
    for campo, valor in cambios.items():
        setattr(p, campo, valor)
    _commit(db)
    return {"ok": True, "data": s.ProductoOut.model_validate(p)}


@inv.delete("/productos/{producto_id}")
def desactivar_producto(producto_id: int, db: Session = Depends(get_db), _=ADMIN):
    """No se borra: el historial de movimientos debe seguir siendo consistente."""
    p = db.get(m.Producto, producto_id)
    if not p:
        raise HTTPException(404, "El producto no existe")
    p.activo = False
    _commit(db)
    return {"ok": True, "data": s.ProductoOut.model_validate(p)}


@inv.get("/inventario/movimientos")
def listar_movimientos(producto_id: int | None = None, limite: int = 100,
                       db: Session = Depends(get_db), _=LEE):
    stmt = select(m.MovimientoInventario).order_by(m.MovimientoInventario.id.desc()).limit(min(limite, 500))
    if producto_id:
        stmt = stmt.where(m.MovimientoInventario.producto_id == producto_id)
    return {"ok": True, "data": [s.MovimientoOut.model_validate(x) for x in db.scalars(stmt)]}


@inv.post("/inventario/movimientos", status_code=201)
def registrar_movimiento(d: s.MovimientoIn, db: Session = Depends(get_db), u=ESCRIBE):
    if d.tipo.startswith("AJUSTE") and u.rol != "admin":
        raise HTTPException(403, "Solo un administrador puede registrar ajustes")

    # Idempotencia: un reintento con la misma referencia devuelve el movimiento original.
    previo = db.scalar(select(m.MovimientoInventario)
                       .where(m.MovimientoInventario.referencia_operacion == d.referencia_operacion))
    if previo:
        if (previo.producto_id, previo.tipo, previo.cantidad) != (d.producto_id, d.tipo, d.cantidad):
            raise HTTPException(409, "Esa referencia ya se usó en otra operación")
        return {"ok": True, "duplicado": True, "data": s.MovimientoOut.model_validate(previo)}

    p = _producto_bloqueado(db, d.producto_id)
    if d.lote_id and not db.get(m.Lote, d.lote_id):
        raise HTTPException(404, "El lote no existe")
    if d.pedido_id and not db.get(m.Pedido, d.pedido_id):
        raise HTTPException(404, "El pedido no existe")
    mov = _registrar(db, p, d.tipo, d.cantidad, d.referencia_operacion, u,
                     lote_id=d.lote_id, pedido_id=d.pedido_id, observacion=d.observacion)
    _commit(db)
    return {"ok": True, "duplicado": False, "data": s.MovimientoOut.model_validate(mov),
            "stock_actual": float(p.stock_actual)}


@inv.post("/inventario/movimientos/{mov_id}/anular", status_code=201)
def anular_movimiento(mov_id: int, db: Session = Depends(get_db), u=ADMIN):
    """Anula con un movimiento compensatorio; el original queda intacto."""
    orig = db.get(m.MovimientoInventario, mov_id)
    if not orig:
        raise HTTPException(404, "El movimiento no existe")
    if orig.compensa_a_id is not None:
        raise HTTPException(400, "No se puede anular un movimiento compensatorio")
    if db.scalar(select(m.MovimientoInventario.id)
                 .where(m.MovimientoInventario.compensa_a_id == mov_id)):
        raise HTTPException(409, "Ese movimiento ya fue anulado")
    p = _producto_bloqueado(db, orig.producto_id)
    comp = _registrar(db, p, OPUESTO[orig.tipo], orig.cantidad, f"anulacion-{mov_id}", u,
                      lote_id=orig.lote_id, pedido_id=orig.pedido_id, compensa_a_id=mov_id,
                      observacion=f"Anulación del movimiento {mov_id}")
    _commit(db)
    return {"ok": True, "data": s.MovimientoOut.model_validate(comp),
            "stock_actual": float(p.stock_actual)}

# ───────────── Trazabilidad ─────────────
tra = APIRouter(prefix="/api", tags=["trazabilidad"])


def _lote_o_404(db: Session, lote_id: int) -> m.Lote:
    l = db.get(m.Lote, lote_id)
    if not l:
        raise HTTPException(404, "El lote no existe")
    return l


# ── Trazabilidad inteligente: esquemas y ayudantes ──
DIAS_SIN_AVANCE = int(os.getenv("TRAZ_DIAS_SIN_AVANCE", "3"))


class EtapaPlanIn(BaseModel):
    nombre: str = Field(min_length=2, max_length=120)
    tipo: str = Field(default="PRODUCCION", pattern="^(PRODUCCION|ALMACENAMIENTO|ENTREGA)$")
    responsable: str = Field(min_length=2, max_length=120)
    observaciones: str | None = None
    fecha_limite: datetime | None = None
    calidad_aprobada: bool | None = None
    fecha: datetime | None = None
    completada: bool = True  # True = ya ocurrió (comportamiento anterior); False = etapa planificada


class CompletarEtapaIn(BaseModel):
    responsable: str | None = Field(default=None, min_length=2, max_length=120)
    observaciones: str | None = None
    calidad_aprobada: bool | None = None


class CalidadEtapaIn(BaseModel):
    aprobada: bool
    observaciones: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _hace(delta: timedelta) -> str:
    horas = int(delta.total_seconds() // 3600)
    return f"{horas // 24} d {horas % 24} h" if horas >= 24 else f"{max(horas, 0)} h"


def _calidad_lote(lote: m.Lote) -> str:
    resultados = [e.calidad_aprobada for e in lote.etapas if e.calidad_aprobada is not None]
    if any(r is False for r in resultados):
        return "REPROBADA"
    return "APROBADA" if resultados else "SIN_CONTROLES"


def _linea_de_tiempo(db: Session, lote: m.Lote, publico: bool = False) -> list[dict]:
    ahora = _utc_now()
    ev: list[dict] = []
    for e in sorted(lote.etapas, key=lambda x: x.id):
        if e.completada_en:
            estado = "COMPLETADA"
        elif e.fecha_limite and _aware(e.fecha_limite) < ahora:
            estado = "RETRASADA"
        else:
            estado = "PENDIENTE"
        ev.append({"etapa_id": e.id, "fecha": e.completada_en or e.fecha, "fase": e.tipo or "PRODUCCION",
                   "titulo": e.nombre, "estado": estado, "responsable": e.responsable,
                   "calidad_aprobada": e.calidad_aprobada, "observaciones": e.observaciones,
                   "fecha_limite": e.fecha_limite})

    ingreso = db.scalar(select(m.MovimientoInventario).where(
        m.MovimientoInventario.referencia_operacion == f"lote-{lote.id}-ingreso"))
    if ingreso:
        u = db.get(m.Usuario, ingreso.usuario_id) if ingreso.usuario_id else None
        ev.append({"etapa_id": None, "fecha": ingreso.fecha, "fase": "ALMACENAMIENTO",
                   "titulo": "Ingreso al inventario", "estado": "COMPLETADA",
                   "responsable": u.nombre if u else None, "calidad_aprobada": None,
                   "observaciones": None, "fecha_limite": None})

    # Despachos: salidas de este lote que no fueron anuladas por un movimiento compensatorio
    anulados = set(db.scalars(select(m.MovimientoInventario.compensa_a_id).where(
        m.MovimientoInventario.lote_id == lote.id,
        m.MovimientoInventario.compensa_a_id.is_not(None))))
    salidas = db.scalars(select(m.MovimientoInventario).where(
        m.MovimientoInventario.lote_id == lote.id, m.MovimientoInventario.tipo == m.SALIDA,
        m.MovimientoInventario.pedido_id.is_not(None)).order_by(m.MovimientoInventario.id)).all()
    por_pedido: dict[int, dict] = {}
    for x in salidas:
        if x.id in anulados:
            continue
        g = por_pedido.setdefault(x.pedido_id, {"fecha": x.fecha, "cantidad": 0, "usuario_id": x.usuario_id})
        g["cantidad"] += x.cantidad
    for pid, g in por_pedido.items():
        ped = db.get(m.Pedido, pid)
        u = db.get(m.Usuario, g["usuario_id"]) if g["usuario_id"] else None
        ev.append({"etapa_id": None, "fecha": g["fecha"], "fase": "ENTREGA",
                   "titulo": "Despacho" if publico else f"Despacho - pedido {ped.codigo}",
                   "estado": "COMPLETADA", "responsable": u.nombre if u else None,
                   "calidad_aprobada": None, "observaciones": None, "fecha_limite": None})
        ent = next((x for x in ped.entregas if x.estado == "ENTREGADA" and x.fecha_entrega), None)
        if ent:
            ev.append({"etapa_id": None, "fecha": ent.fecha_entrega, "fase": "ENTREGA",
                       "titulo": "Entrega confirmada", "estado": "COMPLETADA", "responsable": None,
                       "calidad_aprobada": None, "observaciones": None, "fecha_limite": None})

    ev.sort(key=lambda x: _aware(x["fecha"]) or ahora)
    if publico:  # sin nombres de personas, observaciones internas ni ids
        ev = [{k: e[k] for k in ("fecha", "fase", "titulo", "estado", "calidad_aprobada")} for e in ev]
    return ev


def _alertas_de_lote(lote: m.Lote, ahora: datetime) -> list[dict]:
    out: list[dict] = []

    def add(codigo, nivel, mensaje):
        out.append({"lote_id": lote.id, "lote": lote.codigo, "producto": lote.producto.nombre,
                    "codigo": codigo, "nivel": nivel, "mensaje": mensaje})

    etapas = sorted(lote.etapas, key=lambda x: x.id)
    for i, e in enumerate(etapas):
        if e.completada_en is None and e.fecha_limite and _aware(e.fecha_limite) < ahora:
            retraso = ahora - _aware(e.fecha_limite)
            add("ETAPA_RETRASADA", "critica" if retraso >= timedelta(days=1) else "advertencia",
                f"La etapa «{e.nombre}» venció hace {_hace(retraso)}")
        if e.completada_en is not None and any(p.completada_en is None for p in etapas[:i]):
            add("ETAPA_SALTADA", "advertencia", f"«{e.nombre}» se completó sin cerrar etapas anteriores")
        if e.calidad_aprobada is False:
            add("CALIDAD_REPROBADA", "critica", f"Control de calidad reprobado en «{e.nombre}»")

    actividad = [_aware(lote.creado_en) or ahora] + [_aware(e.completada_en or e.fecha) for e in etapas]
    sin_avance = ahora - max(actividad)
    if sin_avance > timedelta(days=DIAS_SIN_AVANCE):
        if not etapas:
            add("SIN_ETAPAS", "advertencia", f"El lote lleva {_hace(sin_avance)} sin ninguna etapa registrada")
        else:
            add("SIN_AVANCE", "advertencia", f"El lote lleva {_hace(sin_avance)} sin actividad")
    return out


def _detalle_lote(db: Session, lote: m.Lote) -> dict:
    ev = _linea_de_tiempo(db, lote)
    etapas = [x for x in ev if x["etapa_id"] is not None]
    return {"lote_id": lote.id, "codigo": lote.codigo, "producto": lote.producto.nombre,
            "estado": lote.estado, "fecha_produccion": lote.fecha_produccion,
            "cantidad": lote.cantidad_producida, "unidad": lote.producto.unidad,
            "calidad": _calidad_lote(lote), "etapas_total": len(etapas),
            "etapas_completadas": sum(1 for x in etapas if x["estado"] == "COMPLETADA"),
            "alertas": _alertas_de_lote(lote, _utc_now()) if lote.estado == "EN_PROCESO" else [],
            "linea_de_tiempo": ev}


def _pdf_certificado(d: dict, url_qr: str) -> bytes:
    """Certificado PDF con QR. Usa solo reportlab (Python puro, funciona en Vercel) y se arma
    en memoria porque el disco de Vercel no es persistente. ZONA_HORARIA define la hora mostrada."""
    from datetime import timezone as tzmod
    from xml.sax.saxutils import escape
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    try:
        tz = ZoneInfo(os.getenv("ZONA_HORARIA", "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        tz = tzmod.utc
    fases = {"PRODUCCION": "Producción", "ALMACENAMIENTO": "Almacenamiento", "ENTREGA": "Entrega"}
    calidad_txt = {True: "Aprobada", False: "Reprobada", None: "Sin control"}
    resultado_txt = {"APROBADA": ("Controles de calidad aprobados", "#1b7f3b"),
                     "REPROBADA": ("Control de calidad reprobado", "#b42318"),
                     "SIN_CONTROLES": ("Sin controles de calidad registrados", "#8a6100")}
    azul = colors.HexColor("#14365d")

    def fmt(dt):
        if not dt:
            return "-"
        if isinstance(dt, datetime):
            return _aware(dt).astimezone(tz).strftime("%d/%m/%Y %H:%M")
        return dt.strftime("%d/%m/%Y")

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=18 * mm, author="NOVATECH",
                            title=f"Certificado de trazabilidad {d['codigo']}")
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=20, leading=24, alignment=0, textColor=azul, spaceAfter=1)
    sub = ParagraphStyle("sub", parent=ss["Normal"], fontSize=9, textColor=colors.grey)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=12, textColor=azul, spaceBefore=10, spaceAfter=4)
    cel = ParagraphStyle("cel", parent=ss["Normal"], fontSize=8.5, leading=10.5)
    cab = ParagraphStyle("cab", parent=cel, textColor=colors.white, fontName="Helvetica-Bold")
    gris = ParagraphStyle("gris", parent=cel, textColor=colors.grey)
    peq = ParagraphStyle("peq", parent=ss["Normal"], fontSize=7.5, leading=9, textColor=colors.grey)

    def p(txt, st=cel):
        return Paragraph(escape(str(txt)), st)

    datos = [("Lote", d["codigo"]), ("Producto", d["producto"]),
             ("Cantidad producida", f"{d['cantidad']} {d.get('unidad', '')}".strip()),
             ("Fecha de producción", fmt(d["fecha_produccion"])), ("Estado del lote", d["estado"])]
    tabla_datos = Table([[p(k, gris), p(v)] for k, v in datos], colWidths=[38 * mm, 78 * mm])
    tabla_datos.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                     ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 3)]))

    qr = QrCodeWidget(url_qr, barLevel="M")
    x0, y0, x1, y1 = qr.getBounds()
    lado = 34 * mm
    dibujo = Drawing(lado, lado, transform=[lado / (x1 - x0), 0, 0, lado / (y1 - y0), 0, 0])
    dibujo.add(qr)
    cabecera = Table([[tabla_datos, [dibujo, Paragraph("Escanee para verificar este lote", peq)]]],
                     colWidths=[120 * mm, 54 * mm])
    cabecera.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                  ("BOX", (0, 0), (-1, -1), 0.6, colors.lightgrey),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 8),
                                  ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))

    texto, color = resultado_txt.get(d["calidad"], resultado_txt["SIN_CONTROLES"])
    resultado = Paragraph(f'<font color="{color}"><b>{escape(texto)}</b></font>',
                          ParagraphStyle("res", parent=cel, fontSize=10, spaceBefore=8))

    filas = [[Paragraph(t, cab) for t in ("Fecha", "Fase", "Evento", "Responsable", "Calidad")]]
    for e in d["eventos"]:
        evento = e["titulo"] + (" (pendiente)" if e.get("estado") in ("PENDIENTE", "RETRASADA") else "")
        filas.append([p(fmt(e["fecha"])), p(fases.get(e["fase"], e["fase"])), p(evento),
                      p(e.get("responsable") or "-"), p(calidad_txt.get(e.get("calidad_aprobada"), "-"))])
    historial = Table(filas, colWidths=[30 * mm, 28 * mm, 50 * mm, 34 * mm, 32 * mm], repeatRows=1)
    historial.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), azul),
                                   ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f6fa")]),
                                   ("GRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP")]))

    emision = f"Emitido el {fmt(_utc_now())}" + (f" por {d['emitido_por']}" if d.get("emitido_por") else "")
    contenido = [Paragraph("Certificado de trazabilidad", h1), Paragraph("NOVATECH - Trazabilidad inteligente", sub),
                 Spacer(1, 8), cabecera, resultado, Paragraph("Historial del lote", h2), historial, Spacer(1, 10),
                 Paragraph(escape(emision), peq), Paragraph("Verifique la autenticidad en: " + escape(url_qr), peq)]

    def pie(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.grey)
        canvas.drawString(18 * mm, 10 * mm, f"Lote {d['codigo']} - certificado generado automáticamente")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Página {canvas.getPageNumber()}")
        canvas.restoreState()

    doc.build(contenido, onFirstPage=pie, onLaterPages=pie)
    return buf.getvalue()


# Página pública que abre quien escanea el QR (va dentro de /api para que Vercel la enrute al backend)
VERIFICAR_HTML = r"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Verificación de lote - NOVATECH</title>
<style>
:root{--bg:#f4f6f9;--card:#fff;--txt:#16202c;--mut:#667085;--brd:#e2e6ec;--azul:#14365d;--ok:#1b7f3b;--mal:#b42318;--aviso:#8a6100}
@media (prefers-color-scheme:dark){:root{--bg:#0f1620;--card:#18212d;--txt:#e8edf3;--mut:#9aa7b6;--brd:#2a3645;--azul:#8fb8e8;--ok:#4cc27a;--mal:#ff8a80;--aviso:#e6b84d}}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--txt);padding:16px}
main{max-width:560px;margin:0 auto}h1{font-size:1.15rem;color:var(--azul);margin:8px 0 14px}
.card{background:var(--card);border:1px solid var(--brd);border-radius:12px;padding:16px;margin-bottom:14px}
dl{margin:0;display:grid;grid-template-columns:auto 1fr;gap:6px 14px}dt{color:var(--mut)}dd{margin:0;font-weight:600}
.sello{font-weight:700;margin-top:12px}.APROBADA{color:var(--ok)}.REPROBADA{color:var(--mal)}.SIN_CONTROLES{color:var(--aviso)}
ol{list-style:none;margin:0;padding:0}li{padding:10px 0 10px 16px;border-left:3px solid var(--brd);margin-left:6px;position:relative}
li::before{content:"";position:absolute;left:-7px;top:15px;width:11px;height:11px;border-radius:50%;background:var(--azul)}
li.PENDIENTE::before,li.RETRASADA::before{background:var(--card);border:2px solid var(--mut)}
li small{display:block;color:var(--mut)}#error{color:var(--mal);font-weight:600}
</style></head><body><main><h1>Verificación de lote</h1><div id="contenido"><p>Cargando…</p></div></main>
<script>
const FASES={PRODUCCION:"Producción",ALMACENAMIENTO:"Almacenamiento",ENTREGA:"Entrega"};
const CALIDAD={APROBADA:"Controles de calidad aprobados",REPROBADA:"Control de calidad reprobado",SIN_CONTROLES:"Sin controles de calidad registrados"};
const CAL_ETAPA={true:"calidad aprobada",false:"calidad reprobada"};
function el(tag,props={},...hijos){const e=document.createElement(tag);Object.assign(e,props);hijos.forEach(h=>e.append(h));return e}
function fecha(iso){const d=new Date(iso);return isNaN(d)?"":d.toLocaleString("es",{dateStyle:"medium",timeStyle:iso.length>10?"short":undefined})}
async function cargar(){
  const cont=document.getElementById("contenido");const token=new URLSearchParams(location.search).get("t");cont.replaceChildren();
  if(!token){cont.append(el("p",{id:"error",textContent:"Falta el código del lote."}));return}
  try{
    const resp=await fetch("/api/trazabilidad/"+encodeURIComponent(token));const json=await resp.json();
    if(!resp.ok)throw new Error(json.error||"No se pudo consultar el lote");const d=json.data;
    const datos=el("dl");
    [["Lote",d.lote],["Producto",d.producto],["Producción",fecha(d.fecha_produccion)],["Estado",d.estado]].forEach(([k,v])=>datos.append(el("dt",{textContent:k}),el("dd",{textContent:v})));
    cont.append(el("div",{className:"card"},datos,el("div",{className:"sello "+d.calidad,textContent:CALIDAD[d.calidad]||""})));
    const lista=el("ol");
    (d.historial||[]).forEach(ev=>{
      const detalle=[FASES[ev.fase]||ev.fase,fecha(ev.fecha),ev.estado==="PENDIENTE"?"pendiente":ev.estado==="RETRASADA"?"retrasada":"",CAL_ETAPA[ev.calidad_aprobada]||""].filter(Boolean).join(" · ");
      lista.append(el("li",{className:ev.estado},el("strong",{textContent:ev.titulo}),el("small",{textContent:detalle})));
    });
    cont.append(el("div",{className:"card"},el("strong",{textContent:"Historial"}),lista));
  }catch(e){cont.append(el("p",{id:"error",textContent:e.message}))}
}
cargar();
</script></body></html>"""


@tra.get("/lotes")
def listar_lotes(q: str | None = None, estado: str | None = None, producto_id: int | None = None,
                 db: Session = Depends(get_db), _=LEE):
    stmt = select(m.Lote).order_by(m.Lote.id.desc()).limit(300)
    if q:
        stmt = stmt.where(m.Lote.codigo.ilike(f"%{q.strip()}%"))
    if estado:
        stmt = stmt.where(m.Lote.estado == estado)
    if producto_id:
        stmt = stmt.where(m.Lote.producto_id == producto_id)
    return {"ok": True, "data": [s.LoteOut.model_validate(x) for x in db.scalars(stmt)]}


@tra.post("/lotes", status_code=201)
def crear_lote(d: s.LoteIn, db: Session = Depends(get_db), _=ESCRIBE):
    p = db.get(m.Producto, d.producto_id)
    if not p or not p.activo:
        raise HTTPException(404, "El producto no existe o está inactivo")
    if db.scalar(select(m.Lote.id).where(m.Lote.codigo == d.codigo)):
        raise HTTPException(409, "Ya existe un lote con ese código")
    lote = m.Lote(**d.model_dump(exclude_none=True))
    db.add(lote)
    _commit(db)
    return {"ok": True, "data": s.LoteOut.model_validate(lote)}


@tra.get("/lotes/{lote_id}")
def ver_lote(lote_id: int, db: Session = Depends(get_db), _=LEE):
    return {"ok": True, "data": s.LoteOut.model_validate(_lote_o_404(db, lote_id))}


@tra.get("/lotes/{lote_id}/trazabilidad")
def trazabilidad_lote(lote_id: int, db: Session = Depends(get_db), _=LEE):
    """Detalle completo: línea de tiempo (etapas, almacenamiento, despachos) y alertas del lote."""
    return {"ok": True, "data": _detalle_lote(db, _lote_o_404(db, lote_id))}


@tra.post("/lotes/{lote_id}/etapas", status_code=201)
def agregar_etapa(lote_id: int, d: EtapaPlanIn, db: Session = Depends(get_db), _=ESCRIBE):
    lote = _lote_o_404(db, lote_id)
    if lote.estado != "EN_PROCESO":
        raise HTTPException(400, f"No se pueden agregar etapas a un lote {lote.estado}")
    etapa = m.EtapaProduccion(**d.model_dump(exclude={"completada", "fecha"}, exclude_none=True))
    if d.fecha:
        etapa.fecha = d.fecha
    if d.completada:  # sin completada=false se comporta como antes: la etapa ya ocurrió
        etapa.completada_en = d.fecha or _utc_now()
    lote.etapas.append(etapa)
    _commit(db)
    return {"ok": True, "data": _detalle_lote(db, lote)}


def _etapa_editable(db: Session, etapa_id: int) -> m.EtapaProduccion:
    e = db.get(m.EtapaProduccion, etapa_id)
    if not e:
        raise HTTPException(404, "La etapa no existe")
    if e.lote.estado != "EN_PROCESO":
        raise HTTPException(400, f"El lote está {e.lote.estado}: su historial ya no se modifica")
    return e


@tra.post("/etapas/{etapa_id}/completar")
def completar_etapa(etapa_id: int, d: CompletarEtapaIn, db: Session = Depends(get_db), _=ESCRIBE):
    e = _etapa_editable(db, etapa_id)
    if e.completada_en:
        raise HTTPException(409, "La etapa ya estaba completada")
    e.completada_en = _utc_now()
    if d.responsable:
        e.responsable = d.responsable
    if d.observaciones:
        e.observaciones = d.observaciones
    if d.calidad_aprobada is not None:
        e.calidad_aprobada = d.calidad_aprobada
    _commit(db)
    return {"ok": True, "data": _detalle_lote(db, e.lote)}


@tra.post("/etapas/{etapa_id}/calidad")
def registrar_calidad(etapa_id: int, d: CalidadEtapaIn, db: Session = Depends(get_db), _=ESCRIBE):
    e = _etapa_editable(db, etapa_id)
    e.calidad_aprobada = d.aprobada
    if d.observaciones:
        e.observaciones = d.observaciones
    _commit(db)
    return {"ok": True, "data": _detalle_lote(db, e.lote)}


@tra.post("/lotes/{lote_id}/estado")
def cambiar_estado_lote(lote_id: int, d: s.LoteEstadoIn,
                        db: Session = Depends(get_db), u=ESCRIBE):
    lote = _lote_o_404(db, lote_id)
    lote.cambiar_estado(d.estado)  # valida transición y control de calidad
    if d.estado == "FINALIZADO":
        # Un lote finalizado ingresa su producción al inventario, en la misma transacción.
        p = _producto_bloqueado(db, lote.producto_id)
        _registrar(db, p, m.ENTRADA, lote.cantidad_producida, f"lote-{lote.id}-ingreso", u,
                   lote_id=lote.id, observacion=f"Producción del lote {lote.codigo}")
    _commit(db)
    return {"ok": True, "data": s.LoteOut.model_validate(lote)}


# Las alertas se calculan al consultar (no hace falta un proceso permanente en Vercel).
# Esta ruta debe ir ANTES de /trazabilidad/{qr_token} para que "alertas" no se tome como token.
@tra.get("/trazabilidad/alertas")
def alertas_trazabilidad(db: Session = Depends(get_db), _=LEE):
    ahora = _utc_now()
    lotes = db.scalars(select(m.Lote).where(m.Lote.estado == "EN_PROCESO")
                       .options(selectinload(m.Lote.etapas), selectinload(m.Lote.producto))
                       .order_by(m.Lote.id.desc()).limit(300)).all()
    data = [a for l in lotes for a in _alertas_de_lote(l, ahora)]
    data.sort(key=lambda a: (a["nivel"] != "critica", a["lote"]))
    return {"ok": True, "dias_sin_avance": DIAS_SIN_AVANCE,
            "resumen": {"criticas": sum(a["nivel"] == "critica" for a in data),
                        "advertencias": sum(a["nivel"] == "advertencia" for a in data)},
            "data": data}


@tra.get("/trazabilidad/{qr_token}")
def consulta_publica(qr_token: str, db: Session = Depends(get_db)):
    """Pública (la abre quien escanea el QR). Solo expone datos no sensibles."""
    lote = db.scalar(select(m.Lote).where(m.Lote.qr_token == qr_token))
    if not lote:
        raise HTTPException(404, "Código no válido")
    return {"ok": True, "data": {
        "lote": lote.codigo, "producto": lote.producto.nombre,
        "fecha_produccion": lote.fecha_produccion.isoformat(), "estado": lote.estado,
        "calidad": _calidad_lote(lote),
        "etapas": [{"nombre": e.nombre, "fecha": e.fecha.isoformat(),
                    "calidad_aprobada": e.calidad_aprobada} for e in lote.etapas],  # formato anterior
        "historial": _linea_de_tiempo(db, lote, publico=True)}}


@tra.get("/verificar", response_class=HTMLResponse)
def pagina_verificar():
    return HTMLResponse(VERIFICAR_HTML)


@tra.get("/lotes/{lote_id}/certificado")
def certificado_lote(lote_id: int, request: Request, db: Session = Depends(get_db), u=LEE):
    lote = _lote_o_404(db, lote_id)
    if lote.estado != "FINALIZADO":
        raise HTTPException(409, "Solo se emite certificado de lotes FINALIZADOS")
    det = _detalle_lote(db, lote)
    base = (os.getenv("PUBLIC_BASE_URL") or str(request.base_url)).rstrip("/")  # en Vercel: fijar PUBLIC_BASE_URL
    pdf = _pdf_certificado({
        "codigo": lote.codigo, "producto": det["producto"], "cantidad": format(det["cantidad"].normalize(), "f"),
        "unidad": det["unidad"], "fecha_produccion": lote.fecha_produccion, "estado": lote.estado,
        "calidad": det["calidad"], "eventos": det["linea_de_tiempo"], "emitido_por": u.nombre},
        f"{base}/api/verificar?t={lote.qr_token}")
    nombre = re.sub(r"[^A-Za-z0-9_.-]", "_", lote.codigo)
    return StreamingResponse(BytesIO(pdf), media_type="application/pdf",
                             headers={"Content-Disposition": f'inline; filename="certificado-{nombre}.pdf"'})


# ───────────── Logística ─────────────
log = APIRouter(prefix="/api", tags=["logistica"])


def _pedido_o_404(db: Session, pedido_id: int) -> m.Pedido:
    p = db.get(m.Pedido, pedido_id)
    if not p:
        raise HTTPException(404, "El pedido no existe")
    return p


def _entrega_o_404(db: Session, entrega_id: int) -> m.Entrega:
    e = db.get(m.Entrega, entrega_id)
    if not e:
        raise HTTPException(404, "La entrega no existe")
    return e


ACTIVAS = ("PROGRAMADA", "EN_RUTA")


# ── Transportistas ──
@log.get("/transportistas")
def listar_transportistas(inactivos: bool = False, db: Session = Depends(get_db), _=LEE):
    stmt = select(m.Transportista).order_by(m.Transportista.nombre)
    if not inactivos:
        stmt = stmt.where(m.Transportista.activo.is_(True))
    return {"ok": True, "data": [s.TransportistaOut.model_validate(x) for x in db.scalars(stmt)]}


@log.post("/transportistas", status_code=201)
def crear_transportista(d: s.TransportistaIn, db: Session = Depends(get_db), _=ESCRIBE):
    if db.scalar(select(m.Transportista.id).where(m.Transportista.documento == d.documento.strip())):
        raise HTTPException(409, "Ya existe un transportista con ese documento")
    t = m.Transportista(nombre=d.nombre.strip(), documento=d.documento.strip(), telefono=d.telefono)
    db.add(t)
    _commit(db)
    return {"ok": True, "data": s.TransportistaOut.model_validate(t)}


@log.delete("/transportistas/{transportista_id}")
def desactivar_transportista(transportista_id: int, db: Session = Depends(get_db), _=ADMIN):
    t = db.get(m.Transportista, transportista_id)
    if not t:
        raise HTTPException(404, "El transportista no existe")
    if db.scalar(select(m.Entrega.id).where(m.Entrega.transportista_id == t.id,
                                            m.Entrega.estado.in_(ACTIVAS))):
        raise HTTPException(409, "Tiene entregas activas")
    t.activo = False
    _commit(db)
    return {"ok": True, "data": s.TransportistaOut.model_validate(t)}


# ── Vehículos ──
@log.get("/vehiculos")
def listar_vehiculos(transportista_id: int | None = None, db: Session = Depends(get_db), _=LEE):
    stmt = select(m.Vehiculo).order_by(m.Vehiculo.placa)
    if transportista_id:
        stmt = stmt.where(m.Vehiculo.transportista_id == transportista_id)
    return {"ok": True, "data": [s.VehiculoOut.model_validate(x) for x in db.scalars(stmt)]}


@log.post("/vehiculos", status_code=201)
def crear_vehiculo(d: s.VehiculoIn, db: Session = Depends(get_db), _=ESCRIBE):
    if d.transportista_id and not db.get(m.Transportista, d.transportista_id):
        raise HTTPException(404, "El transportista no existe")
    if db.scalar(select(m.Vehiculo.id).where(m.Vehiculo.placa == d.placa)):
        raise HTTPException(409, "Ya existe un vehículo con esa placa")
    v = m.Vehiculo(**d.model_dump())
    db.add(v)
    _commit(db)
    return {"ok": True, "data": s.VehiculoOut.model_validate(v)}


@log.delete("/vehiculos/{vehiculo_id}")
def eliminar_vehiculo(vehiculo_id: int, db: Session = Depends(get_db), _=ADMIN):
    v = db.get(m.Vehiculo, vehiculo_id)
    if not v:
        raise HTTPException(404, "El vehículo no existe")
    if db.scalar(select(m.Entrega.id).where(m.Entrega.vehiculo_id == v.id).limit(1)):
        raise HTTPException(409, "El vehículo tiene entregas en su historial y no se puede eliminar")
    db.delete(v)
    _commit(db)
    return {"ok": True}


# ── Pedidos ──
@log.get("/pedidos")
def listar_pedidos(q: str | None = None, estado: str | None = None,
                   db: Session = Depends(get_db), _=LEE):
    stmt = select(m.Pedido).order_by(m.Pedido.id.desc()).limit(300)
    if estado:
        stmt = stmt.where(m.Pedido.estado == estado)
    if q:
        patron = f"%{q.strip()}%"
        stmt = stmt.where(m.Pedido.codigo.ilike(patron) | m.Pedido.cliente.ilike(patron))
    return {"ok": True, "data": [s.PedidoOut.model_validate(x) for x in db.scalars(stmt)]}


@log.get("/pedidos/{pedido_id}")
def ver_pedido(pedido_id: int, db: Session = Depends(get_db), _=LEE):
    return {"ok": True, "data": s.PedidoOut.model_validate(_pedido_o_404(db, pedido_id))}


@log.post("/pedidos", status_code=201)
def crear_pedido(d: s.PedidoIn, db: Session = Depends(get_db), _=ESCRIBE):
    ped = m.Pedido(codigo=f"TMP-{secrets.token_hex(6)}", cliente=d.cliente.strip(),
                   observaciones=d.observaciones)
    for it in d.detalles:
        p = db.get(m.Producto, it.producto_id)
        if not p or not p.activo:
            raise HTTPException(404, f"El producto {it.producto_id} no existe o está inactivo")
        if it.lote_id:
            lote = db.get(m.Lote, it.lote_id)
            if not lote or lote.producto_id != p.id:
                raise HTTPException(400, "El lote no corresponde al producto")
            if lote.estado != "FINALIZADO":
                raise HTTPException(400, "Solo se pueden usar lotes finalizados")
        ped.detalles.append(m.DetallePedido(**it.model_dump()))
    db.add(ped)
    db.flush()
    ped.codigo = f"PED-{ped.id:05d}"
    _commit(db)
    return {"ok": True, "data": s.PedidoOut.model_validate(ped)}


@log.post("/pedidos/{pedido_id}/estado")
def cambiar_estado_pedido(pedido_id: int, d: s.PedidoEstadoIn,
                          db: Session = Depends(get_db), _=ESCRIBE):
    ped = _pedido_o_404(db, pedido_id)
    if d.estado == "CANCELADO" and any(e.estado in ACTIVAS for e in ped.entregas):
        raise HTTPException(409, "El pedido tiene una entrega activa")
    ped.cambiar_estado(d.estado)
    _commit(db)
    return {"ok": True, "data": s.PedidoOut.model_validate(ped)}


@log.get("/pedidos/{pedido_id}/historial")
def historial_pedido(pedido_id: int, db: Session = Depends(get_db), _=LEE):
    ped = _pedido_o_404(db, pedido_id)
    movs = db.scalars(select(m.MovimientoInventario)
                      .where(m.MovimientoInventario.pedido_id == pedido_id)
                      .order_by(m.MovimientoInventario.id)).all()
    return {"ok": True, "data": {
        "pedido": s.PedidoOut.model_validate(ped),
        "entregas": [s.EntregaOut.model_validate(e) for e in ped.entregas],
        "movimientos": [s.MovimientoOut.model_validate(x) for x in movs],
    }}


# ── Entregas ──
@log.get("/entregas")
def listar_entregas(estado: str | None = None, pedido_id: int | None = None,
                    db: Session = Depends(get_db), _=LEE):
    stmt = select(m.Entrega).order_by(m.Entrega.id.desc()).limit(300)
    if estado:
        stmt = stmt.where(m.Entrega.estado == estado)
    if pedido_id:
        stmt = stmt.where(m.Entrega.pedido_id == pedido_id)
    return {"ok": True, "data": [s.EntregaOut.model_validate(x) for x in db.scalars(stmt)]}


@log.post("/entregas", status_code=201)
def crear_entrega(d: s.EntregaIn, db: Session = Depends(get_db), _=ESCRIBE):
    ped = _pedido_o_404(db, d.pedido_id)
    if ped.estado != "PREPARANDO":
        raise HTTPException(400, f"El pedido debe estar en PREPARANDO (está {ped.estado})")
    if any(e.estado in ACTIVAS for e in ped.entregas):
        raise HTTPException(409, "El pedido ya tiene una entrega activa")
    t = db.get(m.Transportista, d.transportista_id)
    if not t or not t.activo:
        raise HTTPException(404, "El transportista no existe o está inactivo")
    v = db.get(m.Vehiculo, d.vehiculo_id)
    if not v:
        raise HTTPException(404, "El vehículo no existe")
    if v.transportista_id != t.id:
        raise HTTPException(400, "El vehículo no pertenece a ese transportista")
    e = m.Entrega(**d.model_dump())
    db.add(e)
    _commit(db)
    return {"ok": True, "data": s.EntregaOut.model_validate(e)}


@log.post("/entregas/{entrega_id}/despachar")
def despachar_entrega(entrega_id: int, db: Session = Depends(get_db), u=ESCRIBE):
    """Pedido y entrega avanzan y el inventario descuenta, todo en una transacción."""
    e = _entrega_o_404(db, entrega_id)
    ped = _pedido_o_404(db, e.pedido_id)
    e.cambiar_estado("EN_RUTA")
    ped.cambiar_estado("DESPACHADO")
    # Se bloquean los productos en orden de id para evitar bloqueos cruzados
    prods = {i: _producto_bloqueado(db, i) for i in sorted({x.producto_id for x in ped.detalles})}
    for det in ped.detalles:
        partes = ([(det.lote_id, det.cantidad)] if det.lote_id
                  else auto.repartir_por_lotes(db, det.producto_id, det.cantidad))
        for i, (lote_id, cant) in enumerate(partes):
            ref = f"pedido-{ped.id}-det-{det.id}-salida" + (f"-p{i}" if i else "")
            _registrar(db, prods[det.producto_id], m.SALIDA, cant, ref, u,
                       lote_id=lote_id, pedido_id=ped.id,
                       observacion=f"Despacho del pedido {ped.codigo}")
    e.fecha_despacho = datetime.now(timezone.utc)
    _commit(db)
    return {"ok": True, "data": s.EntregaOut.model_validate(e)}


@log.post("/entregas/{entrega_id}/entregar")
def confirmar_entrega(entrega_id: int, db: Session = Depends(get_db), _=ESCRIBE):
    e = _entrega_o_404(db, entrega_id)
    ped = _pedido_o_404(db, e.pedido_id)
    e.cambiar_estado("ENTREGADA")
    ped.cambiar_estado("ENTREGADO")
    e.fecha_entrega = datetime.now(timezone.utc)
    _commit(db)
    return {"ok": True, "data": s.EntregaOut.model_validate(e)}

# ───────────── Eficiencia energética ─────────────
ene = APIRouter(prefix="/api", tags=["energia"])


def _rango(desde: date | None, hasta: date | None) -> tuple[date, date]:
    hasta = hasta or date.today()
    desde = desde or (hasta - timedelta(days=29))
    if desde > hasta:
        raise HTTPException(400, "'desde' no puede ser posterior a 'hasta'")
    return desde, hasta


def _registros(db: Session, desde: date, hasta: date, fuente_nombre: str | None = None,
               tipo_fuente: str | None = None) -> list[m.RegistroEnergetico]:
    stmt = (select(m.RegistroEnergetico)
            .where(m.RegistroEnergetico.fecha >= desde, m.RegistroEnergetico.fecha <= hasta)
            .order_by(m.RegistroEnergetico.fecha, m.RegistroEnergetico.id))
    if fuente_nombre:
        stmt = stmt.where(m.RegistroEnergetico.fuente_nombre.ilike(f"%{fuente_nombre.strip()}%"))
    if tipo_fuente:
        stmt = stmt.where(m.RegistroEnergetico.tipo_fuente == tipo_fuente)
    return list(db.scalars(stmt))


def _totales(filas: list[m.RegistroEnergetico]) -> dict:
    por_tipo: dict[str, dict] = {}
    costo_total, emis_total, sin_factor = 0.0, 0.0, 0
    for r in filas:
        t = por_tipo.setdefault(r.tipo_fuente, {"unidad": r.unidad, "cantidad": 0.0,
                                                "costo": 0.0, "emisiones_kg": 0.0})
        t["cantidad"] += float(r.cantidad)
        t["costo"] += float(r.costo)
        costo_total += float(r.costo)
        e = r.emisiones_kg  # polimorfismo: cada fuente calcula a su manera
        if e is None:
            sin_factor += 1
        else:
            t["emisiones_kg"] += float(e)
            emis_total += float(e)
    return {"registros": len(filas), "costo_total": round(costo_total, 2),
            "emisiones_kg": round(emis_total, 3), "registros_sin_factor": sin_factor,
            "por_tipo": {k: {kk: (round(vv, 3) if isinstance(vv, float) else vv)
                             for kk, vv in v.items()} for k, v in por_tipo.items()}}


def _variacion(actual: float, anterior: float) -> float | None:
    return None if anterior == 0 else round((actual - anterior) / anterior * 100, 1)


@ene.get("/energia/registros")
def listar_registros_energia(desde: date | None = None, hasta: date | None = None,
                             fuente: str | None = None, tipo_fuente: str | None = None,
                             db: Session = Depends(get_db), _=LEE):
    d, h = _rango(desde, hasta)
    filas = _registros(db, d, h, fuente, tipo_fuente)
    return {"ok": True, "desde": d, "hasta": h,
            "data": [s.RegistroEnergiaOut.model_validate(r) for r in reversed(filas)]}


@ene.post("/energia/registros", status_code=201)
def crear_registro_energia(d: s.RegistroEnergiaIn, db: Session = Depends(get_db), _=ESCRIBE):
    r = m.RegistroEnergetico(**d.model_dump())
    r.fuente.validar_cantidad(r.cantidad)  # regla propia de cada fuente
    db.add(r)
    _commit(db)
    return {"ok": True, "data": s.RegistroEnergiaOut.model_validate(r)}


@ene.put("/energia/registros/{registro_id}")
def editar_registro_energia(registro_id: int, d: s.RegistroEnergiaEditar,
                            db: Session = Depends(get_db), _=ESCRIBE):
    r = db.get(m.RegistroEnergetico, registro_id)
    if not r:
        raise HTTPException(404, "El registro no existe")
    for campo, valor in d.model_dump(exclude_unset=True).items():
        setattr(r, campo, valor)
    r.fuente.validar_cantidad(r.cantidad)
    _commit(db)
    return {"ok": True, "data": s.RegistroEnergiaOut.model_validate(r)}


@ene.delete("/energia/registros/{registro_id}")
def eliminar_registro_energia(registro_id: int, db: Session = Depends(get_db), _=ADMIN):
    r = db.get(m.RegistroEnergetico, registro_id)
    if not r:
        raise HTTPException(404, "El registro no existe")
    db.delete(r)
    _commit(db)
    return {"ok": True}


@ene.get("/energia/resumen")
def resumen_energia(desde: date | None = None, hasta: date | None = None,
                    db: Session = Depends(get_db), _=LEE):
    d, h = _rango(desde, hasta)
    filas = _registros(db, d, h)
    por_fuente: dict[str, dict] = {}
    for r in filas:
        f = por_fuente.setdefault(r.fuente_nombre, {"fuente": r.fuente_nombre, "costo": 0.0, "registros": 0})
        f["costo"] += float(r.costo)
        f["registros"] += 1
    ranking = sorted(({**v, "costo": round(v["costo"], 2)} for v in por_fuente.values()),
                     key=lambda x: x["costo"], reverse=True)
    return {"ok": True, "desde": d, "hasta": h, "data": {**_totales(filas), "ranking_costo": ranking}}


@ene.get("/energia/comparacion")
def comparar_periodos(desde: date | None = None, hasta: date | None = None,
                      db: Session = Depends(get_db), _=LEE):
    """Compara el periodo pedido con el periodo inmediatamente anterior de igual duración."""
    d, h = _rango(desde, hasta)
    dias = (h - d).days + 1
    ant_h = d - timedelta(days=1)
    ant_d = ant_h - timedelta(days=dias - 1)
    act, ant = _totales(_registros(db, d, h)), _totales(_registros(db, ant_d, ant_h))
    variacion_tipo = {}
    for tipo in set(act["por_tipo"]) | set(ant["por_tipo"]):
        a = act["por_tipo"].get(tipo, {}).get("cantidad", 0.0)
        b = ant["por_tipo"].get(tipo, {}).get("cantidad", 0.0)
        variacion_tipo[tipo] = _variacion(a, b)
    return {"ok": True, "data": {
        "actual": {"desde": d, "hasta": h, **act},
        "anterior": {"desde": ant_d, "hasta": ant_h, **ant},
        "variacion_costo_pct": _variacion(act["costo_total"], ant["costo_total"]),
        "variacion_cantidad_pct": variacion_tipo,
    }}


@ene.get("/energia/tendencia")
def tendencia_energia(meses: int = 6, db: Session = Depends(get_db), _=LEE):
    meses = max(1, min(meses, 24))
    hoy = date.today()
    claves = []
    for i in range(meses - 1, -1, -1):
        mes, anio = hoy.month - i, hoy.year
        while mes <= 0:
            mes += 12
            anio -= 1
        claves.append((anio, mes))
    inicio = date(claves[0][0], claves[0][1], 1)
    serie = {k: {"mes": f"{k[0]}-{k[1]:02d}", "costo": 0.0, "kwh": 0.0, "emisiones_kg": 0.0}
             for k in claves}
    for r in _registros(db, inicio, hoy):
        k = (r.fecha.year, r.fecha.month)
        if k in serie:
            serie[k]["costo"] += float(r.costo)
            if r.tipo_fuente == "ELECTRICIDAD":
                serie[k]["kwh"] += float(r.cantidad)
            e = r.emisiones_kg
            if e is not None:
                serie[k]["emisiones_kg"] += float(e)
    return {"ok": True, "data": [{k: (round(v, 2) if isinstance(v, float) else v)
                                  for k, v in p.items()} for p in serie.values()]}


@ene.get("/energia/alertas")
def alertas_energia(umbral_pct: float = 30, db: Session = Depends(get_db), _=LEE):
    """Compara el último registro de cada máquina con el promedio de sus 10 anteriores."""
    umbral_pct = max(5.0, min(umbral_pct, 300.0))
    desde = date.today() - timedelta(days=365)
    grupos: dict[tuple, list] = {}
    for r in reversed(_registros(db, desde, date.today())):  # más reciente primero
        grupos.setdefault((r.fuente_nombre, r.tipo_fuente), []).append(r)
    alertas = []
    for (nombre, tipo), lista in grupos.items():
        ultimo, previos = lista[0], lista[1:11]
        if len(previos) < 3:  # sin historial suficiente no se alerta
            continue
        promedio = sum(float(p.cantidad) for p in previos) / len(previos)
        if promedio <= 0:
            continue
        exceso = (float(ultimo.cantidad) - promedio) / promedio * 100
        if exceso >= umbral_pct:
            alertas.append({
                "fuente": nombre, "tipo_fuente": tipo, "unidad": ultimo.unidad,
                "fecha": ultimo.fecha, "cantidad": float(ultimo.cantidad),
                "promedio_previo": round(promedio, 3), "exceso_pct": round(exceso, 1),
                "nivel": "critica" if exceso >= umbral_pct * 2 else "advertencia"})
    alertas.sort(key=lambda a: a["exceso_pct"], reverse=True)
    return {"ok": True, "umbral_pct": umbral_pct, "data": alertas}

app.include_router(auth)
app.include_router(inv)
app.include_router(tra)
app.include_router(log)
app.include_router(ene)
app.include_router(auto.crear_router(LEE, ESCRIBE, ADMIN, _commit))

# Solo para desarrollo local: sirve el frontend. En Vercel lo sirve el CDN.
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
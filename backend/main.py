import os
import secrets
from io import BytesIO
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import bcrypt
import jwt
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend import models as m
from backend import schemas as s
from backend.database import engine, get_db, init_db
from backend.models import ReglaNegocioError, _utc_now

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        init_db()
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
    vars_ok = {v: bool(os.getenv(v)) for v in ("DATABASE_URL", "JWT_SECRET")}
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True, "base_de_datos": "conectada", "variables": vars_ok}
    except Exception as e:
        return JSONResponse(status_code=503, content={"ok": False, "base_de_datos": "sin conexión", "variables": vars_ok, "tipo_error": type(e).__name__})

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

def usuario_actual(cred: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)) -> m.Usuario:
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
    def dep(u: m.Usuario = Depends(usuario_actual)) -> m.Usuario:
        if u.rol not in roles:
            raise HTTPException(403, "No tienes permisos para esta operación")
        return u
    return dep

auth = APIRouter(prefix="/api", tags=["auth"])

@auth.post("/auth/primer-admin", status_code=201)
@auth.post("/auth/primer_admin", status_code=201, include_in_schema=False)
def primer_admin(d: s.UsuarioCrear, db: Session = Depends(get_db)):
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

# Inventario
inv = APIRouter(prefix="/api", tags=["inventario"])
LEE = Depends(usuario_actual)
ESCRIBE = Depends(requiere("admin", "operador"))
ADMIN = Depends(requiere("admin"))

def _producto_bloqueado(db: Session, producto_id: int) -> m.Producto:
    p = db.scalar(select(m.Producto).where(m.Producto.id == producto_id).with_for_update())
    if not p:
        raise HTTPException(404, "El producto no existe")
    return p

def _registrar(db, p, tipo, cantidad, ref, usuario, **extra) -> m.MovimientoInventario:
    stock = p.aplicar_movimiento(tipo, cantidad)
    mov = m.MovimientoInventario(producto_id=p.id, tipo=tipo, cantidad=cantidad, stock_resultante=stock, referencia_operacion=ref, usuario_id=usuario.id, **extra)
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
def listar_productos(q: str | None = None, stock_bajo: bool = False, inactivos: bool = False, db: Session = Depends(get_db), _=LEE):
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
    db.flush()
    if d.stock_inicial > 0:
        _registrar(db, p, m.ENTRADA, d.stock_inicial, f"inicial-{p.id}", u, observacion="Existencia inicial")
    _commit(db)
    return {"ok": True, "data": s.ProductoOut.model_validate(p)}

@inv.delete("/productos/{producto_id}")
def desactivar_producto(producto_id: int, db: Session = Depends(get_db), _=ADMIN):
    p = db.get(m.Producto, producto_id)
    if not p:
        raise HTTPException(404, "El producto no existe")
    p.activo = False
    _commit(db)
    return {"ok": True, "data": s.ProductoOut.model_validate(p)}

@inv.get("/inventario/movimientos")
def listar_movimientos(producto_id: int | None = None, limite: int = 100, db: Session = Depends(get_db), _=LEE):
    stmt = select(m.MovimientoInventario).order_by(m.MovimientoInventario.id.desc()).limit(min(limite, 500))
    if producto_id:
        stmt = stmt.where(m.MovimientoInventario.producto_id == producto_id)
    return {"ok": True, "data": [s.MovimientoOut.model_validate(x) for x in db.scalars(stmt)]}

@inv.post("/inventario/movimientos", status_code=201)
def registrar_movimiento(d: s.MovimientoIn, db: Session = Depends(get_db), u=ESCRIBE):
    if d.tipo.startswith("AJUSTE") and u.rol != "admin":
        raise HTTPException(403, "Solo un administrador puede registrar ajustes")
    previo = db.scalar(select(m.MovimientoInventario).where(m.MovimientoInventario.referencia_operacion == d.referencia_operacion))
    if previo:
        return {"ok": True, "duplicado": True, "data": s.MovimientoOut.model_validate(previo)}
    p = _producto_bloqueado(db, d.producto_id)
    mov = _registrar(db, p, d.tipo, d.cantidad, d.referencia_operacion, u, lote_id=d.lote_id, pedido_id=d.pedido_id, observacion=d.observacion)
    _commit(db)
    return {"ok": True, "duplicado": False, "data": s.MovimientoOut.model_validate(mov), "stock_actual": float(p.stock_actual)}

# Pedidos
ped = APIRouter(prefix="/api", tags=["pedidos"])

def _pedido_a_dict(p: m.Pedido) -> dict:
    out = {}
    for col in p.__table__.columns:
        v = getattr(p, col.key, None)
        if isinstance(v, Decimal):
            v = float(v)
        elif isinstance(v, (datetime, date)):
            v = v.isoformat()
        out[col.key] = v
    return out

@ped.get("/pedidos")
def listar_pedidos(limite: int = 200, db: Session = Depends(get_db), _=LEE):
    stmt = select(m.Pedido).order_by(m.Pedido.id.desc()).limit(min(limite, 500))
    return {"ok": True, "data": [_pedido_a_dict(p) for p in db.scalars(stmt)]}

@ped.get("/pedidos/{pedido_id}")
def obtener_pedido(pedido_id: int, db: Session = Depends(get_db), _=LEE):
    p = db.get(m.Pedido, pedido_id)
    if not p:
        raise HTTPException(404, f"El pedido {pedido_id} no existe")
    return {"ok": True, "data": _pedido_a_dict(p)}

@ped.post("/pedidos", status_code=201)
def crear_pedido(payload: dict, db: Session = Depends(get_db), _=ESCRIBE):
    columnas = {c.key for c in m.Pedido.__table__.columns} - {"id"}
    datos = {k: v for k, v in payload.items() if k in columnas}
    try:
        p = m.Pedido(**datos)
    except TypeError as e:
        raise HTTPException(400, f"Datos de pedido inválidos: {e}")
    db.add(p)
    _commit(db)
    return {"ok": True, "data": _pedido_a_dict(p)}

# Trazabilidad Manual Inteligente
tra = APIRouter(prefix="/api", tags=["trazabilidad"])

def _lote_o_404(db: Session, lote_id: int) -> m.Lote:
    l = db.get(m.Lote, lote_id)
    if not l:
        raise HTTPException(404, "El lote no existe")
    return l

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
        estado = "COMPLETADA" if e.completada_en else ("RETRASADA" if e.fecha_limite and _aware(e.fecha_limite) < ahora else "PENDIENTE")
        ev.append({"etapa_id": e.id, "fecha": e.completada_en or e.fecha, "fase": e.tipo or "PRODUCCION",
                   "titulo": e.nombre, "estado": estado, "responsable": e.responsable,
                   "calidad_aprobada": e.calidad_aprobada, "observaciones": e.observaciones, "fecha_limite": e.fecha_limite})
    ev.sort(key=lambda x: _aware(x["fecha"]) or ahora)
    if publico:
        ev = [{k: e[k] for k in ("fecha", "fase", "titulo", "estado", "calidad_aprobada")} for e in ev]
    return ev

def _alertas_de_lote(lote: m.Lote, ahora: datetime) -> list[dict]:
    out = []
    for e in lote.etapas:
        if e.completada_en is None and e.fecha_limite and _aware(e.fecha_limite) < ahora:
            retraso = ahora - _aware(e.fecha_limite)
            out.append({"lote_id": lote.id, "lote": lote.codigo, "producto": lote.producto.nombre,
                        "codigo": "ETAPA_RETRASADA", "nivel": "critica" if retraso >= timedelta(days=1) else "advertencia",
                        "mensaje": f"La etapa «{e.nombre}» venció hace {_hace(retraso)}"})
        if e.calidad_aprobada is False:
            out.append({"lote_id": lote.id, "lote": lote.codigo, "producto": lote.producto.nombre,
                        "codigo": "CALIDAD_REPROBADA", "nivel": "critica", "mensaje": f"Control reprobado en «{e.nombre}»"})
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

def _limpiar_fecha(fecha_str):
    if not fecha_str:
        return "—"
    try:
        return str(fecha_str).split(".")[0].replace("T", " ")
    except Exception:
        return str(fecha_str)

def _pdf_certificado(d: dict, url_qr: str) -> bytes:
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, Image
    from xml.sax.saxutils import escape

    azul = colors.HexColor("#14365d")
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=16*mm, bottomMargin=18*mm)
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=18, leading=22, textColor=azul, spaceAfter=1)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=12, textColor=azul, spaceBefore=10, spaceAfter=4)
    cel = ParagraphStyle("cel", parent=ss["Normal"], fontSize=8.5, leading=10.5)
    cab = ParagraphStyle("cab", parent=cel, textColor=colors.white, fontName="Helvetica-Bold")
    gris = ParagraphStyle("gris", parent=cel, textColor=colors.grey)
    peq = ParagraphStyle("peq", parent=ss["Normal"], fontSize=7.5, leading=9, textColor=colors.grey)

    def p(txt, st=cel): return Paragraph(escape(str(txt)), st)

    story = []

    logo_path = str(Path(__file__).resolve().parent.parent / "frontend" / "img" / "logo-novatech-negro.png")
    if os.path.exists(logo_path):
        logo = Image(logo_path, width=75, height=30)
        logo.hAlign = 'LEFT'
        story.append(logo)
        story.append(Spacer(1, 6))

    story.append(Paragraph("Certificado de trazabilidad", h1))
    story.append(Spacer(1, 6))

    datos = [
        ("Lote", d["codigo"]), 
        ("Producto", d["producto"]), 
        ("Cantidad", f"{d['cantidad']} {d.get('unidad', '')}".strip()), 
        ("Fecha", _limpiar_fecha(d["fecha_produccion"])), 
        ("Estado", d["estado"])
    ]
    tabla_datos = Table([[p(k, gris), p(v)] for k, v in datos], colWidths=[38*mm, 78*mm])
    
    qr = QrCodeWidget(url_qr, barLevel="M")
    x0, y0, x1, y1 = qr.getBounds()
    lado = 34*mm
    dibujo = Drawing(lado, lado, transform=[lado/(x1-x0), 0, 0, lado/(y1-y0), 0, 0])
    dibujo.add(qr)
    cabecera = Table([[tabla_datos, [dibujo, Paragraph("Escanee para verificar", peq)]]], colWidths=[120*mm, 54*mm])
    cabecera.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "TOP"), ("BOX", (0,0), (-1,-1), 0.6, colors.lightgrey), ("LEFTPADDING", (0,0), (-1,-1), 8), ("TOPPADDING", (0,0), (-1,-1), 8)]))

    filas = [[Paragraph(t, cab) for t in ("Fecha", "Fase", "Evento", "Responsable", "Calidad")]]
    for e in d["linea_de_tiempo"]:
        filas.append([
            p(_limpiar_fecha(e["fecha"])), 
            p(e["fase"]), 
            p(e["titulo"]), 
            p(e.get("responsable") or "-"), 
            p(str(e.get("calidad_aprobada") or "-"))
        ])
    historial = Table(filas, colWidths=[30*mm, 28*mm, 50*mm, 34*mm, 32*mm], repeatRows=1)
    historial.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), azul), ("GRID", (0,0), (-1,-1), 0.3, colors.lightgrey)]))

    story.extend([cabecera, Spacer(1, 10), Paragraph("Historial", h2), historial, Spacer(1, 10), Paragraph("Verifique en: " + url_qr, peq)])
    doc.build(story)
    return buf.getvalue()

@tra.get("/lotes")
def listar_lotes(q: str | None = None, estado: str | None = None, db: Session = Depends(get_db), _=LEE):
    stmt = select(m.Lote).order_by(m.Lote.id.desc()).limit(300)
    if q: stmt = stmt.where(m.Lote.codigo.ilike(f"%{q.strip()}%"))
    if estado: stmt = stmt.where(m.Lote.estado == estado)
    return {"ok": True, "data": [s.LoteOut.model_validate(x) for x in db.scalars(stmt)]}

@tra.post("/lotes", status_code=201)
def crear_lote(d: s.LoteIn, db: Session = Depends(get_db), _=ESCRIBE):
    p = db.get(m.Producto, d.producto_id)
    if not p or not p.activo: raise HTTPException(404, "Producto inactivo o inexistente")
    if db.scalar(select(m.Lote.id).where(m.Lote.codigo == d.codigo)): raise HTTPException(409, "El lote ya existe")
    lote = m.Lote(**d.model_dump(exclude_none=True))
    db.add(lote)
    _commit(db)
    return {"ok": True, "data": s.LoteOut.model_validate(lote)}

@tra.get("/lotes/{lote_id}/trazabilidad")
def trazabilidad_lote(lote_id: int, db: Session = Depends(get_db), _=LEE):
    return {"ok": True, "data": _detalle_lote(db, _lote_o_404(db, lote_id))}

@tra.post("/lotes/{lote_id}/etapas", status_code=201)
def agregar_etapa(lote_id: int, d: s.EtapaIn, db: Session = Depends(get_db), _=ESCRIBE):
    lote = _lote_o_404(db, lote_id)
    if lote.estado != "EN_PROCESO": raise HTTPException(400, "Lote cerrado")
    etapa = m.EtapaProduccion(**d.model_dump(exclude_none=True), completada_en=_utc_now())
    lote.etapas.append(etapa)
    _commit(db)
    return {"ok": True, "data": _detalle_lote(db, lote)}

@tra.post("/lotes/{lote_id}/estado")
def cambiar_estado_lote(lote_id: int, d: s.LoteEstadoIn, db: Session = Depends(get_db), u=ESCRIBE):
    lote = _lote_o_404(db, lote_id)
    lote.cambiar_estado(d.estado)
    if d.estado == "FINALIZADO":
        p = _producto_bloqueado(db, lote.producto_id)
        _registrar(db, p, m.ENTRADA, lote.cantidad_producida, f"lote-{lote.id}-ingreso", u, lote_id=lote.id, observacion=f"Producción lote {lote.codigo}")
    _commit(db)
    return {"ok": True, "data": s.LoteOut.model_validate(lote)}

@tra.get("/lotes/{lote_id}/certificado")
def certificado_lote(lote_id: int, request: Request, db: Session = Depends(get_db)):
    lote = _lote_o_404(db, lote_id)
    if lote.estado != "FINALIZADO": raise HTTPException(409, "Solo lotes finalizados")
    det = _detalle_lote(db, lote)
    base = (os.getenv("PUBLIC_BASE_URL") or str(request.base_url)).rstrip("/")
    pdf = _pdf_certificado(det, f"{base}/api/trazabilidad/{lote.qr_token}")
    return StreamingResponse(BytesIO(pdf), media_type="application/pdf", headers={"Content-Disposition": f"inline; filename=certificado_{lote.codigo}.pdf"})

@tra.get("/trazabilidad/alertas")
def alertas_trazabilidad(db: Session = Depends(get_db), _=LEE):
    ahora = _utc_now()
    lotes = db.scalars(select(m.Lote).where(m.Lote.estado == "EN_PROCESO").options(selectinload(m.Lote.etapas), selectinload(m.Lote.producto))).all()
    data = [a for l in lotes for a in _alertas_de_lote(l, ahora)]
    return {"ok": True, "data": data}

@tra.get("/trazabilidad/{qr_token}")
def consulta_publica(qr_token: str, db: Session = Depends(get_db)):
    lote = db.scalar(select(m.Lote).where(m.Lote.qr_token == qr_token))
    if not lote: raise HTTPException(404, "Código no válido")
    return {"ok": True, "data": {"lote": lote.codigo, "producto": lote.producto.nombre, "estado": lote.estado, "calidad": _calidad_lote(lote), "historial": _linea_de_tiempo(db, lote, publico=True)}}

# Logística Inteligente
log = APIRouter(prefix="/api/logistica", tags=["logistica"])

@log.get("/transportistas")
def listar_transportistas(db: Session = Depends(get_db), _=LEE):
    filas = db.scalars(select(m.Transportista).order_by(m.Transportista.id.desc())).all()
    return {"ok": True, "data": [s.TransportistaOut.model_validate(t) for t in filas]}

@log.post("/transportistas", status_code=201)
def crear_transportista(d: s.TransportistaIn, db: Session = Depends(get_db), _=ESCRIBE):
    if db.scalar(select(m.Transportista.id).where(m.Transportista.documento == d.documento)):
        raise HTTPException(409, "Ya existe un transportista con ese documento")
    t = m.Transportista(**d.model_dump())
    db.add(t)
    _commit(db)
    return {"ok": True, "data": s.TransportistaOut.model_validate(t)}

@log.get("/vehiculos")
def listar_vehiculos(db: Session = Depends(get_db), _=LEE):
    filas = db.scalars(select(m.Vehiculo).order_by(m.Vehiculo.id.desc())).all()
    return {"ok": True, "data": [s.VehiculoOut.model_validate(v) for v in filas]}

@log.post("/vehiculos", status_code=201)
def crear_vehiculo(d: s.VehiculoIn, db: Session = Depends(get_db), _=ESCRIBE):
    if db.scalar(select(m.Vehiculo.id).where(m.Vehiculo.placa == d.placa)):
        raise HTTPException(409, "Ya existe un vehículo con esa placa")
    v = m.Vehiculo(**d.model_dump())
    db.add(v)
    _commit(db)
    return {"ok": True, "data": s.VehiculoOut.model_validate(v)}

@log.get("/entregas")
def listar_entregas(db: Session = Depends(get_db), _=LEE):
    filas = db.scalars(select(m.Entrega).order_by(m.Entrega.id.desc())).all()
    return {"ok": True, "data": [s.EntregaOut.model_validate(e) for e in filas]}

@log.post("/entregas", status_code=201)
def crear_entrega(d: s.EntregaIn, db: Session = Depends(get_db), _=ESCRIBE):
    transportista = db.get(m.Transportista, d.transportista_id)
    if not transportista:
        raise HTTPException(404, "Transportista no encontrado")

    # Si el pedido no existe, lo creamos automáticamente en el momento
    pedido = db.get(m.Pedido, d.pedido_id)
    if not pedido:
        codigo_auto = f"PED-{d.pedido_id:03d}"
        pedido = m.Pedido(
            id=d.pedido_id,
            codigo=codigo_auto,
            cliente="Cliente General / Express",
            estado="PROGRAMADO",
            fecha_pedido=_utc_now(),
            creado_en=_utc_now(),
            actualizado_en=_utc_now()
        )
        db.add(pedido)
        db.flush()

    distancia = d.distancia_km
    tarifa = distancia * transportista.costo_por_km
    comision = tarifa * Decimal("0.15")

    # Upsert si ya existe la entrega
    entrega_existente = db.scalar(select(m.Entrega).where(m.Entrega.pedido_id == d.pedido_id))
    if entrega_existente:
        entrega_existente.transportista_id = d.transportista_id
        entrega_existente.vehiculo_id = d.vehiculo_id
        entrega_existente.origen = d.origen
        entrega_existente.destino = d.destino
        entrega_existente.distancia_km = d.distancia_km
        entrega_existente.tarifa = tarifa
        entrega_existente.comision = comision
        entrega_existente.fecha_despacho = _utc_now()
        _commit(db)
        return {"ok": True, "mensaje": "Entrega actualizada exitosamente", "data": s.EntregaOut.model_validate(entrega_existente)}

    entrega = m.Entrega(
        **d.model_dump(),
        tarifa=tarifa,
        comision=comision,
        fecha_despacho=_utc_now()
    )
    db.add(entrega)
    _commit(db)
    return {"ok": True, "data": s.EntregaOut.model_validate(entrega)}

@log.post("/entregas/auto-asignar", status_code=201)
def auto_asignar_entrega(pedido_id: int | None = None, destino: str | None = None, payload: dict | None = None, db: Session = Depends(get_db), _=ESCRIBE):
    if payload:
        pedido_id = pedido_id or payload.get("pedido_id")
        destino = destino or payload.get("destino", "Calle 72 # 45-20, Barranquilla")

    if not pedido_id:
        raise HTTPException(400, "El campo 'pedido_id' es obligatorio")
    if not destino:
        destino = "Calle 72 # 45-20, Barranquilla"

    pedido = db.get(m.Pedido, pedido_id)
    if not pedido:
        raise HTTPException(404, f"Pedido no encontrado (id {pedido_id})")

    transportista = db.scalar(
        select(m.Transportista)
        .where(m.Transportista.activo.is_(True))
        .order_by(m.Transportista.valoracion.desc(), m.Transportista.costo_por_km.asc())
        .limit(1)
    )
    if not transportista:
        raise HTTPException(400, "No hay transportistas activos disponibles")

    vehiculo = db.scalar(
        select(m.Vehiculo)
        .where(m.Vehiculo.transportista_id == transportista.id, m.Vehiculo.activo.is_(True))
        .limit(1)
    )
    if not vehiculo:
        vehiculo = db.scalar(select(m.Vehiculo).where(m.Vehiculo.activo.is_(True)).limit(1))

    if not vehiculo:
        raise HTTPException(400, "No hay vehículos disponibles para la asignación")

    distancia_km = Decimal("12.50")
    tarifa = distancia_km * transportista.costo_por_km
    comision = tarifa * Decimal("0.15")

    entrega_existente = db.scalar(select(m.Entrega).where(m.Entrega.pedido_id == pedido.id))
    if entrega_existente:
        entrega_existente.transportista_id = transportista.id
        entrega_existente.vehiculo_id = vehiculo.id
        entrega_existente.origen = "Bodega Principal Novatech"
        entrega_existente.destino = destino
        entrega_existente.distancia_km = distancia_km
        entrega_existente.tarifa = tarifa
        entrega_existente.comision = comision
        entrega_existente.estado = "PROGRAMADA"
        entrega_existente.fecha_despacho = _utc_now()
        _commit(db)
        return {
            "ok": True,
            "mensaje": "Ruta y transportista actualizados de forma inteligente",
            "data": s.EntregaOut.model_validate(entrega_existente)
        }

    entrega = m.Entrega(
        pedido_id=pedido.id,
        transportista_id=transportista.id,
        vehiculo_id=vehiculo.id,
        origen="Bodega Principal Novatech",
        destino=destino,
        distancia_km=distancia_km,
        tarifa=tarifa,
        comision=comision,
        estado="PROGRAMADA",
        fecha_despacho=_utc_now()
    )
    db.add(entrega)
    _commit(db)

    return {
        "ok": True,
        "mensaje": "Ruta y transportista asignados de forma inteligente",
        "data": s.EntregaOut.model_validate(entrega)
    }

@log.patch("/entregas/{entrega_id}/estado")
def actualizar_estado_entrega(entrega_id: int, d: s.EntregaEstadoIn, db: Session = Depends(get_db), _=ESCRIBE):
    e = db.get(m.Entrega, entrega_id)
    if not e:
        raise HTTPException(404, "Entrega no encontrada")
    e.cambiar_estado(d.estado)
    if d.estado == "ENTREGADA":
        e.fecha_entrega = _utc_now()
    _commit(db)
    return {"ok": True, "data": s.EntregaOut.model_validate(e)}

app.include_router(auth)
app.include_router(inv)
app.include_router(ped)
app.include_router(tra)
app.include_router(log)

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
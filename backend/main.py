import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import bcrypt
import jwt
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend import automatizacion as auto
from backend import models as m
from backend import schemas as s
from backend.database import engine, get_db, init_db
from backend.models import ReglaNegocioError
import secrets


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Crea las tablas si no existen. Si la BD falla, el servidor sigue arriba
    # y /api/health informa el problema.
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


@tra.post("/lotes/{lote_id}/etapas", status_code=201)
def agregar_etapa(lote_id: int, d: s.EtapaIn, db: Session = Depends(get_db), _=ESCRIBE):
    lote = _lote_o_404(db, lote_id)
    if lote.estado != "EN_PROCESO":
        raise HTTPException(400, f"No se pueden agregar etapas a un lote {lote.estado}")
    lote.etapas.append(m.EtapaProduccion(**d.model_dump(exclude_none=True)))
    _commit(db)
    return {"ok": True, "data": s.LoteOut.model_validate(lote)}


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


@tra.get("/trazabilidad/{qr_token}")
def consulta_publica(qr_token: str, db: Session = Depends(get_db)):
    """Pública (la abre quien escanea el QR). Solo expone datos no sensibles."""
    lote = db.scalar(select(m.Lote).where(m.Lote.qr_token == qr_token))
    if not lote:
        raise HTTPException(404, "Código no válido")
    prod = db.get(m.Producto, lote.producto_id)
    return {"ok": True, "data": {
        "lote": lote.codigo,
        "producto": prod.nombre if prod else None,
        "fecha_produccion": lote.fecha_produccion.isoformat(),
        "estado": lote.estado,
        "etapas": [{"nombre": e.nombre, "fecha": e.fecha.isoformat(),
                    "calidad_aprobada": e.calidad_aprobada} for e in lote.etapas],
    }}


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
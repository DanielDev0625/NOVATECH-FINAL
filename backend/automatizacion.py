import calendar
import hashlib
import math
import secrets
import statistics
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import (Boolean, DateTime, ForeignKey, Numeric, String,
                        UniqueConstraint, case, func, select)
from sqlalchemy.orm import Mapped, Session, mapped_column

from backend import models as m
from backend.database import get_db


# ───────────── Tablas nuevas (no modifican las existentes) ─────────────
class ClaveApi(m.ModeloBase):
    """Credencial para que medidores/scripts envíen datos sin iniciar sesión."""
    __tablename__ = "claves_api"
    nombre: Mapped[str] = mapped_column(String(80))
    prefijo: Mapped[str] = mapped_column(String(12))
    clave_hash: Mapped[str] = mapped_column(String(64), unique=True)  # nunca se guarda la clave
    activa: Mapped[bool] = mapped_column(Boolean, default=True)
    tarifa_defecto: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    factor_defecto: Mapped[Decimal | None] = mapped_column(Numeric(10, 5))
    ultimo_uso: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LecturaIngestada(m.ModeloBase):
    """Evita duplicados: un reintento con el mismo id_externo no crea otro registro."""
    __tablename__ = "lecturas_ingestadas"
    clave_id: Mapped[int] = mapped_column(ForeignKey("claves_api.id"))
    id_externo: Mapped[str] = mapped_column(String(80))
    registro_id: Mapped[int] = mapped_column(ForeignKey("registros_energeticos.id"))
    __table_args__ = (UniqueConstraint("clave_id", "id_externo", name="uq_lectura_clave_externo"),)


# ───────────── Asignación automática de lotes (FIFO) ─────────────
def _disponible_por_lote(db: Session, lote_ids: list[int]) -> dict[int, Decimal]:
    """Existencia de cada lote = movimientos que suman − movimientos que restan."""
    if not lote_ids:
        return {}
    mv = m.MovimientoInventario
    signo = case((mv.tipo.in_(m.TIPOS_QUE_SUMAN), mv.cantidad), else_=-mv.cantidad)
    filas = db.execute(select(mv.lote_id, func.sum(signo))
                       .where(mv.lote_id.in_(lote_ids)).group_by(mv.lote_id)).all()
    return {lid: Decimal(total or 0) for lid, total in filas}


def repartir_por_lotes(db: Session, producto_id: int, cantidad: Decimal) -> list[tuple[int | None, Decimal]]:
    """Reparte una cantidad entre lotes finalizados, el más antiguo primero (FIFO).
    Lo que ningún lote cubre (existencia inicial sin lote) queda con lote None."""
    lotes = db.scalars(select(m.Lote).where(m.Lote.producto_id == producto_id,
                                            m.Lote.estado == "FINALIZADO")
                       .order_by(m.Lote.fecha_produccion, m.Lote.id)).all()
    disp = _disponible_por_lote(db, [l.id for l in lotes])
    partes, resto = [], Decimal(cantidad)
    for l in lotes:
        d = disp.get(l.id, Decimal(0))
        if d <= 0:
            continue
        tomar = min(d, resto)
        partes.append((l.id, tomar))
        resto -= tomar
        if resto <= 0:
            break
    if resto > 0:
        partes.append((None, resto))
    return partes


# ───────────── Esquemas de la ingesta ─────────────
class LecturaIn(BaseModel):
    id_externo: str = Field(min_length=1, max_length=80)
    fuente_nombre: str = Field(min_length=2, max_length=120)
    tipo_fuente: Literal["ELECTRICIDAD", "DIESEL", "GASOLINA"] = "ELECTRICIDAD"
    fecha: date | None = None
    cantidad: Decimal = Field(gt=0)
    tarifa_unitaria: Decimal | None = Field(default=None, ge=0)
    factor_emision: Decimal | None = Field(default=None, ge=0)


class LecturasIn(BaseModel):
    lecturas: list[LecturaIn] = Field(min_length=1, max_length=500)


class ClaveIn(BaseModel):
    nombre: str = Field(min_length=2, max_length=80)
    tarifa_defecto: Decimal | None = Field(default=None, ge=0)
    factor_defecto: Decimal | None = Field(default=None, ge=0)


def _clave_desde_header(x_api_key: str | None = Header(default=None),
                        db: Session = Depends(get_db)) -> ClaveApi:
    if not x_api_key:
        raise HTTPException(401, "Falta la cabecera X-API-Key")
    h = hashlib.sha256(x_api_key.encode()).hexdigest()
    c = db.scalar(select(ClaveApi).where(ClaveApi.clave_hash == h, ClaveApi.activa.is_(True)))
    if not c:
        raise HTTPException(401, "Clave de API inválida")
    return c


def crear_router(lee, escribe, admin, commit) -> APIRouter:
    """Recibe las dependencias de main.py para no importar main (evita ciclos)."""
    router = APIRouter(prefix="/api", tags=["automatizacion"])

    # ── Reposición predictiva ──
    @router.get("/automatizacion/reposicion")
    def reposicion(dias: int = 30, lead_time: int = 3, cobertura: int = 15,
                   db: Session = Depends(get_db), _=lee):
        """Estima cuántos días de stock quedan según el consumo real y los pedidos abiertos."""
        dias, lead_time, cobertura = max(7, min(dias, 180)), max(0, min(lead_time, 60)), max(1, min(cobertura, 90))
        desde = datetime.now(timezone.utc) - timedelta(days=dias)
        mv = m.MovimientoInventario
        anuladas = select(mv.compensa_a_id).where(mv.compensa_a_id.is_not(None))
        ventas = {pid: (float(t or 0), int(n)) for pid, t, n in db.execute(
            select(mv.producto_id, func.sum(mv.cantidad), func.count())
            .where(mv.tipo == m.SALIDA, mv.fecha >= desde, mv.compensa_a_id.is_(None),
                   mv.id.not_in(anuladas))
            .group_by(mv.producto_id))}
        comprometida = {pid: float(t or 0) for pid, t in db.execute(
            select(m.DetallePedido.producto_id, func.sum(m.DetallePedido.cantidad))
            .join(m.Pedido, m.Pedido.id == m.DetallePedido.pedido_id)
            .where(m.Pedido.estado.in_(("PENDIENTE", "PREPARANDO")))
            .group_by(m.DetallePedido.producto_id))}
        filas = []
        for p in db.scalars(select(m.Producto).where(m.Producto.activo.is_(True))):
            total, nmov = ventas.get(p.id, (0.0, 0))
            consumo = total / dias
            stock, comp = float(p.stock_actual), comprometida.get(p.id, 0.0)
            disp = stock - comp
            cob = None if consumo == 0 else round(disp / consumo, 1)
            sugerido = max(0.0, consumo * (lead_time + cobertura) - disp)
            if (disp <= 0 and (consumo > 0 or comp > 0)) or (cob is not None and cob < lead_time):
                nivel = "critico"
            elif sugerido > 0 or p.stock_bajo:
                nivel = "advertencia"
            else:
                nivel = "ok"
            filas.append({
                "producto_id": p.id, "codigo": p.codigo, "nombre": p.nombre, "unidad": p.unidad,
                "stock": stock, "comprometido": comp, "disponible": round(disp, 3),
                "consumo_diario": round(consumo, 3), "dias_cobertura": cob,
                "sugerido": math.ceil(sugerido) if sugerido > 0 else 0, "nivel": nivel,
                "confianza": "baja" if nmov < 3 else "normal"})
        orden = {"critico": 0, "advertencia": 1, "ok": 2}
        filas.sort(key=lambda f: (orden[f["nivel"]], f["dias_cobertura"] if f["dias_cobertura"] is not None else 1e9))
        return {"ok": True, "parametros": {"dias": dias, "lead_time": lead_time, "cobertura": cobertura}, "data": filas}

    # ── Vista previa de la asignación de lotes ──
    @router.get("/automatizacion/pedidos/{pedido_id}/asignacion")
    def asignacion(pedido_id: int, db: Session = Depends(get_db), _=lee):
        ped = db.get(m.Pedido, pedido_id)
        if not ped:
            raise HTTPException(404, "El pedido no existe")
        salida = []
        for det in ped.detalles:
            if det.lote_id:
                partes = [(det.lote_id, det.cantidad)]
            else:
                partes = repartir_por_lotes(db, det.producto_id, det.cantidad)
            codigos = {l.id: l.codigo for l in db.scalars(
                select(m.Lote).where(m.Lote.id.in_([x[0] for x in partes if x[0]])))}
            salida.append({"producto_id": det.producto_id, "cantidad": float(det.cantidad),
                           "fijo": det.lote_id is not None,
                           "partes": [{"lote": codigos.get(lid, "Sin lote (existencia no trazada)"),
                                       "cantidad": float(c)} for lid, c in partes]})
        return {"ok": True, "data": salida}

    # ── Ingesta automática de energía ──
    @router.post("/ingesta/claves", status_code=201)
    def crear_clave(d: ClaveIn, db: Session = Depends(get_db), _=admin):
        plana = "nt_" + secrets.token_urlsafe(32)
        c = ClaveApi(nombre=d.nombre.strip(), prefijo=plana[:8],
                     clave_hash=hashlib.sha256(plana.encode()).hexdigest(),
                     tarifa_defecto=d.tarifa_defecto, factor_defecto=d.factor_defecto)
        db.add(c)
        commit(db)
        return {"ok": True, "clave": plana, "aviso": "Guárdala ahora: no se puede volver a ver",
                "data": {"id": c.id, "nombre": c.nombre, "prefijo": c.prefijo}}

    @router.get("/ingesta/claves")
    def listar_claves(db: Session = Depends(get_db), _=admin):
        return {"ok": True, "data": [
            {"id": c.id, "nombre": c.nombre, "prefijo": c.prefijo, "activa": c.activa,
             "tarifa_defecto": float(c.tarifa_defecto) if c.tarifa_defecto is not None else None,
             "factor_defecto": float(c.factor_defecto) if c.factor_defecto is not None else None,
             "ultimo_uso": c.ultimo_uso}
            for c in db.scalars(select(ClaveApi).order_by(ClaveApi.id.desc()))]}

    @router.delete("/ingesta/claves/{clave_id}")
    def desactivar_clave(clave_id: int, db: Session = Depends(get_db), _=admin):
        c = db.get(ClaveApi, clave_id)
        if not c:
            raise HTTPException(404, "La clave no existe")
        c.activa = False
        commit(db)
        return {"ok": True}

    @router.post("/ingesta/energia")
    def ingestar(d: LecturasIn, c: ClaveApi = Depends(_clave_desde_header),
                 db: Session = Depends(get_db)):
        """Para medidores/scripts. Idempotente por id_externo. Usa X-API-Key."""
        ids = [l.id_externo for l in d.lecturas]
        vistos = set(db.scalars(select(LecturaIngestada.id_externo)
                                .where(LecturaIngestada.clave_id == c.id,
                                       LecturaIngestada.id_externo.in_(ids))))
        nuevas, duplicadas, errores = 0, 0, []
        for l in d.lecturas:
            if l.id_externo in vistos:
                duplicadas += 1
                continue
            tarifa = l.tarifa_unitaria if l.tarifa_unitaria is not None else c.tarifa_defecto
            if tarifa is None:
                errores.append({"id_externo": l.id_externo, "error": "Falta la tarifa (ni en la lectura ni en la clave)"})
                continue
            r = m.RegistroEnergetico(
                fuente_nombre=l.fuente_nombre.strip(), tipo_fuente=l.tipo_fuente,
                fecha=l.fecha or date.today(), cantidad=l.cantidad, tarifa_unitaria=tarifa,
                factor_emision=l.factor_emision if l.factor_emision is not None else c.factor_defecto)
            try:
                r.fuente.validar_cantidad(r.cantidad)
            except m.ReglaNegocioError as e:
                errores.append({"id_externo": l.id_externo, "error": str(e)})
                continue
            db.add(r)
            db.flush()
            db.add(LecturaIngestada(clave_id=c.id, id_externo=l.id_externo, registro_id=r.id))
            vistos.add(l.id_externo)
            nuevas += 1
        c.ultimo_uso = datetime.now(timezone.utc)
        commit(db)
        return {"ok": True, "nuevas": nuevas, "duplicadas": duplicadas, "errores": errores}

    # ── Proyección y anomalías de energía ──
    @router.get("/automatizacion/energia")
    def energia_inteligente(db: Session = Depends(get_db), _=lee):
        hoy = date.today()
        ini = hoy.replace(day=1)
        fin_prev = ini - timedelta(days=1)
        RE = m.RegistroEnergetico
        filas = list(db.scalars(select(RE).where(RE.fecha >= fin_prev.replace(day=1), RE.fecha <= hoy)))
        costo_mes = sum(float(r.costo) for r in filas if r.fecha >= ini)
        costo_prev = sum(float(r.costo) for r in filas if r.fecha < ini)
        dias_mes = calendar.monthrange(hoy.year, hoy.month)[1]
        proy = costo_mes / hoy.day * dias_mes
        # Anomalías robustas: mediana y MAD (no se distorsionan por un pico anterior)
        grupos: dict[tuple, list] = {}
        for r in db.scalars(select(RE).where(RE.fecha >= hoy - timedelta(days=90)).order_by(RE.fecha.desc(), RE.id.desc())):
            grupos.setdefault((r.fuente_nombre, r.tipo_fuente), []).append(r)
        anomalias = []
        for (nombre, tipo), lista in grupos.items():
            ultimo, previos = lista[0], [float(x.cantidad) for x in lista[1:31]]
            if len(previos) < 5:
                continue
            med = statistics.median(previos)
            mad = statistics.median([abs(x - med) for x in previos])
            if mad == 0:
                continue
            z = 0.6745 * (float(ultimo.cantidad) - med) / mad
            if z >= 3.5:
                anomalias.append({"fuente": nombre, "tipo_fuente": tipo, "unidad": ultimo.unidad,
                                  "fecha": ultimo.fecha, "cantidad": float(ultimo.cantidad),
                                  "mediana_previa": round(med, 3), "puntaje": round(z, 1)})
        anomalias.sort(key=lambda a: a["puntaje"], reverse=True)
        return {"ok": True, "data": {
            "costo_mes_actual": round(costo_mes, 2), "costo_mes_anterior": round(costo_prev, 2),
            "proyeccion_mes": round(proy, 2), "dias_transcurridos": hoy.day, "dias_mes": dias_mes,
            "confianza_proyeccion": "baja" if hoy.day < 7 else "normal", "anomalias": anomalias}}

    return router
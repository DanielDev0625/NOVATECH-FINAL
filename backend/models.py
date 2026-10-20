from abc import ABC, abstractmethod
from datetime import date, datetime, timezone
from decimal import Decimal
import secrets

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, ForeignKey, Numeric, String, Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base

def _ahora() -> datetime:
    return datetime.now(timezone.utc)

# Alias para compatibilidad con código existente
def _utc_now() -> datetime:
    return _ahora()

class ReglaNegocioError(Exception):
    pass

class ModeloBase(Base):
    __abstract__ = True
    id: Mapped[int] = mapped_column(primary_key=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_ahora)
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_ahora, onupdate=_ahora)

class Usuario(ModeloBase):
    __tablename__ = "usuarios"
    email: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    nombre: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    rol: Mapped[str] = mapped_column(String(20), default="operador")
    activo: Mapped[bool] = mapped_column(Boolean, default=True)

class Categoria(ModeloBase):
    __tablename__ = "categorias"
    nombre: Mapped[str] = mapped_column(String(80), unique=True)
    productos: Mapped[list["Producto"]] = relationship(back_populates="categoria")

ENTRADA, SALIDA = "ENTRADA", "SALIDA"
AJUSTE_ENTRADA, AJUSTE_SALIDA = "AJUSTE_ENTRADA", "AJUSTE_SALIDA"
TIPOS_QUE_SUMAN = {ENTRADA, AJUSTE_ENTRADA}
TIPOS_QUE_RESTAN = {SALIDA, AJUSTE_SALIDA}
TIPOS_MOVIMIENTO = TIPOS_QUE_SUMAN | TIPOS_QUE_RESTAN

class Producto(ModeloBase):
    __tablename__ = "productos"
    codigo: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    nombre: Mapped[str] = mapped_column(String(160))
    descripcion: Mapped[str | None] = mapped_column(Text)
    categoria_id: Mapped[int | None] = mapped_column(ForeignKey("categorias.id"))
    unidad: Mapped[str] = mapped_column(String(20), default="unidad")
    stock_minimo: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=0)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    _stock: Mapped[Decimal] = mapped_column("stock_actual", Numeric(14, 3), nullable=False)

    categoria: Mapped["Categoria | None"] = relationship(back_populates="productos")
    movimientos: Mapped[list["MovimientoInventario"]] = relationship(back_populates="producto")
    lotes: Mapped[list["Lote"]] = relationship(back_populates="producto")

    __table_args__ = (
        CheckConstraint("stock_actual >= 0", name="ck_producto_stock_no_negativo"),
        CheckConstraint("stock_minimo >= 0", name="ck_producto_minimo_no_negativo"),
    )

    def __init__(self, **kw):
        kw.setdefault("_stock", Decimal("0"))
        super().__init__(**kw)

    @property
    def stock_actual(self) -> Decimal:
        return self._stock

    @property
    def stock_bajo(self) -> bool:
        return self._stock <= (self.stock_minimo or 0)

    def aplicar_movimiento(self, tipo: str, cantidad: Decimal) -> Decimal:
        cantidad = Decimal(cantidad)
        if tipo not in TIPOS_MOVIMIENTO:
            raise ReglaNegocioError(f"Tipo de movimiento inválido: {tipo}")
        if cantidad <= 0:
            raise ReglaNegocioError("La cantidad debe ser mayor que cero")
        if not self.activo:
            raise ReglaNegocioError("El producto está inactivo")
        nuevo = self._stock + cantidad if tipo in TIPOS_QUE_SUMAN else self._stock - cantidad
        if nuevo < 0:
            raise ReglaNegocioError(f"Stock insuficiente: disponible {self._stock}, solicitado {cantidad}")
        self._stock = nuevo
        return nuevo

class MovimientoInventario(ModeloBase):
    __tablename__ = "movimientos_inventario"
    producto_id: Mapped[int] = mapped_column(ForeignKey("productos.id"), index=True)
    lote_id: Mapped[int | None] = mapped_column(ForeignKey("lotes.id"))
    pedido_id: Mapped[int | None] = mapped_column(ForeignKey("pedidos.id"))
    usuario_id: Mapped[int | None] = mapped_column(ForeignKey("usuarios.id"))
    tipo: Mapped[str] = mapped_column(String(20))
    cantidad: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    stock_resultante: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    referencia_operacion: Mapped[str] = mapped_column(String(80), unique=True)
    compensa_a_id: Mapped[int | None] = mapped_column(ForeignKey("movimientos_inventario.id"), unique=True)
    observacion: Mapped[str | None] = mapped_column(Text)
    fecha: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_ahora)

    producto: Mapped["Producto"] = relationship(back_populates="movimientos")

class Lote(ModeloBase):
    __tablename__ = "lotes"
    codigo: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    producto_id: Mapped[int] = mapped_column(ForeignKey("productos.id"), index=True)
    fecha_produccion: Mapped[date] = mapped_column(Date, default=date.today)
    cantidad_producida: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    estado: Mapped[str] = mapped_column(String(20), default="EN_PROCESO")
    qr_token: Mapped[str] = mapped_column(String(64), unique=True, default=lambda: secrets.token_urlsafe(24))

    producto: Mapped["Producto"] = relationship(back_populates="lotes")
    etapas: Mapped[list["EtapaProduccion"]] = relationship(
        back_populates="lote", order_by="EtapaProduccion.fecha", cascade="all, delete-orphan"
    )

    TRANSICIONES = {
        "EN_PROCESO": {"FINALIZADO", "RECHAZADO"},
        "FINALIZADO": set(),
        "RECHAZADO": set(),
    }

    @property
    def control_calidad_fallido(self) -> bool:
        return any(e.calidad_aprobada is False for e in self.etapas)

    def cambiar_estado(self, nuevo: str) -> None:
        if nuevo not in self.TRANSICIONES.get(self.estado, set()):
            raise ReglaNegocioError(f"No se puede pasar de {self.estado} a {nuevo}")
        if nuevo == "FINALIZADO" and self.control_calidad_fallido:
            raise ReglaNegocioError("No se puede finalizar: hay un control de calidad reprobado")
        if nuevo == "FINALIZADO" and any(e.completada_en is None for e in self.etapas):
            raise ReglaNegocioError("No se puede finalizar: hay etapas sin completar")
        self.estado = nuevo

class EtapaProduccion(ModeloBase):
    __tablename__ = "etapas_produccion"
    lote_id: Mapped[int] = mapped_column(ForeignKey("lotes.id"), index=True)
    nombre: Mapped[str] = mapped_column(String(120))
    fecha: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_ahora)
    responsable: Mapped[str] = mapped_column(String(120))
    observaciones: Mapped[str | None] = mapped_column(Text)
    calidad_aprobada: Mapped[bool | None] = mapped_column(Boolean)
    tipo: Mapped[str] = mapped_column(String(20), default="PRODUCCION", server_default="PRODUCCION")
    fecha_limite: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lote: Mapped["Lote"] = relationship(back_populates="etapas")

class Transportista(ModeloBase):
    __tablename__ = "transportistas"
    nombre: Mapped[str] = mapped_column(String(120))
    documento: Mapped[str] = mapped_column(String(30), unique=True)
    telefono: Mapped[str | None] = mapped_column(String(30))
    costo_por_km: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"))
    valoracion: Mapped[Decimal] = mapped_column(Numeric(3, 2), default=Decimal("5.00"))
    activo: Mapped[bool] = mapped_column(Boolean, default=True)

    vehiculos: Mapped[list["Vehiculo"]] = relationship(back_populates="transportista")
    entregas: Mapped[list["Entrega"]] = relationship(back_populates="transportista")

class Vehiculo(ModeloBase):
    __tablename__ = "vehiculos"
    placa: Mapped[str] = mapped_column(String(15), unique=True)
    modelo: Mapped[str] = mapped_column(String(80))
    tipo: Mapped[str] = mapped_column(String(40), default="camión")
    capacidad_kg: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    transportista_id: Mapped[int | None] = mapped_column(ForeignKey("transportistas.id"))
    activo: Mapped[bool] = mapped_column(Boolean, default=True)

    transportista: Mapped["Transportista | None"] = relationship(back_populates="vehiculos")

class Pedido(ModeloBase):
    __tablename__ = "pedidos"
    codigo: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    cliente: Mapped[str] = mapped_column(String(160))
    estado: Mapped[str] = mapped_column(String(20), default="PENDIENTE")
    fecha_pedido: Mapped[date] = mapped_column(Date, default=date.today)
    observaciones: Mapped[str | None] = mapped_column(Text)

    detalles: Mapped[list["DetallePedido"]] = relationship(back_populates="pedido", cascade="all, delete-orphan")
    entregas: Mapped[list["Entrega"]] = relationship(back_populates="pedido")

    TRANSICIONES = {
        "PENDIENTE": {"PREPARANDO", "CANCELADO"},
        "PREPARANDO": {"DESPACHADO", "CANCELADO"},
        "DESPACHADO": {"ENTREGADO"},
        "ENTREGADO": set(),
        "CANCELADO": set(),
    }

    def cambiar_estado(self, nuevo: str) -> None:
        if nuevo not in self.TRANSICIONES.get(self.estado, set()):
            raise ReglaNegocioError(f"No se puede pasar de {self.estado} a {nuevo}")
        self.estado = nuevo

class DetallePedido(ModeloBase):
    __tablename__ = "detalles_pedido"
    pedido_id: Mapped[int] = mapped_column(ForeignKey("pedidos.id"), index=True)
    producto_id: Mapped[int] = mapped_column(ForeignKey("productos.id"))
    lote_id: Mapped[int | None] = mapped_column(ForeignKey("lotes.id"))
    cantidad: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    
    pedido: Mapped["Pedido"] = relationship(back_populates="detalles")
    producto: Mapped["Producto"] = relationship()

class Entrega(ModeloBase):
    __tablename__ = "entregas"
    pedido_id: Mapped[int] = mapped_column(ForeignKey("pedidos.id"), index=True)
    transportista_id: Mapped[int | None] = mapped_column(ForeignKey("transportistas.id"))
    vehiculo_id: Mapped[int | None] = mapped_column(ForeignKey("vehiculos.id"))
    origen: Mapped[str | None] = mapped_column(String(120))
    destino: Mapped[str | None] = mapped_column(String(120))
    distancia_km: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"))
    tarifa: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    comision: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    estado: Mapped[str] = mapped_column(String(20), default="PROGRAMADA")
    fecha_despacho: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fecha_entrega: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observaciones: Mapped[str | None] = mapped_column(Text)

    pedido: Mapped["Pedido"] = relationship(back_populates="entregas")
    transportista: Mapped["Transportista | None"] = relationship(back_populates="entregas")
    vehiculo: Mapped["Vehiculo | None"] = relationship()

    TRANSICIONES = {
        "PROGRAMADA": {"EN_RUTA"},
        "EN_RUTA": {"ENTREGADA"},
        "ENTREGADA": set(),
    }

    def cambiar_estado(self, nuevo: str) -> None:
        if nuevo not in self.TRANSICIONES.get(self.estado, set()):
            raise ReglaNegocioError(f"No se puede pasar de {self.estado} a {nuevo}")
        self.estado = nuevo

class FuenteEmision(ABC):
    codigo: str
    unidad: str

    @abstractmethod
    def validar_cantidad(self, cantidad: Decimal) -> None: ...

    @abstractmethod
    def calcular_emisiones_kg(self, cantidad: Decimal, factor: Decimal | None) -> Decimal | None: ...

class ElectricidadRed(FuenteEmision):
    codigo, unidad = "ELECTRICIDAD", "kWh"
    def validar_cantidad(self, cantidad):
        if cantidad <= 0:
            raise ReglaNegocioError("El consumo eléctrico debe ser mayor que 0 kWh")
    def calcular_emisiones_kg(self, cantidad, factor):
        return None if factor is None else cantidad * factor

class Diesel(FuenteEmision):
    codigo, unidad = "DIESEL", "litros"
    def validar_cantidad(self, cantidad):
        if cantidad <= 0:
            raise ReglaNegocioError("Los litros de diésel deben ser mayores que 0")
    def calcular_emisiones_kg(self, cantidad, factor):
        return None if factor is None else cantidad * factor

class Gasolina(Diesel):
    codigo, unidad = "GASOLINA", "litros"

FUENTES: dict[str, FuenteEmision] = {f.codigo: f for f in (ElectricidadRed(), Diesel(), Gasolina())}

class RegistroEnergetico(ModeloBase):
    __tablename__ = "registros_energeticos"
    fuente_nombre: Mapped[str] = mapped_column(String(120), index=True)
    tipo_fuente: Mapped[str] = mapped_column(String(20), default="ELECTRICIDAD")
    fecha: Mapped[date] = mapped_column(Date, index=True)
    cantidad: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    tarifa_unitaria: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    factor_emision: Mapped[Decimal | None] = mapped_column(Numeric(10, 5))

    @property
    def fuente(self) -> FuenteEmision:
        try:
            return FUENTES[self.tipo_fuente]
        except KeyError:
            raise ReglaNegocioError(f"Fuente desconocida: {self.tipo_fuente}")

    @property
    def costo(self) -> Decimal:
        return self.cantidad * self.tarifa_unitaria
    
    @property
    def unidad(self) -> str:
        return self.fuente.unidad

    @property
    def emisiones_kg(self) -> Decimal | None:
        return self.fuente.calcular_emisiones_kg(self.cantidad, self.factor_emision)
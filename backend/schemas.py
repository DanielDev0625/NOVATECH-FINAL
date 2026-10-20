from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

Tipo = Literal["ENTRADA", "SALIDA", "AJUSTE_ENTRADA", "AJUSTE_SALIDA"]

class Salida(BaseModel):
    model_config = ConfigDict(from_attributes=True)

class LoginIn(BaseModel):
    email: str = Field(min_length=5, max_length=160)
    password: str = Field(min_length=1, max_length=72)

class UsuarioCrear(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=160)
    nombre: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=8, max_length=72)
    rol: Literal["admin", "operador", "consulta"] = "operador"

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        return v.strip().lower()

class UsuarioOut(Salida):
    id: int
    email: str
    nombre: str
    rol: str
    activo: bool

class CategoriaIn(BaseModel):
    nombre: str = Field(min_length=2, max_length=80)

class CategoriaOut(Salida):
    id: int
    nombre: str

class ProductoIn(BaseModel):
    codigo: str = Field(min_length=1, max_length=40)
    nombre: str = Field(min_length=2, max_length=160)
    descripcion: str | None = None
    categoria_id: int | None = None
    unidad: str = Field(default="unidad", max_length=20)
    stock_minimo: Decimal = Field(default=Decimal(0), ge=0)
    stock_inicial: Decimal = Field(default=Decimal(0), ge=0)

    @field_validator("codigo")
    @classmethod
    def _codigo(cls, v):
        return v.strip().upper()

class ProductoEditar(BaseModel):
    nombre: str | None = Field(default=None, min_length=2, max_length=160)
    descripcion: str | None = None
    categoria_id: int | None = None
    unidad: str | None = Field(default=None, max_length=20)
    stock_minimo: Decimal | None = Field(default=None, ge=0)

class ProductoOut(Salida):
    id: int
    codigo: str
    nombre: str
    descripcion: str | None
    categoria_id: int | None
    unidad: str
    stock_minimo: float
    stock_actual: float
    stock_bajo: bool
    activo: bool

class MovimientoIn(BaseModel):
    producto_id: int
    tipo: Tipo
    cantidad: Decimal = Field(gt=0)
    referencia_operacion: str = Field(min_length=8, max_length=80)
    lote_id: int | None = None
    pedido_id: int | None = None
    observacion: str | None = None

class MovimientoOut(Salida):
    id: int
    producto_id: int
    tipo: str
    cantidad: float
    stock_resultante: float
    referencia_operacion: str
    usuario_id: int | None
    compensa_a_id: int | None
    observacion: str | None
    fecha: datetime

class LoteIn(BaseModel):
    codigo: str = Field(min_length=1, max_length=40)
    producto_id: int
    cantidad_producida: Decimal = Field(gt=0)
    fecha_produccion: date | None = None

    @field_validator("codigo")
    @classmethod
    def _codigo(cls, v):
        return v.strip().upper()

class EtapaIn(BaseModel):
    nombre: str = Field(min_length=2, max_length=120)
    responsable: str = Field(min_length=2, max_length=120)
    observaciones: str | None = None
    calidad_aprobada: bool | None = None
    fecha: datetime | None = None

class EtapaOut(Salida):
    id: int
    nombre: str
    fecha: datetime
    responsable: str
    observaciones: str | None
    calidad_aprobada: bool | None

class LoteOut(Salida):
    id: int
    codigo: str
    producto_id: int
    fecha_produccion: date
    cantidad_producida: float
    estado: str
    qr_token: str
    etapas: list[EtapaOut]

class LoteEstadoIn(BaseModel):
    estado: Literal["FINALIZADO", "RECHAZADO"]

class TransportistaIn(BaseModel):
    nombre: str = Field(min_length=2, max_length=120)
    documento: str = Field(min_length=3, max_length=30)
    telefono: str | None = Field(default=None, max_length=30)
    costo_por_km: Decimal = Field(default=Decimal("0.00"), ge=0)
    valoracion: Decimal = Field(default=Decimal("5.00"), ge=1, le=5)

class TransportistaOut(Salida):
    id: int
    nombre: str
    documento: str
    telefono: str | None
    costo_por_km: float
    valoracion: float
    activo: bool

class VehiculoIn(BaseModel):
    placa: str = Field(min_length=3, max_length=15)
    modelo: str = Field(min_length=1, max_length=80)
    tipo: str = Field(default="camión", max_length=40)
    capacidad_kg: Decimal | None = Field(default=None, gt=0)
    transportista_id: int | None = None

    @field_validator("placa")
    @classmethod
    def _placa(cls, v):
        return v.strip().upper()

class VehiculoOut(Salida):
    id: int
    placa: str
    modelo: str
    tipo: str
    capacidad_kg: float | None
    transportista_id: int | None

class DetalleIn(BaseModel):
    producto_id: int
    cantidad: Decimal = Field(gt=0)
    lote_id: int | None = None

class DetalleOut(Salida):
    id: int
    producto_id: int
    lote_id: int | None
    cantidad: float

class PedidoIn(BaseModel):
    cliente: str = Field(min_length=2, max_length=160)
    observaciones: str | None = None
    detalles: list[DetalleIn] = Field(min_length=1)

class PedidoOut(Salida):
    id: int
    codigo: str
    cliente: str
    estado: str
    fecha_pedido: date
    observaciones: str | None
    detalles: list[DetalleOut]

class PedidoEstadoIn(BaseModel):
    estado: Literal["PREPARANDO", "DESPACHADO", "ENTREGADO", "CANCELADO"]

class EntregaIn(BaseModel):
    pedido_id: int
    transportista_id: int
    vehiculo_id: int
    origen: str | None = Field(default=None, max_length=120)
    destino: str = Field(min_length=2, max_length=120)
    distancia_km: Decimal = Field(default=Decimal("15.00"), gt=0)
    observaciones: str | None = None

class EntregaEstadoIn(BaseModel):
    estado: Literal["PROGRAMADA", "EN_RUTA", "ENTREGADA"]

class EntregaOut(Salida):
    id: int
    pedido_id: int
    transportista_id: int | None
    vehiculo_id: int | None
    origen: str | None
    destino: str | None
    distancia_km: float
    tarifa: float
    comision: float
    estado: str
    fecha_despacho: datetime | None
    fecha_entrega: datetime | None
    observaciones: str | None

TipoFuente = Literal["ELECTRICIDAD", "DIESEL", "GASOLINA"]

class RegistroEnergiaIn(BaseModel):
    fuente_nombre: str = Field(min_length=2, max_length=120)
    tipo_fuente: TipoFuente = "ELECTRICIDAD"
    fecha: date
    cantidad: Decimal = Field(gt=0)
    tarifa_unitaria: Decimal = Field(ge=0)
    factor_emision: Decimal | None = Field(default=None, ge=0)

class RegistroEnergiaEditar(BaseModel):
    fuente_nombre: str | None = Field(default=None, min_length=2, max_length=120)
    tipo_fuente: TipoFuente | None = None
    fecha: date | None = None
    cantidad: Decimal | None = Field(default=None, gt=0)
    tarifa_unitaria: Decimal | None = Field(default=None, ge=0)
    factor_emision: Decimal | None = Field(default=None, ge=0)

class RegistroEnergiaOut(Salida):
    id: int
    fuente_nombre: str
    tipo_fuente: str
    unidad: str
    fecha: date
    cantidad: float
    tarifa_unitaria: float
    factor_emision: float | None
    costo: float
    emisiones_kg: float | None
let token = sessionStorage.getItem("nt"), me = null, est = {}, vista = "panel", primer = false, D = {};
const $ = s => document.querySelector(s);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const n = (v, d = 2) => Number(v).toLocaleString("es-CO", {maximumFractionDigits: d});
const fd = v => !v ? "—" : v.length === 10 ? v.split("-").reverse().join("/") : new Date(v).toLocaleString("es-CO");
const lim = f => Object.fromEntries(Object.entries(f).filter(([, v]) => v !== ""));
const hoy = () => new Date().toLocaleDateString("sv");
const BC = {FINALIZADO:"ok", ENTREGADO:"ok", ENTREGADA:"ok", RECHAZADO:"er", CANCELADO:"er", FALLIDA:"er"};
const badge = t => `<span class="bd ${BC[t] || "wn"}">${esc(t)}</span>`;
const bt = (a, id, t, c = "") => `<button class="btn sm ${c}" data-a="${a}" data-id="${id}">${t}</button>`;
const st = (t, v, c = "") => `<div class="stat ${c}"><span>${t}</span><b>${v}</b></div>`;
const puede = () => me && me.rol !== "consulta", admin = () => me && me.rol === "admin";
const buscar = ph => `<input data-f="q" placeholder="${ph}" value="${esc(est.q || "")}">`;
const filtro = (k, ops, todos) => `<select data-f="${k}">${["", ...ops].map(x => `<option value="${x}" ${(est[k] || "") === x ? "selected" : ""}>${x || todos}</option>`).join("")}</select>`;
const tabla = (cols, filas, acc) => filas.length
  ? `<div class="tw"><table><thead><tr>${cols.map(c => `<th>${c[0]}</th>`).join("")}${acc ? "<th></th>" : ""}</tr></thead><tbody>${filas.map(r => `<tr>${cols.map(c => `<td>${c[1](r)}</td>`).join("")}${acc ? `<td class="ac">${acc(r)}</td>` : ""}</tr>`).join("")}</tbody></table></div>`
  : `<p class="vacio">No hay registros.</p>`;

function toast(t, k = "ok") { const e = document.createElement("div"); e.className = "toast " + k; e.textContent = t; $("#toasts").append(e); setTimeout(() => e.remove(), 4000); }
const cerrar = () => $("#modal").classList.add("hidden");
function panel(t, h) { $("#modal").innerHTML = `<div class="card wide"><h3>${esc(t)}</h3>${h}<div class="row"><button class="btn ghost" id="cx">Cerrar</button></div></div>`; $("#modal").classList.remove("hidden"); $("#cx").onclick = cerrar; }
function form(titulo, campos, enviar) {
  const m = $("#modal");
  m.innerHTML = `<form class="card"><h3>${esc(titulo)}</h3>${campos.map(c => `<label>${esc(c.l)}${c.o
    ? `<select name="${c.n}">${c.req ? "" : "<option value=''>—</option>"}${c.o.map(([v, t]) => `<option value="${esc(v)}" ${String(v) === String(c.v) ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>`
    : `<input name="${c.n}" type="${c.t || "text"}" step="any" value="${esc(c.v ?? "")}" ${c.req ? "required" : ""}>`}</label>`).join("")}<div class="row"><button type="button" class="btn ghost" id="cx">Cancelar</button><button class="btn">Guardar</button></div></form>`;
  m.classList.remove("hidden");
  $("#cx").onclick = cerrar;
  m.firstChild.onsubmit = async e => {
    e.preventDefault();
    const b = e.target.querySelector(".btn:not(.ghost)"); b.disabled = true;
    try { await enviar(Object.fromEntries(new FormData(e.target))); cerrar(); }
    catch (x) { toast(x.message, "err"); b.disabled = false; }
  };
}

async function api(p, o = {}) {
  let r;
  try { r = await fetch("/api" + p, {method: o.m || "GET", headers: {"Content-Type": "application/json", ...(token ? {Authorization: "Bearer " + token} : {})}, body: o.b ? JSON.stringify(o.b) : undefined}); }
  catch { throw new Error("Sin conexión con el servidor"); }
  let d = null; try { d = await r.json(); } catch {}
  if (r.status === 401 && token) { salir(); throw new Error(d?.error || "Sesión vencida"); }
  if (!r.ok) throw new Error(d?.error || "Error " + r.status);
  return d;
}
const run = async (fn, ok) => { try { await fn(); toast(ok); go(); } catch (x) { toast(x.message, "err"); } };

// ───── Sesión ─────
function pintaLogin() {
  $("#login").innerHTML = `<form class="card"><h2>NOVATECH</h2><p class="mut">Gestión inteligente para PyMEs</p>${primer ? '<label>Nombre<input name="nombre" required minlength="2"></label>' : ""}<label>Correo<input name="email" type="email" required></label><label>Contraseña<input name="password" type="password" required minlength="${primer ? 8 : 1}"></label><button class="btn">${primer ? "Crear administrador" : "Entrar"}</button><a href="#" id="alt">${primer ? "Ya tengo cuenta" : "Crear primer administrador"}</a></form>`;
  $("#alt").onclick = e => { e.preventDefault(); primer = !primer; pintaLogin(); };
  $("#login form").onsubmit = async e => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target)), b = e.target.querySelector("button"); b.disabled = true;
    try {
      if (primer) await api("/auth/primer-admin", {m: "POST", b: f});
      const r = await api("/auth/login", {m: "POST", b: {email: f.email, password: f.password}});
      token = r.token; sessionStorage.setItem("nt", token); primer = false; await iniciar();
    } catch (x) { toast(x.message, "err"); b.disabled = false; }
  };
}
function salir() { token = null; me = null; sessionStorage.removeItem("nt"); $("#app").classList.add("hidden"); $("#login").classList.remove("hidden"); pintaLogin(); }
const NAV = [["panel", "Panel"], ["inv", "Inventario"], ["tra", "Trazabilidad"], ["log", "Logística"], ["ene", "Energía"]];
async function iniciar() {
  me = (await api("/auth/yo")).data;
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
  $("#user").textContent = `${me.nombre} · ${me.rol}`;
  $("#nav").innerHTML = NAV.map(([k, t]) => `<button data-v="${k}">${t}</button>`).join("");
  est = {}; go("panel");
}
const VISTAS = {panel: vPanel, inv: vInv, tra: vTra, log: vLog, ene: vEne};
async function go(v) {
  if (v) vista = v;
  document.querySelectorAll("#nav button").forEach(b => b.classList.toggle("on", b.dataset.v === vista));
  $("#titulo").textContent = NAV.find(x => x[0] === vista)[1];
  $("#side").classList.remove("open");
  $("#vista").innerHTML = '<p class="vacio">Cargando…</p>';
  try { await VISTAS[vista](); } catch (e) { $("#vista").innerHTML = `<p class="vacio err">${esc(e.message)}</p>`; }
}

// ───── Panel ─────
async function vPanel() {
  const [P, L, O, R, A] = await Promise.all([api("/productos"), api("/lotes"), api("/pedidos"), api("/energia/resumen"), api("/energia/alertas")]);
  const bajos = P.data.filter(p => p.stock_bajo), r = R.data;
  $("#vista").innerHTML = `<div class="stats">${st("Productos", P.data.length)}${st("Stock bajo", bajos.length, bajos.length ? "wn" : "ok")}${st("Lotes en proceso", L.data.filter(l => l.estado === "EN_PROCESO").length)}${st("Pedidos abiertos", O.data.filter(o => ["PENDIENTE", "PREPARANDO", "DESPACHADO"].includes(o.estado)).length)}${st("Costo energía (30 d)", "$" + n(r.costo_total))}${st("CO₂ estimado (kg)", n(r.emisiones_kg))}</div>
  ${r.registros_sin_factor ? `<p class="mut">${r.registros_sin_factor} registro(s) sin factor de emisión no cuentan en el CO₂.</p>` : ""}
  <h3>Productos con stock bajo</h3>${tabla([["Código", p => esc(p.codigo)], ["Producto", p => esc(p.nombre)], ["Stock", p => n(p.stock_actual)], ["Mínimo", p => n(p.stock_minimo)]], bajos)}
  <h3>Alertas de consumo energético</h3>${tabla([["Fuente", a => esc(a.fuente)], ["Fecha", a => fd(a.fecha)], ["Consumo", a => `${n(a.cantidad)} ${esc(a.unidad)}`], ["Promedio previo", a => n(a.promedio_previo)], ["Exceso", a => `+${a.exceso_pct}%`], ["Nivel", a => `<span class="bd ${a.nivel === "critica" ? "er" : "wn"}">${esc(a.nivel)}</span>`]], A.data)}`;
}

// ───── Inventario ─────
async function vInv() {
  const qs = new URLSearchParams(); if (est.q) qs.set("q", est.q); if (est.bajo) qs.set("stock_bajo", "true");
  const [P, C] = await Promise.all([api("/productos?" + qs), api("/categorias")]);
  D.prods = P.data; D.cats = C.data;
  $("#vista").innerHTML = `<div class="bar">${buscar("Buscar código o nombre")}<label class="chk"><input type="checkbox" data-f="bajo" ${est.bajo ? "checked" : ""}> Solo stock bajo</label><span class="sp"></span>${puede() ? bt("ncat", "", "+ Categoría", "ghost") + bt("nprod", "", "+ Producto") + bt("mov", "", "Movimiento") : ""}</div>` +
    tabla([["Código", p => esc(p.codigo)], ["Producto", p => esc(p.nombre)], ["Categoría", p => esc(D.cats.find(c => c.id === p.categoria_id)?.nombre || "—")], ["Stock", p => `<b class="${p.stock_bajo ? "err" : ""}">${n(p.stock_actual)}</b> ${esc(p.unidad)}`], ["Mínimo", p => n(p.stock_minimo)]], D.prods,
      p => bt("hist", p.id, "Historial", "ghost") + (puede() ? bt("mov", p.id, "Movimiento") : "") + (admin() ? bt("desact", p.id, "Desactivar", "er") : ""));
}

// ───── Trazabilidad ─────
async function vTra() {
  const qs = new URLSearchParams(); if (est.q) qs.set("q", est.q); if (est.estado) qs.set("estado", est.estado);
  const [L, P] = await Promise.all([api("/lotes?" + qs), api("/productos")]);
  D.lotes = L.data; D.prods = P.data;
  const pn = id => D.prods.find(p => p.id === id)?.nombre || id;
  $("#vista").innerHTML = `<div class="bar">${buscar("Buscar lote")}${filtro("estado", ["EN_PROCESO", "FINALIZADO", "RECHAZADO"], "Todos los estados")}<span class="sp"></span>${puede() ? bt("nlote", "", "+ Lote") : ""}</div>` +
    tabla([["Lote", l => esc(l.codigo)], ["Producto", l => esc(pn(l.producto_id))], ["Fecha", l => fd(l.fecha_produccion)], ["Cantidad", l => n(l.cantidad_producida)], ["Etapas", l => l.etapas.length], ["Estado", l => badge(l.estado)]], D.lotes,
      l => bt("ver", l.id, "Ver", "ghost") + (puede() && l.estado === "EN_PROCESO" ? bt("etapa", l.id, "+ Etapa") + bt("fin", l.id, "Finalizar") + bt("rech", l.id, "Rechazar", "er") : ""));
}
async function cambioLote(id, e) {
  const l = D.lotes.find(x => x.id == id);
  if (!confirm(e === "FINALIZADO" ? `¿Finalizar el lote ${l.codigo}? Ingresarán ${n(l.cantidad_producida)} unidades al inventario.` : `¿Rechazar el lote ${l.codigo}?`)) return;
  await run(() => api(`/lotes/${id}/estado`, {m: "POST", b: {estado: e}}), "Lote actualizado");
}

// ───── Logística ─────
async function vLog() {
  est.sub = est.sub || "ped";
  const tabs = `<div class="tabs">${[["ped", "Pedidos"], ["ent", "Entregas"], ["flo", "Flota"]].map(([k, t]) => `<button class="tab ${est.sub === k ? "on" : ""}" data-a="sub" data-id="${k}">${t}</button>`).join("")}</div>`;
  let h = "";
  if (est.sub === "ped") {
    const qs = new URLSearchParams(); if (est.q) qs.set("q", est.q); if (est.estado) qs.set("estado", est.estado);
    const [O, P] = await Promise.all([api("/pedidos?" + qs), api("/productos")]);
    D.peds = O.data; D.prods = P.data;
    const pn = id => D.prods.find(p => p.id === id)?.nombre || id;
    h = `<div class="bar">${buscar("Buscar código o cliente")}${filtro("estado", ["PENDIENTE", "PREPARANDO", "DESPACHADO", "ENTREGADO", "CANCELADO"], "Todos los estados")}<span class="sp"></span>${puede() ? bt("nped", "", "+ Pedido") : ""}</div>` +
      tabla([["Código", o => esc(o.codigo)], ["Cliente", o => esc(o.cliente)], ["Fecha", o => fd(o.fecha_pedido)], ["Ítems", o => o.detalles.map(d => `${esc(pn(d.producto_id))} × ${n(d.cantidad)}`).join("<br>")], ["Estado", o => badge(o.estado)]], D.peds,
        o => !puede() ? "" : o.estado === "PENDIENTE" ? bt("prep", o.id, "Preparar") + bt("canc", o.id, "Cancelar", "er") : o.estado === "PREPARANDO" ? bt("nent", o.id, "Crear entrega") + bt("canc", o.id, "Cancelar", "er") : "");
  } else if (est.sub === "ent") {
    const [E, O, V] = await Promise.all([api("/entregas"), api("/pedidos"), api("/vehiculos")]);
    h = tabla([["Pedido", e => esc(O.data.find(o => o.id === e.pedido_id)?.codigo || e.pedido_id)], ["Vehículo", e => esc(V.data.find(v => v.id === e.vehiculo_id)?.placa || "—")], ["Ruta", e => `${esc(e.origen || "—")} → ${esc(e.destino || "—")}`], ["Despacho", e => fd(e.fecha_despacho)], ["Entrega", e => fd(e.fecha_entrega)], ["Estado", e => badge(e.estado)]], E.data,
      e => !puede() ? "" : e.estado === "PROGRAMADA" ? bt("desp", e.id, "Despachar") : e.estado === "EN_RUTA" ? bt("entr", e.id, "Confirmar entrega") : "");
  } else {
    const [T, V] = await Promise.all([api("/transportistas"), api("/vehiculos")]);
    D.trans = T.data; D.veh = V.data;
    h = `<div class="bar"><h3 style="margin:0">Transportistas</h3><span class="sp"></span>${puede() ? bt("ntrans", "", "+ Transportista") : ""}</div>` +
      tabla([["Nombre", t => esc(t.nombre)], ["Documento", t => esc(t.documento)], ["Teléfono", t => esc(t.telefono || "—")]], D.trans, admin() ? t => bt("dtrans", t.id, "Desactivar", "er") : null) +
      `<div class="bar" style="margin-top:22px"><h3 style="margin:0">Vehículos</h3><span class="sp"></span>${puede() ? bt("nveh", "", "+ Vehículo") : ""}</div>` +
      tabla([["Placa", v => esc(v.placa)], ["Modelo", v => esc(v.modelo)], ["Tipo", v => esc(v.tipo)], ["Capacidad (kg)", v => v.capacidad_kg ? n(v.capacidad_kg) : "—"], ["Transportista", v => esc(D.trans.find(t => t.id === v.transportista_id)?.nombre || "—")]], D.veh);
  }
  $("#vista").innerHTML = tabs + h;
}

// ───── Energía ─────
async function vEne() {
  const qs = new URLSearchParams(lim({desde: est.desde || "", hasta: est.hasta || ""}));
  const [R, C, T, A, G] = await Promise.all([api("/energia/resumen?" + qs), api("/energia/comparacion?" + qs), api("/energia/tendencia"), api("/energia/alertas"), api("/energia/registros?" + qs)]);
  const r = R.data, v = C.data.variacion_costo_pct, mx = Math.max(...T.data.map(p => p.costo), 1);
  $("#vista").innerHTML = `<div class="bar"><label class="chk">Desde <input type="date" data-f="desde" value="${R.desde}"></label><label class="chk">Hasta <input type="date" data-f="hasta" value="${R.hasta}"></label><span class="sp"></span>${puede() ? bt("nreg", "", "+ Registro") : ""}</div>
  <div class="stats">${st("Costo del periodo", "$" + n(r.costo_total))}${st("CO₂ estimado (kg)", n(r.emisiones_kg))}${st("Registros", r.registros)}${st("Costo vs periodo anterior", v === null ? "Sin datos previos" : (v > 0 ? "+" : "") + v + " %", v === null ? "" : v > 0 ? "wn" : "ok")}</div>
  ${r.registros_sin_factor ? `<p class="mut">${r.registros_sin_factor} registro(s) sin factor de emisión no cuentan en el CO₂.</p>` : ""}
  <h3>Consumo por tipo de fuente</h3>${tabla([["Fuente", x => esc(x.k)], ["Cantidad", x => `${n(x.cantidad)} ${esc(x.unidad)}`], ["Costo", x => "$" + n(x.costo)], ["CO₂ (kg)", x => n(x.emisiones_kg)]], Object.entries(r.por_tipo).map(([k, x]) => ({k, ...x})))}
  <h3>Costo mensual (últimos 6 meses)</h3><div class="bars">${T.data.map(p => `<div class="bar-i" title="$${n(p.costo)}"><i style="height:${p.costo / mx * 100}%"></i><small>${p.mes.slice(5)}</small></div>`).join("")}</div>
  <h3>Alertas</h3>${tabla([["Fuente", a => esc(a.fuente)], ["Fecha", a => fd(a.fecha)], ["Consumo", a => `${n(a.cantidad)} ${esc(a.unidad)}`], ["Exceso", a => `+${a.exceso_pct}%`], ["Nivel", a => `<span class="bd ${a.nivel === "critica" ? "er" : "wn"}">${esc(a.nivel)}</span>`]], A.data)}
  <h3>Registros</h3>${tabla([["Fecha", g => fd(g.fecha)], ["Fuente", g => esc(g.fuente_nombre)], ["Tipo", g => esc(g.tipo_fuente)], ["Cantidad", g => `${n(g.cantidad)} ${esc(g.unidad)}`], ["Costo", g => "$" + n(g.costo)], ["CO₂ (kg)", g => g.emisiones_kg === null ? "—" : n(g.emisiones_kg)]], G.data, admin() ? g => bt("delreg", g.id, "Eliminar", "er") : null)}`;
}

// ───── Acciones ─────
const A = {
  sub: id => { est.sub = id; est.q = ""; est.estado = ""; go(); },
  ncat: () => form("Nueva categoría", [{n: "nombre", l: "Nombre", req: 1}], async f => { await api("/categorias", {m: "POST", b: f}); toast("Categoría creada"); go(); }),
  nprod: () => form("Nuevo producto", [{n: "codigo", l: "Código", req: 1}, {n: "nombre", l: "Nombre", req: 1}, {n: "categoria_id", l: "Categoría", o: D.cats.map(c => [c.id, c.nombre])}, {n: "unidad", l: "Unidad", v: "unidad"}, {n: "stock_minimo", l: "Stock mínimo", t: "number", v: 0}, {n: "stock_inicial", l: "Existencia inicial", t: "number", v: 0}],
    async f => { await api("/productos", {m: "POST", b: lim(f)}); toast("Producto creado"); go(); }),
  desact: id => confirm("¿Desactivar este producto? El historial se conserva.") && run(() => api("/productos/" + id, {m: "DELETE"}), "Producto desactivado"),
  mov: id => { const ref = crypto.randomUUID(); form("Movimiento de inventario", [
    {n: "producto_id", l: "Producto", req: 1, v: id, o: D.prods.map(p => [p.id, `${p.codigo} — ${p.nombre}`])},
    {n: "tipo", l: "Tipo", req: 1, o: [["ENTRADA", "Entrada"], ["SALIDA", "Salida"], ...(admin() ? [["AJUSTE_ENTRADA", "Ajuste (+)"], ["AJUSTE_SALIDA", "Ajuste (−)"]] : [])]},
    {n: "cantidad", l: "Cantidad", t: "number", req: 1}, {n: "observacion", l: "Observación"}],
    async f => { const r = await api("/inventario/movimientos", {m: "POST", b: lim({...f, referencia_operacion: ref})}); toast(r.duplicado ? "Operación ya registrada" : "Movimiento registrado. Stock: " + n(r.stock_actual)); go(); }); },
  hist: async id => { const r = await api("/inventario/movimientos?producto_id=" + id); panel("Historial de movimientos", tabla([["Fecha", m => fd(m.fecha)], ["Tipo", m => esc(m.tipo)], ["Cantidad", m => n(m.cantidad)], ["Stock", m => n(m.stock_resultante)], ["Obs.", m => esc(m.observacion || "")]], r.data, admin() ? m => m.compensa_a_id ? "" : bt("anular", m.id, "Anular", "er") : null)); },
  anular: async id => { if (!confirm("¿Anular este movimiento? Se creará un movimiento compensatorio.")) return; try { await api(`/inventario/movimientos/${id}/anular`, {m: "POST"}); toast("Movimiento anulado"); cerrar(); go(); } catch (x) { toast(x.message, "err"); } },
  nlote: () => form("Nuevo lote", [{n: "codigo", l: "Código del lote", req: 1}, {n: "producto_id", l: "Producto", req: 1, o: D.prods.map(p => [p.id, `${p.codigo} — ${p.nombre}`])}, {n: "cantidad_producida", l: "Cantidad producida", t: "number", req: 1}, {n: "fecha_produccion", l: "Fecha", t: "date", v: hoy()}],
    async f => { await api("/lotes", {m: "POST", b: lim(f)}); toast("Lote creado"); go(); }),
  etapa: id => form("Nueva etapa", [{n: "nombre", l: "Etapa", req: 1}, {n: "responsable", l: "Responsable", req: 1}, {n: "observaciones", l: "Observaciones"}, {n: "calidad_aprobada", l: "Control de calidad", o: [["true", "Aprobado"], ["false", "Reprobado"]]}],
    async f => { await api(`/lotes/${id}/etapas`, {m: "POST", b: lim(f)}); toast("Etapa registrada"); go(); }),
  fin: id => cambioLote(id, "FINALIZADO"),
  rech: id => cambioLote(id, "RECHAZADO"),
  ver: id => { const l = D.lotes.find(x => x.id == id); panel("Lote " + l.codigo, `<p class="mut">Consulta pública: <a href="/api/trazabilidad/${esc(l.qr_token)}" target="_blank">abrir enlace de trazabilidad</a></p>` + tabla([["Etapa", e => esc(e.nombre)], ["Fecha", e => fd(e.fecha)], ["Responsable", e => esc(e.responsable)], ["Calidad", e => e.calidad_aprobada === null ? "—" : e.calidad_aprobada ? "✔ Aprobada" : "✘ Reprobada"], ["Obs.", e => esc(e.observaciones || "")]], l.etapas)); },
  nped: () => form("Nuevo pedido", [{n: "cliente", l: "Cliente", req: 1}, {n: "producto_id", l: "Producto", req: 1, o: D.prods.map(p => [p.id, `${p.codigo} — ${p.nombre}`])}, {n: "cantidad", l: "Cantidad", t: "number", req: 1}, {n: "observaciones", l: "Observaciones"}],
    async f => { await api("/pedidos", {m: "POST", b: lim({cliente: f.cliente, observaciones: f.observaciones, detalles: [{producto_id: +f.producto_id, cantidad: f.cantidad}]})}); toast("Pedido creado"); go(); }),
  prep: id => run(() => api(`/pedidos/${id}/estado`, {m: "POST", b: {estado: "PREPARANDO"}}), "Pedido en preparación"),
  canc: id => confirm("¿Cancelar este pedido?") && run(() => api(`/pedidos/${id}/estado`, {m: "POST", b: {estado: "CANCELADO"}}), "Pedido cancelado"),
  nent: async id => { const V = (await api("/vehiculos")).data.filter(v => v.transportista_id); if (!V.length) return toast("Primero registra un transportista con vehículo", "err");
    form("Crear entrega", [{n: "vehiculo_id", l: "Vehículo", req: 1, o: V.map(v => [v.id, `${v.placa} — ${v.modelo}`])}, {n: "origen", l: "Origen"}, {n: "destino", l: "Destino", req: 1}],
      async f => { const v = V.find(x => x.id == f.vehiculo_id); await api("/entregas", {m: "POST", b: lim({pedido_id: +id, transportista_id: v.transportista_id, vehiculo_id: v.id, origen: f.origen, destino: f.destino})}); toast("Entrega programada"); est.sub = "ent"; go(); }); },
  desp: id => confirm("¿Despachar? Se descontará el stock del inventario.") && run(() => api(`/entregas/${id}/despachar`, {m: "POST"}), "Entrega despachada"),
  entr: id => run(() => api(`/entregas/${id}/entregar`, {m: "POST"}), "Entrega confirmada"),
  ntrans: () => form("Nuevo transportista", [{n: "nombre", l: "Nombre", req: 1}, {n: "documento", l: "Documento", req: 1}, {n: "telefono", l: "Teléfono"}], async f => { await api("/transportistas", {m: "POST", b: lim(f)}); toast("Transportista creado"); go(); }),
  dtrans: id => confirm("¿Desactivar transportista?") && run(() => api("/transportistas/" + id, {m: "DELETE"}), "Transportista desactivado"),
  nveh: () => form("Nuevo vehículo", [{n: "placa", l: "Placa", req: 1}, {n: "modelo", l: "Modelo", req: 1}, {n: "tipo", l: "Tipo", v: "camión"}, {n: "capacidad_kg", l: "Capacidad (kg)", t: "number"}, {n: "transportista_id", l: "Transportista", o: D.trans.map(t => [t.id, t.nombre])}],
    async f => { await api("/vehiculos", {m: "POST", b: lim(f)}); toast("Vehículo creado"); go(); }),
  nreg: () => form("Registro energético", [{n: "fuente_nombre", l: "Máquina o proceso", req: 1}, {n: "tipo_fuente", l: "Fuente", req: 1, o: [["ELECTRICIDAD", "Electricidad (kWh)"], ["DIESEL", "Diésel (litros)"], ["GASOLINA", "Gasolina (litros)"]]}, {n: "fecha", l: "Fecha", t: "date", req: 1, v: hoy()}, {n: "cantidad", l: "Cantidad", t: "number", req: 1}, {n: "tarifa_unitaria", l: "Tarifa por unidad", t: "number", req: 1}, {n: "factor_emision", l: "Factor de emisión (kgCO₂/unidad, opcional)", t: "number"}],
    async f => { await api("/energia/registros", {m: "POST", b: lim(f)}); toast("Registro guardado"); go(); }),
  delreg: id => confirm("¿Eliminar este registro?") && run(() => api("/energia/registros/" + id, {m: "DELETE"}), "Registro eliminado"),
};

// ───── Eventos globales ─────
document.addEventListener("click", e => {
  const a = e.target.closest("[data-a]");
  if (a) { Promise.resolve(A[a.dataset.a]?.(a.dataset.id)).catch(x => toast(x.message, "err")); return; }
  const v = e.target.closest("[data-v]"); if (v) { est = {}; go(v.dataset.v); }
});
document.addEventListener("change", e => {
  const f = e.target.dataset.f; if (!f) return;
  est[f] = e.target.type === "checkbox" ? e.target.checked : e.target.value; go();
});
$("#salir").onclick = salir;
$("#menu").onclick = () => $("#side").classList.toggle("open");
token ? iniciar().catch(() => salir()) : salir();
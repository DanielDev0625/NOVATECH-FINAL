(() => {
  const nv = x => x.nivel === "critico" ? "er" : x.nivel === "ok" ? "ok" : "wn";
  const chip = x => `<span class="bd ${nv(x)}">${esc(x.nivel)}</span>`;
  let REP = [], PRODS = [];

  async function vAut() {
    const [R, E, O, P, K] = await Promise.all([
      api("/automatizacion/reposicion"), api("/automatizacion/energia"),
      api("/pedidos?estado=PREPARANDO"), api("/productos"),
      admin() ? api("/ingesta/claves") : Promise.resolve({data: []})]);
    REP = R.data; PRODS = P.data;
    const e = E.data;
    $("#vista").innerHTML = `
      <h3>Reposición predictiva</h3>
      <p class="mut">Consumo de los últimos ${R.parametros.dias} días, con ${R.parametros.lead_time} día(s) de reposición y ${R.parametros.cobertura} de cobertura objetivo. Los parámetros son un punto de partida.</p>
      ${tabla([["Producto", r => esc(r.nombre)], ["Stock", r => n(r.stock)], ["Comprometido", r => n(r.comprometido)], ["Consumo/día", r => n(r.consumo_diario)],
        ["Cobertura", r => r.dias_cobertura === null ? "Sin consumo" : n(r.dias_cobertura, 1) + " días"], ["Sugerido", r => r.sugerido ? n(r.sugerido, 0) + " " + esc(r.unidad) : "—"],
        ["Nivel", r => chip(r) + (r.confianza === "baja" ? ' <span class="mut">poco historial</span>' : "")]],
        REP, r => puede() && r.sugerido ? bt("lotesug", r.producto_id, "Crear lote") : "")}
      <h3>Pedidos en preparación: lotes asignados automáticamente</h3>
      ${tabla([["Pedido", o => esc(o.codigo)], ["Cliente", o => esc(o.cliente)]], O.data, o => bt("asig", o.id, "Ver asignación", "ghost"))}
      <h3>Energía inteligente</h3>
      <div class="stats">${st("Costo del mes", "$" + n(e.costo_mes_actual))}${st("Proyección a fin de mes", "$" + n(e.proyeccion_mes), e.proyeccion_mes > e.costo_mes_anterior && e.costo_mes_anterior > 0 ? "wn" : "ok")}${st("Mes anterior", "$" + n(e.costo_mes_anterior))}</div>
      <p class="mut">Proyección: costo acumulado ÷ ${e.dias_transcurridos} día(s) × ${e.dias_mes}. ${e.confianza_proyeccion === "baja" ? "Confianza baja: llevan pocos días del mes." : ""}</p>
      ${tabla([["Fuente", a => esc(a.fuente)], ["Fecha", a => fd(a.fecha)], ["Consumo", a => `${n(a.cantidad)} ${esc(a.unidad)}`], ["Mediana previa", a => n(a.mediana_previa)], ["Puntaje", a => a.puntaje]], e.anomalias)}
      ${admin() ? `<div class="bar" style="margin-top:22px"><h3 style="margin:0">Claves de API para medidores</h3><span class="sp"></span>${bt("nclave", "", "+ Clave")}</div>
      ${tabla([["Nombre", k => esc(k.nombre)], ["Prefijo", k => esc(k.prefijo)], ["Tarifa por defecto", k => k.tarifa_defecto === null ? "—" : n(k.tarifa_defecto)], ["Último uso", k => fd(k.ultimo_uso)], ["Estado", k => k.activa ? badge("ACTIVA").replace("wn", "ok") : badge("CANCELADO")]], K.data, k => k.activa ? bt("dclave", k.id, "Desactivar", "er") : "")}` : ""}`;
  }

  Object.assign(A, {
    lotesug: id => { const r = REP.find(x => x.producto_id == id); form("Lote sugerido", [
      {n: "codigo", l: "Código del lote", req: 1}, {n: "producto_id", l: "Producto", req: 1, v: id, o: PRODS.map(p => [p.id, `${p.codigo} — ${p.nombre}`])},
      {n: "cantidad_producida", l: "Cantidad (sugerida por el sistema)", t: "number", req: 1, v: r.sugerido}, {n: "fecha_produccion", l: "Fecha", t: "date", v: hoy()}],
      async f => { await api("/lotes", {m: "POST", b: lim(f)}); toast("Lote creado"); go(); }); },
    asig: async id => { const r = await api(`/automatizacion/pedidos/${id}/asignacion`);
      panel("Asignación de lotes (FIFO)", r.data.map(d => `<p><b>${esc(PRODS.find(p => p.id === d.producto_id)?.nombre || d.producto_id)}</b> × ${n(d.cantidad)} ${d.fijo ? '<span class="mut">(lote elegido a mano)</span>' : ""}</p>` +
        tabla([["Lote", x => esc(x.lote)], ["Cantidad", x => n(x.cantidad)]], d.partes)).join("")); },
    nclave: () => form("Nueva clave de API", [{n: "nombre", l: "Nombre (ej. Medidor planta 1)", req: 1}, {n: "tarifa_defecto", l: "Tarifa por defecto (por unidad)", t: "number"}, {n: "factor_defecto", l: "Factor de emisión por defecto", t: "number"}],
      async f => { const r = await api("/ingesta/claves", {m: "POST", b: lim(f)});
        panel("Clave creada", `<p class="err">Cópiala ahora. No se podrá volver a ver.</p><input readonly value="${esc(r.clave)}" onclick="this.select()">`); go(); }),
    dclave: id => confirm("¿Desactivar esta clave? Los medidores que la usen dejarán de enviar datos.") && run(() => api("/ingesta/claves/" + id, {m: "DELETE"}), "Clave desactivada"),
  });

  NAV.push(["aut", "Automatización"]);
  VISTAS.aut = vAut;
  if (me) $("#nav").innerHTML = NAV.map(([k, t]) => `<button data-v="${k}">${ICO[k] || ""}<span>${t}</span></button>`).join("");
})();
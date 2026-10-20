let token = sessionStorage.getItem("nt"),
    me = null,
    vista = "panel",
    primer = false,
    D = {};

const puede = () => me && me.rol !== "consulta";
const admin = () => me && me.rol === "admin";
const $ = s => document.querySelector(s);

const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[c]));

const n = (v, d = 2) => Number(v).toLocaleString("es-CO", { maximumFractionDigits: d });
const fd = v => !v ? "—" : v.length === 10 ? v.split("-").reverse().join("/") : new Date(v).toLocaleString("es-CO");
const lim = f => Object.fromEntries(Object.entries(f).filter(([, v]) => v !== ""));

const BC = {
  FINALIZADO: "ok", ENTREGADO: "ok", ENTREGADA: "ok",
  EN_PROCESO: "wn", RECHAZADO: "er", CANCELADO: "er", PROGRAMADA: "wn", EN_RUTA: "wn"
};

const badge = t => `<span class="bd ${BC[t] || "wn"}">${esc(t)}</span>`;
const bt = (a, id, t, c = "") => `<button class="btn sm ${c}" data-a="${a}" data-id="${id}">${t}</button>`;

const tabla = (cols, filas, acc) => filas.length 
  ? `<div class="tw"><table><thead><tr>${cols.map(c => `<th>${c[0]}</th>`).join("")}${acc ? "<th></th>" : ""}</tr></thead><tbody>${filas.map(r => `<tr>${cols.map(c => `<td>${c[1](r)}</td>`).join("")}${acc ? `<td class="ac">${acc(r)}</td>` : ""}</tr>`).join("")}</tbody></table></div>` 
  : `<p class="mut" style="padding: 24px; text-align: center;">No hay registros disponibles.</p>`;

function toast(t, k = "ok") {
  const e = document.createElement("div");
  e.className = "toast " + k;
  e.textContent = t;
  $("#toasts").append(e);
  setTimeout(() => e.remove(), 4000);
}

const cerrar = () => $("#modal").classList.add("hidden");

function panel(t, h) {
  $("#modal").innerHTML = `<div class="card wide"><h3>${esc(t)}</h3>${h}<div class="row"><button class="btn ghost" id="cx">Cerrar</button></div></div>`;
  $("#modal").classList.remove("hidden");
  $("#cx").onclick = cerrar;
}

function form(titulo, campos, enviar) {
  const m = $("#modal");
  m.innerHTML = `<form class="card"><h3>${esc(titulo)}</h3>${campos.map(c => `<label>${esc(c.l)}${c.o ? `<select name="${c.n}">${c.o.map(([v, t]) => `<option value="${esc(v)}" ${String(v) === String(c.v) ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>` : `<input name="${c.n}" type="${c.t || "text"}" step="any" value="${esc(c.v ?? "")}" ${c.req ? "required" : ""}>`}</label>`).join("")}<div class="row"><button type="button" class="btn ghost" id="cx">Cancelar</button><button class="btn">Guardar</button></div></form>`;
  m.classList.remove("hidden");
  $("#cx").onclick = cerrar;
  m.firstChild.onsubmit = async e => {
    e.preventDefault();
    try {
      await enviar(Object.fromEntries(new FormData(e.target)));
      cerrar();
    } catch (x) {
      toast(x.message, "err");
    }
  };
}

async function api(p, o = {}) {
  let r = await fetch("/api" + p, {
    method: o.m || "GET",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: "Bearer " + token } : {})
    },
    body: o.b ? JSON.stringify(o.b) : undefined
  });
  let d = null;
  try { d = await r.json(); } catch {}
  if (r.status === 401 && token) {
    salir();
    throw new Error("Sesión vencida");
  }
  if (!r.ok) throw new Error(d?.error || "Error " + r.status);
  return d;
}

function pintaLogin() {
  $("#login").className = "login-container";
  $("#login").innerHTML = `
    <div class="login-left">
      <div class="brand-header">
        <img src="/img/logo-Novatech.png" alt="NOVATECH" style="height: 52px; object-fit: contain;">
        <div class="brand-slogan">Innovación · Control · Eficiencia</div>
      </div>
      <div>
        <h1 style="font-size: 38px; line-height: 1.25; margin-bottom: 12px; color: #fff;">Tu inventario,<br><span style="color: #38bdf8;">en buenas manos</span></h1>
        <p class="mut" style="font-size: 15px; max-width: 440px; color: #cbd5e1;">Gestiona, controla y da seguimiento a tus procesos de forma simple, rápida y segura.</p>
      </div>
      <div class="login-features">
        <div class="feature-item">
          <div style="font-weight: 700; color: #fff;">Seguro</div>
          <div class="mut" style="font-size: 12px; color: #cbd5e1;">Tus datos protegidos</div>
        </div>
        <div class="feature-item">
          <div style="font-weight: 700; color: #fff;">Rápido</div>
          <div class="mut" style="font-size: 12px; color: #cbd5e1;">Acceso inmediato</div>
        </div>
        <div class="feature-item">
          <div style="font-weight: 700; color: #fff;">Eficiente</div>
          <div class="mut" style="font-size: 12px; color: #cbd5e1;">Mayor productividad</div>
        </div>
      </div>
    </div>
    <div class="login-right">
      <form class="login-card">
        <div>
          <h2 style="font-size: 24px; margin-bottom: 6px;">Bienvenido a <span style="color: var(--ac-blue);">NOVATECH</span></h2>
          <p class="mut" style="font-size: 13px;">Inicia sesión para acceder a tu panel principal.</p>
        </div>
        ${primer ? '<label>Nombre<input name="nombre" placeholder="Nombre completo" required></label>' : ""}
        <label>Correo Electrónico<input name="email" type="email" placeholder="Ingresa tu correo" required></label>
        <label>Contraseña<input name="password" type="password" placeholder="Ingresa tu contraseña" required></label>
        <button class="btn" style="width: 100%; margin-top: 8px;">→ Ingresar</button>
        <a href="#" id="alt" style="text-align: center; font-size: 13px; margin-top: 6px;">${primer ? "Ya tengo cuenta" : "Configurar primer administrador"}</a>
        <div style="background: #f8fafc; border: 1px solid var(--bd); padding: 12px 16px; border-radius: 12px; margin-top: 10px;">
          <div style="font-weight: 700; font-size: 12px;">Sistema de Gestión e Inventario</div>
          <div class="mut" style="font-size: 11px;">NOVATECH | Administración y trazabilidad de procesos</div>
        </div>
      </form>
    </div>
  `;

  $("#alt").onclick = e => {
    e.preventDefault();
    primer = !primer;
    pintaLogin();
  };

  $("#login form").onsubmit = async e => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    try {
      if (primer) await api("/auth/primer-admin", { m: "POST", b: f });
      const r = await api("/auth/login", { m: "POST", b: f });
      token = r.token;
      sessionStorage.setItem("nt", token);
      primer = false;
      await iniciar();
    } catch (x) {
      toast(x.message, "err");
    }
  };
}

function salir() {
  token = null;
  me = null;
  sessionStorage.removeItem("nt");
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  pintaLogin();
}

const NAV = [["panel", "Dashboard"], ["inv", "Inventario"], ["tra", "Trazabilidad"], ["logistica", "Logística"]];

async function iniciar() {
  me = (await api("/auth/yo")).data;
  $("#login").className = "modal hidden";
  $("#app").classList.remove("hidden");
  
  $("#side").innerHTML = `
    <div style="padding: 10px 4px; display: flex; align-items: center; justify-content: flex-start;">
      <img src="/img/logo-Novatech.png" alt="NOVATECH" style="height: 42px; object-fit: contain;">
    </div>
    <nav id="nav"></nav>
    <div class="side-foot">
      <div class="user-badge"><b>${esc(me.nombre)}</b><br><span style="text-transform: uppercase; font-size: 10px; opacity: 0.8;">${esc(me.rol)}</span></div>
      <button class="out" id="salir">Cerrar Sesión</button>
    </div>
  `;

  $("#nav").innerHTML = NAV.map(([k, t]) => `<button data-v="${k}">${t}</button>`).join("");
  $("#salir").onclick = salir;
  go("panel");
}

async function go(v) {
  if (v) vista = v;
  document.querySelectorAll("#nav button").forEach(b => b.classList.toggle("on", b.dataset.v === vista));
  $("#vista").innerHTML = '<p class="mut" style="padding: 20px;">Cargando información...</p>';
  try {
    if (vista === "panel") {
      const [P, L] = await Promise.all([api("/productos"), api("/lotes")]);
      const totalStock = P.data.reduce((acc, p) => acc + (p.stock_actual || 0), 0);
      const lotesProceso = L.data.filter(l => l.estado === "EN_PROCESO").length;
      const stockBajo = P.data.filter(p => (p.stock_actual || 0) < 10).length;
      const fechaActual = new Date().toLocaleDateString("es-CO", { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });

      $("#vista").innerHTML = `
        <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 24px;">
          <div>
            <h2 style="font-size: 26px; margin-bottom: 4px;">Hola, ${esc(me.nombre)} </h2>
            <p class="mut">Aquí tienes un resumen de la actividad de tu inventario.</p>
          </div>
          <div class="mut" style="text-align: right; font-size: 13px; font-weight: 600;">
            ${fechaActual}
          </div>
        </div>

        <div class="metrics-grid">
          <div class="metric-card">
            <div class="mut" style="font-size: 12px; font-weight: 700;">Total de productos</div>
            <div style="font-size: 30px; font-weight: 800; color: var(--tx);">${P.data.length}</div>
            <div style="font-size: 12px; color: var(--ac-green); font-weight: 600;">✓ Registrados</div>
          </div>
          <div class="metric-card">
            <div class="mut" style="font-size: 12px; font-weight: 700;">Stock en Bodega</div>
            <div style="font-size: 30px; font-weight: 800; color: var(--tx);">${n(totalStock, 0)}</div>
            <div style="font-size: 12px; color: var(--ac-blue); font-weight: 600;">Unidades físicas</div>
          </div>
          <div class="metric-card">
            <div class="mut" style="font-size: 12px; font-weight: 700;">Lotes en Proceso</div>
            <div style="font-size: 30px; font-weight: 800; color: var(--ac-orange);">${lotesProceso}</div>
            <div style="font-size: 12px; color: var(--ac-orange); font-weight: 600;">En producción</div>
          </div>
          <div class="metric-card">
            <div class="mut" style="font-size: 12px; font-weight: 700;">Stock bajo</div>
            <div style="font-size: 30px; font-weight: 800; color: ${stockBajo > 0 ? 'var(--ac-red)' : 'var(--tx)'};">${stockBajo}</div>
            <div style="font-size: 12px; color: ${stockBajo > 0 ? 'var(--ac-red)' : 'var(--ac-green)'}; font-weight: 600;">${stockBajo > 0 ? 'Necesitan reposición' : 'Sin alertas'}</div>
          </div>
        </div>

        <div class="dashboard-grid">
          <div>
            <h3 style="margin-bottom: 16px;">Productos Recientes</h3>
            ${tabla([["Código", p => esc(p.codigo)], ["Nombre", p => esc(p.nombre)], ["Stock", p => n(p.stock_actual)], ["Estado", p => p.stock_actual < 10 ? '<span class="bd wn">Stock bajo</span>' : '<span class="bd ok">En stock</span>']], P.data.slice(0, 5))}
          </div>
          <div>
            <h3 style="margin-bottom: 16px;">Acciones rápidas</h3>
            <div style="background: var(--card-bg); border: 1px solid var(--bd); border-radius: var(--radius); padding: 20px; display: flex; flex-direction: column; gap: 12px; box-shadow: var(--shadow);">
              ${puede() ? '<button class="btn" data-a="nprod" style="width: 100%;">+ Agregar producto</button><button class="btn ghost" data-a="nlote" style="width: 100%;">+ Crear nuevo lote</button>' : '<p class="mut">Modo de consulta activo.</p>'}
            </div>
          </div>
        </div>
      `;
    } else if (vista === "inv") {
      const P = await api("/productos");
      D.prods = P.data;
      $("#vista").innerHTML = `<div class="bar">${puede() ? bt("nprod", "", "+ Agregar producto") : ""}</div>` + tabla([["Código", p => esc(p.codigo)], ["Nombre", p => esc(p.nombre)], ["Stock Actual", p => n(p.stock_actual)]], D.prods);
    } else if (vista === "tra") {
      const [L, P] = await Promise.all([api("/lotes"), api("/productos")]);
      D.lotes = L.data;
      D.prods = P.data;
      
      const pn = id => D.prods.find(p => p.id === id)?.nombre || id;

      $("#vista").innerHTML = `<div class="bar">${puede() ? bt("nlote", "", "+ Crear nuevo lote") : ""}</div>` +
        tabla([
          ["Código Lote", l => esc(l.codigo)], 
          ["Producto", l => esc(pn(l.producto_id))], 
          ["Estado", l => badge(l.estado)]
        ], D.lotes, l => bt("ver", l.id, "Ver", "ghost") + (l.estado === "FINALIZADO" ? `<a class="btn sm ghost" href="/api/lotes/${l.id}/certificado" target="_blank">PDF</a>` : "") + (puede() && l.estado === "EN_PROCESO" ? bt("etapa", l.id, "+ Etapa") + bt("fin", l.id, "Finalizar", "er") : ""));
    } else if (vista === "logistica") {
      const [T, V, E] = await Promise.all([api("/logistica/transportistas"), api("/logistica/vehiculos"), api("/logistica/entregas")]);
      D.transportistas = T.data;
      D.vehiculos = V.data;
      D.entregas = E.data;

      const tn = id => D.transportistas.find(t => t.id === id)?.nombre || id;
      const vp = id => D.vehiculos.find(v => v.id === id)?.placa || id;

      $("#vista").innerHTML = `
        <div class="bar">
          ${puede() ? bt("ntrans", "", "+ Nuevo Transportista") + bt("nveh", "", "+ Nuevo Vehículo") + bt("nent", "", "+ Nueva Entrega") + bt("auto_ruta", "", " Auto-Asignar Ruta", "btn") : ""}
        </div>
        <h3 style="margin-top: 20px; margin-bottom: 12px;">Entregas Programadas y En Ruta</h3>
        ${tabla([
          ["ID", e => e.id],
          ["Pedido", e => e.pedido_id],
          ["Transportista", e => esc(tn(e.transportista_id))],
          ["Vehículo", e => esc(vp(e.vehiculo_id))],
          ["Destino", e => esc(e.destino)],
          ["Tarifa", e => `$${n(e.tarifa)}`],
          ["Comisión", e => `$${n(e.comision)}`],
          ["Estado", e => badge(e.estado)]
        ], D.entregas, e => (puede() && e.estado !== "ENTREGADA" ? bt("ent_est", e.id, "Avanzar Estado", "ghost") : ""))}
        
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-top: 30px;">
          <div>
            <h3>Transportistas</h3>
            ${tabla([["Nombre", t => esc(t.nombre)], ["Doc", t => esc(t.documento)], ["Costo/km", t => `$${n(t.costo_por_km)}`], ["Valoración", t => t.valoracion]], D.transportistas)}
          </div>
          <div>
            <h3>Vehículos</h3>
            ${tabla([["Placa", v => esc(v.placa)], ["Tipo", v => esc(v.tipo)], ["Modelo", v => esc(v.modelo)], ["Capacidad", v => `${v.capacidad_kg || 0} kg`]], D.vehiculos)}
          </div>
        </div>
      `;
    }
  } catch (e) {
    $("#vista").innerHTML = `<p class="mut err" style="padding: 20px;">${esc(e.message)}</p>`;
  }
}

const A = {
  nprod: () => form("Registrar Nuevo Producto", [
    { n: "codigo", l: "Código", req: 1 }, 
    { n: "nombre", l: "Nombre", req: 1 }, 
    { n: "unidad", l: "Unidad de medida", v: "und" }
  ], async f => { 
    await api("/productos", { m: "POST", b: lim(f) }); 
    toast("Producto creado correctamente"); 
    go(); 
  }),

  nlote: () => form("Crear Nuevo Lote", [
    { n: "codigo", l: "Código del Lote", req: 1, v: "LOT-001" },
    { n: "producto_id", l: "Seleccionar Producto", req: 1, o: (D.prods || []).map(p => [p.id, `${p.codigo} — ${p.nombre}`]) },
    { n: "cantidad_producida", l: "Cantidad Producida", t: "number", req: 1, v: 10 }
  ], async f => {
    await api("/lotes", { m: "POST", b: lim(f) });
    toast("Lote creado con éxito");
    go();
  }),

  etapa: id => form("Registrar Etapa de Producción", [
    { 
      n: "nombre", 
      l: "Nombre de la Etapa", 
      req: 1, 
      o: [
        ["Recepción de materias primas", "Recepción de materias primas"],
        ["Ensamblaje y producción", "Ensamblaje y producción"],
        ["Control de calidad", "Control de calidad"],
        ["Empaquetado y etiquetado", "Empaquetado y etiquetado"],
        ["Almacenamiento", "Almacenamiento"]
      ] 
    },
    { n: "responsable", l: "Responsable a cargo", req: 1 }
  ], async f => { 
    await api(`/lotes/${id}/etapas`, { m: "POST", b: lim(f) }); 
    toast("Etapa agregada correctamente"); 
    go(); 
  }),

  fin: id => confirm("¿Desea finalizar el lote? Esta acción sumará la cantidad producida al inventario.") && 
    api(`/lotes/${id}/estado`, { m: "POST", b: { estado: "FINALIZADO" } }).then(() => { 
      toast("Lote finalizado con éxito"); 
      go(); 
    }),

  ver: async id => {
    const r = await api(`/lotes/${id}/trazabilidad`);
    panel("Detalle de Trazabilidad del Lote", `<p style="margin-top:0;"><b>Lote:</b> ${esc(r.data.codigo)} &nbsp;|&nbsp; <b>Estado:</b> ${badge(r.data.estado)}</p>` + tabla([["Etapa", e => esc(e.titulo)], ["Fecha y Hora", e => fd(e.fecha)], ["Responsable", e => esc(e.responsable || "—")]], r.data.linea_de_tiempo));
  },

  ntrans: () => form("Registrar Nuevo Transportista", [
    { n: "nombre", l: "Nombre Completo", req: 1 },
    { n: "documento", l: "Documento / Cédula", req: 1 },
    { n: "telefono", l: "Teléfono" },
    { n: "costo_por_km", l: "Costo por Km ($)", t: "number", req: 1, v: 2500 },
    { n: "valoracion", l: "Valoración (1 a 5)", t: "number", req: 1, v: 5 }
  ], async f => {
    await api("/logistica/transportistas", { m: "POST", b: lim(f) });
    toast("Transportista registrado con éxito");
    go();
  }),

  nveh: () => form("Registrar Nuevo Vehículo", [
    { n: "placa", l: "Placa", req: 1, v: "ABC-123" },
    { n: "modelo", l: "Modelo / Marca", req: 1, v: "Chevrolet NHR" },
    { n: "tipo", l: "Tipo de Vehículo", req: 1, v: "Camión" },
    { n: "capacidad_kg", l: "Capacidad (kg)", t: "number", req: 1, v: 1500 }
  ], async f => {
    await api("/logistica/vehiculos", { m: "POST", b: lim(f) });
    toast("Vehículo registrado con éxito");
    go();
  }),

  nent: () => form("Registrar Nueva Entrega", [
    { n: "pedido_id", l: "ID del Pedido", t: "number", req: 1, v: 1 },
    { n: "transportista_id", l: "Seleccionar Transportista", req: 1, o: (D.transportistas || []).map(t => [t.id, `${t.nombre} ($${t.costo_por_km}/km)`]) },
    { n: "vehiculo_id", l: "Seleccionar Vehículo", req: 1, o: (D.vehiculos || []).map(v => [v.id, `${v.placa} — ${v.tipo}`]) },
    { n: "origen", l: "Dirección de Origen", v: "Bodega Principal Novatech" },
    { n: "destino", l: "Dirección de Destino", req: 1, v: "Calle 72 # 45-20, Barranquilla" },
    { n: "distancia_km", l: "Distancia estimada (km)", t: "number", req: 1, v: 12 }
  ], async f => {
    await api("/logistica/entregas", { m: "POST", b: lim(f) });
    toast("Entrega programada con éxito");
    go();
  }),

  auto_ruta: async () => {
    try {
      const pedidoId = prompt("Ingresa el ID del pedido a rutear automáticamente:", "1");
      if (!pedidoId) return;
      
      const destino = prompt("Ingresa la dirección de destino:", "Calle 72 # 45-20, Barranquilla");
      if (!destino) return;

      await api(`/logistica/entregas/auto-asignar?pedido_id=${pedidoId}&destino=${encodeURIComponent(destino)}`, { m: "POST" });
      toast("¡Ruta óptima calculada y transportista asignado con éxito!");
      go();
    } catch (x) {
      toast(x.message, "err");
    }
  },

  ent_est: id => form("Actualizar Estado de Entrega", [
    { n: "estado", l: "Nuevo Estado", req: 1, o: [["EN_RUTA", "EN_RUTA"], ["ENTREGADA", "ENTREGADA"]] }
  ], async f => {
    await api(`/logistica/entregas/${id}/estado`, { m: "PATCH", b: f });
    toast("Estado de entrega actualizado");
    go();
  })
};

document.addEventListener("click", e => {
  const a = e.target.closest("[data-a]");
  if (a) A[a.dataset.a]?.(a.dataset.id);
  const v = e.target.closest("[data-v]");
  if (v) go(v.dataset.v);
});

token ? iniciar().catch(() => salir()) : salir();
// cad2revit — interfaz web (sin dependencias; el visor 3D carga three.js bajo demanda)
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const h = (tag, attrs = {}, ...hijos) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of hijos.flat()) if (c != null) e.append(c);
  return e;
};
const SVGNS = "http://www.w3.org/2000/svg";
const s = (tag, attrs = {}) => {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
};

const AYUDA = {
  "proyecto.yaml": "SOLO ESTE PROYECTO · Niveles y qué plano DXF corresponde a cada nivel. Copia las rutas desde la pestaña Planos.",
  "materialidades.yaml": "COMÚN A TODOS LOS PROYECTOS · Materiales, tipos de muro/losa/cubierta con sus capas y reglas de asignación por capa CAD y espesor.",
  "estructuracion.yaml": "COMÚN A TODOS LOS PROYECTOS · Sistema estructural, muros estructurales, columnas, vigas, losas, fundaciones y ejes.",
  "nivel_detalle.yaml": "COMÚN A TODOS LOS PROYECTOS · Hasta dónde llega el modelo: preset LOD 100/200/300/350 y ajustes por elemento.",
  "capas_cad.yaml": "COMÚN A TODOS LOS PROYECTOS · Qué capa del CAD es qué cosa y qué bloque corresponde a qué familia de Revit.",
};
const PALETA = ["#2563eb", "#d97706", "#059669", "#db2777", "#7c3aed", "#0891b2", "#65a30d", "#dc2626", "#4b5563"];

const estado = {
  proyecto: null, entradas: {}, archivo: "materialidades.yaml", sucio: new Set(),
  corridas: [], corrida: null, plantas: {}, nivel: null, vista: "2d", cadCache: {}, colores: {},
};

// ------------------------------------------------------------------ API
async function api(metodo, ruta, cuerpo) {
  const op = { method: metodo, headers: {} };
  if (cuerpo instanceof FormData) op.body = cuerpo;
  else if (cuerpo !== undefined) { op.body = JSON.stringify(cuerpo); op.headers["Content-Type"] = "application/json"; }
  const r = await fetch(ruta, op);
  if (r.status === 204) return null;
  const datos = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof datos.detail === "string" ? datos.detail : JSON.stringify(datos.detail || r.statusText));
  return datos;
}
const P = (resto = "") => `/api/proyectos/${encodeURIComponent(estado.proyecto)}${resto}`;

let tToast;
function toast(msg, error = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (error ? " error" : "");
  clearTimeout(tToast);
  tToast = setTimeout(() => t.classList.add("oculto"), error ? 8000 : 3000);
}
async function intentar(fn) {
  try { return await fn(); } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ pestañas
function irA(tab) {
  $$(".pestanas button").forEach(b => b.classList.toggle("activa", b.dataset.tab === tab));
  $$(".tab").forEach(t => t.classList.toggle("activa", t.id === "tab-" + tab));
  try { localStorage.setItem("c2r-tab", tab); } catch {}
  if (tab === "resultado") dibujarVisor();
}
$$(".pestanas button").forEach(b => b.addEventListener("click", () => irA(b.dataset.tab)));

// ------------------------------------------------------------------ proyectos
const etiquetaProyecto = (nombre, n) => `${nombre}  (${n} ${n === 1 ? "corrida" : "corridas"})`;
async function cargarProyectos(seleccionar) {
  const lista = await api("GET", "/api/proyectos");
  const sel = $("#sel-proyecto");
  sel.replaceChildren(...lista.map(p => h("option", { value: p.id }, etiquetaProyecto(p.nombre, p.corridas))));
  if (!lista.length) {
    sel.append(h("option", { value: "" }, "— crea un proyecto —"));
    estado.proyecto = null;
    $("#dlg-nuevo").showModal();
    return;
  }
  let guardado = null;
  try { guardado = localStorage.getItem("c2r-proyecto"); } catch {}
  const id = seleccionar || (lista.some(p => p.id === guardado) ? guardado : lista[0].id);
  sel.value = id;
  await abrirProyecto(id);
}

async function abrirProyecto(id) {
  if (estado.sucio.size && !confirm("Hay cambios sin guardar en los lineamientos. ¿Descartarlos?")) {
    $("#sel-proyecto").value = estado.proyecto;
    return;
  }
  estado.proyecto = id;
  estado.sucio.clear();
  estado.cadCache = {};
  try { localStorage.setItem("c2r-proyecto", id); } catch {}
  await Promise.all([cargarPlanos(), cargarEntradas(), cargarCorridas()]);
}

$("#sel-proyecto").addEventListener("change", e => intentar(() => abrirProyecto(e.target.value)));
$("#btn-nuevo").addEventListener("click", () => $("#dlg-nuevo").showModal());
$("#dlg-nuevo").addEventListener("close", () => {
  if ($("#dlg-nuevo").returnValue !== "ok") return;
  const nombre = $("#nuevo-nombre").value.trim();
  if (!nombre) return;
  intentar(async () => {
    const p = await api("POST", "/api/proyectos", { nombre, con_ejemplo: $("#nuevo-ejemplo").checked });
    $("#nuevo-nombre").value = "";
    toast(`Proyecto "${p.nombre}" creado`);
    await cargarProyectos(p.id);
  });
});

// ------------------------------------------------------------------ planos
async function cargarPlanos() {
  const planos = await api("GET", P("/planos"));
  const tb = $("#tabla-planos tbody");
  if (!planos.length) {
    tb.replaceChildren(h("tr", {}, h("td", { colspan: 5, class: "tenue" }, "Sin planos todavía.")));
    return;
  }
  tb.replaceChildren(...planos.map(pl => h("tr", {},
    h("td", {}, h("strong", {}, pl.nombre), h("div", { class: "tenue" }, `${(pl.bytes / 1024).toFixed(0)} KB`),
      pl.error ? h("div", { class: "estado-error" }, pl.error) : null),
    h("td", {}, h("code", {}, pl.ruta_en_proyecto), " ",
      h("button", { class: "sec", title: "Copiar", onclick: () => navigator.clipboard?.writeText(pl.ruta_en_proyecto).then(() => toast("Ruta copiada")) }, "⧉")),
    h("td", {}, h("div", { class: "chips" }, pl.capas.map(c => h("span", { class: "chip" }, c)))),
    h("td", {}, h("div", { class: "chips" }, pl.bloques.map(c => h("span", { class: "chip" }, c)))),
    h("td", {}, h("div", { class: "acciones" },
      h("button", { class: "sec", onclick: () => intentar(() => verPlano(pl.nombre)) }, "Ver"),
      h("button", { class: "sec", onclick: () => intentar(async () => {
        if (!confirm(`¿Eliminar ${pl.nombre}?`)) return;
        await api("DELETE", P(`/planos/${encodeURIComponent(pl.nombre)}`));
        delete estado.cadCache[pl.nombre];
        await cargarPlanos();
      }) }, "Eliminar"))),
  )));
}

async function subir(files) {
  const fd = new FormData();
  [...files].forEach(f => fd.append("archivos", f));
  await intentar(async () => {
    const r = await api("POST", P("/planos"), fd);
    toast(`Subidos: ${r.guardados.join(", ")}`);
    r.guardados.forEach(n => delete estado.cadCache[n]);
    await cargarPlanos();
  });
}
$("#inp-planos").addEventListener("change", e => { subir(e.target.files); e.target.value = ""; });
const zona = $("#zona-subida");
zona.addEventListener("dragover", e => { e.preventDefault(); zona.classList.add("encima"); });
zona.addEventListener("dragleave", () => zona.classList.remove("encima"));
zona.addEventListener("drop", e => { e.preventDefault(); zona.classList.remove("encima"); subir(e.dataTransfer.files); });

async function geometriaCAD(nombre) {
  if (!estado.cadCache[nombre]) estado.cadCache[nombre] = await api("GET", P(`/planos/${encodeURIComponent(nombre)}/geometria`));
  return estado.cadCache[nombre];
}

async function verPlano(nombre) {
  const g = await geometriaCAD(nombre);
  $("#vista-plano").classList.remove("oculto");
  $("#tit-vista-plano").textContent = nombre + (g.recortado ? " (vista recortada)" : "");
  const svg = $("#svg-plano");
  const capas = [...new Set(g.segmentos.map(x => x[4]))];
  const col = Object.fromEntries(capas.map((c, i) => [c, PALETA[i % PALETA.length]]));
  const raiz = s("g", { transform: "scale(1,-1)" });
  const lineas = {};
  for (const [x1, y1, x2, y2, capa] of g.segmentos) {
    (lineas[capa] ||= []).push(`M${x1} ${y1}L${x2} ${y2}`);
  }
  for (const [capa, d] of Object.entries(lineas)) {
    const p = s("path", { d: d.join(""), stroke: col[capa], "stroke-width": 1.2, fill: "none", "vector-effect": "non-scaling-stroke" });
    p.append(s("title")); p.firstChild.textContent = capa;
    raiz.append(p);
  }
  svg.replaceChildren(raiz);
  encuadrar(svg, g.segmentos.flatMap(([a, b, c, d]) => [[a, b], [c, d]]));
  $("#vista-plano").scrollIntoView({ behavior: "smooth" });
}
$("#cerrar-vista-plano").addEventListener("click", () => $("#vista-plano").classList.add("oculto"));

// ------------------------------------------------------------------ lineamientos
async function cargarEntradas() {
  const r = await api("GET", P("/entradas"));
  estado.entradas = r.archivos;
  mostrarErroresEntradas(r.errores);
  pintarListaArchivos();
  abrirArchivo(estado.archivo);
}

function mostrarErroresEntradas(errores) {
  const box = $("#errores-entradas");
  const badge = $("#badge-errores");
  if (errores.length) {
    box.textContent = "Los lineamientos tienen problemas (la corrida fallará hasta corregirlos):\n• " + errores.join("\n• ");
    box.classList.remove("oculto");
    badge.textContent = errores.length;
    badge.classList.remove("oculto");
  } else {
    box.classList.add("oculto");
    badge.classList.add("oculto");
  }
}

function pintarListaArchivos() {
  $("#lista-archivos").replaceChildren(...Object.keys(estado.entradas).map(a =>
    h("button", { class: a === estado.archivo ? "activa" : "", onclick: () => abrirArchivo(a) },
      h("span", {}, a, estado.sucio.has(a) ? h("span", { class: "sucio" }, " ●") : null, h("small", {}, AYUDA[a] || "")))));
}

function abrirArchivo(a) {
  // conserva lo escrito en el archivo anterior
  if (estado.archivo && $("#ed-texto").dataset.archivo === estado.archivo)
    estado.entradas[estado.archivo] = $("#ed-texto").value;
  estado.archivo = a;
  $("#ed-titulo").textContent = a;
  $("#ed-ayuda").textContent = AYUDA[a] || "";
  const ta = $("#ed-texto");
  ta.value = estado.entradas[a] ?? "";
  ta.dataset.archivo = a;
  $("#ed-estado").textContent = estado.sucio.has(a) ? "sin guardar" : "";
  pintarListaArchivos();
}

$("#ed-texto").addEventListener("input", () => {
  estado.entradas[estado.archivo] = $("#ed-texto").value;
  if (!estado.sucio.has(estado.archivo)) { estado.sucio.add(estado.archivo); pintarListaArchivos(); }
  $("#ed-estado").textContent = "sin guardar";
});
$("#ed-texto").addEventListener("keydown", e => {
  if (e.key === "Tab") {   // YAML: insertar 2 espacios
    e.preventDefault();
    const ta = e.target, i = ta.selectionStart;
    ta.setRangeText("  ", i, ta.selectionEnd, "end");
    ta.dispatchEvent(new Event("input"));
  }
});

async function guardarArchivo(a = estado.archivo) {
  const r = await api("PUT", P(`/entradas/${a}`), { texto: estado.entradas[a] });
  estado.sucio.delete(a);
  if (a === estado.archivo) $("#ed-estado").textContent = "guardado ✓";
  mostrarErroresEntradas(r.errores);
  pintarListaArchivos();
  return r;
}
async function guardarTodo() {
  for (const a of [...estado.sucio]) await guardarArchivo(a);
}
$("#btn-guardar").addEventListener("click", () => intentar(() => guardarArchivo()));

document.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s" && $("#tab-lineamientos").classList.contains("activa")) {
    e.preventDefault();
    intentar(() => guardarArchivo());
  }
});
window.addEventListener("beforeunload", e => { if (estado.sucio.size) { e.preventDefault(); e.returnValue = ""; } });

// ------------------------------------------------------------------ ejecutar
$("#btn-ejecutar").addEventListener("click", () => intentar(async () => {
  if (!estado.proyecto) return toast("Primero crea un proyecto", true);
  const b = $("#btn-ejecutar");
  b.disabled = true; b.textContent = "Procesando…";
  try {
    await guardarTodo();
    const r = await api("POST", P("/corridas"), { notas: $("#notas-corrida").value });
    $("#notas-corrida").value = "";
    await cargarCorridas(r.n);
    irA("resultado");
    toast(r.ok ? `Corrida ${r.n} lista` : `Corrida ${r.n} con errores`, !r.ok);
  } finally {
    b.disabled = false; b.textContent = "▶ Ejecutar";
  }
}));

// ------------------------------------------------------------------ corridas
async function cargarCorridas(seleccionar) {
  estado.corridas = await api("GET", P("/corridas"));
  pintarHistorial();
  const op = $(`#sel-proyecto option[value="${CSS.escape(estado.proyecto)}"]`);
  if (op) op.textContent = etiquetaProyecto(op.textContent.replace(/\s+\(\d+ corridas?\)$/, ""), estado.corridas.length);
  const hay = estado.corridas.length > 0;
  $("#sin-resultado").classList.toggle("oculto", hay);
  $("#con-resultado").classList.toggle("oculto", !hay);
  $("#sel-corrida").replaceChildren(...estado.corridas.map(c =>
    h("option", { value: c.n }, `Corrida ${c.n} · ${new Date(c.fecha).toLocaleString()}${c.notas ? " · " + c.notas.slice(0, 40) : ""}`)));
  if (hay) await abrirCorrida(seleccionar || estado.corridas[0].n);
  else estado.corrida = null;
}
$("#sel-corrida").addEventListener("change", e => intentar(() => abrirCorrida(+e.target.value)));

async function abrirCorrida(n) {
  const r = await api("GET", P(`/corridas/${n}`));
  estado.corrida = r;
  estado.plantas = r.plantas;
  $("#sel-corrida").value = n;
  const res = r.resumen;
  $("#res-estado").replaceChildren(res.ok
    ? h("span", { class: "estado-ok" }, `✔ Correcta · LOD ${res.lod}`)
    : h("span", { class: "estado-error" }, "✖ Con errores"));
  const err = $("#res-error");
  const textoErr = res.error || (res.errores_esquema?.length ? "El modelo no cumple el esquema:\n• " + res.errores_esquema.join("\n• ") : "");
  err.textContent = textoErr;
  err.classList.toggle("oculto", !textoErr);
  const dl = $("#btn-descargar");
  dl.classList.toggle("oculto", !r.modelo);
  dl.href = P(`/corridas/${n}/modelo.json`);

  // tarjetas con diferencia respecto de la corrida anterior
  const idx = estado.corridas.findIndex(c => c.n === n);
  const prev = estado.corridas.slice(idx + 1).find(c => c.conteos);
  const etiquetas = { muros: "Muros", muros_estructurales: "Estructurales", puertas: "Puertas", ventanas: "Ventanas",
    losas: "Losas", columnas: "Columnas", vigas: "Vigas", fundaciones: "Fundaciones", recintos: "Recintos",
    cielos: "Cielos", piezas_entramado: "Piezas de madera", ejes: "Ejes", niveles: "Niveles", advertencias: "Advertencias" };
  $("#tarjetas").replaceChildren(...(res.conteos ? Object.entries(etiquetas).filter(([k]) => res.conteos[k] != null).map(([k, eti]) => {
    const v = res.conteos[k], pv = prev?.conteos?.[k];
    const d = pv == null ? null : v - pv;
    return h("div", { class: "tarjeta" }, h("div", {}, h("span", { class: "num" }, v),
      d ? h("span", { class: "delta tenue" }, (d > 0 ? "+" : "") + d) : null), h("div", { class: "eti" }, eti));
  }) : []));

  const m = r.modelo;
  $("#lista-advertencias").replaceChildren(...(m?.advertencias?.length
    ? m.advertencias.map(a => h("li", {}, a)) : [h("li", { class: "tenue" }, "Sin advertencias.")]));

  // niveles
  const niveles = m?.niveles?.map(x => x.nombre) || [];
  const sel = $("#sel-nivel");
  sel.replaceChildren(...niveles.map(x => h("option", { value: x }, x)));
  if (!niveles.includes(estado.nivel)) estado.nivel = niveles[0];
  sel.value = estado.nivel || "";

  // colores por tipo de muro, estables
  (m?.tipos_muro || []).forEach(t => {
    if (!estado.colores[t.nombre]) estado.colores[t.nombre] = PALETA[Object.keys(estado.colores).length % PALETA.length];
  });
  dibujarVisor();
}

$("#sel-nivel").addEventListener("change", e => { estado.nivel = e.target.value; dibujarVisor(); });
$$("#capas-2d input").forEach(i => i.addEventListener("change", dibujarVisor));
$("#chk-corte").addEventListener("change", dibujarVisor);
$$(".seg button").forEach(b => b.addEventListener("click", () => {
  estado.vista = b.dataset.vista;
  $$(".seg button").forEach(x => x.classList.toggle("activa", x === b));
  dibujarVisor();
}));
$("#btn-como-revit").addEventListener("click", () => $("#dlg-revit").showModal());

// ------------------------------------------------------------------ visor
function dibujarVisor() {
  if (!$("#tab-resultado").classList.contains("activa") || !estado.corrida?.modelo) return;
  const es3d = estado.vista === "3d";
  $("#svg-resultado").classList.toggle("oculto", es3d);
  $("#visor-3d").classList.toggle("oculto", !es3d);
  $("#capas-2d").classList.toggle("oculto", es3d);
  $("#corte-3d").classList.toggle("oculto", !es3d);
  pintarLeyenda();
  if (es3d) {
    import("./vista3d.js").then(mod => mod.mostrar($("#visor-3d"), estado.corrida.modelo, estado.colores,
      $("#chk-corte").checked ? estado.nivel : null))
      .catch(e => toast("No se pudo cargar el visor 3D (¿sin internet para three.js?): " + e.message, true));
  } else {
    intentar(dibujar2D);
  }
}

function pintarLeyenda() {
  const m = estado.corrida.modelo;
  const usados = new Set(m.muros.map(x => x.tipo));
  $("#leyenda").replaceChildren(h("strong", {}, "Tipos de muro"),
    ...m.tipos_muro.filter(t => usados.has(t.nombre)).map(t =>
      h("div", {}, h("i", { style: `background:${estado.colores[t.nombre]}` }), t.nombre)),
    h("div", { class: "tenue" }, "borde grueso = estructural"));
}

const visible = capa => $(`#capas-2d input[data-capa="${capa}"]`).checked;

async function dibujar2D() {
  const m = estado.corrida.modelo, nivel = estado.nivel;
  const svg = $("#svg-resultado");
  const raiz = s("g", { transform: "scale(1,-1)" });
  const puntos = [];
  const titulo = (el, txt) => { const t = s("title"); t.textContent = txt; el.append(t); return el; };
  const texto = (x, y, txt, clase = {}) => {
    const t = s("text", { x, y: -y, transform: "scale(1,-1)", "font-size": 0.28, "text-anchor": "middle",
      fill: "var(--texto)", ...clase });
    t.textContent = txt;
    return t;
  };

  // CAD de fondo
  const archivo = estado.plantas[nivel];
  if (visible("cad") && archivo) {
    try {
      const g = await geometriaCAD(archivo);
      const d = g.segmentos.map(([a, b, c, e]) => `M${a} ${b}L${c} ${e}`).join("");
      raiz.append(s("path", { d, stroke: "var(--cad)", "stroke-width": 0.8, fill: "none", "vector-effect": "non-scaling-stroke" }));
      g.segmentos.forEach(([a, b, c, e]) => puntos.push([a, b], [c, e]));
    } catch { /* el plano pudo ser eliminado después de la corrida */ }
  }

  if (visible("losas")) for (const l of m.losas.filter(x => x.nivel === nivel && !x.terminacion)) {
    raiz.append(titulo(s("polygon", { points: l.contorno.map(p => p.join(",")).join(" "), fill: "var(--acento)",
      "fill-opacity": 0.06, stroke: "var(--acento)", "stroke-dasharray": "6 4", "stroke-width": 1, "vector-effect": "non-scaling-stroke" }),
      `${l.id} · ${l.tipo}`));
    puntos.push(...l.contorno);
  }

  if (visible("ejes")) for (const e of m.ejes) {
    raiz.append(s("line", { x1: e.inicio[0], y1: e.inicio[1], x2: e.fin[0], y2: e.fin[1], stroke: "var(--tenue)",
      "stroke-dasharray": "12 3 2 3", "stroke-width": 0.8, "vector-effect": "non-scaling-stroke" }));
    raiz.append(s("circle", { cx: e.inicio[0], cy: e.inicio[1], r: 0.3, fill: "var(--superficie)", stroke: "var(--tenue)", "vector-effect": "non-scaling-stroke" }));
    raiz.append(texto(e.inicio[0], e.inicio[1] - 0.1, e.nombre));
  }

  const muros = m.muros.filter(x => x.nivel === nivel);
  const porId = Object.fromEntries(muros.map(x => [x.id, x]));
  if (visible("muros")) for (const w of muros) {
    const [x1, y1] = w.inicio, [x2, y2] = w.fin;
    const L = Math.hypot(x2 - x1, y2 - y1) || 1, nx = -(y2 - y1) / L * w.espesor / 2, ny = (x2 - x1) / L * w.espesor / 2;
    const pts = [[x1 + nx, y1 + ny], [x2 + nx, y2 + ny], [x2 - nx, y2 - ny], [x1 - nx, y1 - ny]];
    raiz.append(titulo(s("polygon", { points: pts.map(p => p.join(",")).join(" "), fill: estado.colores[w.tipo] || "#888",
      "fill-opacity": 0.55, stroke: estado.colores[w.tipo] || "#888", "stroke-width": w.estructural ? 2.2 : 0.8,
      "vector-effect": "non-scaling-stroke" }),
      `${w.id}\n${w.tipo}\nespesor medido ${w.espesor} m · largo ${L.toFixed(2)} m\n${w.estructural ? "estructural" : "no estructural"}${w.perimetral ? " · perimetral" : ""}\ncapa CAD: ${w.capa_cad}`));
    puntos.push(...pts);
  }

  if (visible("muros")) for (const e of (m.entramados || []).filter(x => x.nivel === nivel)) {
    for (const p of e.piezas.filter(p => p.inicio[0] === p.fin[0] && p.inicio[1] === p.fin[1])) {
      raiz.append(titulo(s("rect", { x: p.inicio[0] - (Math.abs(e.direccion[0]) * p.ancho + Math.abs(e.direccion[1]) * p.profundidad) / 2,
        y: p.inicio[1] - (Math.abs(e.direccion[1]) * p.ancho + Math.abs(e.direccion[0]) * p.profundidad) / 2,
        width: Math.abs(e.direccion[0]) * p.ancho + Math.abs(e.direccion[1]) * p.profundidad,
        height: Math.abs(e.direccion[1]) * p.ancho + Math.abs(e.direccion[0]) * p.profundidad,
        fill: "#a8743f" }), `${e.muro} · ${p.rol} ${p.ancho * 1000}×${p.profundidad * 1000} mm`));
    }
  }

  if (visible("aberturas")) for (const a of m.aberturas.filter(x => porId[x.muro])) {
    const w = porId[a.muro];
    const L = Math.hypot(w.fin[0] - w.inicio[0], w.fin[1] - w.inicio[1]) || 1;
    const ux = (w.fin[0] - w.inicio[0]) / L, uy = (w.fin[1] - w.inicio[1]) / L;
    const col = a.categoria === "puerta" ? "#b45309" : "#0ea5e9";
    raiz.append(titulo(s("line", { x1: a.centro[0] - ux * a.ancho / 2, y1: a.centro[1] - uy * a.ancho / 2,
      x2: a.centro[0] + ux * a.ancho / 2, y2: a.centro[1] + uy * a.ancho / 2, stroke: col, "stroke-width": 6,
      "stroke-linecap": "butt", "vector-effect": "non-scaling-stroke" }),
      `${a.id} · ${a.categoria} (${a.modo})\n${a.familia ?? ""} : ${a.tipo ?? ""}\n${a.ancho} × ${a.alto} m, antepecho ${a.antepecho} m\nbloque CAD: ${a.bloque_cad}`));
  }

  if (visible("estructura")) {
    for (const c of m.columnas.filter(x => x.nivel === nivel)) {
      raiz.append(titulo(s("rect", { x: c.centro[0] - c.b / 2, y: c.centro[1] - c.h / 2, width: c.b, height: c.h,
        transform: `rotate(${c.rotacion || 0} ${c.centro[0]} ${c.centro[1]})`, fill: "var(--texto)", "fill-opacity": 0.8 }),
        `${c.id} · columna ${c.b} × ${c.h} m\n${c.familia ?? ""}`));
      puntos.push(c.centro);
    }
    // vigas que se apoyan en el nivel superior a esta planta
    const iNiv = m.niveles.findIndex(x => x.nombre === nivel);
    const sup = m.niveles[iNiv + 1]?.nombre;
    for (const v of m.vigas.filter(x => x.nivel === sup)) {
      raiz.append(titulo(s("line", { x1: v.inicio[0], y1: v.inicio[1], x2: v.fin[0], y2: v.fin[1], stroke: "var(--texto)",
        "stroke-dasharray": "8 4", "stroke-width": 2, "vector-effect": "non-scaling-stroke" }),
        `${v.id} · viga ${v.b} × ${v.h} m (en ${v.nivel})`));
    }
    if (iNiv === 0) for (const z of m.fundaciones.aisladas) {
      raiz.append(titulo(s("rect", { x: z.centro[0] - z.b / 2, y: z.centro[1] - z.l / 2, width: z.b, height: z.l,
        fill: "none", stroke: "var(--tenue)", "stroke-dasharray": "3 3", "vector-effect": "non-scaling-stroke" }),
        `zapata ${z.b} × ${z.l} × ${z.h} m`));
    }
  }

  if (visible("recintos")) for (const r of m.recintos.filter(x => x.nivel === nivel)) {
    raiz.append(texto(r.punto[0], r.punto[1], r.nombre, { "font-weight": 600 }));
  }

  svg.replaceChildren(raiz);
  if (svg.dataset.clave !== `${estado.corrida.resumen.n}-${nivel}`) {
    encuadrar(svg, puntos.length ? puntos : [[0, 0], [10, 10]]);
    svg.dataset.clave = `${estado.corrida.resumen.n}-${nivel}`;
  }
}

// ------------------------------------------------------------------ zoom / pan SVG
function encuadrar(svg, puntos) {
  const xs = puntos.map(p => p[0]), ys = puntos.map(p => -p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const m = Math.max(x1 - x0, y1 - y0) * 0.06 + 0.5;
  svg.setAttribute("viewBox", `${x0 - m} ${y0 - m} ${x1 - x0 + 2 * m} ${y1 - y0 + 2 * m}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
}

function habilitarZoom(svg) {
  const vb = () => svg.getAttribute("viewBox").split(" ").map(Number);
  svg.addEventListener("wheel", e => {
    e.preventDefault();
    const [x, y, w, hh] = vb();
    const r = svg.getBoundingClientRect();
    const escala = Math.max(w / r.width, hh / r.height);
    const px = x + (e.clientX - r.left - (r.width - w / escala) / 2) * escala;
    const py = y + (e.clientY - r.top - (r.height - hh / escala) / 2) * escala;
    const k = e.deltaY > 0 ? 1.15 : 1 / 1.15;
    svg.setAttribute("viewBox", `${px - (px - x) * k} ${py - (py - y) * k} ${w * k} ${hh * k}`);
  }, { passive: false });
  let arr = null;
  svg.addEventListener("pointerdown", e => { arr = { x: e.clientX, y: e.clientY, vb: vb() }; svg.setPointerCapture(e.pointerId); });
  svg.addEventListener("pointermove", e => {
    if (!arr) return;
    const r = svg.getBoundingClientRect();
    const [x, y, w, hh] = arr.vb;
    const escala = Math.max(w / r.width, hh / r.height);
    svg.setAttribute("viewBox", `${x - (e.clientX - arr.x) * escala} ${y - (e.clientY - arr.y) * escala} ${w} ${hh}`);
  });
  svg.addEventListener("pointerup", () => { arr = null; });
}
habilitarZoom($("#svg-resultado"));
habilitarZoom($("#svg-plano"));

// ------------------------------------------------------------------ historial
function pintarHistorial() {
  const tb = $("#tabla-historial tbody");
  if (!estado.corridas.length) {
    tb.replaceChildren(h("tr", {}, h("td", { colspan: 11, class: "tenue" }, "Sin corridas todavía.")));
    return;
  }
  tb.replaceChildren(...estado.corridas.map((c, i) => {
    const k = c.conteos || {};
    const anterior = estado.corridas[i + 1];
    const notas = h("input", { value: c.notas || "", placeholder: "qué cambió / qué falta" });
    notas.addEventListener("change", () => intentar(async () => {
      await api("PUT", P(`/corridas/${c.n}/notas`), { notas: notas.value });
      c.notas = notas.value;
      toast("Notas guardadas");
    }));
    return h("tr", {},
      h("td", {}, h("strong", {}, c.n)),
      h("td", {}, new Date(c.fecha).toLocaleString()),
      h("td", {}, c.lod ?? "—"),
      h("td", {}, c.ok ? h("span", { class: "estado-ok" }, "✔") : h("span", { class: "estado-error", title: c.error || "" }, "✖")),
      h("td", {}, k.muros ?? "—"), h("td", {}, k.puertas != null ? k.puertas + k.ventanas : "—"),
      h("td", {}, k.columnas ?? "—"), h("td", {}, k.vigas ?? "—"), h("td", {}, k.advertencias ?? "—"),
      h("td", {}, notas),
      h("td", {}, h("div", { class: "acciones" },
        h("button", { class: "sec", onclick: () => intentar(async () => { await abrirCorrida(c.n); irA("resultado"); }) }, "Ver"),
        anterior ? h("button", { class: "sec", onclick: () => intentar(() => comparar(anterior.n, c.n)) }, `Comparar con ${anterior.n}`) : null,
        h("button", { class: "sec", title: "Devuelve los lineamientos a los usados en esta corrida", onclick: () => intentar(async () => {
          if (!confirm(`¿Reemplazar los lineamientos actuales por los de la corrida ${c.n}?`)) return;
          estado.sucio.clear();
          await api("POST", P(`/corridas/${c.n}/restaurar`));
          await cargarEntradas();
          toast(`Lineamientos restaurados desde la corrida ${c.n}`);
        }) }, "Restaurar"))));
  }));
}

async function comparar(a, b) {
  const r = await api("GET", P(`/corridas/${a}/comparar/${b}`));
  $("#comparacion").classList.remove("oculto");
  $("#tit-comparacion").textContent = `Corrida ${a} → corrida ${b}`;
  const filas = Object.entries(r.conteos).filter(([, [x, y]]) => x !== y);
  $("#cmp-conteos").replaceChildren(filas.length
    ? h("table", { class: "tabla" }, h("thead", {}, h("tr", {}, h("th", {}, "Elemento"), h("th", {}, `#${a}`), h("th", {}, `#${b}`))),
        h("tbody", {}, filas.map(([k, [x, y]]) => h("tr", {}, h("td", {}, k), h("td", {}, x ?? "—"), h("td", {}, y ?? "—")))))
    : h("p", { class: "tenue" }, "Mismos conteos de elementos."));
  const difs = Object.entries(r.diferencias);
  $("#cmp-difs").replaceChildren(...(difs.length ? difs.map(([f, d]) => h("div", {}, h("h3", {}, f),
    h("pre", { class: "dif" }, d.split("\n").map(l => h("span", { class: l.startsWith("+") ? "mas" : l.startsWith("-") ? "menos" : "" }, l + "\n")))))
    : [h("p", { class: "tenue" }, "Los lineamientos no cambiaron entre estas corridas.")]));
  $("#comparacion").scrollIntoView({ behavior: "smooth" });
}
$("#cerrar-comparacion").addEventListener("click", () => $("#comparacion").classList.add("oculto"));

// ------------------------------------------------------------------ inicio
fetch("/salud").then(r => r.json()).then(v => {
  $("#version").textContent = `v${v.version} · ${v.commit}`;
  $("#version").title = "Versión publicada (commit de GitHub)";
}).catch(() => {});

intentar(async () => {
  await cargarProyectos();
  let tab = null;
  try { tab = localStorage.getItem("c2r-tab"); } catch {}
  if (tab && $(`#tab-${tab}`)) irA(tab);
});

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
  if (!r.ok) throw new Error(typeof datos.detail === "string" ? datos.detail : (datos.detail ? JSON.stringify(datos.detail) : `el servidor respondió ${r.status} ${r.statusText}`.trim()));
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
  if (tab === "estructura") intentar(cargarEstructura);
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
  await Promise.all([cargarPlanos(), cargarEntradas(), cargarCorridas(), cargarLegos()]);
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
// ------------------------------------------------------------------ niveles y uso de planos
const dist = { niveles: [], usos: {} };

function filaNivel(n = { nombre: "", cota: 0 }) {
  return h("tr", {},
    h("td", {}, h("input", { "data-k": "nombre", value: n.nombre, placeholder: "N1" })),
    h("td", {}, h("input", { "data-k": "cota", type: "number", step: "0.01", value: n.cota })),
    h("td", {}, h("button", { class: "sec", title: "Quitar", onclick: e => e.target.closest("tr").remove() }, "✕")));
}

function nivelesEnPantalla() {
  return $$("#tabla-niveles tbody tr").map(tr => ({
    nombre: tr.querySelector('[data-k="nombre"]').value.trim(), cota: +tr.querySelector('[data-k="cota"]').value || 0 }));
}

async function guardarDistribucion(nota) {
  const r = await api("PUT", P("/distribucion"), { niveles: nivelesEnPantalla(), usos: dist.usos });
  mostrarErroresEntradas(r.errores);
  toast(nota);
  await Promise.all([cargarPlanos(), cargarEntradas(), cargarLegos()]);
}

$("#btn-agregar-nivel").addEventListener("click", () => {
  const ns = nivelesEnPantalla();
  const ultima = ns.length ? Math.max(...ns.map(n => n.cota)) : -2.7;
  $("#tabla-niveles tbody").append(filaNivel({ nombre: `N${ns.length + 1}`, cota: +(ultima + 2.7).toFixed(2) }));
});
$("#btn-guardar-niveles").addEventListener("click", () => intentar(() => guardarDistribucion("Niveles guardados")));

async function cargarCapas() {
  $("#lista-capas").replaceChildren(h("p", { class: "tenue" }, "Leyendo las capas de tus plantas… (un plano grande puede tardar hasta un minuto la primera vez)"));
  let c;
  try { c = await api("GET", P("/capas")); }
  catch (e) {
    $("#lista-capas").replaceChildren(h("p", { class: "estado-error" }, `No se pudieron leer las capas: ${e.message}. Recarga la página para reintentar.`));
    return;
  }
  if (!c.capas_plano.length) {
    $("#lista-capas").replaceChildren(h("p", { class: "tenue" }, "Asigna al menos una planta a un nivel para ver sus capas."));
    return;
  }
  $("#lista-capas").replaceChildren(...c.categorias.map(cat => {
    const marcadas = new Set(cat.usadas);
    const chips = c.capas_plano.map(capa => {
      const chip = h("span", { class: "chip-capa" + (marcadas.has(capa) ? " on" : ""), title: capa }, capa);
      chip.addEventListener("click", () => intentar(async () => {
        chip.classList.toggle("on");
        const elegidas = [...chip.parentElement.querySelectorAll(".chip-capa.on")].map(x => x.textContent);
        $("#capas-estado").textContent = "guardando…";
        await api("PUT", P("/capas"), { categoria: cat.clave, capas: elegidas });
        $("#capas-estado").textContent = "guardado ✓ (presiona ▶ Ejecutar para aplicarlo)";
        await cargarCapas();
        await cargarLegos();
      }));
      return chip;
    });
    const nuevas = (cat.sugeridas || []).filter(x => !marcadas.has(x.capa));
    const sugerencia = nuevas.length ? h("div", { class: "sugerencia" },
      h("span", {}, "Por su dibujo (líneas paralelas) parecen muros: " +
        nuevas.map(x => `${x.capa} (${x.metros} m)`).join(", ") + " "),
      h("button", { class: "pri", style: "padding:2px 10px;font-size:12px", onclick: () => intentar(async () => {
        await api("PUT", P("/capas"), { categoria: cat.clave, capas: [...cat.usadas, ...nuevas.map(x => x.capa)] });
        $("#capas-estado").textContent = "guardado ✓ (presiona ▶ Ejecutar para aplicarlo)";
        await cargarCapas();
      }) }, cat.usadas.length ? "Agregarlas" : "Usarlas como muros")) : null;
    return h("div", { class: "cat-capas" },
      h("div", {}, h("strong", {}, cat.nombre),
        h("div", { class: "origen" }, cat.elegidas ? "elegidas por ti" : (cat.usadas.length ? "según la regla general" : "ninguna reconocida")),
        cat.elegidas ? h("button", { class: "sec", style: "margin-top:4px;padding:2px 8px;font-size:12px", onclick: () => intentar(async () => {
          await api("PUT", P("/capas"), { categoria: cat.clave, capas: null });
          await cargarCapas();
        }) }, "Usar regla general") : null),
      h("div", {}, sugerencia, h("div", { class: "chips" }, chips)));
  }));
}

async function cargarPlanos() {
  $("#tit-proyecto-planos").textContent = $("#sel-proyecto").selectedOptions[0]?.textContent.replace(/\s*\(\d+ corridas?\)$/, "") ?? "";
  // niveles primero (respuesta inmediata); luego capas y planos, que leen los archivos
  const d = await api("GET", P("/distribucion"));
  dist.niveles = d.niveles; dist.usos = d.usos;
  $("#tabla-niveles tbody").replaceChildren(...d.niveles.map(filaNivel));
  const tb0 = $("#tabla-planos tbody");
  tb0.replaceChildren(h("tr", {}, h("td", { colspan: 5, class: "tenue" }, "Leyendo planos…")));
  let planos;
  try { planos = await api("GET", P("/planos")); }
  catch (e) {
    tb0.replaceChildren(h("tr", {}, h("td", { colspan: 5, class: "estado-error" }, `No se pudieron leer los planos: ${e.message}`)));
    throw e;
  }
  cargarCapas();
  const ejemplos = ["planta_n1.dxf", "planta_n2.dxf", "elevacion_norte.dxf"];
  const sinUsar = planos.filter(p => p.uso === "no").map(p => p.nombre);
  const soloEjemplos = Object.keys(d.usos).length && Object.keys(d.usos).every(n => ejemplos.includes(n));
  const aviso = $("#aviso-planos");
  const msg = [];
  if (sinUsar.length) msg.push(`Estos planos no se están usando: ${sinUsar.join(", ")}. Elige su uso en la tabla (planta de un nivel o elevación).`);
  if (soloEjemplos && sinUsar.length) msg.push("El proyecto todavía usa los planos de ejemplo: cámbialos a «No usar» o elimínalos.");
  aviso.textContent = msg.join(" ");
  aviso.classList.toggle("oculto", !msg.length);
  const tb = $("#tabla-planos tbody");
  if (!planos.length) {
    tb.replaceChildren(h("tr", {}, h("td", { colspan: 5, class: "tenue" }, "Sin planos todavía.")));
    return;
  }
  tb.replaceChildren(...planos.flatMap(pl => [h("tr", {},
    h("td", {}, h("strong", {}, pl.nombre), pl.ejemplo ? h("span", { class: "chip", style: "margin-left:6px" }, "ejemplo") : null,
      h("div", { class: "tenue" }, `${(pl.bytes / 1024).toFixed(0)} KB` +
        (pl.desde_dwg ? ` · convertido desde ${pl.desde_dwg}` : "")),
      pl.error ? h("div", { class: "estado-error" }, pl.error) : null),
    h("td", {}, pl.vistas.length ? h("div", { class: "aviso-vistas" },
      h("strong", {}, `Este plano trae ${pl.vistas.length} vistas`),
      " de un mismo edificio. La planta genera los muros; los cortes y fachadas dan las alturas (niveles, alero, " +
      "cumbrera y pendiente) y la planta de techo da la forma de la cubierta. Ninguno crea estructura aparte.",
      h("div", {}, h("button", { class: "sec", style: "margin-top:6px", onclick: () => intentar(() =>
        guardarVistas(pl, Object.fromEntries(pl.vistas.map(v => [v.k, sugerenciaVista(v)])), "Vistas según la sugerencia"))
      }, "Usar la sugerencia"))) : (() => {
      const sel = h("select", {},
        h("option", { value: "no", selected: pl.uso === "no" }, "No usar"),
        ...dist.niveles.map(n => h("option", { value: `planta:${n.nombre}`, selected: pl.uso === `planta:${n.nombre}` }, `Planta de ${n.nombre}`)),
        h("option", { value: "elevacion", selected: pl.uso === "elevacion" }, "Elevación / corte (cotas)"));
      sel.addEventListener("change", () => intentar(async () => {
        // un nivel tiene una sola planta: si otro plano la tenía, pasa a "No usar"
        if (sel.value.startsWith("planta:"))
          for (const [k, v] of Object.entries(dist.usos)) if (v === sel.value && k !== pl.nombre) dist.usos[k] = "no";
        dist.usos[pl.nombre] = sel.value;
        await guardarDistribucion(`${pl.nombre}: ${sel.options[sel.selectedIndex].text}`);
      }));
      return h("div", {}, sel, pl.uso === "no" && pl.parece_elevacion
        ? h("div", { class: "tenue" }, "Tiene textos de nivel (NPT): parece una elevación") : null);
    })()),
    h("td", {}, h("div", { class: "chips" }, pl.capas.map(c => h("span", { class: "chip" }, c)))),
    h("td", {}, h("div", { class: "chips" }, pl.bloques.map(c => h("span", { class: "chip" }, c)))),
    h("td", {}, h("div", { class: "acciones" },
      h("button", { class: "sec", onclick: () => intentar(() => verPlano(pl.nombre)) }, "Ver"),
      h("a", { class: "boton sec", href: P(`/planos/${encodeURIComponent(pl.nombre)}/archivo`), download: pl.nombre,
               title: "Descargar el DXF con que trabaja la app" }, "DXF"),
      h("button", { class: "sec", onclick: () => intentar(async () => {
        if (!confirm(`¿Eliminar ${pl.nombre}?`)) return;
        await api("DELETE", P(`/planos/${encodeURIComponent(pl.nombre)}`));
        delete estado.cadCache[pl.nombre];
        await cargarPlanos();
      }) }, "Eliminar"))),
  ), filaVistas(pl)].filter(Boolean)));
}

// ---- vistas dentro de un plano (planta y fachadas lado a lado)
function sugerenciaVista(v) {
  if (v.sugerido === "planta") return `planta:${dist.niveles[0]?.nombre ?? "N1"}`;
  return ["elevacion", "techo"].includes(v.sugerido) ? v.sugerido : "no";
}

async function guardarVistas(pl, nuevos, nota) {
  delete dist.usos[pl.nombre];
  for (const v of pl.vistas) dist.usos[`${pl.nombre}#${v.k}`] = nuevos[v.k] ?? v.uso;
  // un nivel tiene planta de un solo plano: si otro plano la tenía, pasa a "No usar"
  const plantas = new Set(Object.values(nuevos).filter(u => u.startsWith("planta:")));
  for (const [k, u] of Object.entries(dist.usos))
    if (plantas.has(u) && k.split("#")[0] !== pl.nombre) dist.usos[k] = "no";
  await guardarDistribucion(nota);
}

const NOMBRE_SUGERENCIA = { planta: "planta", elevacion: "corte o fachada", techo: "planta de techo", no: "no usar" };

function filaVistas(pl) {
  if (!pl.vistas.length) return null;
  const tarjetas = pl.vistas.map(v => {
    const svg = s("svg", { class: "mini-vista" });
    geometriaCAD(pl.nombre).then(g => {
      const [x0, y0, x1, y1] = v.caja, m = 0.5;
      const d = [];
      for (const [a, b, c, e] of g.segmentos) {
        const mx = (a + c) / 2, my = (b + e) / 2;
        if (mx >= x0 - m && mx <= x1 + m && my >= y0 - m && my <= y1 + m) d.push(`M${a} ${b}L${c} ${e}`);
      }
      const raiz = s("g", { transform: "scale(1,-1)" });
      raiz.append(s("path", { d: d.join(""), stroke: "currentColor", "stroke-width": 1, fill: "none", "vector-effect": "non-scaling-stroke" }));
      svg.replaceChildren(raiz);
      encuadrar(svg, [[x0, y0], [x1, y1]]);
    }).catch(() => {});
    const sel = h("select", {},
      h("option", { value: "no", selected: v.uso === "no" }, "No usar"),
      ...dist.niveles.map(n => h("option", { value: `planta:${n.nombre}`, selected: v.uso === `planta:${n.nombre}` }, `Planta de ${n.nombre}`)),
      h("option", { value: "elevacion", selected: v.uso === "elevacion" }, "Corte / fachada (alturas)"),
      h("option", { value: "techo", selected: v.uso === "techo" }, "Planta de techo (cubierta)"));
    sel.addEventListener("change", () => intentar(() =>
      guardarVistas(pl, { [v.k]: sel.value }, `Vista ${v.k + 1}: ${sel.options[sel.selectedIndex].text}`)));
    const datos = [`${v.ancho} × ${v.alto} m`, v.aberturas ? `${v.aberturas} puertas/ventanas` : null,
      v.muros_m ? `${v.muros_m} m de muros` : null, v.textos_nivel ? `${v.textos_nivel} cotas de nivel` : null].filter(Boolean);
    return h("div", { class: "tarjeta-vista" + (v.uso !== "no" ? " usada" : "") }, svg,
      h("div", {}, h("strong", {}, v.titulo ? `${v.k + 1} · ${v.titulo}` : `Vista ${v.k + 1}`), " ",
        h("span", { class: "tenue" }, `parece ${v.tipo ?? NOMBRE_SUGERENCIA[v.sugerido]}`)),
      h("div", { class: "tenue" }, datos.join(" · ")), sel);
  });
  return h("tr", { class: "fila-vistas" }, h("td", { colspan: 5 }, h("div", { class: "vistas" }, tarjetas)));
}

async function subir(files) {
  const fd = new FormData();
  [...files].forEach(f => fd.append("archivos", f));
  await intentar(async () => {
    const r = await api("POST", P("/planos"), fd);
    const usos = Object.entries(r.asignados || {}).map(([n, u]) => `${n} → ${u === "elevacion" ? "elevación" : "planta de " + u.slice(7)}`);
    toast([`Subidos: ${r.guardados.join(", ")}.`, usos.length ? `Asignados: ${usos.join("; ")}.` : "",
      r.ejemplos_quitados?.length ? "Se quitaron los planos de ejemplo." : ""].join(" "));
    r.guardados.forEach(n => delete estado.cadCache[n]);
    await cargarPlanos();
    await cargarLegos();
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

// ------------------------------------------------------------------ legos
async function cargarLegos() {
  const [cat, sel] = await Promise.all([api("GET", "/api/catalogo"), api("GET", P("/legos"))]);
  estado.catalogo = cat;
  const porNombre = Object.fromEntries(cat.muros.map(m => [m.nombre, m]));
  const opciones = (lista, actual, auto) => [h("option", { value: "" }, auto),
    ...lista.map(t => h("option", { value: t.nombre, selected: t.nombre === actual }, t.nombre))];
  const espesor = n => porNombre[n] ? `${(porNombre[n].espesor_total * 100).toFixed(1)} cm` : "según reglas";

  const tb = $("#tabla-legos tbody");
  if (!sel.capas_muro.length) {
    tb.replaceChildren(h("tr", {}, h("td", { colspan: 3, class: "tenue" },
      "No hay capas de muros en los planos de este proyecto (revisa la pestaña Planos y proyecto.yaml).")));
  } else {
    tb.replaceChildren(...sel.capas_muro.map(capa => {
      const s = h("select", { "data-capa": capa }, opciones(cat.muros, sel.muros[capa], "Automático (según reglas)"));
      const td = h("td", { class: "tenue" }, espesor(sel.muros[capa]));
      s.addEventListener("change", () => { td.textContent = espesor(s.value); marcarUsados(); $("#legos-estado").textContent = "sin guardar"; });
      return h("tr", {}, h("td", {}, h("code", {}, capa)), h("td", {}, s), td);
    }));
  }
  // marcos de acero
  const sistemaDe = n => ({ momento: "de momento", arriostrado_x: "arriostrado en cruz" })[cat.marcos.find(m => m.nombre === n)?.sistema] ?? "según reglas";
  $("#bloque-marcos").classList.toggle("oculto", !sel.capas_marco.length);
  $("#tabla-marcos tbody").replaceChildren(...sel.capas_marco.map(capa => {
    const s = h("select", { "data-capa-marco": capa }, opciones(cat.marcos, sel.marcos[capa], "Automático (según reglas)"));
    const td = h("td", { class: "tenue" }, sistemaDe(sel.marcos[capa]));
    s.addEventListener("change", () => { td.textContent = sistemaDe(s.value); marcarUsados(); $("#legos-estado").textContent = "sin guardar"; });
    return h("tr", {}, h("td", {}, h("code", {}, capa)), h("td", {}, s), td);
  }));
  $("#sel-lego-losa").replaceChildren(...opciones(cat.losas, sel.losa, `Por defecto (${cat.por_defecto.losa ?? "—"})`));
  $("#sel-lego-cubierta").replaceChildren(...opciones(cat.cubiertas, sel.cubierta, `Por defecto (${cat.por_defecto.cubierta ?? "—"})`));
  [$("#sel-lego-losa"), $("#sel-lego-cubierta")].forEach(x => x.onchange = () => { marcarUsados(); $("#legos-estado").textContent = "sin guardar"; });
  $("#legos-estado").textContent = "";
  pintarCatalogo(cat);
}

function legosElegidos() {
  const muros = {};
  $$("#tabla-legos select").forEach(s => { muros[s.dataset.capa] = s.value || null; });
  const marcos = {};
  $$("#tabla-marcos select").forEach(s => { marcos[s.dataset.capaMarco] = s.value || null; });
  return { muros, marcos, losa: $("#sel-lego-losa").value || null, cubierta: $("#sel-lego-cubierta").value || null };
}

function pintarCatalogo(cat) {
  const tarjeta = (t, grupo) => {
    const total = t.capas.reduce((a, c) => a + c.espesor, 0) || 1;
    const e = t.entramado;
    return h("div", { class: "lego", "data-nombre": t.nombre },
      h("h5", {}, t.nombre),
      h("div", { class: "etiquetas" },
        h("span", { class: "etiqueta" }, `${(t.espesor_total * 100).toFixed(1)} cm`),
        t.estructural ? h("span", { class: "etiqueta" }, "estructural") : null,
        e ? h("span", { class: "etiqueta madera" }, `entramado ${e.material}`) : null),
      h("div", { class: "capas-barra", title: "Capas (de exterior a interior / de arriba hacia abajo)" },
        t.capas.map(c => h("span", { style: `flex:${Math.round(c.espesor * 1000)} 1 0;background:${c.color ? `rgb(${c.color})` : "#bbb"}`,
          title: `${c.material ?? "genérico"} · ${(c.espesor * 1000).toFixed(0)} mm` }))),
      h("ul", {}, t.capas.map(c => h("li", {}, `${c.material ?? "genérico"} · ${(c.espesor * 1000).toFixed(0)} mm`)),
        e ? h("li", {}, `Piezas ${(e.pie_derecho.b * 1000).toFixed(0)}×${(e.pie_derecho.h * 1000).toFixed(0)} mm · ` +
          `pies derechos @${(e.separacion_pies_derechos ?? 0.4) * 100} cm · cadenetas @${(e.separacion_horizontales ?? 0.4) * 100} cm`) : null,
        e?.revit ? h("li", {}, `Revit: ${e.revit.familia_vertical?.familia ?? "?"} / ${e.revit.familia_horizontal?.familia ?? "?"}`) : null),
      h("div", { class: "acciones-lego" },
        h("button", { class: "sec", onclick: () => abrirEditorLego(grupo, t, false) }, "Editar"),
        h("button", { class: "sec", onclick: () => abrirEditorLego(grupo, t, true) }, "Duplicar"),
        h("button", { class: "sec", onclick: () => intentar(async () => {
          if (!confirm(`¿Eliminar el lego "${t.nombre}"?`)) return;
          await api("DELETE", `/api/catalogo/${grupo}/${encodeURIComponent(t.nombre)}`);
          toast(`"${t.nombre}" eliminado`);
          await recargarTodo();
        }) }, "Eliminar")));
  };
  const grupo = (titulo, clave, lista) => h("div", { class: "grupo-legos" },
    h("div", { class: "tit" }, h("h4", {}, titulo),
      h("button", { class: "sec", onclick: () => abrirEditorLego(clave, null, false) }, "+ Nuevo")),
    h("div", { class: "legos" }, lista.map(t => tarjeta(t, clave))));
  const es = cat.estructura;
  $("#catalogo").replaceChildren(
    grupo("Muros y tabiques", "muros", cat.muros), grupo("Losas", "losas", cat.losas), grupo("Cubiertas", "cubiertas", cat.cubiertas),
    h("div", { class: "grupo-legos" },
      h("div", { class: "tit" }, h("h4", {}, "Marcos de acero"),
        h("button", { class: "sec", onclick: () => abrirEditorMarco(null, false) }, "+ Nuevo")),
      h("div", { class: "legos" }, cat.marcos.map(tarjetaMarco))),
    h("div", { class: "grupo-legos" }, h("h4", {}, "Estructura"),
      h("div", { class: "lego" }, h("h5", {}, `Sistema: ${es.sistema ?? "—"}`),
        h("ul", {},
          h("li", {}, `Material estructural: ${es.material ?? "—"}`),
          es.columnas ? h("li", {}, `Pilares ${es.columnas.forma ?? "rectangular"} ${es.columnas.seccion_defecto?.b * 100}×${es.columnas.seccion_defecto?.h * 100} cm · ${es.columnas.familia_revit ?? ""}`) : null,
          es.vigas ? h("li", {}, `Vigas ${es.vigas.forma ?? "rectangular"} ${es.vigas.seccion_defecto?.b * 100}×${es.vigas.seccion_defecto?.h * 100} cm · ${es.vigas.familia_revit ?? ""}`) : null))));
  marcarUsados();
}

function marcarUsados() {
  const el = legosElegidos();
  const usados = new Set(Object.values(el.muros).concat(Object.values(el.marcos)).filter(Boolean)
    .concat([$("#sel-lego-losa").value, $("#sel-lego-cubierta").value].filter(Boolean)));
  $$("#catalogo .lego").forEach(c => c.classList.toggle("usado", usados.has(c.dataset.nombre)));
}

// ------------------------------------------------------------------ estructuración por tramos
const TIPOLOGIA = {
  muro: { nombre: "Muro estructural", color: "#94a3b8" },
  tabique: { nombre: "Tabique (no estructural)", color: "#14b8a6" },
  momento: { nombre: "Marco de momento", color: "#f59e0b" },
  arriostrado: { nombre: "Marco arriostrado en X", color: "#2563eb" },
  ninguno: { nombre: "Sin elemento", color: "#cbd5e1" },
};
const est = { modelo: null, elecciones: {}, tramo: null, nivel: null, ocupado: false };
/** "tabique:TM-11 …" -> "Tabique (no estructural) · TM-11 …" */
const nombreTipologia = (v, tipo) => {
  const [base, ...resto] = (v || "").split(":");
  const lego = tipo ?? (resto.join(":") || null);
  return (TIPOLOGIA[base]?.nombre ?? base) + (lego ? ` · ${lego}` : "");
};
const chipTipo = (t, marca, tipo) => h("span", { class: "chip-tipo" }, h("i", { style: `background:${TIPOLOGIA[t]?.color}` }),
  nombreTipologia(t, tipo), marca ? " ✎" : "");

async function cargarEstructura() {
  if (!estado.catalogo) estado.catalogo = await api("GET", "/api/catalogo");    // legos de muro para los tramos
  const ultima = estado.corridas.find(c => c.ok);
  $("#est-vacio").classList.toggle("oculto", !!ultima);
  $("#est-contenido").classList.toggle("oculto", !ultima);
  if (!ultima) {
    const fallida = estado.corridas[0];
    $("#est-vacio").textContent = fallida
      ? `La última corrida tuvo errores, así que no hay estructuración que mostrar: ${fallida.error ?? "revisa la pestaña Resultado"}`
      : "Presiona ▶ Ejecutar para que la app proponga la estructuración.";
    return;
  }
  const [r, el] = await Promise.all([api("GET", P(`/corridas/${ultima.n}`)), api("GET", P("/tramos"))]);
  est.modelo = r.modelo;
  est.elecciones = el.elecciones || {};
  const tramos = est.modelo.tramos || [];
  if (!tramos.length) {
    $("#est-contenido").classList.add("oculto");
    $("#est-vacio").classList.remove("oculto");
    const m = est.modelo;
    $("#est-vacio").textContent = !m.muros.length
      ? `La corrida ${ultima.n} no encontró muros ni ejes en tus plantas. Ve a Planos: revisa que cada planta esté asignada a un ` +
        "nivel y, en «Capas de tu plano», marca cuáles son tus capas de muros y de ejes. Luego presiona ▶ Ejecutar."
      : !m.ejes.length
        ? "No hay ejes: marca tu capa de ejes en Planos → «Capas de tu plano» (o activa la generación desde muros) y ejecuta de nuevo."
        : "Los ejes encontrados no se cruzan entre sí, por lo que no forman tramos. Revisa la capa de ejes en Planos.";
    return;
  }
  const niveles = [...new Set(tramos.flatMap(t => Object.keys(t.niveles)))];
  if (!niveles.includes(est.nivel)) est.nivel = niveles[0];
  $("#est-nivel").replaceChildren(...niveles.map(n => h("option", { value: n, selected: n === est.nivel }, n)));
  $("#est-leyenda").replaceChildren(...Object.keys(TIPOLOGIA).map(t => chipTipo(t)));
  if (!tramos.some(t => t.id === est.tramo)) est.tramo = null;
  pintarEstructura();
}
$("#est-nivel").addEventListener("change", e => { est.nivel = e.target.value; pintarEstructura(); });

function infoTramo(t, nivel) { return t.niveles[nivel]; }
const elegido = (id, nivel) => est.elecciones[`${id}@${nivel}`] ?? est.elecciones[id];

function pintarEstructura() {
  pintarPlantaEst();
  pintarDetalleEst();
  pintarElevacionEst();
  pintarTablaEst();
}

function pintarPlantaEst() {
  const m = est.modelo, nivel = est.nivel, svg = $("#est-planta");
  const raiz = s("g", { transform: "scale(1,-1)" });
  const puntos = [];
  const texto = (x, y, txt, extra = {}) => {
    const t = s("text", { x, y: -y, transform: "scale(1,-1)", "font-size": 0.35, "text-anchor": "middle",
      "dominant-baseline": "middle", fill: "var(--texto)", ...extra });
    t.textContent = txt; return t;
  };
  for (const w of m.muros.filter(x => x.nivel === nivel)) {
    const [x1, y1] = w.inicio, [x2, y2] = w.fin, L = Math.hypot(x2 - x1, y2 - y1) || 1;
    const nx = -(y2 - y1) / L * w.espesor / 2, ny = (x2 - x1) / L * w.espesor / 2;
    raiz.append(s("polygon", { points: [[x1 + nx, y1 + ny], [x2 + nx, y2 + ny], [x2 - nx, y2 - ny], [x1 - nx, y1 - ny]].map(p => p.join(",")).join(" "),
      fill: "var(--cad)", "fill-opacity": 0.35 }));
    puntos.push(w.inicio, w.fin);
  }
  for (const e of m.ejes) {
    raiz.append(s("line", { x1: e.inicio[0], y1: e.inicio[1], x2: e.fin[0], y2: e.fin[1], stroke: "var(--tenue)",
      "stroke-dasharray": "12 3 2 3", "stroke-width": 0.8, "vector-effect": "non-scaling-stroke" }));
    raiz.append(s("circle", { cx: e.inicio[0], cy: e.inicio[1], r: 0.35, fill: "var(--superficie)", stroke: "var(--tenue)", "vector-effect": "non-scaling-stroke" }));
    raiz.append(texto(e.inicio[0], e.inicio[1], e.nombre, { "font-weight": 600 }));
    puntos.push(e.inicio, e.fin);
  }
  for (const t of m.tramos) {
    const inf = infoTramo(t, nivel); if (!inf) continue;
    const sel = t.id === est.tramo;
    const linea = s("line", { x1: t.inicio[0], y1: t.inicio[1], x2: t.fin[0], y2: t.fin[1],
      stroke: TIPOLOGIA[inf.final].color, "stroke-width": sel ? 11 : 7, "stroke-linecap": "butt",
      "stroke-dasharray": inf.final === "ninguno" ? "6 4" : "", "vector-effect": "non-scaling-stroke", style: "cursor:pointer" });
    const tit = s("title"); tit.textContent = `${t.id} · ${nombreTipologia(inf.final, inf.tipo_muro)}\n${inf.motivo}`; linea.append(tit);
    linea.addEventListener("click", ev => { ev.stopPropagation(); est.tramo = t.id; pintarEstructura(); });
    raiz.append(linea);
    if (sel) raiz.append(s("line", { x1: t.inicio[0], y1: t.inicio[1], x2: t.fin[0], y2: t.fin[1], stroke: "var(--texto)",
      "stroke-width": 2, "stroke-dasharray": "2 3", "vector-effect": "non-scaling-stroke", "pointer-events": "none" }));
    if (elegido(t.id, nivel)) {
      const mx = (t.inicio[0] + t.fin[0]) / 2, my = (t.inicio[1] + t.fin[1]) / 2;
      raiz.append(texto(mx, my + 0.45, "✎", { "font-size": 0.45, "pointer-events": "none" }));
    }
  }
  // marcos dibujados en el CAD (no vienen de tramos): solo como referencia
  for (const f of (m.marcos || []).filter(x => x.nivel === nivel && x.origen === "plano CAD")) {
    raiz.append(s("line", { x1: f.inicio[0], y1: f.inicio[1], x2: f.fin[0], y2: f.fin[1], stroke: TIPOLOGIA[f.sistema === "momento" ? "momento" : "arriostrado"].color,
      "stroke-width": 4, "stroke-opacity": 0.5, "vector-effect": "non-scaling-stroke" }));
    puntos.push(f.inicio, f.fin);
  }
  svg.replaceChildren(raiz);
  if (svg.dataset.clave !== `${m.proyecto}|${nivel}` && puntos.length) {
    encuadrar(svg, puntos);
    svg.dataset.clave = `${m.proyecto}|${nivel}`;
  }
}

function opcionesTramo(t, actual, conIgual, propuestaTxt) {
  const elegido = actual ?? (conIgual ? "" : "auto");
  const op = (v, n) => h("option", { value: v, selected: v === elegido }, n);
  const ops = conIgual ? [op("", "Igual que todos los pisos")] : [op("auto", `Automático (propuesta: ${propuestaTxt})`)];
  const legos = (estado.catalogo?.muros || []).map(m => m.nombre);
  const grupo = (base, titulo) => h("optgroup", { label: titulo }, ...legos.map(n => op(`${base}:${n}`, n)));
  return [...ops, ...Object.entries(TIPOLOGIA).map(([k, v]) => op(k, v.nombre)),
    ...(legos.length ? [grupo("tabique", "Tabique con el lego…"), grupo("muro", "Muro estructural con el lego…")] : [])];
}

function pintarDetalleEst() {
  const box = $("#est-detalle");
  const t = est.modelo.tramos.find(x => x.id === est.tramo);
  if (!t) { box.replaceChildren(h("p", { class: "tenue" }, "Selecciona un tramo en la planta o en la tabla.")); return; }
  const niveles = Object.keys(t.niveles);
  const propuestas = [...new Set(niveles.map(n => nombreTipologia(t.niveles[n].propuesta)))].join(" / ");
  const todos = h("select", { disabled: est.ocupado }, opcionesTramo(t, est.elecciones[t.id], false, propuestas));
  todos.addEventListener("change", () => {
    if (todos.value === "auto") delete est.elecciones[t.id]; else est.elecciones[t.id] = todos.value;
    aplicarTramos(`Tramo ${t.id}: ${todos.value === "auto" ? "automático" : nombreTipologia(todos.value)}`);
  });
  // tramo sin muro en el plano: agregar uno con un lego del catálogo (todos los pisos)
  const sinMuro = niveles.every(n => (t.niveles[n].cobertura_muros ?? 0) < 0.3 && !["muro", "tabique"].includes(t.niveles[n].final));
  let agregar = null;
  if (sinMuro && (estado.catalogo?.muros || []).length) {
    const lego = h("select", {}, estado.catalogo.muros.map(m => h("option", { value: m.nombre }, m.nombre)));
    const caracter = h("select", {}, h("option", { value: "muro" }, "Muro estructural"), h("option", { value: "tabique" }, "Tabique (no estructural)"));
    const sinc = () => { const m = estado.catalogo.muros.find(x => x.nombre === lego.value); caracter.value = m?.estructural ? "muro" : "tabique"; };
    lego.addEventListener("change", sinc); sinc();
    agregar = h("div", { class: "agregar-muro" },
      h("strong", {}, "＋ Agregar muro en este tramo"),
      h("div", { class: "tenue" }, "En el plano no hay muro aquí. Elige el lego y se crea a lo largo del tramo, en todos los pisos."),
      lego, caracter,
      h("button", { class: "pri", disabled: est.ocupado, onclick: () => {
        est.elecciones[t.id] = `${caracter.value}:${lego.value}`;
        aplicarTramos(`Tramo ${t.id}: se agrega ${nombreTipologia(est.elecciones[t.id])}`);
      } }, "Agregar muro"));
  }
  box.replaceChildren(
    h("h4", {}, `Tramo ${t.id}`),
    h("div", { class: "tenue" }, `Eje ${t.eje} entre ${t.entre[0]} y ${t.entre[1]} · ${t.largo.toFixed(2)} m · dirección ${t.direccion}`),
    agregar,
    h("label", { style: "display:flex;flex-direction:column;gap:4px;margin-top:10px" }, h("strong", {}, "Todos los pisos"), todos),
    ...niveles.map(n => {
      const inf = t.niveles[n];
      const sel = h("select", { disabled: est.ocupado }, opcionesTramo(t, est.elecciones[`${t.id}@${n}`], true));
      sel.addEventListener("change", () => {
        const k = `${t.id}@${n}`;
        if (!sel.value) delete est.elecciones[k]; else est.elecciones[k] = sel.value;
        aplicarTramos(`Tramo ${t.id} en ${n}: ${sel.value ? nombreTipologia(sel.value) : "igual que todos"}`);
      });
      return h("div", { class: "piso" },
        h("div", {}, h("strong", {}, n), " · resultado: ", chipTipo(inf.final, inf.eleccion !== "auto", inf.tipo_muro)),
        h("div", { class: "tenue" }, `Propuesta: ${nombreTipologia(inf.propuesta)}. ${inf.motivo}.`),
        h("label", {}, "Solo este piso", sel));
    }));
}

function pintarElevacionEst() {
  const m = est.modelo, svg = $("#est-elevacion");
  const t0 = m.tramos.find(x => x.id === est.tramo);
  if (!t0) { svg.replaceChildren(); $("#est-eje-tit").textContent = "(selecciona un tramo)"; return; }
  $("#est-eje-tit").textContent = t0.eje;
  const delEje = m.tramos.filter(x => x.eje === t0.eje);
  const ux = (t0.fin[0] - t0.inicio[0]) / t0.largo, uy = (t0.fin[1] - t0.inicio[1]) / t0.largo;
  const origen = delEje.reduce((a, x) => Math.min(a, (x.inicio[0] - t0.inicio[0]) * ux + (x.inicio[1] - t0.inicio[1]) * uy), 0);
  const pos = p => (p[0] - t0.inicio[0]) * ux + (p[1] - t0.inicio[1]) * uy - origen;
  const cota = Object.fromEntries(m.niveles.map(n => [n.nombre, n.cota]));
  const orden = m.niveles.map(n => n.nombre);
  const g = s("g", {});
  let maxX = 0, maxZ = 0;
  const linea = (x1, z1, x2, z2, color, ancho, extra = {}) => g.append(s("line", { x1, y1: -z1, x2, y2: -z2, stroke: color,
    "stroke-width": ancho, "vector-effect": "non-scaling-stroke", ...extra }));
  for (const t of delEje) {
    const a = pos(t.inicio), b = pos(t.fin), x0 = Math.min(a, b), x1 = Math.max(a, b);
    maxX = Math.max(maxX, x1);
    for (const [n, inf] of Object.entries(t.niveles)) {
      const sup = orden[orden.indexOf(n) + 1];
      if (sup === undefined) continue;
      const z0 = cota[n], z1 = cota[sup]; maxZ = Math.max(maxZ, z1);
      const c = TIPOLOGIA[inf.final].color;
      if (inf.final === "muro") g.append(s("rect", { x: x0, y: -z1, width: x1 - x0, height: z1 - z0, fill: c, "fill-opacity": 0.55 }));
      if (inf.final === "tabique") {
        g.append(s("rect", { x: x0, y: -z1, width: x1 - x0, height: z1 - z0, fill: c, "fill-opacity": 0.18 }));
        // pies derechos cada 40 cm (indicativo)
        for (let x = x0 + 0.4; x < x1 - 0.05; x += 0.4) linea(x, z0, x, z1, c, 1);
      }
      if (inf.final === "momento" || inf.final === "arriostrado") {
        linea(x0, z0, x0, z1, "var(--texto)", 3); linea(x1, z0, x1, z1, "var(--texto)", 3); linea(x0, z1, x1, z1, "var(--texto)", 3);
        if (inf.final === "arriostrado") { linea(x0, z0, x1, z1, c, 2); linea(x1, z0, x0, z1, c, 2); }
        else for (const x of [x0, x1]) g.append(s("rect", { x: x - 0.12, y: -z1, width: 0.24, height: 0.24, fill: c }));
      }
      if (inf.final === "ninguno") g.append(s("rect", { x: x0, y: -z1, width: x1 - x0, height: z1 - z0, fill: "none",
        stroke: c, "stroke-dasharray": "4 3", "vector-effect": "non-scaling-stroke" }));
      if (t.id === est.tramo) g.append(s("rect", { x: x0, y: -z1, width: x1 - x0, height: z1 - z0, fill: "none",
        stroke: "var(--acento)", "stroke-width": 2.5, "vector-effect": "non-scaling-stroke" }));
      const r = s("rect", { x: x0, y: -z1, width: x1 - x0, height: z1 - z0, fill: "transparent", style: "cursor:pointer" });
      const tit = s("title"); tit.textContent = `${t.id} · ${n}: ${nombreTipologia(inf.final, inf.tipo_muro)}\n${inf.motivo}`; r.append(tit);
      r.addEventListener("click", () => { est.tramo = t.id; pintarEstructura(); });
      g.append(r);
    }
    for (const [x, nombre] of [[pos(t.inicio), t.entre[0]], [pos(t.fin), t.entre[1]]]) {
      const tx = s("text", { x, y: 0.55, "font-size": 0.32, "text-anchor": "middle", fill: "var(--tenue)" });
      tx.textContent = nombre; g.append(tx);
    }
  }
  for (const n of m.niveles) {
    if (n.cota > maxZ + 1e-6) continue;
    linea(-0.3, n.cota, maxX + 0.3, n.cota, "var(--tenue)", 0.8, { "stroke-dasharray": "4 3" });
    const tx = s("text", { x: -0.45, y: -n.cota, "font-size": 0.3, "text-anchor": "end", "dominant-baseline": "middle", fill: "var(--tenue)" });
    tx.textContent = `${n.nombre} ${n.cota.toFixed(2)}`; g.append(tx);
  }
  svg.replaceChildren(g);
  svg.setAttribute("viewBox", `${-2.2} ${-maxZ - 0.6} ${maxX + 3} ${maxZ + 1.4}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
}

function pintarTablaEst() {
  const m = est.modelo;
  const niveles = [...new Set(m.tramos.flatMap(t => Object.keys(t.niveles)))];
  $("#est-tabla thead").replaceChildren(h("tr", {}, h("th", {}, "Tramo"), h("th", {}, "Largo"), h("th", {}, "Dir."),
    ...niveles.map(n => h("th", {}, n)), h("th", {}, `Motivo (${est.nivel})`)));
  $("#est-tabla tbody").replaceChildren(...m.tramos.map(t => h("tr", { class: t.id === est.tramo ? "sel" : "",
      onclick: () => { est.tramo = t.id; pintarEstructura(); } },
    h("td", {}, h("strong", {}, t.id)), h("td", {}, `${t.largo.toFixed(2)} m`), h("td", {}, t.direccion),
    ...niveles.map(n => h("td", {}, t.niveles[n] ? chipTipo(t.niveles[n].final, t.niveles[n].eleccion !== "auto", t.niveles[n].tipo_muro) : "—")),
    h("td", { class: "tenue" }, t.niveles[est.nivel]?.motivo ?? ""))));
}

/** Guarda las elecciones y recalcula el modelo (una corrida nueva con nota). */
async function aplicarTramos(nota) {
  est.ocupado = true;
  $("#est-estado").textContent = "Recalculando…";
  pintarDetalleEst();
  try {
    const r = await api("PUT", P("/tramos"), { elecciones: est.elecciones });
    mostrarErroresEntradas(r.errores);
    const c = await api("POST", P("/corridas"), { notas: nota });
    await cargarCorridas(c.n);
    await cargarEntradas();
    toast(c.ok ? `${nota} · corrida ${c.n}` : `Corrida ${c.n} con errores`, !c.ok);
  } catch (e) {
    toast(e.message, true);
  } finally {
    est.ocupado = false;
    $("#est-estado").textContent = "";
    await intentar(cargarEstructura);
  }
}

// ------------------------------------------------------------------ marcos de acero
const ROLES_MARCO = { columna: "Columna", viga: "Viga", diagonal: "Diagonal" };
const FORMAS = { W: "W / I (doble T)", C: "C (canal)", L: "L (ángulo)", tubo: "Tubo", rectangular: "Rectangular", circular: "Circular" };

/** Esquema del marco (2 vanos): columnas, vigas, nudos de momento o cruz de diagonales. */
function esquemaMarco(svg, sistema, techo = "horizontal", pilar = true) {
  const S = (t, a) => { const e = document.createElementNS("http://www.w3.org/2000/svg", t); for (const k in a) e.setAttribute(k, a[k]); return e; };
  const dosAguas = techo === "dos_aguas";
  // dos aguas sin pilar central: un solo vano libre bajo la cumbrera
  const xs = dosAguas && !pilar ? [15, 205] : [15, 110, 205], y0 = 98;
  const ys = dosAguas ? (pilar ? [44, 12, 44] : [44, 44]) : [14, 14, 14];
  const hijos = [S("line", { x1: 5, y1: y0 + 4, x2: 215, y2: y0 + 4, class: "suelo" })];
  if (sistema === "arriostrado_x")
    for (let i = 0; i < xs.length - 1; i++) {
      hijos.push(S("line", { x1: xs[i], y1: y0, x2: xs[i + 1], y2: ys[i + 1], class: "dia" }));
      hijos.push(S("line", { x1: xs[i + 1], y1: y0, x2: xs[i], y2: ys[i], class: "dia" }));
    }
  xs.forEach((x, i) => hijos.push(S("line", { x1: x, y1: y0, x2: x, y2: ys[i], class: "col" })));
  if (dosAguas && !pilar) {        // viga a dos aguas con cumbrera sin columna
    hijos.push(S("line", { x1: xs[0], y1: ys[0], x2: 110, y2: 12, class: "vig" }));
    hijos.push(S("line", { x1: 110, y1: 12, x2: xs[1], y2: ys[1], class: "vig" }));
  } else for (let i = 0; i < xs.length - 1; i++) hijos.push(S("line", { x1: xs[i], y1: ys[i], x2: xs[i + 1], y2: ys[i + 1], class: "vig" }));
  if (sistema === "momento") xs.forEach((x, i) => hijos.push(S("rect", { x: x - 6, y: ys[i] - 1, width: 12, height: 12, class: "nudo" })));
  else xs.forEach((x, i) => hijos.push(S("circle", { cx: x, cy: ys[i] + 5, r: 4, fill: "var(--superficie)", stroke: "var(--texto)", "stroke-width": 2 })));
  svg.replaceChildren(...hijos);
  return svg;
}

function tarjetaMarco(t) {
  const svg = esquemaMarco(document.createElementNS("http://www.w3.org/2000/svg", "svg"), t.sistema);
  svg.setAttribute("viewBox", "0 0 220 110"); svg.setAttribute("class", "esquema-marco");
  const roles = ["columna", "viga"].concat(t.sistema === "arriostrado_x" ? ["diagonal"] : []);
  return h("div", { class: "lego", "data-nombre": t.nombre },
    h("h5", {}, t.nombre),
    h("div", { class: "etiquetas" },
      h("span", { class: "etiqueta" }, t.sistema === "momento" ? "marco de momento" : "arriostrado en X"),
      h("span", { class: "etiqueta" }, t.material),
      t.vano_maximo ? h("span", { class: "etiqueta" }, `vano máx. ${t.vano_maximo} m`) : null,
      t.techo === "dos_aguas" ? h("span", { class: "etiqueta" }, `viga a dos aguas${t.pendiente ? ` ${t.pendiente}°` : ""}` +
        (t.pilar_central ? " · con pilar central" : " · sin pilar central")) : null),
    svg,
    h("ul", {}, roles.map(r => {
      const p = t[r] || {};
      return h("li", {}, `${ROLES_MARCO[r]}: ${p.perfil || FORMAS[p.forma] || p.forma} · ${Math.round(p.b * 1000)}×${Math.round(p.h * 1000)} mm` +
        (p.familia ? ` · ${p.familia}` : ""));
    })),
    h("div", { class: "acciones-lego" },
      h("button", { class: "sec", onclick: () => abrirEditorMarco(t, false) }, "Editar"),
      h("button", { class: "sec", onclick: () => abrirEditorMarco(t, true) }, "Duplicar"),
      h("button", { class: "sec", onclick: () => intentar(async () => {
        if (!confirm(`¿Eliminar el lego "${t.nombre}"?`)) return;
        await api("DELETE", `/api/catalogo/marcos/${encodeURIComponent(t.nombre)}`);
        toast(`"${t.nombre}" eliminado`);
        await recargarTodo();
      }) }, "Eliminar")));
}

const editorMarco = { original: null };

function abrirEditorMarco(t, duplicar) {
  editorMarco.original = t && !duplicar ? t.nombre : null;
  $("#marco-titulo").textContent = (t ? (duplicar ? "Duplicar" : "Editar") : "Nuevo") + " marco de acero";
  $("#marco-nombre").value = t ? (duplicar ? `${t.nombre} (copia)` : t.nombre) : "";
  $("#marco-sistema").value = t?.sistema ?? "momento";
  const metales = estado.catalogo.materiales.filter(m => /metal|acero/i.test(m.clase ?? ""));
  $("#marco-material").replaceChildren(...(metales.length ? metales : estado.catalogo.materiales)
    .map(m => h("option", { value: m.nombre, selected: m.nombre === t?.material }, m.nombre)));
  $("#marco-vano").value = t?.vano_maximo ?? "";
  $("#marco-techo").value = t?.techo ?? "horizontal";
  $("#marco-pendiente").value = t?.pendiente ?? "";
  $("#marco-pilar").value = t?.pilar_central ? "si" : "no";
  const base = { columna: { forma: "W", b: 0.2, h: 0.2 }, viga: { forma: "W", b: 0.1, h: 0.3 }, diagonal: { forma: "L", b: 0.075, h: 0.075 } };
  $("#marco-perfiles tbody").replaceChildren(...Object.keys(ROLES_MARCO).map(rol => {
    const p = t?.[rol] ?? base[rol];
    return h("tr", { "data-rol": rol },
      h("td", {}, h("strong", {}, ROLES_MARCO[rol])),
      h("td", {}, h("input", { "data-k": "perfil", value: p.perfil ?? "", placeholder: "ej. W310X21" })),
      h("td", {}, h("select", { "data-k": "forma" }, Object.entries(FORMAS).map(([v, n]) => h("option", { value: v, selected: v === p.forma }, n)))),
      h("td", {}, h("input", { "data-k": "b", type: "number", step: "1", min: "1", value: Math.round((p.b ?? 0.1) * 1000) })),
      h("td", {}, h("input", { "data-k": "h", type: "number", step: "1", min: "1", value: Math.round((p.h ?? 0.1) * 1000) })),
      h("td", {}, h("input", { "data-k": "familia", value: p.familia ?? "", placeholder: "opcional" })));
  }));
  // al cambiar la forma de un perfil, la familia anterior ya no corresponde
  $$('#marco-perfiles select[data-k="forma"]').forEach(sel => sel.addEventListener("change", () => {
    sel.closest("tr").querySelector('[data-k="familia"]').value = "";
  }));
  actualizarSistemaMarco();
  $("#marco-error").classList.add("oculto");
  $("#dlg-marco").showModal();
}

function actualizarSistemaMarco() {
  const sis = $("#marco-sistema").value, techo = $("#marco-techo").value, pilar = $("#marco-pilar").value === "si";
  esquemaMarco($("#marco-esquema"), sis, techo, pilar);
  $("#marco-pendiente").closest("label").classList.toggle("oculto", techo !== "dos_aguas");
  $("#marco-pilar").closest("label").classList.toggle("oculto", techo !== "dos_aguas");
  const fila = $('#marco-perfiles tr[data-rol="diagonal"]');
  if (fila) fila.classList.toggle("oculto", sis !== "arriostrado_x");
}
$("#marco-sistema").addEventListener("change", actualizarSistemaMarco);
$("#marco-techo").addEventListener("change", actualizarSistemaMarco);
$("#marco-pilar").addEventListener("change", actualizarSistemaMarco);

$("#form-marco").addEventListener("submit", ev => {
  if (ev.submitter?.value !== "ok") return;
  ev.preventDefault();
  const datos = { nombre: $("#marco-nombre").value.trim(), sistema: $("#marco-sistema").value,
                  material: $("#marco-material").value, vano_maximo: +$("#marco-vano").value || null,
                  techo: $("#marco-techo").value, pendiente: +$("#marco-pendiente").value || null,
                  pilar_central: $("#marco-pilar").value === "si" };
  $$("#marco-perfiles tbody tr").forEach(tr => {
    if (tr.classList.contains("oculto")) return;
    const v = k => tr.querySelector(`[data-k="${k}"]`).value;
    datos[tr.dataset.rol] = { perfil: v("perfil").trim(), forma: v("forma"), b: +v("b") / 1000, h: +v("h") / 1000, familia: v("familia").trim() };
  });
  (async () => {
    try {
      if (editorMarco.original) await api("PUT", `/api/catalogo/marcos/${encodeURIComponent(editorMarco.original)}`, datos);
      else await api("POST", "/api/catalogo/marcos", datos);
      $("#dlg-marco").close();
      toast(`Lego "${datos.nombre}" guardado`);
      await recargarTodo();
    } catch (e) {
      $("#marco-error").textContent = e.message;
      $("#marco-error").classList.remove("oculto");
    }
  })();
});

// ------------------------------------------------------------------ editor de legos
const FUNCIONES = { terminacion1: "Terminación exterior / superior", sustrato: "Sustrato", aislacion: "Aislación",
  membrana: "Membrana", estructura: "Estructura (núcleo)", terminacion2: "Terminación interior / inferior" };
const lego = { grupo: null, original: null };

async function recargarTodo() {
  await cargarLegos();
  await cargarEntradas();
}

function filaCapa(c = { funcion: "estructura", material: "", espesor: 0.1 }) {
  const mats = estado.catalogo.materiales;
  const fn = h("select", {}, Object.entries(FUNCIONES).map(([v, t]) => h("option", { value: v, selected: v === c.funcion }, t)));
  const mt = h("select", {}, mats.map(m => h("option", { value: m.nombre, selected: m.nombre === c.material }, `${m.nombre} (${m.clase ?? "—"})`)));
  const es = h("input", { type: "number", step: "1", min: "0", value: Math.round(c.espesor * 1000) });
  const tr = h("tr", {}, h("td", {}, fn), h("td", {}, mt), h("td", {}, es), h("td", {},
    h("button", { type: "button", class: "sec", title: "Subir", onclick: () => { tr.previousElementSibling?.before(tr); vistaPreviaLego(); } }, "↑"), " ",
    h("button", { type: "button", class: "sec", title: "Bajar", onclick: () => { tr.nextElementSibling?.after(tr); vistaPreviaLego(); } }, "↓"), " ",
    h("button", { type: "button", class: "sec", title: "Quitar", onclick: () => { tr.remove(); vistaPreviaLego(); } }, "✕")));
  [fn, mt, es].forEach(x => x.addEventListener("input", vistaPreviaLego));
  return tr;
}

function capasDelEditor() {
  return $$("#lego-capas tbody tr").map(tr => {
    const [fn, mt] = tr.querySelectorAll("select");
    return { funcion: fn.value, material: mt.value, espesor: (+tr.querySelector("input").value || 0) / 1000 };
  });
}

function vistaPreviaLego() {
  const capas = capasDelEditor();
  const color = Object.fromEntries(estado.catalogo.materiales.map(m => [m.nombre, m.color]));
  $("#lego-total").textContent = Math.round(capas.reduce((a, c) => a + c.espesor, 0) * 1000);
  $("#lego-barra").replaceChildren(...capas.map(c => h("span", {
    style: `flex:${Math.max(1, Math.round(c.espesor * 1000))} 1 0;background:${color[c.material] ? `rgb(${color[c.material]})` : "#bbb"}`,
    title: `${c.material} · ${Math.round(c.espesor * 1000)} mm` })));
}

function abrirEditorLego(grupo, t, duplicar) {
  lego.grupo = grupo;
  lego.original = t && !duplicar ? t.nombre : null;
  const titulo = { muros: "muro / tabique", losas: "losa", cubiertas: "cubierta" }[grupo];
  $("#lego-titulo").textContent = (t ? (duplicar ? "Duplicar " : "Editar ") : "Nuevo ") + titulo;
  $("#lego-orden").textContent = grupo === "muros" ? "(de exterior a interior)" : "(de arriba hacia abajo)";
  $("#lego-nombre").value = t ? (duplicar ? `${t.nombre} (copia)` : t.nombre) : "";
  $("#lego-estructural").checked = !!t?.estructural;
  $("#lego-capas tbody").replaceChildren(...(t?.capas?.length ? t.capas : [{ funcion: "estructura", material: estado.catalogo.materiales[0]?.nombre, espesor: 0.1 }]).map(filaCapa));
  // entramado (solo muros)
  $("#lego-entramado-box").classList.toggle("oculto", grupo !== "muros");
  const e = t?.entramado;
  $("#lego-entramado").checked = !!e;
  const maderas = estado.catalogo.materiales.filter(m => /mader/i.test(m.clase ?? ""));
  $("#ent-material").replaceChildren(...(maderas.length ? maderas : estado.catalogo.materiales)
    .map(m => h("option", { value: m.nombre, selected: m.nombre === e?.material }, m.nombre)));
  $("#ent-b").value = Math.round((e?.pie_derecho?.b ?? 0.05) * 1000);
  $("#ent-h").value = Math.round((e?.pie_derecho?.h ?? 0.076) * 1000);
  $("#ent-sv").value = Math.round((e?.separacion_pies_derechos ?? 0.4) * 100);
  $("#ent-sh").value = Math.round((e?.separacion_horizontales ?? 0.4) * 100);
  $("#ent-soleras").checked = e?.soleras ?? true;
  $("#ent-refuerzo").checked = e?.refuerzo_vanos ?? true;
  $("#ent-fv").value = e?.revit?.familia_vertical?.familia ?? "Timber-Column";
  $("#ent-fvt").value = e?.revit?.familia_vertical?.tipo ?? "2x3";
  $("#ent-fh").value = e?.revit?.familia_horizontal?.familia ?? "Dimension Lumber";
  $("#ent-fht").value = e?.revit?.familia_horizontal?.tipo ?? "2x3";
  $("#lego-entramado-campos").classList.toggle("oculto", !e);
  $("#lego-error").classList.add("oculto");
  vistaPreviaLego();
  $("#dlg-lego").showModal();
}

$("#lego-entramado").addEventListener("change", e => $("#lego-entramado-campos").classList.toggle("oculto", !e.target.checked));
// la capa de estructura (núcleo) acompaña la profundidad de las piezas del entramado
$("#ent-h").addEventListener("input", () => {
  if (!$("#lego-entramado").checked) return;
  const fila = $$("#lego-capas tbody tr").find(tr => tr.querySelector("select").value === "estructura");
  if (fila) { fila.querySelector("input").value = $("#ent-h").value; vistaPreviaLego(); }
});
$("#lego-agregar-capa").addEventListener("click", () => {
  $("#lego-capas tbody").append(filaCapa({ funcion: "terminacion2", material: estado.catalogo.materiales[0]?.nombre, espesor: 0.015 }));
  vistaPreviaLego();
});
$("#lego-nuevo-material").addEventListener("click", () => $("#dlg-material").showModal());
$("#dlg-material").addEventListener("close", () => {
  if ($("#dlg-material").returnValue !== "ok" || !$("#mat-nombre").value.trim()) return;
  const hex = $("#mat-color").value;
  const color = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16));
  intentar(async () => {
    await api("POST", "/api/materiales", { nombre: $("#mat-nombre").value.trim(), clase: $("#mat-clase").value, color });
    estado.catalogo = await api("GET", "/api/catalogo");
    // refrescar las listas de material del editor sin perder lo escrito
    const capas = capasDelEditor();
    $("#lego-capas tbody").replaceChildren(...capas.map(filaCapa));
    const actual = $("#ent-material").value;
    const maderas = estado.catalogo.materiales.filter(m => /mader/i.test(m.clase ?? ""));
    $("#ent-material").replaceChildren(...maderas.map(m => h("option", { value: m.nombre, selected: m.nombre === actual }, m.nombre)));
    vistaPreviaLego();
    toast(`Material "${$("#mat-nombre").value.trim()}" creado`);
    $("#mat-nombre").value = "";
  });
});

$("#form-lego").addEventListener("submit", ev => {
  if (ev.submitter?.value !== "ok") return;
  ev.preventDefault();                                  // el diálogo se cierra solo si se guardó
  const datos = { nombre: $("#lego-nombre").value.trim(), estructural: $("#lego-estructural").checked, capas: capasDelEditor() };
  if (lego.grupo === "muros" && $("#lego-entramado").checked) {
    datos.entramado = {
      material: $("#ent-material").value,
      pie_derecho: { b: +$("#ent-b").value / 1000, h: +$("#ent-h").value / 1000 },
      separacion_pies_derechos: +$("#ent-sv").value / 100, separacion_horizontales: +$("#ent-sh").value / 100,
      soleras: $("#ent-soleras").checked, refuerzo_vanos: $("#ent-refuerzo").checked,
      revit: { familia_vertical: { familia: $("#ent-fv").value.trim(), tipo: $("#ent-fvt").value.trim() },
               familia_horizontal: { familia: $("#ent-fh").value.trim(), tipo: $("#ent-fht").value.trim() } },
    };
  }
  (async () => {
    try {
      if (lego.original) await api("PUT", `/api/catalogo/${lego.grupo}/${encodeURIComponent(lego.original)}`, datos);
      else await api("POST", `/api/catalogo/${lego.grupo}`, datos);
      $("#dlg-lego").close();
      toast(`Lego "${datos.nombre}" guardado`);
      await recargarTodo();
    } catch (e) {
      $("#lego-error").textContent = e.message;
      $("#lego-error").classList.remove("oculto");
    }
  })();
});

$("#btn-guardar-legos").addEventListener("click", () => intentar(async () => {
  const r = await api("PUT", P("/legos"), legosElegidos());
  $("#legos-estado").textContent = "guardado ✓";
  mostrarErroresEntradas(r.errores);
  await cargarEntradas();        // proyecto.yaml cambió
  toast("Selección de legos guardada. Presiona ▶ Ejecutar para aplicarla.");
}));

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
  // (lo escrito ya se guarda en estado.entradas con cada tecla; aquí no se copia nada de la
  //  pantalla, para no pisar con texto viejo lo que acaba de llegar del servidor)
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
  intentar(cargarLegos);
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
    cielos: "Cielos", piezas_entramado: "Piezas de madera", marcos: "Marcos de acero", perfiles_acero: "Perfiles de acero", ejes: "Ejes", niveles: "Niveles", advertencias: "Advertencias" };
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

  if (visible("estructura")) for (const f of (m.marcos || []).filter(x => x.nivel === nivel)) {
    raiz.append(titulo(s("line", { x1: f.inicio[0], y1: f.inicio[1], x2: f.fin[0], y2: f.fin[1], stroke: "#64748b",
      "stroke-width": 3, "stroke-dasharray": f.sistema === "arriostrado_x" ? "10 4" : "", "vector-effect": "non-scaling-stroke" }),
      `${f.id} · ${f.tipo}\n${f.sistema === "momento" ? "marco de momento" : "arriostrado en X"} · ${f.vanos} vano(s)`));
    const pc = f.perfiles.columna;
    for (const c of f.columnas) {
      raiz.append(titulo(s("rect", { x: c.punto[0] - pc.b / 2, y: c.punto[1] - pc.h / 2, width: pc.b, height: pc.h, fill: "#475569",
        transform: `rotate(${Math.atan2(f.direccion[1], f.direccion[0]) * 180 / Math.PI} ${c.punto[0]} ${c.punto[1]})` }),
        `${f.id} · columna ${pc.perfil || pc.forma}`));
      puntos.push(c.punto);
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
  // el arrastre empieza recién al mover unos píxeles, para no tapar los clics sobre los elementos
  svg.addEventListener("pointerdown", e => { arr = { x: e.clientX, y: e.clientY, vb: vb(), moviendo: false, id: e.pointerId }; });
  svg.addEventListener("pointermove", e => {
    if (!arr) return;
    if (!arr.moviendo) {
      if (Math.hypot(e.clientX - arr.x, e.clientY - arr.y) < 4) return;
      arr.moviendo = true;
      svg.setPointerCapture(arr.id);
    }
    const r = svg.getBoundingClientRect();
    const [x, y, w, hh] = arr.vb;
    const escala = Math.max(w / r.width, hh / r.height);
    svg.setAttribute("viewBox", `${x - (e.clientX - arr.x) * escala} ${y - (e.clientY - arr.y) * escala} ${w} ${hh}`);
  });
  svg.addEventListener("pointerup", () => { arr = null; });
}
habilitarZoom($("#svg-resultado"));
habilitarZoom($("#svg-plano"));
habilitarZoom($("#est-planta"));

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
  if (!v.dwg) $("#nota-dwg").textContent = "Este servidor aún no tiene el convertidor de DWG: por ahora sube los planos como DXF " +
    "(AutoCAD: GUARDARCOMO → DXF).";
  $("#version").textContent = `v${v.version} · ${v.commit}`;
  $("#version").title = "Versión publicada (commit de GitHub)";
}).catch(() => {});

intentar(async () => {
  await cargarProyectos();
  let tab = null;
  try { tab = localStorage.getItem("c2r-tab"); } catch {}
  if (tab && $(`#tab-${tab}`)) irA(tab);
});

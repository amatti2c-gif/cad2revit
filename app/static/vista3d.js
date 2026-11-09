// Visor 3D del modelo intermedio (three.js). Plano (x, y, z) -> three (x, z, -y).
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

let ctx = null;

function iniciar(contenedor) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  contenedor.replaceChildren(renderer.domElement);
  const escena = new THREE.Scene();
  const camara = new THREE.PerspectiveCamera(45, 1, 0.1, 2000);
  const controles = new OrbitControls(camara, renderer.domElement);
  controles.enableDamping = true;
  escena.add(new THREE.HemisphereLight(0xffffff, 0x8a8f99, 1.1));
  const sol = new THREE.DirectionalLight(0xffffff, 1.4);
  sol.position.set(30, 50, 20);
  escena.add(sol);
  const grupo = new THREE.Group();
  escena.add(grupo);

  const ajustar = () => {
    const w = contenedor.clientWidth, h = contenedor.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camara.aspect = w / h;
    camara.updateProjectionMatrix();
  };
  new ResizeObserver(ajustar).observe(contenedor);
  ajustar();
  renderer.setAnimationLoop(() => { controles.update(); renderer.render(escena, camara); });
  return { renderer, escena, camara, controles, grupo, contenedor, clave: null };
}

const mat = (color, extra = {}) => new THREE.MeshStandardMaterial({ color, roughness: 0.85, metalness: 0, ...extra });

function caja(grupo, material, cx, cy, cz, lx, ly, lz, rotY = 0) {
  const m = new THREE.Mesh(new THREE.BoxGeometry(lx, ly, lz), material);
  m.position.set(cx, cy, cz);
  m.rotation.y = rotY;
  grupo.add(m);
  return m;
}

export function mostrar(contenedor, modelo, colores, hastaNivel) {
  if (!ctx || ctx.contenedor !== contenedor) ctx = iniciar(contenedor);
  const { grupo } = ctx;
  grupo.clear();

  const cota = Object.fromEntries(modelo.niveles.map(n => [n.nombre, n.cota]));
  const orden = modelo.niveles.map(n => n.nombre);
  // "hastaNivel": muestra los elementos de ese nivel y los inferiores (para ver el interior)
  const limite = orden.indexOf(hastaNivel);
  const ver = nivel => limite < 0 || orden.indexOf(nivel) <= limite;
  const espesorTipo = Object.fromEntries(modelo.tipos_losa.map(t => [t.nombre, t.capas.reduce((a, c) => a + c.espesor, 0)]));
  const materiales = {};
  const matTipo = t => (materiales[t] ||= mat(colores[t] || "#9ca3af"));
  const muroPorId = {};

  // Muros con entramado: semitransparentes para ver las piezas de madera
  const conEntramado = new Set((modelo.entramados || []).map(e => e.muro));
  const matTransparente = t => (materiales["~" + t] ||= mat(colores[t] || "#9ca3af",
    { transparent: true, opacity: 0.18, depthWrite: false }));
  const losaTecho = modelo.losas.find(l => l.cubierta && l.pendiente > 0 && (l.bordes_pendiente || []).length);
  const alturaTecho = losaTecho ? funcionTecho(losaTecho) : null;
  for (const w of modelo.muros) {
    muroPorId[w.id] = w;
    if (!ver(w.nivel)) continue;
    const z0 = cota[w.nivel];
    const alto = w.nivel_superior != null ? cota[w.nivel_superior] - z0 : (w.altura ?? 2.6);
    const [x1, y1] = w.inicio, [x2, y2] = w.fin;
    const L = Math.hypot(x2 - x1, y2 - y1);
    if (w.buhardilla && alturaTecho) {
      // muro de buhardilla: desde el nivel de cubierta hasta la línea del techo (frontón)
      const forma = new THREE.Shape();
      forma.moveTo(0, 0);
      const n = 32;
      for (let k = 0; k <= n; k++) {
        const t = L * k / n;
        forma.lineTo(t, alturaTecho([x1 + (x2 - x1) * t / L, y1 + (y2 - y1) * t / L]));
      }
      forma.lineTo(L, 0);
      const geo = new THREE.ExtrudeGeometry(forma, { depth: w.espesor, bevelEnabled: false });
      geo.translate(0, 0, -w.espesor / 2);
      const malla = new THREE.Mesh(geo, conEntramado.has(w.id) ? matTransparente(w.tipo) : matTipo(w.tipo));
      malla.position.set(x1, z0, -y1);
      malla.rotation.y = Math.atan2(y2 - y1, x2 - x1);
      grupo.add(malla);
      continue;
    }
    caja(grupo, conEntramado.has(w.id) ? matTransparente(w.tipo) : matTipo(w.tipo),
      (x1 + x2) / 2, z0 + alto / 2, -(y1 + y2) / 2, L, alto, w.espesor, Math.atan2(y2 - y1, x2 - x1));
  }

  // Entramado de madera: cada pieza es un prisma orientado según su eje
  const mMadera = mat("#c8955a"), mMaderaRef = mat("#a8743f");
  const eje = new THREE.Vector3(), centro = new THREE.Vector3(), nrm = new THREE.Vector3(), lat = new THREE.Vector3();
  for (const e of modelo.entramados || []) {
    if (!ver(e.nivel)) continue;
    nrm.set(-e.direccion[1], 0, -e.direccion[0]);       // normal al muro en coordenadas three
    for (const p of e.piezas) {
      const a = new THREE.Vector3(p.inicio[0], p.inicio[2], -p.inicio[1]);
      const b = new THREE.Vector3(p.fin[0], p.fin[2], -p.fin[1]);
      eje.subVectors(b, a);
      const L = eje.length();
      if (L < 1e-4) continue;
      eje.normalize();
      lat.crossVectors(eje, nrm).normalize();
      const m = new THREE.Mesh(new THREE.BoxGeometry(p.ancho, L, p.profundidad),
        ["jamba", "dintel", "alfeizar"].includes(p.rol) ? mMaderaRef : mMadera);
      // ejes locales de la caja: X = lateral (ancho), Y = eje de la pieza, Z = normal (profundidad)
      m.quaternion.setFromRotationMatrix(new THREE.Matrix4().makeBasis(lat, eje, nrm.clone().normalize()));
      m.position.copy(centro.addVectors(a, b).multiplyScalar(0.5));
      grupo.add(m);
    }
  }

  const mPuerta = mat("#b45309"), mVentana = mat("#38bdf8", { transparent: true, opacity: 0.7 });
  for (const a of modelo.aberturas) {
    const w = muroPorId[a.muro];
    if (!w || !ver(w.nivel)) continue;
    const ang = Math.atan2(w.fin[1] - w.inicio[1], w.fin[0] - w.inicio[0]);
    caja(grupo, a.categoria === "puerta" ? mPuerta : mVentana, a.centro[0], cota[w.nivel] + a.antepecho + a.alto / 2,
      -a.centro[1], a.ancho, a.alto, w.espesor + 0.04, ang);
  }

  const mLosa = mat("#cbd5e1"), mTerm = mat("#e7d8c3"), mCub = mat("#94a3b8");
  for (const l of modelo.losas) {
    if (!ver(l.nivel)) continue;
    if (l.cubierta && l.pendiente > 0 && (l.bordes_pendiente || []).length) {
      techoInclinado(grupo, l, cota[l.nivel], mat("#b45309"), null);   // frontones: solo como muros de buhardilla
      continue;
    }
    const e = espesorTipo[l.tipo] || 0.15;
    const forma = new THREE.Shape(l.contorno.map(([x, y]) => new THREE.Vector2(x, y)));
    const geo = new THREE.ExtrudeGeometry(forma, { depth: e, bevelEnabled: false });
    geo.rotateX(-Math.PI / 2);   // (x, y, z) -> (x, z, -y): la extrusión crece hacia arriba
    const m = new THREE.Mesh(geo, l.cubierta ? mCub : l.terminacion ? mTerm : mLosa);
    m.position.y = cota[l.nivel] + (l.desfase || 0) - e;
    grupo.add(m);
  }

  const mEst = mat("#6b7280");
  for (const c of modelo.columnas) {
    if (!ver(c.nivel)) continue;
    const z0 = cota[c.nivel];
    const alto = c.nivel_superior != null ? cota[c.nivel_superior] - z0 : (c.altura ?? 2.6);
    caja(grupo, mEst, c.centro[0], z0 + alto / 2, -c.centro[1], c.b, alto, c.h, (c.rotacion || 0) * Math.PI / 180);
  }
  for (const v of modelo.vigas) {
    if (!ver(v.nivel) && orden.indexOf(v.nivel) - 1 > limite) continue;
    const [x1, y1] = v.inicio, [x2, y2] = v.fin;
    caja(grupo, mEst, (x1 + x2) / 2, cota[v.nivel] - v.h / 2, -(y1 + y2) / 2, Math.hypot(x2 - x1, y2 - y1), v.h, v.b,
      Math.atan2(y2 - y1, x2 - x1));
  }

  // Marcos de acero: columnas, vigas (bajo el nivel) y diagonales como prismas de sección b × h
  const mAcero = mat("#64748b", { metalness: 0.3, roughness: 0.5 }), mDiag = mat("#2563eb", { metalness: 0.3, roughness: 0.5 });
  const barra = (a, b, ancho, alto, material, normal) => {
    const A = new THREE.Vector3(a[0], a[2], -a[1]), B = new THREE.Vector3(b[0], b[2], -b[1]);
    const ejeB = new THREE.Vector3().subVectors(B, A); const L = ejeB.length(); if (L < 1e-4) return;
    ejeB.normalize();
    const n = normal.clone().sub(ejeB.clone().multiplyScalar(normal.dot(ejeB))).normalize();
    const lat = new THREE.Vector3().crossVectors(ejeB, n).normalize();
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(ancho, L, alto), material);
    mesh.quaternion.setFromRotationMatrix(new THREE.Matrix4().makeBasis(lat, ejeB, n));
    mesh.position.copy(A).add(B).multiplyScalar(0.5);
    grupo.add(mesh);
  };
  for (const f of modelo.marcos || []) {
    if (!ver(f.nivel)) continue;
    const dir = new THREE.Vector3(f.direccion[0], 0, -f.direccion[1]);
    const enPlano = new THREE.Vector3(-f.direccion[1], 0, -f.direccion[0]);   // normal al plano del marco
    const pc = f.perfiles.columna, pv = f.perfiles.viga, pd = f.perfiles.diagonal;
    for (const c of f.columnas) barra([c.punto[0], c.punto[1], c.z0], [c.punto[0], c.punto[1], c.z1], pc.h, pc.b, mAcero, enPlano);
    for (const v of f.vigas) barra([v.inicio[0], v.inicio[1], v.inicio[2] - pv.h / 2], [v.fin[0], v.fin[1], v.fin[2] - pv.h / 2],
                                   pv.b, pv.h, mAcero, new THREE.Vector3(0, 1, 0));
    if (pd) for (const d of f.diagonales) barra(d.inicio, d.fin, pd.b, pd.h, mDiag, enPlano);
    void dir;
  }

  const mFund = mat("#a8a29e");
  const base = modelo.niveles[0]?.cota ?? 0;
  for (const f of modelo.fundaciones.corridas) {
    const w = muroPorId[f.muro];
    if (!w) continue;
    const [x1, y1] = w.inicio, [x2, y2] = w.fin;
    caja(grupo, mFund, (x1 + x2) / 2, base - f.alto / 2, -(y1 + y2) / 2, Math.hypot(x2 - x1, y2 - y1) + f.ancho - w.espesor,
      f.alto, f.ancho, Math.atan2(y2 - y1, x2 - x1));
  }
  for (const z of modelo.fundaciones.aisladas) {
    caja(grupo, mFund, z.centro[0], base + z.cota_sello + z.h / 2, -z.centro[1], z.b, z.h, z.l);
  }

  // Encuadre de cámara solo al cambiar de corrida
  const clave = JSON.stringify([modelo.proyecto, modelo.muros.length, modelo.niveles]);
  if (ctx.clave !== clave) {
    const caja3 = new THREE.Box3().setFromObject(grupo);
    if (!caja3.isEmpty()) {
      const centro = caja3.getCenter(new THREE.Vector3());
      const tam = caja3.getSize(new THREE.Vector3()).length();
      ctx.controles.target.copy(centro);
      ctx.camara.position.copy(centro).add(new THREE.Vector3(tam * 0.8, tam * 0.6, tam * 0.9));
      ctx.camara.near = tam / 200;
      ctx.camara.far = tam * 20;
      ctx.camara.updateProjectionMatrix();
    }
    ctx.clave = clave;
  }
}


/** Cubierta inclinada: malla fina sobre el contorno con la altura del techo en cada punto (planos de pendiente
 *  con su zona de influencia, como el techo por contorno de Revit). */
function techoInclinado(grupo, l, z0, mTecho) {
  const altura = funcionTecho(l);
  const pts = l.contorno.map(p => [p[0], p[1]]);
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const paso = Math.max(0.3, Math.max(x1 - x0, y1 - y0) / 160);
  // recorta el contorno a una celda rectangular (Sutherland–Hodgman contra cada lado de la celda)
  const recortar = (poli, eje, valor, menor) => {
    const out = [], dentro = q => (menor ? q[eje] <= valor + 1e-9 : q[eje] >= valor - 1e-9);
    for (let i = 0; i < poli.length; i++) {
      const p = poli[i], q = poli[(i + 1) % poli.length];
      if (dentro(p)) out.push(p);
      if (dentro(p) !== dentro(q)) {
        const k = (valor - p[eje]) / (q[eje] - p[eje]);
        out.push([p[0] + (q[0] - p[0]) * k, p[1] + (q[1] - p[1]) * k]);
      }
    }
    return out;
  };
  const pos = [];
  const P3 = q => [q[0], z0 + altura(q), -q[1]];
  for (let cx = x0; cx < x1; cx += paso) for (let cy = y0; cy < y1; cy += paso) {
    let c = pts;
    c = recortar(c, 0, cx, false); if (c.length < 3) continue;
    c = recortar(c, 0, cx + paso, true); if (c.length < 3) continue;
    c = recortar(c, 1, cy, false); if (c.length < 3) continue;
    c = recortar(c, 1, cy + paso, true); if (c.length < 3) continue;
    for (const [a, b, d] of THREE.ShapeUtils.triangulateShape(c.map(q => new THREE.Vector2(q[0], q[1])), []))
      pos.push(...P3(c[a]), ...P3(c[b]), ...P3(c[d]));
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  geo.computeVertexNormals();
  const mt = mTecho.clone(); mt.side = THREE.DoubleSide;
  grupo.add(new THREE.Mesh(geo, mt));
}

/** Altura del techo inclinado sobre su nivel en un punto de la planta. Cada plano de pendiente actúa solo de su
 *  lado del borde y frente a él (margen de 45°): un acceso a dos aguas perpendicular no corta el resto. */
function funcionTecho(l) {
  let pts = l.contorno.map(p => [p[0], p[1]]);
  const n = pts.length;
  let bordes = l.bordes_pendiente.slice();
  const area = pts.reduce((a, p, i) => { const q = pts[(i + 1) % n]; return a + p[0] * q[1] - q[0] * p[1]; }, 0);
  if (area < 0) { pts = pts.slice().reverse(); bordes = bordes.map(i => (i === n - 1 ? n - 1 : n - 2 - i)); }
  const t = Math.tan(l.pendiente * Math.PI / 180);
  const planos = bordes.map(i => {
    const a = pts[i], b = pts[(i + 1) % n], L = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
    const nx = -(b[1] - a[1]) / L, ny = (b[0] - a[0]) / L;
    return { c0: -(a[0] * nx + a[1] * ny) * t, cx: nx * t, cy: ny * t, a, ux: (b[0] - a[0]) / L, uy: (b[1] - a[1]) / L, L };
  });
  return p => {
    const validos = [], todos = [];
    for (const pl of planos) {
      const hv = pl.c0 + pl.cx * p[0] + pl.cy * p[1];
      if (hv < -1e-9) continue;
      todos.push(hv);
      const tp = (p[0] - pl.a[0]) * pl.ux + (p[1] - pl.a[1]) * pl.uy;
      const d = Math.abs((p[0] - pl.a[0]) * pl.uy - (p[1] - pl.a[1]) * pl.ux);
      if (tp >= -d - 1e-6 && tp <= pl.L + d + 1e-6) validos.push(hv);
    }
    const lista = validos.length ? validos : todos;
    return lista.length ? Math.max(0, Math.min(...lista)) : 0;
  };
}

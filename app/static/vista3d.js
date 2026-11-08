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
  for (const w of modelo.muros) {
    muroPorId[w.id] = w;
    if (!ver(w.nivel)) continue;
    const z0 = cota[w.nivel];
    const alto = w.nivel_superior != null ? cota[w.nivel_superior] - z0 : (w.altura ?? 2.6);
    const [x1, y1] = w.inicio, [x2, y2] = w.fin;
    const L = Math.hypot(x2 - x1, y2 - y1);
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
      techoInclinado(grupo, l, cota[l.nivel], mat("#b45309"), mat("#e7e5e4"));
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


/** Cubierta inclinada como la hace Revit (techo por contorno): cada borde con pendiente define un plano
 *  que sube hacia adentro; el techo es el más bajo de esos planos. Los bordes sin pendiente son frontones. */
function techoInclinado(grupo, l, z0, mTecho, mFronton) {
  let pts = l.contorno.map(p => [p[0], p[1]]);
  const n = pts.length;
  let bordes = l.bordes_pendiente.slice();
  const area = pts.reduce((a, p, i) => { const q = pts[(i + 1) % n]; return a + p[0] * q[1] - q[0] * p[1]; }, 0);
  if (area < 0) {                      // dejar el contorno antihorario (el interior queda a la izquierda)
    pts = pts.slice().reverse();
    bordes = bordes.map(i => (i === n - 1 ? n - 1 : n - 2 - i));
  }
  const t = Math.tan(l.pendiente * Math.PI / 180);
  const planos = bordes.map(i => {
    const a = pts[i], b = pts[(i + 1) % n];
    const L = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
    const nx = -(b[1] - a[1]) / L, ny = (b[0] - a[0]) / L;      // normal hacia el interior
    // h(p) = c0 + cx*x + cy*y
    return { c0: -(a[0] * nx + a[1] * ny) * t, cx: nx * t, cy: ny * t };
  });
  const h = (pl, p) => pl.c0 + pl.cx * p[0] + pl.cy * p[1];
  const altura = p => Math.max(0, Math.min(...planos.map(pl => h(pl, p))));
  const recortar = (poli, f) => {      // Sutherland–Hodgman: deja la parte con f(p) <= 0
    const out = [];
    for (let i = 0; i < poli.length; i++) {
      const p = poli[i], q = poli[(i + 1) % poli.length], fp = f(p), fq = f(q);
      if (fp <= 1e-9) out.push(p);
      if ((fp < -1e-9 && fq > 1e-9) || (fp > 1e-9 && fq < -1e-9)) {
        const k = fp / (fp - fq);
        out.push([p[0] + (q[0] - p[0]) * k, p[1] + (q[1] - p[1]) * k]);
      }
    }
    return out;
  };
  const pos = [];
  const tri = (a, b, c) => pos.push(...a, ...b, ...c);
  const P3 = (p, z) => [p[0], z, -p[1]];
  planos.forEach((pl, i) => {
    let cara = pts;
    planos.forEach((otro, k) => {
      if (k !== i) cara = recortar(cara, p => h(pl, p) - h(otro, p));
    });
    if (cara.length < 3) return;
    const idx = THREE.ShapeUtils.triangulateShape(cara.map(p => new THREE.Vector2(p[0], p[1])), []);
    for (const [a, b, c] of idx) tri(P3(cara[a], z0 + h(pl, cara[a])), P3(cara[b], z0 + h(pl, cara[b])), P3(cara[c], z0 + h(pl, cara[c])));
  });
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  geo.computeVertexNormals();
  const mt = mTecho.clone(); mt.side = THREE.DoubleSide;
  grupo.add(new THREE.Mesh(geo, mt));
  // frontones: bajo los bordes sin pendiente, el muro sube hasta el techo
  const fpos = [];
  for (let i = 0; i < n; i++) {
    if (bordes.includes(i)) continue;
    const a = pts[i], b = pts[(i + 1) % n], m = 24;
    for (let k = 0; k < m; k++) {
      const p = [a[0] + (b[0] - a[0]) * k / m, a[1] + (b[1] - a[1]) * k / m];
      const q = [a[0] + (b[0] - a[0]) * (k + 1) / m, a[1] + (b[1] - a[1]) * (k + 1) / m];
      const hp = altura(p), hq = altura(q);
      if (hp < 1e-3 && hq < 1e-3) continue;
      fpos.push(...P3(p, z0), ...P3(q, z0), ...P3(q, z0 + hq), ...P3(p, z0), ...P3(q, z0 + hq), ...P3(p, z0 + hp));
    }
  }
  if (fpos.length) {
    const g2 = new THREE.BufferGeometry();
    g2.setAttribute("position", new THREE.Float32BufferAttribute(fpos, 3));
    g2.computeVertexNormals();
    const mf = mFronton.clone(); mf.side = THREE.DoubleSide;
    grupo.add(new THREE.Mesh(g2, mf));
  }
}

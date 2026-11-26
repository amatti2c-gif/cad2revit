// Visor 3D del modelo de análisis (pestaña 6, modo 3D): barras, láminas de los muros, apoyos, elementos excluidos
// (fantasmas) y deformadas. Análisis (x, y, z) con z vertical -> three (x, z, -y). Clic en un elemento: onClick(origen).
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

let ctx = null;

function iniciar(contenedor) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  contenedor.replaceChildren(renderer.domElement);
  const escena = new THREE.Scene();
  const camara = new THREE.PerspectiveCamera(40, 1, 0.05, 5000);
  const controles = new OrbitControls(camara, renderer.domElement);
  controles.enableDamping = true;
  escena.add(new THREE.HemisphereLight(0xffffff, 0x8a8f99, 1.2));
  const sol = new THREE.DirectionalLight(0xffffff, 1.2);
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
  const c = { renderer, escena, camara, controles, grupo, contenedor, clave: null, onClick: null, objetos: [] };
  // clic (sin arrastre) → elemento bajo el cursor
  let abajo = null;
  renderer.domElement.addEventListener("pointerdown", ev => { abajo = [ev.clientX, ev.clientY]; });
  renderer.domElement.addEventListener("pointerup", ev => {
    if (!abajo || Math.hypot(ev.clientX - abajo[0], ev.clientY - abajo[1]) > 4 || !c.onClick) return;
    const r = renderer.domElement.getBoundingClientRect();
    const ray = new THREE.Raycaster();
    ray.params.Line.threshold = c.umbral || 0.1;
    ray.setFromCamera(new THREE.Vector2(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1), camara);
    const hit = ray.intersectObjects(c.objetos, false)[0];
    if (hit?.object?.userData?.origen) c.onClick(hit.object.userData.origen, hit.object.userData);
  });
  return c;
}

const V = p => new THREE.Vector3(p[0], p[2], -p[1]);

export function mostrar(contenedor, d, onClick) {
  if (!ctx || ctx.contenedor !== contenedor || !contenedor.contains(ctx.renderer.domElement)) ctx = iniciar(contenedor);
  ctx.onClick = onClick;
  const g = ctx.grupo;
  g.children.slice().forEach(o => { g.remove(o); o.geometry?.dispose?.(); });
  ctx.objetos = [];
  const pts = Object.values(d.nudos);
  if (!pts.length && !(d.fantasmas || []).length) return;
  const todos = pts.concat((d.fantasmas || []).flatMap(f => f.p));
  const min = [0, 1, 2].map(k => Math.min(...todos.map(p => p[k]))), max = [0, 1, 2].map(k => Math.max(...todos.map(p => p[k])));
  const tam = Math.max(max[0] - min[0], max[1] - min[1], max[2] - min[2], 1);
  ctx.umbral = tam / 150;
  // deformada: el desplazamiento mayor se dibuja como esc·tam/10
  let pos = d.nudos;
  if (d.desp) {
    const dm = Math.max(...Object.values(d.desp).map(u => Math.hypot(...u)), 1e-12);
    const f = (d.escala ?? 1) * tam / 10 / dm;
    pos = Object.fromEntries(Object.entries(d.nudos).map(([n, p]) => {
      const u = d.desp[n] || [0, 0, 0];
      return [n, [p[0] + f * u[0], p[1] + f * u[1], p[2] + f * u[2]]];
    }));
    // el modelo sin deformar, tenue
    const lin = [];
    d.barras.forEach(b => { lin.push(V(d.nudos[b.ni]), V(d.nudos[b.nj])); });
    if (lin.length) g.add(new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(lin),
      new THREE.LineBasicMaterial({ color: 0xbbbbbb, transparent: true, opacity: 0.6 })));
  }
  // barras
  d.barras.forEach(b => {
    const geo = new THREE.BufferGeometry().setFromPoints([V(pos[b.ni]), V(pos[b.nj])]);
    const l = new THREE.Line(geo, new THREE.LineBasicMaterial({ color: b.color, linewidth: 2 }));
    l.userData = { origen: b.origen, tipo: "barra" };
    g.add(l); ctx.objetos.push(l);
  });
  // láminas de los muros
  const porColor = {};
  d.quads.forEach(q => {
    const p = q.nudos.map(n => V(pos[n]));
    const tri = p.length === 4 ? [p[0], p[1], p[2], p[0], p[2], p[3]] : [p[0], p[1], p[2]];
    const geo = new THREE.BufferGeometry().setFromPoints(tri);
    geo.computeVertexNormals();
    const m = new THREE.Mesh(geo, porColor[q.color] ||= new THREE.MeshStandardMaterial({ color: q.color, side: THREE.DoubleSide,
      transparent: true, opacity: 0.55, roughness: 0.9, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 }));
    m.userData = { origen: q.origen, tipo: "muro" };
    g.add(m); ctx.objetos.push(m);
    const bordes = [];
    for (let k = 0; k < p.length; k++) bordes.push(p[k], p[(k + 1) % p.length]);
    g.add(new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(bordes),
      new THREE.LineBasicMaterial({ color: 0x8a6a30, transparent: true, opacity: 0.35 })));
  });
  // apoyos
  const r = tam / 120;
  const mats = { empotrado: 0x1b6e20, rotula: 0x1f4e9c, deslizante: 0xc06000, x: 0x7a3fb0 };
  Object.entries(d.apoyos || {}).forEach(([n, t]) => {
    if (!pos[n]) return;
    const geo = t === "empotrado" ? new THREE.BoxGeometry(2.4 * r, 1.2 * r, 2.4 * r) : new THREE.ConeGeometry(1.4 * r, 2.4 * r, 4);
    const m = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: mats[t] || 0x444444 }));
    const v = V(pos[n]);
    m.position.set(v.x, v.y - (t === "empotrado" ? 0.6 : 1.2) * r, v.z);
    g.add(m);
  });
  // elementos excluidos o sin apoyo: fantasmas a trazos (clic para volver a incluirlos)
  (d.fantasmas || []).forEach(f => {
    const p = f.p.map(V);
    if (f.cerrado) p.push(p[0]);
    const l = new THREE.Line(new THREE.BufferGeometry().setFromPoints(p),
      new THREE.LineDashedMaterial({ color: f.color || 0x999999, dashSize: tam / 80, gapSize: tam / 120 }));
    l.computeLineDistances();
    l.userData = { origen: f.origen, tipo: "fantasma" };
    g.add(l); ctx.objetos.push(l);
  });
  // cámara: solo al cambiar de modelo (los cambios de vista conservan el encuadre)
  const centro = V([(min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2]);
  if (ctx.clave !== d.clave) {
    ctx.clave = d.clave;
    ctx.controles.target.copy(centro);
    ctx.camara.position.set(centro.x + 1.1 * tam, centro.y + 0.8 * tam, centro.z + 1.3 * tam);
    ctx.camara.near = tam / 500; ctx.camara.far = tam * 50;
    ctx.camara.updateProjectionMatrix();
  }
}

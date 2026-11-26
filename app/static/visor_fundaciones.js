// Visor 3D de las fundaciones (pestaña Fundaciones): geometría exportada de Revit (cimientos en anillo, zapatas,
// sobrecimientos, dados, radier), tramos del emparrillado de cálculo y apoyos del modelo 3D. Planta (x, y), z arriba
// -> three (x, z, -y).
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

let ctx = null;

function iniciar(cont) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  cont.replaceChildren(renderer.domElement);
  const escena = new THREE.Scene();
  const camara = new THREE.PerspectiveCamera(40, 1, 0.05, 5000);
  const controles = new OrbitControls(camara, renderer.domElement);
  controles.enableDamping = true;
  escena.add(new THREE.HemisphereLight(0xffffff, 0x8a8f99, 1.2));
  const sol = new THREE.DirectionalLight(0xffffff, 1.3);
  sol.position.set(30, 60, 20);
  escena.add(sol);
  const grupo = new THREE.Group();
  escena.add(grupo);
  const ajustar = () => {
    const w = cont.clientWidth, h = cont.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camara.aspect = w / h;
    camara.updateProjectionMatrix();
  };
  new ResizeObserver(ajustar).observe(cont);
  ajustar();
  renderer.setAnimationLoop(() => { controles.update(); renderer.render(escena, camara); });
  return { renderer, camara, controles, grupo, cont, clave: null };
}

const mat = (color, op = 1) => new THREE.MeshStandardMaterial({ color, roughness: 0.85, transparent: op < 1, opacity: op, side: THREE.DoubleSide });

export function mostrar(cont, d) {
  if (!ctx || ctx.cont !== cont || !cont.contains(ctx.renderer.domElement)) ctx = iniciar(cont);
  const g = ctx.grupo;
  g.children.slice().forEach(o => { g.remove(o); o.geometry?.dispose?.(); });
  const [ox, oy] = d.origen;
  const V = (x, y, z) => new THREE.Vector3(x - ox, z, -(y - oy));
  const pts = [];
  // prisma vertical de una planta (polígono con huecos) entre z0 y z1
  const prisma = (ext, huecos, z0, z1, material) => {
    const forma = new THREE.Shape(ext.map(([x, y]) => new THREE.Vector2(x - ox, y - oy)));
    for (const h of huecos || []) forma.holes.push(new THREE.Path(h.map(([x, y]) => new THREE.Vector2(x - ox, y - oy))));
    const geo = new THREE.ExtrudeGeometry(forma, { depth: Math.max(z1 - z0, 0.01), bevelEnabled: false });
    geo.rotateX(-Math.PI / 2);                     // shape en XY → plano horizontal; extrusión hacia arriba (+Y three)
    const m = new THREE.Mesh(geo, material);
    m.position.y = z0;
    g.add(m);
    const bordes = new THREE.LineSegments(new THREE.EdgesGeometry(geo, 25), new THREE.LineBasicMaterial({ color: 0x555555 }));
    bordes.position.y = z0;
    g.add(bordes);
    ext.forEach(([x, y]) => pts.push([x, y, z0], [x, y, z1]));
  };
  const caja = (a, b, ancho, z0, z1, material) => {
    const ux = b[0] - a[0], uy = b[1] - a[1], L = Math.hypot(ux, uy) || ancho;
    const [cx, cy] = L > 1e-6 ? [ux / L, uy / L] : [1, 0];
    const nx = -cy * ancho / 2, ny = cx * ancho / 2;
    prisma([[a[0] + nx, a[1] + ny], [b[0] + nx, b[1] + ny], [b[0] - nx, b[1] - ny], [a[0] - nx, a[1] - ny]], [], z0, z1, material);
  };
  for (const r of d.anillos || []) prisma(r.ext, [r.int], r.z_inf, r.z_sup, mat(0xb9b9b9));
  for (const e of d.cajas || []) caja(e.a, e.b, e.ancho, e.z_inf, e.z_sup, mat(e.clase === "muro" ? 0xa9a9a9 : 0xc4c4c4));
  for (const e of d.dados || []) caja(e.a, e.b, e.ancho, e.z_inf, e.z_sup, mat(0x8d8d8d));
  for (const e of d.radieres || []) {
    if (e.ext) prisma(e.ext, [], e.z_inf, e.z_sup, mat(0xd8d2c4, 0.35));
    else caja(e.a, e.b, e.ancho, e.z_inf, e.z_sup, mat(0xd8d2c4, 0.35));
  }
  // tramos del emparrillado (línea media de cada fundación), con el color de su peor razón si hay resultados
  for (const t of d.tramos || []) {
    const z = t.z + 0.02;
    const geo = new THREE.BufferGeometry().setFromPoints([V(t.a[0], t.a[1], z), V(t.b[0], t.b[1], z)]);
    g.add(new THREE.Line(geo, new THREE.LineBasicMaterial({ color: t.color || 0x2d6cdf })));
  }
  // apoyos del modelo 3D: verde sobre una fundación, rojo si no cae sobre ninguna
  const tamPts = pts.concat((d.apoyos || []).map(a => a.p));
  const xs = tamPts.map(p => p[0]), ys = tamPts.map(p => p[1]), zs = tamPts.map(p => p[2]);
  const tam = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), 1);
  for (const a of d.apoyos || []) {
    const h = tam / 25;
    const m = new THREE.Mesh(new THREE.CylinderGeometry(tam / 250, tam / 250, h, 10), mat(a.ok ? 0x1b6e20 : 0xd0302a));
    const p = V(a.p[0], a.p[1], a.p[2]);
    m.position.set(p.x, p.y + h / 2, p.z);
    g.add(m);
    const c = new THREE.Mesh(new THREE.ConeGeometry(tam / 120, tam / 70, 12), mat(a.ok ? 0x1b6e20 : 0xd0302a));
    c.rotation.x = Math.PI;
    c.position.set(p.x, p.y + tam / 140, p.z);
    g.add(c);
  }
  if (d.clave !== ctx.clave && tamPts.length) {
    ctx.clave = d.clave;
    const cx = (Math.max(...xs) + Math.min(...xs)) / 2, cy = (Math.max(...ys) + Math.min(...ys)) / 2, cz = (Math.max(...zs) + Math.min(...zs)) / 2;
    const c = V(cx, cy, cz);
    ctx.controles.target.copy(c);
    ctx.camara.position.set(c.x + 0.45 * tam, c.y + 0.4 * tam, c.z + 0.6 * tam);
    ctx.camara.near = tam / 500; ctx.camara.far = tam * 50;
    ctx.camara.updateProjectionMatrix();
  }
}

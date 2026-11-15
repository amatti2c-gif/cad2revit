using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;

namespace Cad2Revit
{
    public partial class Constructor
    {
        /// <summary>Comentario de las piezas que crea «Entramado en muros» (no empieza con la marca de importación:
        /// así una reimportación o «Reconstruir todo» no las borra).</summary>
        public const string MarcaEntramadoRevit = "cad2revit-entramado muro ";

        /// <summary>
        /// Entramado de madera para muros dibujados en Revit (p. ej. tipo «marco plataforma»), con la misma lógica de la
        /// app: lee de cada muro su eje, altura, encuentros con otros muros y sus puertas, ventanas y vanos.
        /// Si el muro ya tenía un entramado creado así, se reemplaza.
        /// </summary>
        public Informe EntramadoEnMuros(IList<Wall> muros, ConfEntramado conf)
        {
            double ox = U.OrigenX, oy = U.OrigenY;
            U.OrigenX = U.OrigenY = 0;                        // coordenadas de Revit tal cual
            try
            {
                if (!string.IsNullOrEmpty(conf.Material))
                {
                    var m = new FilteredElementCollector(_doc).OfClass(typeof(Material)).Cast<Material>()
                        .FirstOrDefault(x => x.Name.Equals(conf.Material, StringComparison.OrdinalIgnoreCase))
                        ?? new FilteredElementCollector(_doc).OfClass(typeof(Material)).Cast<Material>()
                        .FirstOrDefault(x => x.Name.IndexOf("pino", StringComparison.OrdinalIgnoreCase) >= 0);
                    if (m != null) _materiales[conf.Material] = m.Id;
                    _m.Materiales.Add(new MaterialDef { Nombre = conf.Material, Clase = "madera" });
                }
                var todos = new FilteredElementCollector(_doc).OfClass(typeof(Wall)).Cast<Wall>()
                    .Where(w => w.Location is LocationCurve lc && lc.Curve is Line).ToList();

                using (var grupo = new TransactionGroup(_doc, "cad2revit: entramado en muros"))
                {
                    grupo.Start();
                    // muros apilados justo encima en la misma línea (p. ej. el triángulo de un frontón): también
                    var lista = muros.ToList();
                    for (int i = 0; i < lista.Count; i++)
                    {
                        var w0 = lista[i];
                        if (!(w0.Location is LocationCurve lc0) || !(lc0.Curve is Line l0)) continue;
                        var (_, zt0) = AlturaMuro(w0);
                        foreach (var o in todos)
                        {
                            if (lista.Any(x => x.Id == o.Id)) continue;
                            var lo = (Line)((LocationCurve)o.Location).Curve;
                            if (Math.Abs(lo.Direction.X * l0.Direction.Y - lo.Direction.Y * l0.Direction.X) > 0.01) continue;
                            var pl = Line.CreateUnbound(new XYZ(l0.GetEndPoint(0).X, l0.GetEndPoint(0).Y, 0), new XYZ(l0.Direction.X, l0.Direction.Y, 0));
                            var oa = new XYZ(lo.GetEndPoint(0).X, lo.GetEndPoint(0).Y, 0); var ob = new XYZ(lo.GetEndPoint(1).X, lo.GetEndPoint(1).Y, 0);
                            if (Mt(pl.Distance(oa)) > 0.05 || Mt(pl.Distance(ob)) > 0.05) continue;
                            var (zb1, _) = AlturaMuro(o);
                            if (Math.Abs(zb1 - zt0) > 0.20) continue;
                            lista.Add(o);
                            _inf.Advertencia($"Muro {o.Id}: incluido (está sobre el muro {w0.Id}, en la misma línea)");
                        }
                    }
                    muros = lista;
                    Etapa("Entramado en muros de Revit", () =>
                    {
                        foreach (var w in muros)
                            Intentar($"muro {w.Id}", () => EntramadoDeMuro(w, todos, conf));
                    });
                    grupo.Assimilate();
                }
            }
            finally
            {
                U.OrigenX = ox; U.OrigenY = oy;
            }
            return _inf;
        }

        private static double Mt(double pies) => UnitUtils.ConvertFromInternalUnits(pies, UnitTypeId.Meters);

        private void EntramadoDeMuro(Wall w, List<Wall> todos, ConfEntramado confBase)
        {
            if (!(w.Location is LocationCurve lc) || !(lc.Curve is Line linea))
            {
                _inf.Advertencia($"Muro {w.Id}: no es recto; se omite (el entramado se genera en muros rectos)");
                return;
            }
            var nivel = _doc.GetElement(w.LevelId) as Level
                        ?? throw new Exception("el muro no tiene nivel de base");
            // borrar el entramado anterior de este muro
            var marca = MarcaEntramadoRevit + w.Id;
            var previos = new FilteredElementCollector(_doc).WhereElementIsNotElementType()
                .Where(e => e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.AsString()?.StartsWith(marca + " ") == true)
                .Select(e => e.Id).ToList();
            if (previos.Count > 0) _doc.Delete(previos);

            // geometría del muro (m)
            XYZ pa = linea.GetEndPoint(0), pb = linea.GetEndPoint(1);
            double[] a = { Mt(pa.X), Mt(pa.Y) }, b = { Mt(pb.X), Mt(pb.Y) };
            double largo = Math.Sqrt(Math.Pow(b[0] - a[0], 2) + Math.Pow(b[1] - a[1], 2));
            double ux = (b[0] - a[0]) / largo, uy = (b[1] - a[1]) / largo;
            double baseOff = w.get_Parameter(BuiltInParameter.WALL_BASE_OFFSET)?.AsDouble() ?? 0;
            double zBase = Mt(nivel.Elevation + baseOff);
            double alto;
            var tope = w.get_Parameter(BuiltInParameter.WALL_HEIGHT_TYPE)?.AsElementId();
            if (tope != null && tope != ElementId.InvalidElementId && _doc.GetElement(tope) is Level nivelTope)
                alto = Mt(nivelTope.Elevation + (w.get_Parameter(BuiltInParameter.WALL_TOP_OFFSET)?.AsDouble() ?? 0)) - zBase;
            else
                alto = Mt(w.get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM)?.AsDouble() ?? 0);
            if (alto < 0.3) throw new Exception($"altura del muro {alto:0.00} m");

            // espesor de las piezas: el del núcleo estructural del muro (si no se fijó otro)
            var conf = new ConfEntramado
            {
                B = confBase.B, H = confBase.H, SeparacionPies = confBase.SeparacionPies,
                SeparacionHorizontales = confBase.SeparacionHorizontales, Soleras = confBase.Soleras,
                RefuerzoVanos = confBase.RefuerzoVanos, Material = confBase.Material,
                FamiliaVertical = confBase.FamiliaVertical, FamiliaHorizontal = confBase.FamiliaHorizontal,
            };
            {
                var cs = w.WallType.GetCompoundStructure();
                var est = cs?.GetLayers().Where(l => l.Function == MaterialFunctionAssignment.Structure).Sum(l => l.Width) ?? 0;
                double nucleo = est > 0 ? Mt(est) : Mt(w.Width);
                if (conf.H <= 0) conf.H = est > 0 ? Mt(est) : Math.Min(Mt(w.Width), 0.14);
                else if (conf.H > nucleo + 0.005)
                    _inf.Advertencia($"Muro {w.Id}: las piezas de {conf.H * 1000:0} mm son más gruesas que su capa estructural " +
                                     $"({nucleo * 1000:0} mm); sobresalen de ella. Cambia el tipo de muro si corresponde");
            }

            // encuentros: cuánto recortar en cada extremo para terminar en la cara del muro vecino
            double Recorte(double[] p)
            {
                double r = 0;
                foreach (var o in todos)
                {
                    if (o.Id == w.Id || !(o.Location is LocationCurve olc) || !(olc.Curve is Line ol)) continue;
                    var q = new XYZ(U.M(p[0]), U.M(p[1]), ol.GetEndPoint(0).Z);
                    var d = ol.Distance(q);
                    if (Mt(d) <= Mt(o.Width) / 2 + 0.02) r = Math.Max(r, Mt(o.Width) / 2);
                }
                return r;
            }
            double r0 = Recorte(a), r1 = Recorte(b);

            // vanos: puertas, ventanas y aberturas alojadas en el muro
            var vanos = new List<VanoMuro>();
            // los vanos del propio muro y los de muros paralelos superpuestos con él (p. ej. el muro importado y tu
            // muro de marco plataforma en el mismo lugar): el entramado no debe pasar por ninguno
            var insertos = new HashSet<ElementId>(w.FindInserts(true, false, true, true));
            foreach (var o in todos)
            {
                if (o.Id == w.Id || !(o.Location is LocationCurve olc2) || !(olc2.Curve is Line ol2)) continue;
                if (Math.Abs(ol2.Direction.X * uy - ol2.Direction.Y * ux) > 0.02) continue;
                double dPerp(XYZ q) => Math.Abs((Mt(q.X) - a[0]) * uy - (Mt(q.Y) - a[1]) * ux);
                if (dPerp(ol2.GetEndPoint(0)) > (Mt(o.Width) + Mt(w.Width)) / 2) continue;
                double ta = (Mt(ol2.GetEndPoint(0).X) - a[0]) * ux + (Mt(ol2.GetEndPoint(0).Y) - a[1]) * uy;
                double tb = (Mt(ol2.GetEndPoint(1).X) - a[0]) * ux + (Mt(ol2.GetEndPoint(1).Y) - a[1]) * uy;
                if (Math.Max(ta, tb) < 0 || Math.Min(ta, tb) > largo) continue;
                foreach (var id2 in o.FindInserts(true, false, true, true)) insertos.Add(id2);
            }
            foreach (var id in insertos)
            {
                var e = _doc.GetElement(id);
                double t0, t1, z0, z1;
                bool puerta = false;
                if (e is Opening op && op.BoundaryRect != null && op.BoundaryRect.Count >= 2)
                {
                    var q0 = op.BoundaryRect[0]; var q1 = op.BoundaryRect[1];
                    double ta = (Mt(q0.X) - a[0]) * ux + (Mt(q0.Y) - a[1]) * uy, tb = (Mt(q1.X) - a[0]) * ux + (Mt(q1.Y) - a[1]) * uy;
                    t0 = Math.Min(ta, tb); t1 = Math.Max(ta, tb);
                    z0 = Mt(Math.Min(q0.Z, q1.Z)); z1 = Mt(Math.Max(q0.Z, q1.Z));
                }
                else if (e is FamilyInstance fi && fi.Location is LocationPoint lp)
                {
                    puerta = fi.Category?.Id == new ElementId(BuiltInCategory.OST_Doors);
                    double tc = (Mt(lp.Point.X) - a[0]) * ux + (Mt(lp.Point.Y) - a[1]) * uy;
                    double Par(BuiltInParameter bip, params string[] nombres)
                    {
                        var p = fi.get_Parameter(bip) ?? fi.Symbol.get_Parameter(bip);
                        if (p == null || p.StorageType != StorageType.Double || p.AsDouble() <= 0)
                            p = nombres.Select(n => fi.LookupParameter(n) ?? fi.Symbol.LookupParameter(n))
                                       .FirstOrDefault(x => x != null && x.StorageType == StorageType.Double && x.AsDouble() > 0);
                        return p != null ? Mt(p.AsDouble()) : 0;
                    }
                    double ancho = Par(BuiltInParameter.FAMILY_ROUGH_WIDTH_PARAM, "Rough Width", "Ancho aproximado");
                    if (ancho <= 0) ancho = Par(BuiltInParameter.FAMILY_WIDTH_PARAM, "Width", "Ancho", "Anchura");
                    double altoV = Par(BuiltInParameter.FAMILY_ROUGH_HEIGHT_PARAM, "Rough Height", "Altura aproximada");
                    if (altoV <= 0) altoV = Par(BuiltInParameter.FAMILY_HEIGHT_PARAM, "Height", "Altura");
                    double antepecho = puerta ? 0 : Mt(fi.get_Parameter(BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)?.AsDouble() ?? 0);
                    if (ancho <= 0 || altoV <= 0)
                    {
                        var bbx = fi.get_BoundingBox(null);
                        if (bbx == null) continue;
                        var ts = new[] { bbx.Min, bbx.Max, new XYZ(bbx.Min.X, bbx.Max.Y, 0), new XYZ(bbx.Max.X, bbx.Min.Y, 0) }
                            .Select(q => (Mt(q.X) - a[0]) * ux + (Mt(q.Y) - a[1]) * uy).ToList();
                        ancho = ancho > 0 ? ancho : ts.Max() - ts.Min();
                        if (altoV <= 0) { antepecho = Mt(bbx.Min.Z) - zBase; altoV = Mt(bbx.Max.Z - bbx.Min.Z); }
                    }
                    t0 = tc - ancho / 2; t1 = tc + ancho / 2;
                    z0 = zBase + antepecho; z1 = z0 + altoV;
                }
                else continue;
                vanos.Add(new VanoMuro { T0 = t0, T1 = t1, ZInf = z0, ZSup = z1, Puerta = puerta });
            }

            // pilares y vigas existentes que atraviesan el muro: el entramado no se superpone con ellos y se apoya en ellos
            // borde superior real del muro (frontón, muro unido al techo o con perfil editado): envolvente superior
            // de su geometría a lo largo del eje
            var perfil = PerfilSuperior(w, a, ux, uy, largo, zBase + alto);
            double PerfilEn(double t)
            {
                if (perfil.Count < 2) return zBase + alto;
                if (t <= perfil[0].t) return perfil[0].z;
                for (int i = 0; i + 1 < perfil.Count; i++)
                    if (t <= perfil[i + 1].t)
                    {
                        var (ta, za) = perfil[i]; var (tb, zb2) = perfil[i + 1];
                        return tb - ta < 1e-9 ? Math.Min(za, zb2) : za + (zb2 - za) * (t - ta) / (tb - ta);
                    }
                return perfil[^1].z;
            }
            double zTope = perfil.Count >= 2 ? perfil.Max(x => x.z) : zBase + alto;
            var obs = ObstaculosEnMuro(w, a, ux, uy, largo, zBase, zTope, PerfilEn);
            vanos.AddRange(obs.Vanos);
            // tope del entramado: bajo las vigas que corren dentro del muro en su parte alta (horizontales o inclinadas)
            Func<double, double> topeEn = t =>
            {
                double z = PerfilEn(t);
                foreach (var (t0, t1, z0, z1) in obs.Vigas)
                    if (t >= t0 - 1e-6 && t <= t1 + 1e-6)
                        z = Math.Min(z, t1 - t0 < 1e-6 ? z0 : z0 + (z1 - z0) * (t - t0) / (t1 - t0));
                return z;
            };
            // en los extremos de cada viga el tope cambia de golpe: quiebres a ambos lados (escalón, no rampa)
            var quiebres = obs.Vigas.SelectMany(v => new[] { v.t0 - 0.001, v.t0, v.t1, v.t1 + 0.001 })
                                    .Concat(perfil.Select(x => x.t)).ToList();
            if (perfil.Count > 2 || (perfil.Count == 2 && Math.Abs(perfil[0].z - perfil[1].z) > 0.01))
                _inf.Advertencia($"Muro {w.Id}: el entramado sigue su borde superior inclinado (cumbrera a {perfil.Max(x => x.z) - zBase:0.00} m)");
            // tramos entre pilares: cada uno se arma por separado y sus soleras y pies derechos extremos llegan a la cara
            // del pilar (sin superponerse)
            var tramos = new List<(double s0, double s1)>();
            double inicio = r0;
            foreach (var (c0, c1) in obs.Pilares.OrderBy(x => x.t0))
            {
                if (c1 <= inicio) continue;
                if (c0 > inicio) tramos.Add((inicio, Math.Min(c0, largo - r1)));
                inicio = Math.Max(inicio, c1);
            }
            if (inicio < largo - r1) tramos.Add((inicio, largo - r1));
            // vanos que quedan detrás de un pilar o del borde del muro: la jamba queda en la cara del pilar/borde
            int nPropios = vanos.Count - obs.Vanos.Count;
            foreach (var v in vanos.Take(nPropios))
            {
                bool libre = tramos.Any(tr => v.T0 >= tr.s0 + conf.B - 0.005 && v.T1 <= tr.s1 - conf.B + 0.005);
                if (!libre && tramos.Any(tr => v.T1 > tr.s0 && v.T0 < tr.s1))
                    _inf.Advertencia($"Muro {w.Id}: el vano en {v.T0:0.00}-{v.T1:0.00} m queda en parte sobre un pilar o el borde " +
                                     "del muro; su jamba se puso en la cara del pilar/borde (revisa su posición)");
            }
            var piezas = new List<PiezaDef>();
            foreach (var (t0, t1) in tramos)
            {
                // sub-tramos según las vigas en la base: sobre ellas el entramado parte de su cara superior
                var cortes = new SortedSet<double> { t0, t1 };
                foreach (var (b0, b1, _) in obs.Bases) { if (b0 > t0 && b0 < t1) cortes.Add(b0); if (b1 > t0 && b1 < t1) cortes.Add(b1); }
                var cs = cortes.ToList();
                for (int k = 0; k + 1 < cs.Count; k++)
                {
                    double s0 = cs[k], s1 = cs[k + 1], tm = (s0 + s1) / 2;
                    if (s1 - s0 <= 2 * conf.B + 0.02) continue;
                    double zb = zBase;
                    foreach (var (b0, b1, zs) in obs.Bases) if (tm >= b0 && tm <= b1) zb = Math.Max(zb, zs);
                    var vanosAqui = zb > zBase ? VanosDesde(vanos, zb) : vanos;
                    piezas.AddRange(GeneradorEntramado.Generar(a, b, zb, zTope, s0, largo - s1, vanosAqui, conf, topeEn, quiebres));
                }
            }
            if (obs.Bases.Count > 0)
                _inf.Advertencia($"Muro {w.Id}: el entramado parte sobre {obs.Bases.Count} viga(s) en su base");
            // vigas con muro por encima (p. ej. la del alero en un frontón): sobre ellas se arma otra zona de entramado,
            // con su solera inferior apoyada en la viga y hasta el borde superior del muro
            for (int iv = 0; iv < obs.Vigas.Count; iv++)
            {
                var (vt0, vt1, _, _) = obs.Vigas[iv];
                var (sup0, sup1) = obs.CaraSuperior[iv];
                double zSobre = Math.Max(sup0, sup1);
                bool sigue = Enumerable.Range(0, 11).Select(k => vt0 + (vt1 - vt0) * k / 10.0)
                                       .Any(t => PerfilEn(t) - zSobre > 0.3);
                if (!sigue) continue;
                Func<double, double> topeArriba = t =>
                {
                    double z = PerfilEn(t);
                    for (int j = 0; j < obs.Vigas.Count; j++)
                    {
                        var (t0j, t1j, z0j, z1j) = obs.Vigas[j];
                        if (j == iv || t < t0j - 1e-6 || t > t1j + 1e-6) continue;
                        double zj = t1j - t0j < 1e-6 ? z0j : z0j + (z1j - z0j) * (t - t0j) / (t1j - t0j);
                        if (zj > zSobre + 0.05) z = Math.Min(z, zj);             // otra viga más arriba
                    }
                    return z;
                };
                var vanosArriba = VanosDesde(vanos, zSobre);
                int antes = piezas.Count;
                foreach (var (t0, t1) in tramos)
                {
                    double a0 = Math.Max(t0, vt0), a1 = Math.Min(t1, vt1);
                    if (a1 - a0 > 2 * conf.B + 0.02)
                        piezas.AddRange(GeneradorEntramado.Generar(a, b, zSobre, zTope, a0, largo - a1, vanosArriba, conf, topeArriba, quiebres));
                }
                if (piezas.Count > antes)
                    _inf.Advertencia($"Muro {w.Id}: entramado también sobre la viga (a {zSobre - zBase:0.00} m), hasta el borde superior del muro");
            }
            if (obs.Pilares.Count + obs.Vigas.Count + obs.Vanos.Count > 0)
                _inf.Advertencia($"Muro {w.Id}: entramado ajustado a {obs.Pilares.Count} pilar(es), {obs.Vigas.Count} viga(s) en su parte alta " +
                                 $"y {obs.Vanos.Count} elemento(s) que lo atraviesan" +
                                 (obs.Ignorados.Count > 0 ? $"; no se consideraron: {string.Join(", ", obs.Ignorados)}" : ""));
            // revisión final: ninguna pieza puede pasar por un vano (puerta, ventana o abertura)
            // (un vano que llega al borde superior se revisa solo hasta bajo la solera superior y su dintel)
            double sol = conf.Soleras ? conf.B : 0;
            var revisar = vanos.Select(v => new VanoMuro
            {
                T0 = v.T0, T1 = v.T1, ZInf = v.ZInf, Puerta = v.Puerta,
                ZSup = Math.Min(v.ZSup, Math.Min(topeEn(v.T0), topeEn(v.T1)) - sol - conf.B),
            }).Where(v => v.ZSup - v.ZInf > 0.05).ToList();
            // (las piezas que encajonan el vano —jambas, dinteles, alféizares y soleras— se conservan siempre: si el vano
            // pasa detrás de un pilar o del borde del muro, la jamba queda en el borde y lo cierra)
            var marco = new HashSet<string> { "jamba", "dintel", "alfeizar", "solera_superior", "solera_inferior" };
            int quitadas = piezas.RemoveAll(p => !marco.Contains(p.Rol) && revisar.Any(v => Invade(p, v, a, ux, uy, conf.B)));
            if (quitadas > 0)
                _inf.Advertencia($"Muro {w.Id}: se quitaron {quitadas} pieza(s) que pasaban por un vano");
            var vanosLeidos = vanos.Count - obs.Vanos.Count;
            _inf.Advertencia($"Muro {w.Id}: {vanosLeidos} vano(s) leídos" + (vanosLeidos > 0 ? ": " + string.Join("; ",
                vanos.Take(vanosLeidos).Select(v => $"{v.T0:0.00}-{v.T1:0.00} m, z {v.ZInf - zBase:0.00}-{v.ZSup - zBase:0.00} m")) : ""));
            if (piezas.Count == 0) { _inf.Advertencia($"Muro {w.Id}: muy corto para entramado"); return; }

            // familias de madera con la sección pedida (las mismas del entramado de la app)
            string sufijo = $"{conf.B * 1000:0}x{conf.H * 1000:0}";
            var sv = SimboloPorMaterial(BuiltInCategory.OST_StructuralColumns, conf.FamiliaVertical?.Familia, conf.FamiliaVertical?.Tipo,
                                        $"muro {w.Id} (piezas verticales)", conf.Material, "rectangular", NombresAnchoMadera, NombresAltoMadera);
            var sh = SimboloPorMaterial(BuiltInCategory.OST_StructuralFraming, conf.FamiliaHorizontal?.Familia, conf.FamiliaHorizontal?.Tipo,
                                        $"muro {w.Id} (piezas horizontales)", conf.Material, "rectangular", NombresAnchoMadera, NombresAltoMadera);
            if (sv == null || sh == null)
                throw new Exception("no hay familias de madera (Dimension Lumber); cárgalas con Insertar → Cargar familia de Autodesk");
            sv = ConMedidas(sv, sufijo, (null, NombresAnchoMadera, conf.B), (null, NombresAltoMadera, conf.H));
            sh = ConMedidas(sh, sufijo + " plana", (null, NombresAnchoMadera, conf.H), (null, NombresAltoMadera, conf.B));

            var mat = IdMaterial(conf.Material);
            double angulo = Math.Atan2(uy, ux);
            int nv = 0, nh = 0;
            foreach (var p in piezas)
            {
                bool vertical = RolesVerticales.Contains(p.Rol);
                if (Intentar($"muro {w.Id} {p.Rol}", () =>
                {
                    if (vertical) ColocarVertical(p, sv, nivel, angulo, mat, "", $"{marca} {p.Rol}");
                    else ColocarHorizontal(p, sh, nivel, mat, "", $"{marca} {p.Rol}");
                }))
                {
                    if (vertical) nv++; else nh++;
                }
            }
            _inf.Contar("Muros con entramado", 1);
            _inf.Contar("Entramado: piezas verticales", nv);
            _inf.Contar("Entramado: piezas horizontales", nh);
        }

        /// <summary>Vanos que llegan por encima de <paramref name="z"/>, recortados para partir en z (una puerta que
        /// parte en el piso, bajo una viga de base, sigue siendo un vano en la zona sobre la viga).</summary>
        private static List<VanoMuro> VanosDesde(List<VanoMuro> vanos, double z) =>
            vanos.Where(v => v.ZSup > z + 0.05)
                 .Select(v => new VanoMuro { T0 = v.T0, T1 = v.T1, ZInf = Math.Max(v.ZInf, z), ZSup = v.ZSup, Puerta = v.Puerta || v.ZInf <= z + 0.01 })
                 .ToList();

        /// <summary>¿La pieza (con su sección) entra en el vano?</summary>
        private static bool Invade(PiezaDef p, VanoMuro v, double[] a, double ux, double uy, double b)
        {
            double T(double[] q) => (q[0] - a[0]) * ux + (q[1] - a[1]) * uy;
            double ti = T(p.Inicio), tf = T(p.Fin);
            double t0 = Math.Min(ti, tf), t1 = Math.Max(ti, tf);
            double z0 = Math.Min(p.Inicio[2], p.Fin[2]), z1 = Math.Max(p.Inicio[2], p.Fin[2]);
            const double e = 0.005;
            if (t1 - t0 < 1e-4) { t0 -= b / 2; t1 += b / 2; }               // vertical: su ancho a lo largo del muro
            else
            {
                // horizontal o inclinada: su recorrido sobre el vano, con su alto
                double ta = Math.Max(t0, v.T0), tb = Math.Min(t1, v.T1);
                if (tb - ta < e) return false;
                double Zen(double t) => p.Inicio[2] + (p.Fin[2] - p.Inicio[2]) * (t - ti) / (tf - ti);
                z0 = Math.Min(Zen(ta), Zen(tb)) - b / 2; z1 = Math.Max(Zen(ta), Zen(tb)) + b / 2;
                t0 = ta; t1 = tb;
            }
            return t0 < v.T1 - e && t1 > v.T0 + e && z0 < v.ZSup - e && z1 > v.ZInf + e;
        }

        /// <summary>Pilares y vigas (estructurales) que atraviesan un muro, en sus coordenadas: t a lo largo del eje y z.</summary>
        private class Obstaculos
        {
            public List<(double t0, double t1)> Pilares = new();                    // de toda la altura: cortan el muro en tramos
            public List<(double t0, double t1, double z0, double z1)> Vigas = new(); // en la parte alta: cara inferior en t0 y t1
            public List<(double z0, double z1)> CaraSuperior = new();               // de cada viga, en t0 y t1
            public List<(double t0, double t1, double zSup)> Bases = new();         // vigas en la base: el entramado parte sobre ellas
            public List<VanoMuro> Vanos = new();                                    // el resto: se rodea como un vano
            public List<string> Ignorados = new();
        }

        private Obstaculos ObstaculosEnMuro(Wall w, double[] a, double ux, double uy, double largo, double zBase, double zTope,
                                            Func<double, double> perfil = null)
        {
            var o = new Obstaculos();
            double esp = Mt(w.Width), alto = zTope - zBase;
            var caja = w.get_BoundingBox(null);
            if (caja == null) return o;
            var filtroCaja = new BoundingBoxIntersectsFilter(new Outline(caja.Min - new XYZ(0.1, 0.1, 0.1), caja.Max + new XYZ(0.1, 0.1, 0.1)));
            var cats = new ElementMulticategoryFilter(new List<BuiltInCategory>
                { BuiltInCategory.OST_StructuralColumns, BuiltInCategory.OST_StructuralFraming });
            var candidatos = new FilteredElementCollector(_doc).WhereElementIsNotElementType().WherePasses(cats).WherePasses(filtroCaja)
                .Where(e =>
                {
                    var c = e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.AsString() ?? "";
                    return !c.StartsWith("cad2revit-entramado") && !c.StartsWith(Marca + "entramado");   // ni muros ni pisos de madera
                }).ToList();
            foreach (var e in candidatos)
            {
                var pts = PuntosGeometria(e);
                if (pts.Count == 0) continue;
                var loc = pts.Select(q => (t: (Mt(q.X) - a[0]) * ux + (Mt(q.Y) - a[1]) * uy,
                                          d: (Mt(q.X) - a[0]) * uy - (Mt(q.Y) - a[1]) * ux, z: Mt(q.Z))).ToList();
                double t0 = loc.Min(x => x.t), t1 = loc.Max(x => x.t), d0 = loc.Min(x => x.d), d1 = loc.Max(x => x.d);
                double z0 = loc.Min(x => x.z), z1 = loc.Max(x => x.z);
                // ¿atraviesa el muro? (dentro de su espesor, de su largo y de su altura)
                if (d1 < -esp / 2 + 0.005 || d0 > esp / 2 - 0.005 || t1 < 0.01 || t0 > largo - 0.01 || z1 < zBase + 0.01 || z0 > zTope - 0.01)
                    continue;
                t0 = Math.Max(0, t0); t1 = Math.Min(largo, t1);
                string nombre = $"{e.Category?.Name} {e.Id}";
                bool pilar = e.Category?.Id == new ElementId(BuiltInCategory.OST_StructuralColumns);
                if (pilar)
                {
                    // pilar que corta el muro en tramos: parte abajo y llega al borde superior del muro en su posición
                    // (en un frontón, los pilares de los extremos llegan al alero, no a la cumbrera) o a la viga que
                    // lo remata (hasta 0,8 m bajo el borde)
                    double arriba = perfil == null ? zTope
                        : Enumerable.Range(0, 5).Select(k => perfil(t0 + (t1 - t0) * k / 4.0)).Min();
                    if (z0 <= zBase + 0.30 && z1 >= Math.Min(zTope, arriba) - 0.80) o.Pilares.Add((t0, t1));
                    else o.Vanos.Add(new VanoMuro { T0 = t0, T1 = t1, ZInf = Math.Max(z0, zBase), ZSup = Math.Min(z1, zTope), Puerta = z0 <= zBase + 0.01 });
                    continue;
                }
                // viga: ¿corre dentro del muro (paralela) o lo cruza?
                if (e.Location is LocationCurve lc && lc.Curve is Line ln)
                {
                    var dir = ln.Direction;
                    double horiz = Math.Sqrt(dir.X * dir.X + dir.Y * dir.Y);
                    bool paralela = horiz > 1e-6 && Math.Abs(dir.X / horiz * uy - dir.Y / horiz * ux) < 0.1;
                    if (paralela)
                    {
                        // eje de la viga en (t, z) y su sección por encima/debajo del eje: caras inferior y superior en
                        // cualquier t (las aristas rectas solo dan puntos en sus extremos)
                        XYZ e0 = ln.GetEndPoint(0), e1 = ln.GetEndPoint(1);
                        double te0 = (Mt(e0.X) - a[0]) * ux + (Mt(e0.Y) - a[1]) * uy, te1 = (Mt(e1.X) - a[0]) * ux + (Mt(e1.Y) - a[1]) * uy;
                        double Eje(double tt) => Math.Abs(te1 - te0) < 1e-6 ? Mt(Math.Max(e0.Z, e1.Z))
                                                 : Mt(e0.Z) + (Mt(e1.Z) - Mt(e0.Z)) * (tt - te0) / (te1 - te0);
                        double bajo = loc.Min(x => x.z - Eje(x.t)), sobre = loc.Max(x => x.z - Eje(x.t));
                        double Inf(double tt) => Eje(tt) + bajo;
                        bool horizontal = Math.Abs(Inf(t0) - Inf(t1)) < 0.05;
                        // viga inclinada que sigue el borde superior del muro (el par de un marco bajo el techo del
                        // frontón): el entramado termina bajo ella, con solera superior inclinada
                        double Sup(double tt) => Eje(tt) + sobre;
                        bool sigueTecho = !horizontal && perfil != null &&
                                          new[] { t0, (t0 + t1) / 2, t1 }.All(tt => Math.Abs(perfil(tt) - Sup(tt)) < 0.5 ||
                                                                             (Sup(tt) <= perfil(tt) + 0.05 && perfil(tt) - Sup(tt) < 0.5));
                        if (sigueTecho)
                        {
                            o.Vigas.Add((t0, t1, Inf(t0), Inf(t1)));
                            o.CaraSuperior.Add((Sup(t0), Sup(t1)));
                            continue;
                        }
                        if (!horizontal && z0 < zBase + 0.3 * alto)
                        {
                            o.Ignorados.Add(nombre + " (diagonal)");      // arriostramiento de altura completa
                            continue;
                        }
                        if (horizontal && z0 <= zBase + 0.3)
                        {
                            o.Bases.Add((t0, t1, Math.Min(z1, zTope)));      // viga en la base del muro: el entramado parte sobre ella
                            continue;
                        }
                        if (horizontal || z0 >= zBase + 0.5 * alto)
                        {
                            // cara inferior en cada extremo del tramo que ocupa en el muro
                            double Inferior(double tt) => Inf(tt);
                            double Superior(double tt) => Sup(tt);
                            o.Vigas.Add((t0, t1, Inferior(t0), Inferior(t1)));
                            o.CaraSuperior.Add((Superior(t0), Superior(t1)));
                            continue;
                        }
                    }
                }
                o.Vanos.Add(new VanoMuro { T0 = t0, T1 = t1, ZInf = Math.Max(z0, zBase), ZSup = Math.Min(z1, zTope), Puerta = z0 <= zBase + 0.01 });
            }
            return o;
        }

        /// <summary>Borde superior del muro a lo largo de su eje: envolvente superior (convexa) de los vértices de su
        /// geometría, en (t, z) m. En un frontón da los dos tramos inclinados hasta la cumbrera; en un muro recto, su tope.</summary>
        private List<(double t, double z)> PerfilSuperior(Wall w, double[] a, double ux, double uy, double largo, double zDefecto)
        {
            var pts = PuntosGeometria(w)
                .Select(q => (t: Math.Min(Math.Max((Mt(q.X) - a[0]) * ux + (Mt(q.Y) - a[1]) * uy, 0), largo), z: Mt(q.Z)))
                .OrderBy(x => x.t).ThenBy(x => x.z).ToList();
            if (pts.Count < 2) return new List<(double, double)>();
            // cadena superior del casco convexo (monotone chain)
            var sup = new List<(double t, double z)>();
            foreach (var p in pts.OrderBy(x => x.t).ThenByDescending(x => x.z))
            {
                while (sup.Count >= 2)
                {
                    var (t1, z1) = sup[^2]; var (t2, z2) = sup[^1];
                    if ((t2 - t1) * (p.z - z1) - (z2 - z1) * (p.t - t1) >= -1e-9) sup.RemoveAt(sup.Count - 1);
                    else break;
                }
                if (sup.Count > 0 && Math.Abs(sup[^1].t - p.t) < 1e-6) continue;   // mismo t: se queda el más alto
                sup.Add(p);
            }
            // sin quiebres de menos de 1 cm de altura
            var limpio = new List<(double t, double z)> { sup[0] };
            for (int i = 1; i + 1 < sup.Count; i++)
            {
                var (ta, za) = limpio[^1]; var (tb, zb) = sup[i]; var (tc, zc) = sup[i + 1];
                double zl = za + (zc - za) * (tb - ta) / Math.Max(tc - ta, 1e-9);
                if (Math.Abs(zb - zl) > 0.01) limpio.Add(sup[i]);
            }
            limpio.Add(sup[^1]);
            return limpio.Count >= 2 && limpio.Max(x => x.z) > zDefecto - 2 ? limpio : new List<(double, double)>();
        }

        /// <summary>Puntos de la geometría (aristas de los sólidos) de un elemento, en coordenadas del modelo.</summary>
        private static List<XYZ> PuntosGeometria(Element e)
        {
            var pts = new List<XYZ>();
            void Recorrer(GeometryElement ge)
            {
                if (ge == null) return;
                foreach (var g in ge)
                {
                    if (g is Solid sd && sd.Volume > 1e-9)
                        foreach (Edge ed in sd.Edges) pts.AddRange(ed.Tessellate());
                    else if (g is GeometryInstance gi) Recorrer(gi.GetInstanceGeometry());
                }
            }
            try { Recorrer(e.get_Geometry(new Options { DetailLevel = ViewDetailLevel.Fine, ComputeReferences = false })); }
            catch { /* sin geometría */ }
            if (pts.Count == 0 && e.get_BoundingBox(null) is BoundingBoxXYZ bb)
            {
                pts.Add(bb.Min); pts.Add(bb.Max);
                pts.Add(new XYZ(bb.Min.X, bb.Max.Y, bb.Min.Z)); pts.Add(new XYZ(bb.Max.X, bb.Min.Y, bb.Max.Z));
            }
            return pts;
        }
    }
}

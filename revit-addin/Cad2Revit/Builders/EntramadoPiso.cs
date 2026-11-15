using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;

namespace Cad2Revit
{
    /// <summary>Entramado horizontal de piso (todo en m).</summary>
    public class ConfEntramadoPiso
    {
        public double B = 0.05;                  // ancho de la pieza
        public double H = 0.075;                 // altura de la pieza (2x3)
        public string Escuadria = "2x3";
        public double Separacion = 0.40;         // entre ejes de vigas
        public bool LuzCorta = true;             // las vigas salvan la luz corta del piso
        public double Cadenetas = 0.60;          // separación de las líneas de cadenetas (0 = sin); el último espacio, lo que sobre
        public bool Perimetrales = true;         // vigas de borde en todo el contorno
        public string Material = "Pino radiata";
        public FamiliaTipoDef Familia = new() { Familia = "Dimension Lumber", Tipo = "2x3" };
    }

    public partial class Constructor
    {
        public const string MarcaEntramadoPiso = "cad2revit-entramado piso ";

        /// <summary>
        /// Entramado de piso bajo un piso (Floor) de Revit: vigas de borde en el contorno (y en los vanos), vigas a la
        /// separación pedida salvando la luz corta (o la larga) y líneas de cadenetas entre ellas. Las piezas quedan con su
        /// cara superior en la cara superior del piso (al mismo nivel que él). Si el piso ya tenía este entramado, se reemplaza.
        /// </summary>
        public Informe EntramadoDePisos(IList<Floor> pisos, ConfEntramadoPiso c)
        {
            double ox = U.OrigenX, oy = U.OrigenY;
            U.OrigenX = U.OrigenY = 0;                        // coordenadas de Revit tal cual
            try
            {
                if (!string.IsNullOrEmpty(c.Material))
                {
                    var m = new FilteredElementCollector(_doc).OfClass(typeof(Material)).Cast<Material>()
                        .FirstOrDefault(x => x.Name.Equals(c.Material, StringComparison.OrdinalIgnoreCase))
                        ?? new FilteredElementCollector(_doc).OfClass(typeof(Material)).Cast<Material>()
                        .FirstOrDefault(x => x.Name.IndexOf("pino", StringComparison.OrdinalIgnoreCase) >= 0);
                    if (m != null) _materiales[c.Material] = m.Id;
                    _m.Materiales.Add(new MaterialDef { Nombre = c.Material, Clase = "madera" });
                }
                using var grupo = new TransactionGroup(_doc, "cad2revit: entramado de piso");
                grupo.Start();
                Etapa("Entramado de piso", () =>
                {
                    foreach (var f in pisos) Intentar($"piso {f.Id}", () => EntramadoDePiso(f, c));
                });
                grupo.Assimilate();
            }
            finally { U.OrigenX = ox; U.OrigenY = oy; }
            return _inf;
        }

        private void EntramadoDePiso(Floor piso, ConfEntramadoPiso c)
        {
            var nivel = _doc.GetElement(piso.LevelId) as Level ?? throw new Exception("el piso no tiene nivel");
            var marca = MarcaEntramadoPiso + piso.Id;
            var previos = new FilteredElementCollector(_doc).WhereElementIsNotElementType()
                .Where(e => e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.AsString()?.StartsWith(marca + " ") == true)
                .Select(e => e.Id).ToList();
            if (previos.Count > 0) _doc.Delete(previos);

            // cara inferior del piso: contorno (y vanos) en planta, en m
            PlanarFace cara = null;
            void Buscar(GeometryElement ge)
            {
                foreach (var g in ge)
                {
                    if (g is Solid sd && sd.Volume > 1e-9)
                        foreach (Face fc in sd.Faces)
                            if (fc is PlanarFace pf && pf.FaceNormal.Z < -0.99 && (cara == null || pf.Area > cara.Area)) cara = pf;
                    if (g is GeometryInstance gi) Buscar(gi.GetInstanceGeometry());
                }
            }
            Buscar(piso.get_Geometry(new Options { ComputeReferences = false, DetailLevel = ViewDetailLevel.Fine }));
            if (cara == null) throw new Exception("no se encontró la cara inferior del piso");
            // el entramado va al mismo nivel que el piso: su cara superior en la cara superior del piso
            double zInf = Mt(piso.get_BoundingBox(null)?.Max.Z ?? cara.Origin.Z);
            var lazos = cara.GetEdgesAsCurveLoops()
                .Select(l => l.SelectMany(cv => cv.Tessellate().Take(cv.Tessellate().Count - 1)).Select(q => (x: Mt(q.X), y: Mt(q.Y))).ToList())
                .Where(l => l.Count >= 3).ToList();
            if (lazos.Count == 0) throw new Exception("el contorno del piso está vacío");
            double Area(List<(double x, double y)> l) =>
                Math.Abs(Enumerable.Range(0, l.Count).Sum(i => l[i].x * l[(i + 1) % l.Count].y - l[(i + 1) % l.Count].x * l[i].y)) / 2;
            var exterior = lazos.OrderByDescending(Area).First();

            // dirección: el lado más largo del contorno; las vigas salvan la luz corta (perpendiculares a él)
            var lados = Enumerable.Range(0, exterior.Count).Select(i => (a: exterior[i], b: exterior[(i + 1) % exterior.Count])).ToList();
            var largo = lados.OrderByDescending(s => Math.Pow(s.b.x - s.a.x, 2) + Math.Pow(s.b.y - s.a.y, 2)).First();
            double L0 = Math.Sqrt(Math.Pow(largo.b.x - largo.a.x, 2) + Math.Pow(largo.b.y - largo.a.y, 2));
            (double x, double y) ue = ((largo.b.x - largo.a.x) / L0, (largo.b.y - largo.a.y) / L0);
            (double x, double y) ud = (-ue.y, ue.x);
            if (!c.LuzCorta) (ue, ud) = (ud, ue);
            // coordenadas locales: s a lo largo de la línea de vigas (reparto), r a lo largo de cada viga
            (double s, double r) Loc((double x, double y) p) => (p.x * ue.x + p.y * ue.y, p.x * ud.x + p.y * ud.y);
            (double x, double y) Glo(double s, double r) => (s * ue.x + r * ud.x, s * ue.y + r * ud.y);
            var lazosL = lazos.Select(l => l.Select(Loc).ToList()).ToList();


            double B = c.B, zc = zInf - c.H / 2;                  // eje de las piezas: cara superior en la cara superior del piso
            var piezas = new List<PiezaDef>();
            void Pieza((double x, double y) a, (double x, double y) b, string rol)
            {
                if (Math.Sqrt(Math.Pow(b.x - a.x, 2) + Math.Pow(b.y - a.y, 2)) < 0.05) return;
                piezas.Add(new PiezaDef { Rol = rol, Inicio = new[] { a.x, a.y, zc }, Fin = new[] { b.x, b.y, zc }, Ancho = B, Profundidad = c.H });
            }

            // ---- orden del entramado: 1) vigas de borde en TODO el contorno del piso, 2) vigas entre ellas,
            // 3) cadenetas entre las vigas. No se toman en cuenta otros elementos del modelo ----
            const double Holgura = 0.0;
            var hulls = new List<List<(double s, double r)>>();
            // el contorno se ajusta a los elementos que ya existen en sus bordes (vigas, muros, pilares): si el piso
            // pasa por debajo de uno, su borde se corre a la cara interior del elemento; si queda corto (hasta 60 cm),
            // se alarga hasta esa cara. Así las vigas de borde quedan contra ellos y no escondidas dentro.
            var existentes = ElementosDeBorde(piso, zInf - c.H, zInf, 0.6)
                .Select(pts => Casco(pts.Select(q => Loc((q.x, q.y))).ToList())).Where(h => h.Count >= 3).ToList();
            int bordesAjustados = 0;
            if (existentes.Count > 0)
            {
                var ajustados = new List<List<(double s, double r)>>();
                foreach (var l in lazosL) ajustados.Add(AjustarContorno(l, lazosL, existentes, ref bordesAjustados));
                lazosL = ajustados;
            }
            List<(double a, double b)> CortaPoligono(List<(double s, double r)> pol, bool porS, double v)
            {
                // intervalos (en r si porS, en s si no) donde la recta s = v (o r = v) está dentro del polígono
                var xs = new List<double>();
                for (int k = 0; k < pol.Count; k++)
                {
                    var p = pol[k]; var q = pol[(k + 1) % pol.Count];
                    double pv = porS ? p.s : p.r, qv = porS ? q.s : q.r, po = porS ? p.r : p.s, qo = porS ? q.r : q.s;
                    if ((pv - v) * (qv - v) < 0) xs.Add(po + (qo - po) * (v - pv) / (qv - pv));
                }
                xs.Sort();
                var res = new List<(double, double)>();
                for (int k = 0; k + 1 < xs.Count; k += 2) res.Add((xs[k], xs[k + 1]));
                return res;
            }
            List<(double r0, double r1)> Cortes(double sv)
            {
                // con vanos: pares sucesivos de cruces de todos los lazos
                var xs = lazosL.SelectMany(l => CortaPoligono(l, true, sv).SelectMany(t => new[] { t.a, t.b })).OrderBy(x => x).ToList();
                var tramos = new List<(double, double)>();
                for (int k = 0; k + 1 < xs.Count; k += 2) tramos.Add((xs[k], xs[k + 1]));
                return tramos;
            }
            bool Dentro(double sv, double rv) => Cortes(sv).Any(t => rv > t.r0 && rv < t.r1);
            bool EnBorde(double sv, double rv) => hulls.Any(h => CortaPoligono(h, true, sv).Any(t => rv > t.a - 1e-6 && rv < t.b + 1e-6));

            // tramo libre de una viga en la línea s: del piso (alargado hasta Holgura) menos los elementos de borde;
            // cada extremo queda en la cara del elemento de borde o, si no hay, en el borde del piso (menos la viga de borde)
            double rec = c.Perimetrales ? B : 0;
            List<(double r0, double r1)> Libres(double sv)
            {
                var res = new List<(double, double)>();
                var ocup = hulls.SelectMany(h => CortaPoligono(h, true, sv)).OrderBy(t => t.a).ToList();
                foreach (var (a, b) in Cortes(sv))
                {
                    double lo = a - Holgura, hi = b + Holgura;
                    var libres = new List<(double x0, double x1, bool topeIni, bool topeFin)>();
                    double x = lo; bool tope = false;
                    foreach (var (o0, o1) in ocup)
                    {
                        if (o1 <= x) { if (o1 > lo) tope = true; continue; }
                        if (o0 >= hi) break;
                        if (o0 > x) libres.Add((x, o0, tope, true));
                        x = Math.Max(x, o1); tope = true;
                    }
                    if (x < hi) libres.Add((x, hi, tope, false));
                    foreach (var (x0, x1, t0, t1) in libres)
                    {
                        double r0 = t0 ? x0 : Math.Max(x0, a + rec), r1 = t1 ? x1 : Math.Min(x1, b - rec);
                        if (Math.Min(r1, b) - Math.Max(r0, a) > 0.1 && r1 - r0 > 0.1) res.Add((r0, r1));
                    }
                }
                return res;
            }

            // vigas de borde nuevas: solo donde el contorno del piso no tiene un elemento de borde que ya exista
            int bordesExistentes = 0;
            if (c.Perimetrales)
                foreach (var l in lazosL)
                    for (int k = 0; k < l.Count; k++)
                    {
                        var p = l[k]; var q = l[(k + 1) % l.Count];
                        double len = Math.Sqrt(Math.Pow(q.s - p.s, 2) + Math.Pow(q.r - p.r, 2));
                        if (len < 0.05) continue;
                        (double s, double r) t = ((q.s - p.s) / len, (q.r - p.r) / len), nrm = (-t.r, t.s);
                        var mid = ((p.s + q.s) / 2, (p.r + q.r) / 2);
                        double lado = Dentro(mid.Item1 + nrm.s * B, mid.Item2 + nrm.r * B) ? 1 : -1;
                        // ¿hay un elemento de borde a lo largo de esta arista (a menos de Holgura, hacia afuera o adentro)?
                        int cubiertos = 0;
                        for (int m = 1; m <= 5; m++)
                        {
                            double f = m / 6.0;
                            var pm = (s: p.s + (q.s - p.s) * f, r: p.r + (q.r - p.r) * f);
                            if (Enumerable.Range(-6, 13).Any(z => EnBorde(pm.s + nrm.s * lado * z * 0.1, pm.r + nrm.r * lado * z * 0.1))) cubiertos++;
                        }
                        if (cubiertos >= 3) { bordesExistentes++; continue; }
                        double corte = Math.Abs(t.r) > Math.Cos(Math.PI / 18) ? B : 0;
                        var a = (s: p.s + nrm.s * lado * B / 2 + t.s * corte, r: p.r + nrm.r * lado * B / 2 + t.r * corte);
                        var b = (s: q.s + nrm.s * lado * B / 2 - t.s * corte, r: q.r + nrm.r * lado * B / 2 - t.r * corte);
                        Pieza(Glo(a.s, a.r), Glo(b.s, b.r), "viga_borde");
                    }

            // ---- reparto de las vigas: cada «Separacion» entre ejes, desde la cara del elemento de borde que ya existe
            // (o desde el eje de la viga de borde nueva); el último espacio, lo que quede ----
            double sMin = lazosL.SelectMany(l => l).Min(p => p.s), sMax = lazosL.SelectMany(l => l).Max(p => p.s);
            double rMid = (lazosL.SelectMany(l => l).Min(p => p.r) + lazosL.SelectMany(l => l).Max(p => p.r)) / 2;
            var enS = hulls.SelectMany(h => CortaPoligono(h, false, rMid)).ToList();
            var caraIni = enS.Where(o => o.b > sMin - Holgura && o.b < sMin + Holgura).Select(o => (double?)o.b).Max();
            var caraFin = enS.Where(o => o.a > sMax - Holgura && o.a < sMax + Holgura).Select(o => (double?)o.a).Min();
            // líneas: (s, media pieza a cada lado, tramos)
            var lineas = new List<(double s, double medio, List<(double r0, double r1)> tramos)>();
            void Viga(double sv)
            {
                var tramos = Libres(sv);
                foreach (var (r0, r1) in tramos) Pieza(Glo(sv, r0), Glo(sv, r1), "viga");
                lineas.Add((sv, B / 2, tramos));
            }
            double sIni, sFin;
            if (caraIni.HasValue) { sIni = caraIni.Value; lineas.Add((sIni, 0, Libres(sIni + 0.01))); }
            else { sIni = sMin + B / 2; if (c.Perimetrales) lineas.Add((sIni, B / 2, Libres(sIni + 0.001))); else Viga(sIni + 0.001); }
            sFin = caraFin ?? sMax - B / 2;
            double margenFin = caraFin.HasValue ? B / 2 : B;
            for (double sv = sIni + c.Separacion; sv < sFin - margenFin - 0.01; sv += c.Separacion) Viga(sv);
            if (caraFin.HasValue) lineas.Add((sFin, 0, Libres(sFin - 0.01)));
            else if (c.Perimetrales) lineas.Add((sFin, B / 2, Libres(sFin - 0.001)));
            else Viga(sFin - 0.001);
            if (hulls.Count > 0)
                _inf.Advertencia($"Piso {piso.Id}: el entramado se apoya en {hulls.Count} elemento(s) que ya existen" +
                                 (bordesExistentes > 0 ? $"; {bordesExistentes} borde(s) del piso ya tienen su elemento (sin viga de borde nueva)" : ""));

            // cadenetas: líneas a lo largo de la luz, entre vigas vecinas
            int nCad = 0;
            if (c.Cadenetas > 0)
                for (int i = 0; i + 1 < lineas.Count; i++)
                {
                    var (s0, m0, t0) = lineas[i]; var (s1, m1, t1) = lineas[i + 1];
                    foreach (var (a0, a1) in t0)
                        foreach (var (b0, b1) in t1)
                        {
                            double r0 = Math.Max(a0, b0), r1 = Math.Min(a1, b1);
                            // líneas cada «Cadenetas» desde la cara de apoyo (elemento de borde o viga de borde); el último
                            // espacio queda con lo que sobre
                            for (double r = r0 + c.Cadenetas; r < r1 - B / 2 - 0.01; r += c.Cadenetas)
                            {
                                Pieza(Glo(s0 + m0, r), Glo(s1 - m1, r), "cadeneta");
                                nCad++;
                            }
                        }
                }

            // tipo de madera con la sección pedida y colocación
            var sh = SimboloPorMaterial(BuiltInCategory.OST_StructuralFraming, c.Familia?.Familia, c.Familia?.Tipo,
                                        $"piso {piso.Id}", c.Material, "rectangular", NombresAnchoMadera, NombresAltoMadera);
            if (sh == null)
                throw new Exception("no hay familias de madera (Dimension Lumber); cárgalas con Insertar → Cargar familia de Autodesk");
            sh = ConMedidas(sh, $"{c.B * 1000:0}x{c.H * 1000:0}", (null, NombresAnchoMadera, c.B), (null, NombresAltoMadera, c.H));
            var mat = IdMaterial(c.Material);
            int hechas = 0;
            var colocadas = new List<FamilyInstance>();
            foreach (var p in piezas)
                if (Intentar($"piso {piso.Id} {p.Rol}", () => colocadas.Add(ColocarHorizontal(p, sh, nivel, mat, "", $"{marca} {p.Rol}")))) hechas++;

            // eje fuerte vertical: la altura de cada pieza debe ser H (no B); si la familia la dejó acostada, se gira 90°
            _doc.Regenerate();
            int giradas = 0, malas = 0;
            foreach (var fi in colocadas)
            {
                var bb = fi.get_BoundingBox(null);
                if (bb == null) continue;
                double alto = Mt(bb.Max.Z - bb.Min.Z);
                if (Math.Abs(alto - c.H) < 0.01) continue;
                var giro = fi.get_Parameter(BuiltInParameter.STRUCTURAL_BEND_DIR_ANGLE);
                if (giro != null && !giro.IsReadOnly)
                {
                    giro.Set(giro.AsDouble() + Math.PI / 2);
                    giradas++;
                }
                else malas++;
            }
            if (giradas > 0)
            {
                _doc.Regenerate();
                malas += colocadas.Count(fi => fi.get_BoundingBox(null) is BoundingBoxXYZ b2 && Math.Abs(Mt(b2.Max.Z - b2.Min.Z) - c.H) >= 0.01);
                _inf.Advertencia($"Piso {piso.Id}: {giradas} pieza(s) se giraron 90° para dejar su eje fuerte vertical");
            }
            if (malas > 0)
                _inf.Advertencia($"Piso {piso.Id}: {malas} pieza(s) no quedaron con la altura de {c.H * 1000:0} mm en vertical; revisa la familia");
            else
                _inf.Advertencia($"Piso {piso.Id}: comprobado, todas las piezas tienen su altura de {c.H * 1000:0} mm en vertical (eje fuerte)");
            if (bordesAjustados > 0)
                _inf.Advertencia($"Piso {piso.Id}: {bordesAjustados} borde(s) se ajustaron a la cara de vigas o muros que ya existen");
            _inf.Contar("Pisos con entramado", 1);
            _inf.Contar("Entramado de piso: piezas", hechas);
            _inf.Advertencia($"Piso {piso.Id}: {piezas.Count(p => p.Rol == "viga")} vigas {c.Escuadria} ({c.B * 1000:0}x{c.H * 1000:0}) " +
                             $"a {c.Separacion * 100:0} cm, {piezas.Count(p => p.Rol == "viga_borde")} vigas de borde y {nCad} cadenetas; " +
                             $"cara superior a {zInf - Mt(nivel.Elevation):+0.000;-0.000} m del nivel {nivel.Name}");
        }

        /// <summary>Puntos (m) de los elementos que ya existen (vigas, muros, pilares) que cruzan la capa del entramado
        /// bajo el piso o están a menos de «holgura» de su contorno. No cuenta el propio entramado de madera.</summary>
        private List<List<(double x, double y)>> ElementosDeBorde(Floor piso, double z0, double z1, double holgura)
        {
            var res = new List<List<(double x, double y)>>();
            var bb = piso.get_BoundingBox(null);
            if (bb == null) return res;
            double h = U.M(holgura);
            var caja = new Outline(new XYZ(bb.Min.X - h, bb.Min.Y - h, U.M(z0) + U.M(0.005)), new XYZ(bb.Max.X + h, bb.Max.Y + h, U.M(z1) - U.M(0.005)));
            var cats = new ElementMulticategoryFilter(new List<BuiltInCategory>
                { BuiltInCategory.OST_StructuralFraming, BuiltInCategory.OST_Walls, BuiltInCategory.OST_StructuralColumns });
            foreach (var e in new FilteredElementCollector(_doc).WhereElementIsNotElementType().WherePasses(cats)
                         .WherePasses(new BoundingBoxIntersectsFilter(caja)))
            {
                if (e.Id == piso.Id) continue;
                var com = e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.AsString() ?? "";
                if (com.StartsWith("cad2revit-entramado") || com.StartsWith(Marca + "entramado")) continue;
                var pts = PuntosGeometria(e).Where(q => Mt(q.Z) > z0 - 0.3 && Mt(q.Z) < z1 + 0.3).ToList();
                if (pts.Count == 0) pts = PuntosGeometria(e);
                if (pts.Count >= 3) res.Add(pts.Select(q => (Mt(q.X), Mt(q.Y))).ToList());
            }
            return res;
        }

        private static bool EnPoligono(List<(double s, double r)> pol, double s, double r)
        {
            bool dentro = false;
            for (int i = 0, j = pol.Count - 1; i < pol.Count; j = i++)
                if ((pol[i].r > r) != (pol[j].r > r) &&
                    s < (pol[j].s - pol[i].s) * (r - pol[i].r) / (pol[j].r - pol[i].r) + pol[i].s)
                    dentro = !dentro;
            return dentro;
        }

        /// <summary>Corre cada arista del lazo hasta la cara del elemento existente que la cubre (hacia adentro) o que
        /// está a menos de 60 cm (hacia afuera), y recalcula los vértices como cruces de aristas vecinas.</summary>
        private static List<(double s, double r)> AjustarContorno(List<(double s, double r)> l, List<List<(double s, double r)>> todos,
                                                                  List<List<(double s, double r)>> existentes, ref int ajustados)
        {
            int n = l.Count;
            bool DentroPiso(double s, double r)
            {
                int k = 0;
                foreach (var x in todos) if (EnPoligono(x, s, r)) k++;
                return k % 2 == 1;
            }
            bool EnExistente(double s, double r) => existentes.Any(h => EnPoligono(h, s, r));
            var lineas = new List<((double s, double r) p, (double s, double r) t)>();
            for (int i = 0; i < n; i++)
            {
                var p = l[i]; var q = l[(i + 1) % n];
                double len = Math.Sqrt(Math.Pow(q.s - p.s, 2) + Math.Pow(q.r - p.r, 2));
                if (len < 1e-6) { lineas.Add((p, (1, 0))); continue; }
                (double s, double r) t = ((q.s - p.s) / len, (q.r - p.r) / len), nr = (-t.r, t.s);
                var mid = ((p.s + q.s) / 2, (p.r + q.r) / 2);
                if (!DentroPiso(mid.Item1 + nr.s * 0.02, mid.Item2 + nr.r * 0.02)) nr = (-nr.s, -nr.r);   // nr: hacia adentro
                // desplazamiento en 5 puntos de la arista: + hacia adentro (el elemento cubre el borde), − hacia afuera
                var despl = new List<double>();
                for (int m = 1; m <= 5; m++)
                {
                    double f = m / 6.0;
                    var pm = (s: p.s + (q.s - p.s) * f, r: p.r + (q.r - p.r) * f);
                    double? d = null;
                    if (EnExistente(pm.s + nr.s * 0.005, pm.r + nr.r * 0.005))
                    {
                        double x = 0.005;
                        while (x < 0.6 && EnExistente(pm.s + nr.s * x, pm.r + nr.r * x)) x += 0.005;
                        if (x < 0.6) d = x;
                    }
                    else
                        for (double x = 0.005; x <= 0.6; x += 0.005)
                            if (EnExistente(pm.s - nr.s * x, pm.r - nr.r * x)) { d = -(x - 0.005); break; }
                    if (d.HasValue) despl.Add(d.Value);
                }
                double dd = despl.Count >= 3 ? despl.OrderBy(x => x).ElementAt(despl.Count / 2) : 0;
                if (Math.Abs(dd) > 0.005) ajustados++; else dd = 0;
                lineas.Add(((p.s + nr.s * dd, p.r + nr.r * dd), t));
            }
            var res = new List<(double s, double r)>();
            for (int i = 0; i < n; i++)
            {
                var (p1, t1) = lineas[(i - 1 + n) % n]; var (p2, t2) = lineas[i];
                double den = t1.s * t2.r - t1.r * t2.s;
                if (Math.Abs(den) < 1e-9) { res.Add(p2); continue; }               // aristas paralelas: se queda el punto
                double u = ((p2.s - p1.s) * t2.r - (p2.r - p1.r) * t2.s) / den;
                res.Add((p1.s + t1.s * u, p1.r + t1.r * u));
            }
            return res;
        }

        /// <summary>Casco convexo (monotone chain) de puntos 2D.</summary>
        private static List<(double s, double r)> Casco(List<(double s, double r)> pts)
        {
            var p = pts.Distinct().OrderBy(a => a.s).ThenBy(a => a.r).ToList();
            if (p.Count < 3) return p;
            double X((double s, double r) o, (double s, double r) a, (double s, double r) b) => (a.s - o.s) * (b.r - o.r) - (a.r - o.r) * (b.s - o.s);
            var h = new List<(double s, double r)>();
            foreach (var q in p) { while (h.Count >= 2 && X(h[^2], h[^1], q) <= 1e-12) h.RemoveAt(h.Count - 1); h.Add(q); }
            int inf = h.Count + 1;
            for (int i = p.Count - 2; i >= 0; i--) { var q = p[i]; while (h.Count >= inf && X(h[^2], h[^1], q) <= 1e-12) h.RemoveAt(h.Count - 1); h.Add(q); }
            h.RemoveAt(h.Count - 1);
            return h;
        }
    }
}

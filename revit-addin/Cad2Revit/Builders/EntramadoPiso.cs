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
        public double H = 0.20;                  // altura de la pieza (2x8)
        public string Escuadria = "2x8";
        public double Separacion = 0.40;         // entre ejes de vigas
        public bool LuzCorta = true;             // las vigas salvan la luz corta del piso
        public double Cadenetas = 1.20;          // separación de las líneas de cadenetas (0 = sin)
        public bool Perimetrales = true;         // vigas de borde en todo el contorno
        public string Material = "Pino radiata";
        public FamiliaTipoDef Familia = new() { Familia = "Dimension Lumber", Tipo = "2x8" };
    }

    public partial class Constructor
    {
        public const string MarcaEntramadoPiso = "cad2revit-entramado piso ";

        /// <summary>
        /// Entramado de piso bajo un piso (Floor) de Revit: vigas de borde en el contorno (y en los vanos), vigas a la
        /// separación pedida salvando la luz corta (o la larga) y líneas de cadenetas entre ellas. Las piezas quedan con su
        /// cara superior en la cara inferior del piso. Si el piso ya tenía este entramado, se reemplaza.
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
            double zInf = Mt(cara.Origin.Z);
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

            List<(double r0, double r1)> Cortes(double s)
            {
                var rs = new List<double>();
                foreach (var l in lazosL)
                    for (int i = 0; i < l.Count; i++)
                    {
                        var p = l[i]; var q = l[(i + 1) % l.Count];
                        if ((p.s - s) * (q.s - s) < 0)
                            rs.Add(p.r + (q.r - p.r) * (s - p.s) / (q.s - p.s));
                    }
                rs.Sort();
                var tramos = new List<(double, double)>();
                for (int i = 0; i + 1 < rs.Count; i += 2) tramos.Add((rs[i], rs[i + 1]));
                return tramos;
            }
            bool Dentro(double s, double r) => Cortes(s).Any(t => r > t.r0 && r < t.r1);

            double B = c.B, zc = zInf - c.H / 2;                  // eje de las piezas: cara superior en la cara inferior del piso
            var piezas = new List<PiezaDef>();
            void Pieza((double x, double y) a, (double x, double y) b, string rol)
            {
                if (Math.Sqrt(Math.Pow(b.x - a.x, 2) + Math.Pow(b.y - a.y, 2)) < 0.05) return;
                piezas.Add(new PiezaDef { Rol = rol, Inicio = new[] { a.x, a.y, zc }, Fin = new[] { b.x, b.y, zc }, Ancho = B, Profundidad = c.H });
            }

            // vigas de borde: por dentro del contorno (y de los vanos), a B/2 de la arista
            double rec = c.Perimetrales ? B : 0;
            if (c.Perimetrales)
                foreach (var l in lazosL)
                    for (int i = 0; i < l.Count; i++)
                    {
                        var p = l[i]; var q = l[(i + 1) % l.Count];
                        double len = Math.Sqrt(Math.Pow(q.s - p.s, 2) + Math.Pow(q.r - p.r, 2));
                        if (len < 0.05) continue;
                        (double s, double r) t = ((q.s - p.s) / len, (q.r - p.r) / len), nrm = (-t.r, t.s);
                        var mid = ((p.s + q.s) / 2, (p.r + q.r) / 2);
                        double lado = Dentro(mid.Item1 + nrm.s * B, mid.Item2 + nrm.r * B) ? 1 : -1;
                        // las paralelas a las vigas se acortan en B en cada extremo (las otras corren de punta a punta)
                        double corte = Math.Abs(t.r) > Math.Cos(Math.PI / 18) ? B : 0;
                        var a = (s: p.s + nrm.s * lado * B / 2 + t.s * corte, r: p.r + nrm.r * lado * B / 2 + t.r * corte);
                        var b = (s: q.s + nrm.s * lado * B / 2 - t.s * corte, r: q.r + nrm.r * lado * B / 2 - t.r * corte);
                        Pieza(Glo(a.s, a.r), Glo(b.s, b.r), "viga_borde");
                    }

            // vigas: a la separación pedida desde el borde
            double sMin = lazosL.SelectMany(l => l).Min(p => p.s), sMax = lazosL.SelectMany(l => l).Max(p => p.s);
            var lineas = new List<(double s, List<(double r0, double r1)> tramos)>();
            if (c.Perimetrales) lineas.Add((sMin + B / 2, Cortes(sMin + B / 2 + 0.001)));
            for (double s = sMin + c.Separacion; s < sMax - B - 0.05; s += c.Separacion)
            {
                var tramos = Cortes(s).Select(t => (t.r0 + rec, t.r1 - rec)).Where(t => t.Item2 - t.Item1 > 0.1).ToList();
                foreach (var (r0, r1) in tramos) Pieza(Glo(s, r0), Glo(s, r1), "viga");
                lineas.Add((s, tramos));
            }
            if (c.Perimetrales) lineas.Add((sMax - B / 2, Cortes(sMax - B / 2 - 0.001)));

            // cadenetas: líneas a lo largo de la luz, entre vigas vecinas
            int nCad = 0;
            if (c.Cadenetas > 0)
                for (int i = 0; i + 1 < lineas.Count; i++)
                {
                    var (s0, t0) = lineas[i]; var (s1, t1) = lineas[i + 1];
                    foreach (var (a0, a1) in t0)
                        foreach (var (b0, b1) in t1)
                        {
                            double r0 = Math.Max(a0, b0), r1 = Math.Min(a1, b1);
                            if (r1 - r0 < c.Cadenetas * 0.75) continue;
                            int n = (int)Math.Floor((r1 - r0) / c.Cadenetas);
                            for (int k = 1; k <= n; k++)
                            {
                                double r = r0 + k * (r1 - r0) / (n + 1);
                                Pieza(Glo(s0 + B / 2, r), Glo(s1 - B / 2, r), "cadeneta");
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
            _inf.Contar("Pisos con entramado", 1);
            _inf.Contar("Entramado de piso: piezas", hechas);
            _inf.Advertencia($"Piso {piso.Id}: {piezas.Count(p => p.Rol == "viga")} vigas {c.Escuadria} ({c.B * 1000:0}x{c.H * 1000:0}) " +
                             $"a {c.Separacion * 100:0} cm, {piezas.Count(p => p.Rol == "viga_borde")} vigas de borde y {nCad} cadenetas; " +
                             $"cara superior a {zInf - Mt(nivel.Elevation):+0.000;-0.000} m del nivel {nivel.Name}");
        }
    }
}

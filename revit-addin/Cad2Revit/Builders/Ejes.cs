using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;

namespace Cad2Revit
{
    /// <summary>Qué hay sobre un eje y qué llega a él (muros, pilares, vigas).</summary>
    public class ContenidoEje
    {
        public Grid Eje;
        public Line Linea;                                         // del eje, en z = 0 (pies)
        public List<Wall> MurosSobre = new(), MurosQueLlegan = new();
        public List<(Wall muro, int extremo)> ExtremosMuros = new();
        public List<FamilyInstance> Pilares = new();
        public List<FamilyInstance> VigasSobre = new();
        public List<(FamilyInstance viga, int extremo)> ExtremosVigas = new();
        public int Vanos;                                          // puertas, ventanas y vanos en los muros sobre el eje
        public int MurosConEntramado;
    }

    public partial class Constructor
    {
        private static readonly double TolEje = UnitUtils.ConvertToInternalUnits(0.02, UnitTypeId.Meters);   // 2 cm

        private static Line LineaPlana(Curve c)
        {
            XYZ a = c.GetEndPoint(0), b = c.GetEndPoint(1);
            return Line.CreateBound(new XYZ(a.X, a.Y, 0), new XYZ(b.X, b.Y, 0));
        }

        private static double DistRecta(XYZ p, Line recta)                 // a la recta infinita, en planta
        {
            var u = recta.Direction; var a = recta.GetEndPoint(0);
            var v = new XYZ(p.X - a.X, p.Y - a.Y, 0);
            return Math.Abs(v.X * u.Y - v.Y * u.X);
        }

        private static bool Paralelas(XYZ u, XYZ v) => Math.Abs(u.X * v.Y - u.Y * v.X) < 0.01;

        private bool TieneEntramado(Wall w) => new FilteredElementCollector(_doc).WhereElementIsNotElementType()
            .Any(e => e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.AsString()?
                .StartsWith(MarcaEntramadoRevit + w.Id + " ") == true);

        /// <summary>Elementos sobre un eje (paralelos, a menos de 2 cm) o con un extremo en él.</summary>
        public ContenidoEje ContenidoDe(Grid g)
        {
            var c = new ContenidoEje { Eje = g };
            if (!(g.Curve is Line gl)) return c;
            c.Linea = LineaPlana(gl);
            var u = c.Linea.Direction;
            foreach (var w in new FilteredElementCollector(_doc).OfClass(typeof(Wall)).Cast<Wall>())
            {
                if (!(w.Location is LocationCurve lc) || !(lc.Curve is Line wl)) continue;
                var l = LineaPlana(wl);
                if (Paralelas(l.Direction, u))
                {
                    if (DistRecta(l.GetEndPoint(0), c.Linea) < TolEje && DistRecta(l.GetEndPoint(1), c.Linea) < TolEje)
                    {
                        c.MurosSobre.Add(w);
                        c.Vanos += w.FindInserts(true, false, true, true).Count;
                        if (TieneEntramado(w)) c.MurosConEntramado++;
                    }
                    continue;
                }
                for (int k = 0; k < 2; k++)
                    if (DistRecta(l.GetEndPoint(k), c.Linea) < TolEje)
                    {
                        c.MurosQueLlegan.Add(w); c.ExtremosMuros.Add((w, k));
                        if (TieneEntramado(w)) c.MurosConEntramado++;
                    }
            }
            foreach (var f in new FilteredElementCollector(_doc).OfClass(typeof(FamilyInstance)).Cast<FamilyInstance>())
            {
                if (f.StructuralType == StructuralType.Column && f.Location is LocationPoint lp)
                {
                    if (DistRecta(lp.Point, c.Linea) < TolEje) c.Pilares.Add(f);
                }
                else if ((f.StructuralType == StructuralType.Beam || f.StructuralType == StructuralType.Brace)
                         && f.Location is LocationCurve lc && lc.Curve is Line bl)
                {
                    var c0 = (f.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.AsString() ?? "");
                    if (c0.StartsWith(MarcaEntramadoRevit) || c0.StartsWith(Marca + "entramado")) continue;   // piezas del entramado
                    var l = LineaPlana(bl);
                    if (l.Length < 1e-6) continue;
                    if (Paralelas(l.Direction, u) && DistRecta(l.GetEndPoint(0), c.Linea) < TolEje && DistRecta(l.GetEndPoint(1), c.Linea) < TolEje)
                        c.VigasSobre.Add(f);
                    else
                        for (int k = 0; k < 2; k++)
                            if (DistRecta(l.GetEndPoint(k), c.Linea) < TolEje) c.ExtremosVigas.Add((f, k));
                }
            }
            return c;
        }

        public static string Resumen(ContenidoEje c) =>
            $"Eje {c.Eje.Name}: {c.MurosSobre.Count} muros sobre el eje ({c.Vanos} puertas/ventanas/vanos), " +
            $"{c.ExtremosMuros.Count} extremos de muros que llegan, {c.Pilares.Count} pilares, {c.VigasSobre.Count} vigas sobre el eje, " +
            $"{c.ExtremosVigas.Count} extremos de vigas que llegan, {c.MurosConEntramado} muros con entramado";

        /// <summary>Qué hay en cada eje del modelo.</summary>
        public string ReporteEjes()
        {
            var sb = new StringBuilder();
            foreach (var g in new FilteredElementCollector(_doc).OfClass(typeof(Grid)).Cast<Grid>().OrderBy(x => x.Name, new OrdenNatural()))
                sb.AppendLine(Resumen(ContenidoDe(g)));
            return sb.ToString();
        }

        /// <summary>
        /// Mueve un eje <paramref name="metros"/> en perpendicular (positivo: hacia la izquierda de su dirección) y con él
        /// todo lo que está sobre el eje; los muros y vigas que llegan a él se alargan o acortan hasta su nueva posición.
        /// Luego rehace el entramado de los muros afectados que lo tenían.
        /// </summary>
        public Informe MoverEje(Grid g, double metros)
        {
            var c = ContenidoDe(g);
            if (c.Linea == null) { _inf.Error($"El eje {g.Name} no es recto"); return _inf; }
            var u = c.Linea.Direction;
            // sentido positivo: a la derecha (+X) para un eje vertical, hacia arriba (+Y) para uno horizontal
            var normal = new XYZ(-u.Y, u.X, 0);
            if (Math.Abs(normal.X) >= Math.Abs(normal.Y) ? normal.X < 0 : normal.Y < 0) normal = -normal;
            var mov = normal * U.M(metros);

            XYZ AlNuevoEje(XYZ extremo, XYZ otro)
            {
                // el extremo se corre a lo largo de su propio elemento hasta cruzar el eje en su nueva posición
                var dir = (extremo - otro); dir = new XYZ(dir.X, dir.Y, 0);
                if (dir.GetLength() < 1e-9) return extremo + mov;
                // cruce en planta de la recta del elemento (otro + s·d) con el eje movido (p0 + r·u)
                var d = dir.Normalize(); var p0 = c.Linea.GetEndPoint(0) + mov;
                double den = d.X * u.Y - d.Y * u.X;
                if (Math.Abs(den) < 1e-9) return extremo + mov;
                double s = ((p0.X - otro.X) * u.Y - (p0.Y - otro.Y) * u.X) / den;
                var q = new XYZ(otro.X + d.X * s, otro.Y + d.Y * s, 0);
                return new XYZ(q.X, q.Y, extremo.Z);
            }

            var bloqueados = new List<string>();
            var afectados = new HashSet<ElementId>();
            using (var grupo = new TransactionGroup(_doc, $"cad2revit: mover eje {g.Name}"))
            {
                grupo.Start();
                Etapa($"Mover eje {g.Name}", () =>
                {
                    var mover = new List<Element> { g };
                    mover.AddRange(c.MurosSobre); mover.AddRange(c.Pilares); mover.AddRange(c.VigasSobre);
                    foreach (var e in mover)
                    {
                        if (e.Pinned) { bloqueados.Add($"{e.Category?.Name} {e.Id}"); continue; }
                        Intentar($"mover {e.Id}", () => ElementTransformUtils.MoveElement(_doc, e.Id, mov));
                        if (e is Wall) afectados.Add(e.Id);
                    }
                    foreach (var (w, k) in c.ExtremosMuros)
                    {
                        if (w.Pinned) { bloqueados.Add($"Muro {w.Id}"); continue; }
                        Intentar($"muro {w.Id} (extremo)", () =>
                        {
                            var lc = (LocationCurve)w.Location; var l = (Line)lc.Curve;
                            XYZ a = l.GetEndPoint(0), b = l.GetEndPoint(1);
                            lc.Curve = k == 0 ? Line.CreateBound(AlNuevoEje(a, b), b) : Line.CreateBound(a, AlNuevoEje(b, a));
                        });
                        afectados.Add(w.Id);
                    }
                    foreach (var (f, k) in c.ExtremosVigas)
                    {
                        if (f.Pinned) { bloqueados.Add($"Viga {f.Id}"); continue; }
                        Intentar($"viga {f.Id} (extremo)", () =>
                        {
                            var lc = (LocationCurve)f.Location; var l = (Line)lc.Curve;
                            XYZ a = l.GetEndPoint(0), b = l.GetEndPoint(1);
                            lc.Curve = k == 0 ? Line.CreateBound(AlNuevoEje(a, b), b) : Line.CreateBound(a, AlNuevoEje(b, a));
                        });
                    }
                });
                // entramado de los muros movidos o alargados que lo tenían
                var conEntramado = afectados.Select(id => _doc.GetElement(id) as Wall).Where(w => w != null && TieneEntramado(w)).ToList();
                if (conEntramado.Count > 0)
                {
                    var todos = new FilteredElementCollector(_doc).OfClass(typeof(Wall)).Cast<Wall>()
                        .Where(w => w.Location is LocationCurve lc && lc.Curve is Line).ToList();
                    double ox = U.OrigenX, oy = U.OrigenY;
                    U.OrigenX = U.OrigenY = 0;
                    try
                    {
                        Etapa("Entramado de los muros movidos", () =>
                        {
                            foreach (var w in conEntramado)
                                Intentar($"muro {w.Id} (entramado)", () => EntramadoDeMuro(w, todos, new ConfEntramado { H = 0 }));
                        });
                    }
                    finally { U.OrigenX = ox; U.OrigenY = oy; }
                }
                grupo.Assimilate();
                _inf.Contar("Muros con entramado rehecho", conEntramado.Count);
            }
            _inf.Advertencia($"Eje {g.Name} movido {metros:+0.000;-0.000} m");
            _inf.Contar("Muros movidos con el eje", c.MurosSobre.Count);
            _inf.Contar("Muros alargados o acortados", c.ExtremosMuros.Count);
            _inf.Contar("Pilares movidos", c.Pilares.Count);
            _inf.Contar("Vigas movidas", c.VigasSobre.Count);
            _inf.Contar("Vigas alargadas o acortadas", c.ExtremosVigas.Count);
            if (bloqueados.Count > 0)
                _inf.Advertencia("Fijados (con chincheta), no se movieron: " + string.Join(", ", bloqueados));
            _inf.Advertencia("Losas, techos y cotas no se ajustan solos: revísalos junto al eje movido.");
            return _inf;
        }

        /// <summary>
        /// Amarra los elementos a los ejes con restricciones de Revit, para mover los ejes con la herramienta Mover:
        /// pilares en un eje → «Se mueve con ejes»; muros y vigas sobre un eje → candado de alineación entre el eje y
        /// su línea de ubicación (en la vista de planta activa).
        /// </summary>
        public Informe VincularAEjes(View vista)
        {
            int pilares = 0, candados = 0, sinCandado = 0;
            var ejes = new FilteredElementCollector(_doc).OfClass(typeof(Grid)).Cast<Grid>().ToList();
            Etapa("Vincular a los ejes", () =>
            {
                foreach (var g in ejes)
                {
                    var c = ContenidoDe(g);
                    foreach (var p in c.Pilares)
                    {
                        var par = p.get_Parameter(BuiltInParameter.INSTANCE_MOVES_WITH_GRID_PARAM);
                        if (par != null && !par.IsReadOnly && par.AsInteger() != 1) { par.Set(1); pilares++; }
                    }
                    if (!(vista is ViewPlan)) continue;
                    foreach (var e in c.MurosSobre.Cast<Element>().Concat(c.VigasSobre))
                    {
                        var r = ReferenciaEje(e);
                        if (r == null) { sinCandado++; continue; }
                        if (Intentar($"candado {e.Id} - eje {g.Name}", () =>
                        {
                            var d = _doc.Create.NewAlignment(vista, new Reference(g), r);
                            d.IsLocked = true;
                        }, false)) candados++;
                        else sinCandado++;
                    }
                }
            });
            _inf.Contar("Pilares que se mueven con los ejes", pilares);
            _inf.Contar("Muros y vigas con candado al eje", candados);
            if (sinCandado > 0)
                _inf.Advertencia($"{sinCandado} muros o vigas sobre un eje no aceptaron el candado (usa «Mover eje» para moverlos)");
            if (!(vista is ViewPlan))
                _inf.Advertencia("Para poner candados a muros y vigas, ejecuta el botón desde una vista de planta.");
            return _inf;
        }

        /// <summary>Referencia de la línea de ubicación (eje) de un muro o una viga, si Revit la expone.</summary>
        private static Reference ReferenciaEje(Element e)
        {
            if (e is FamilyInstance fi)
            {
                var r = fi.GetReferences(FamilyInstanceReferenceType.CenterLeftRight).FirstOrDefault();
                if (r != null) return r;
            }
            var op = new Options { ComputeReferences = true, IncludeNonVisibleObjects = true, DetailLevel = ViewDetailLevel.Fine };
            foreach (var g in e.get_Geometry(op) ?? Enumerable.Empty<GeometryObject>())
                if (g is Line l && l.Reference != null) return l.Reference;    // la línea de ubicación (no visible)
            return null;
        }
    }

    /// <summary>Orden de nombres de ejes: 1, 2, …, 10 y A, B, …</summary>
    public class OrdenNatural : IComparer<string>
    {
        public int Compare(string x, string y)
        {
            bool nx = int.TryParse(x, out var ix), ny = int.TryParse(y, out var iy);
            if (nx && ny) return ix.CompareTo(iy);
            if (nx != ny) return nx ? -1 : 1;
            return string.Compare(x, y, StringComparison.OrdinalIgnoreCase);
        }
    }
}

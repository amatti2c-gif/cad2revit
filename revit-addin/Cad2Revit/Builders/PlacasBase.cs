using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;

namespace Cad2Revit
{
    /// <summary>Medidas de la placa base y sus pernos de anclaje (todo en mm).</summary>
    public class ConfPlacaBase
    {
        public double Vuelo = 75;            // placa: lo que sobresale del perfil a cada lado (si ancho/largo = 0)
        public double Ancho = 0;             // placa, en la dirección del ala (0 = perfil + 2 vuelos)
        public double Largo = 0;             // placa, en la dirección del alma (0 = perfil + 2 vuelos)
        public double Espesor = 15;
        public double DiametroPerno = 16;
        public double LargoPerno = 500;      // total
        public double Sobresale = 60;        // del perno sobre la cara superior de la placa
        public double BordeAncho = 40;       // del centro del perno al borde de la placa, medido en la dirección del ancho
        public double BordeLargo = 40;       // ídem en la dirección del largo
        public double LadoGolilla = 50;      // golilla cuadrada del extremo embebido
        public double EspesorGolilla = 5;
    }

    public partial class Constructor
    {
        public const string MarcaPlaca = "cad2revit-placa pilar ";

        /// <summary>
        /// Placa base rectangular bajo cada pilar (su cara superior en la base del pilar) con 4 pernos de anclaje
        /// preinstalados, para embeber en el hormigón: en el extremo inferior de cada perno, golilla cuadrada y tuerca
        /// por abajo. Se crean como formas directas (DirectShape) de conexión estructural, en acero. Si el pilar ya tenía
        /// una placa hecha con este botón, se reemplaza.
        /// </summary>
        public Informe PlacasBase(IList<FamilyInstance> pilares, ConfPlacaBase c)
        {
            double mm(double v) => U.M(v / 1000.0);
            var acero = new FilteredElementCollector(_doc).OfClass(typeof(Material)).Cast<Material>()
                .FirstOrDefault(m => new[] { "acero", "steel", "a36", "a270", "a630" }
                    .Any(k => m.Name.IndexOf(k, StringComparison.OrdinalIgnoreCase) >= 0));
            var idAcero = acero?.Id ?? ElementId.InvalidElementId;
            if (acero == null) _inf.Advertencia("No hay un material de acero en el proyecto: las placas quedan sin material");
            var opciones = new SolidOptions(idAcero, ElementId.InvalidElementId);

            ElementId Categoria(params BuiltInCategory[] cats)
            {
                foreach (var bc in cats)
                {
                    var id = new ElementId(bc);
                    if (DirectShape.IsValidCategoryId(id, _doc)) return id;
                }
                return new ElementId(BuiltInCategory.OST_GenericModel);
            }
            var catPlaca = Categoria(BuiltInCategory.OST_StructConnectionPlates, BuiltInCategory.OST_StructConnections);
            var catPerno = Categoria(BuiltInCategory.OST_StructConnectionAnchors, BuiltInCategory.OST_StructConnections);

            int hechas = 0;
            Etapa("Placas base", () =>
            {
                foreach (var p in pilares)
                {
                    Intentar($"pilar {p.Id} (placa base)", () =>
                    {
                        // placa anterior de este pilar
                        var marca = MarcaPlaca + p.Id;
                        var previas = new FilteredElementCollector(_doc).OfClass(typeof(DirectShape))
                            .Where(e => e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.AsString()?.StartsWith(marca + " ") == true)
                            .Select(e => e.Id).ToList();
                        if (previas.Count > 0) _doc.Delete(previas);

                        if (!(p.Location is LocationPoint lp))
                            throw new Exception("pilar inclinado o sin punto de ubicación: se omite");
                        var tr = p.GetTransform();
                        var ex = new XYZ(tr.BasisX.X, tr.BasisX.Y, 0);
                        if (ex.GetLength() < 1e-6) ex = XYZ.BasisX;
                        ex = ex.Normalize();
                        var ey = XYZ.BasisZ.CrossProduct(ex);

                        // sección del perfil (en sus ejes locales) cerca de su base
                        var pts = PuntosGeometria(p);
                        if (pts.Count == 0) throw new Exception("el pilar no tiene geometría");
                        double zPie = pts.Min(q => q.Z);
                        var base0 = pts.Where(q => q.Z < zPie + U.M(0.30)).ToList();
                        double X(XYZ q) => (q - lp.Point).DotProduct(ex);
                        double Y(XYZ q) => (q - lp.Point).DotProduct(ey);
                        double x0 = base0.Min(X), x1 = base0.Max(X), y0 = base0.Min(Y), y1 = base0.Max(Y);
                        var centro = new XYZ(lp.Point.X, lp.Point.Y, zPie) + ex * ((x0 + x1) / 2) + ey * ((y0 + y1) / 2);

                        double Redondear(double pies) => U.M(Math.Ceiling(Mt(pies) * 100 - 1e-6) / 100);   // a 10 mm
                        double ax = c.Ancho > 0 ? mm(c.Ancho) : Redondear((x1 - x0) + 2 * mm(c.Vuelo));
                        double ay = c.Largo > 0 ? mm(c.Largo) : Redondear((y1 - y0) + 2 * mm(c.Vuelo));
                        double e = mm(c.Espesor), bx = mm(c.BordeAncho), by = mm(c.BordeLargo), d = mm(c.DiametroPerno);
                        if (ax < x1 - x0 || ay < y1 - y0)
                            _inf.Advertencia($"Pilar {p.Id}: la placa ({Mt(ax) * 1000:0}x{Mt(ay) * 1000:0}) es más chica que el perfil");
                        if (ax / 2 - bx < (x1 - x0) / 2 + d / 2 && ay / 2 - by < (y1 - y0) / 2 + d / 2)
                            _inf.Advertencia($"Pilar {p.Id}: los pernos chocan con el perfil; aumenta el vuelo o reduce la distancia al borde");

                        if (2 * bx >= ax - d || 2 * by >= ay - d)
                            throw new Exception("la distancia de los pernos al borde es mayor que la mitad de la placa");
                        // placa: cara superior en la base del pilar
                        var placa = Caja(centro, ex, ey, ax, ay, -e, 0, opciones);

                        // pernos en las 4 esquinas, con golilla cuadrada y tuerca por abajo (extremo embebido)
                        double zSup = mm(c.Sobresale), zInf = zSup - mm(c.LargoPerno);
                        double hTuerca = 0.8 * d, sTuerca = 1.5 * d, g = mm(c.EspesorGolilla), lg = mm(c.LadoGolilla);
                        double zTuerca = zInf + U.M(0.005);
                        // golilla plana (ISO 7089: diámetro exterior ≈ 1,9 d, espesor ≈ 0,18 d, mín. 2 mm)
                        double dGolilla = 1.875 * d, eGolilla = Math.Max(0.18 * d, U.M(0.002));
                        double necesario = eGolilla + 2 * hTuerca + U.M(0.003);
                        if (mm(c.Sobresale) < necesario)
                            _inf.Advertencia($"Pilar {p.Id}: el perno sobresale {c.Sobresale:0} mm y la golilla con doble tuerca " +
                                             $"necesita {Mt(necesario) * 1000:0} mm; aumenta «Perno sobre la cara superior de la placa»");
                        var anclajes = new List<GeometryObject>();
                        foreach (var (sx, sy) in new[] { (-1, -1), (1, -1), (1, 1), (-1, 1) })
                        {
                            var q = centro + ex * (sx * (ax / 2 - bx)) + ey * (sy * (ay / 2 - by));
                            anclajes.Add(Cilindro(q, d / 2, zInf, zSup, opciones));
                            anclajes.Add(Hexagono(q, ex, sTuerca / 2, zTuerca, zTuerca + hTuerca, opciones));
                            anclajes.Add(Caja(q, ex, ey, lg, lg, zTuerca + hTuerca, zTuerca + hTuerca + g, opciones));
                            // arriba, sobre la placa: golilla plana y doble tuerca (tuerca y contratuerca)
                            anclajes.Add(Anillo(q, dGolilla / 2, d / 2 + U.M(0.001), 0, eGolilla, opciones));
                            anclajes.Add(Hexagono(q, ex, sTuerca / 2, eGolilla, eGolilla + hTuerca, opciones));
                            anclajes.Add(Hexagono(q, ex, sTuerca / 2, eGolilla + hTuerca, eGolilla + 2 * hTuerca, opciones));
                        }

                        string nombre = $"Placa base {Mt(ax) * 1000:0}x{Mt(ay) * 1000:0}x{c.Espesor:0}";
                        var dsP = DirectShape.CreateElement(_doc, catPlaca);
                        dsP.SetShape(new List<GeometryObject> { placa });
                        try { dsP.Name = nombre; } catch { }
                        dsP.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.Set($"{marca} placa");
                        var dsA = DirectShape.CreateElement(_doc, catPerno);
                        dsA.SetShape(anclajes);
                        try { dsA.Name = $"4 pernos Ø{c.DiametroPerno:0} L={c.LargoPerno:0}, golilla {c.LadoGolilla:0}x{c.LadoGolilla:0}x{c.EspesorGolilla:0} y tuerca abajo; golilla plana y doble tuerca arriba"; } catch { }
                        dsA.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.Set($"{marca} pernos");
                        hechas++;
                        _inf.Advertencia($"Pilar {p.Id}: {nombre} mm, pernos a {c.BordeAncho:0} mm del borde en el ancho y {c.BordeLargo:0} mm en el largo " +
                                         $"(separación {Mt(ax - 2 * bx) * 1000:0} x {Mt(ay - 2 * by) * 1000:0} mm), " +
                                         $"{c.LargoPerno - c.Sobresale - c.Espesor:0} mm bajo la placa");
                    });
                }
            });
            _inf.Contar("Placas base", hechas);
            return _inf;
        }

        // ---- sólidos simples (en pies; z relativas a la base del pilar) ----

        private static Solid Caja(XYZ c, XYZ ex, XYZ ey, double ax, double ay, double z0, double z1, SolidOptions o)
        {
            var b = new XYZ(c.X, c.Y, c.Z + z0);
            var p = new[] { b - ex * ax / 2 - ey * ay / 2, b + ex * ax / 2 - ey * ay / 2, b + ex * ax / 2 + ey * ay / 2, b - ex * ax / 2 + ey * ay / 2 };
            var loop = new CurveLoop();
            for (int i = 0; i < 4; i++) loop.Append(Line.CreateBound(p[i], p[(i + 1) % 4]));
            return GeometryCreationUtilities.CreateExtrusionGeometry(new List<CurveLoop> { loop }, XYZ.BasisZ, z1 - z0, o);
        }

        private static Solid Cilindro(XYZ c, double r, double z0, double z1, SolidOptions o)
        {
            var b = new XYZ(c.X, c.Y, c.Z + z0);
            var loop = new CurveLoop();
            loop.Append(Arc.Create(b, r, 0, Math.PI, XYZ.BasisX, XYZ.BasisY));
            loop.Append(Arc.Create(b, r, Math.PI, 2 * Math.PI, XYZ.BasisX, XYZ.BasisY));
            return GeometryCreationUtilities.CreateExtrusionGeometry(new List<CurveLoop> { loop }, XYZ.BasisZ, z1 - z0, o);
        }

        private static Solid Anillo(XYZ c, double rExt, double rInt, double z0, double z1, SolidOptions o)
        {
            var b = new XYZ(c.X, c.Y, c.Z + z0);
            CurveLoop Circulo(double r)
            {
                var l = new CurveLoop();
                l.Append(Arc.Create(b, r, 0, Math.PI, XYZ.BasisX, XYZ.BasisY));
                l.Append(Arc.Create(b, r, Math.PI, 2 * Math.PI, XYZ.BasisX, XYZ.BasisY));
                return l;
            }
            var interior = Circulo(rInt);
            interior.Flip();
            return GeometryCreationUtilities.CreateExtrusionGeometry(new List<CurveLoop> { Circulo(rExt), interior }, XYZ.BasisZ, z1 - z0, o);
        }

        private static Solid Hexagono(XYZ c, XYZ ex, double apotema, double z0, double z1, SolidOptions o)
        {
            var b = new XYZ(c.X, c.Y, c.Z + z0);
            double r = apotema / Math.Cos(Math.PI / 6);
            var ey = XYZ.BasisZ.CrossProduct(ex);
            var p = Enumerable.Range(0, 6).Select(i => b + ex * (r * Math.Cos(i * Math.PI / 3)) + ey * (r * Math.Sin(i * Math.PI / 3))).ToArray();
            var loop = new CurveLoop();
            for (int i = 0; i < 6; i++) loop.Append(Line.CreateBound(p[i], p[(i + 1) % 6]));
            return GeometryCreationUtilities.CreateExtrusionGeometry(new List<CurveLoop> { loop }, XYZ.BasisZ, z1 - z0, o);
        }
    }
}

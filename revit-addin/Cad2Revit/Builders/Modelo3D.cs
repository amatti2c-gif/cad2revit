using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;

namespace Cad2Revit
{
    /// <summary>
    /// Exporta los elementos seleccionados en Revit a un archivo «*.modelo3d.json» para el análisis y diseño 3D de la app
    /// (pestaña 6, modo 3D): pilares, vigas y diagonales de acero (línea en 3D, sección, dimensiones y giro de la
    /// sección) y muros (línea de base en planta, borde superior y vanos, en coordenadas del propio muro). Coordenadas en
    /// m, absolutas del modelo.
    /// </summary>
    public partial class Constructor
    {
        private string Material3D(Element e)
        {
            var id = e.get_Parameter(BuiltInParameter.STRUCTURAL_MATERIAL_PARAM)?.AsElementId();
            var m = id != null && id != ElementId.InvalidElementId ? _doc.GetElement(id) as Material : null;
            var n = (m?.Name ?? "") + " " + (m?.MaterialClass ?? "");
            if (n.IndexOf("madera", StringComparison.OrdinalIgnoreCase) >= 0 || n.IndexOf("wood", StringComparison.OrdinalIgnoreCase) >= 0 ||
                n.IndexOf("pino", StringComparison.OrdinalIgnoreCase) >= 0) return "madera";
            if (n.IndexOf("hormig", StringComparison.OrdinalIgnoreCase) >= 0 || n.IndexOf("concrete", StringComparison.OrdinalIgnoreCase) >= 0) return "hormigon";
            return "acero";
        }

        private static Dictionary<string, double> Dims3D(FamilyInstance f)
        {
            var r = new Dictionary<string, double>();
            void Leer(string clave, params string[] nombres)
            {
                foreach (var n in nombres)
                {
                    var p = f.Symbol.LookupParameter(n) ?? f.LookupParameter(n);
                    if (p != null && p.StorageType == StorageType.Double && p.AsDouble() > 0)
                    {
                        r[clave] = Math.Round(UnitUtils.ConvertFromInternalUnits(p.AsDouble(), UnitTypeId.Meters), 4);
                        return;
                    }
                }
            }
            Leer("d", "d", "h", "Height", "Altura", "Ht", "H");
            Leer("bf", "bf", "b", "Width", "Ancho", "B");
            Leer("tw", "tw", "Web Thickness", "s", "Espesor alma");
            Leer("tf", "tf", "Flange Thickness", "Espesor ala");
            Leer("t", "t", "tdes", "Wall Thickness", "Wall Nominal Thickness", "Espesor", "Espesor de pared", "e");
            Leer("D", "D", "OD", "Outside Diameter", "Diameter", "Diámetro", "Diametro", "Diámetro exterior");
            return r;
        }

        /// <summary>Extremos de la línea a–b recortados a la extensión del sólido del elemento proyectado sobre ella.</summary>
        private static (XYZ, XYZ) RecortarAGeometria(Element e, XYZ a, XYZ b)
        {
            var u = (b - a).Normalize();
            double L = a.DistanceTo(b), tmin = double.MaxValue, tmax = double.MinValue;
            void Puntos(GeometryElement ge)
            {
                if (ge == null) return;
                foreach (var go in ge)
                {
                    if (go is Solid so && so.Volume > 1e-9)
                        foreach (Edge ed in so.Edges)
                            foreach (var q in ed.Tessellate())
                            {
                                double t = (q - a).DotProduct(u);
                                tmin = Math.Min(tmin, t); tmax = Math.Max(tmax, t);
                            }
                    else if (go is GeometryInstance gi) Puntos(gi.GetInstanceGeometry());
                }
            }
            try { Puntos(e.get_Geometry(new Options { DetailLevel = ViewDetailLevel.Fine, IncludeNonVisibleObjects = false })); }
            catch { return (a, b); }
            if (tmin == double.MaxValue || tmax - tmin < 0.1 * L) return (a, b);   // sin sólido útil: la línea tal cual
            // la viga dibujada manda: se recorta (extensiones negativas, uniones) o se alarga (Start/End Extension)
            return (a + u * tmin, a + u * tmax);
        }

        public Informe ExportarModelo3D(ICollection<ElementId> seleccion, string ruta, string nombre)
        {
            double R(double v) => Math.Round(v, 4);
            double[] P(XYZ q) => new[] { R(Mt(q.X)), R(Mt(q.Y)), R(Mt(q.Z)) };
            var pilares = new List<object>();
            var vigas = new List<object>();
            var muros = new List<object>();
            int omitidos = 0;
            var motivos = new Dictionary<string, int>();
            var recortadas = new List<string>();
            void Omitir(Element el, string motivo)
            {
                omitidos++;
                var k = $"{motivo} ({el?.Category?.Name ?? "sin categoría"})";
                motivos[k] = motivos.TryGetValue(k, out var n) ? n + 1 : 1;
            }
            bool EsCat(Element el, BuiltInCategory c) => el?.Category != null && el.Category.Id.Value == (long)c;
            var enSistemas = new HashSet<ElementId>(new FilteredElementCollector(_doc).OfClass(typeof(BeamSystem))
                .Cast<BeamSystem>().SelectMany(bs => bs.GetBeamIds()));
            var todosMuros = new FilteredElementCollector(_doc).OfClass(typeof(Wall)).Cast<Wall>()
                .Where(w => w.Location is LocationCurve lc2 && lc2.Curve is Line).ToList();
            foreach (var id in seleccion)
            {
                var e = _doc.GetElement(id);
                var com = e?.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.AsString() ?? "";
                if (e == null) { omitidos++; continue; }
                if (com.StartsWith("cad2revit-entramado") || com.StartsWith(Marca + "entramado")) { Omitir(e, "entramado de madera"); continue; }
                if (e is FamilyInstance f && (f.StructuralType == StructuralType.Column || EsCat(f, BuiltInCategory.OST_StructuralColumns)))
                {
                    var bb = f.get_BoundingBox(null);
                    if (bb == null) { Omitir(f, "sin geometría"); continue; }
                    XYZ pb, pt;
                    double giro = 0;
                    if (f.Location is LocationPoint lp)
                    {
                        double zb = bb.Min.Z, zt = bb.Max.Z;
                        var nb = _doc.GetElement(f.get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_PARAM)?.AsElementId() ?? ElementId.InvalidElementId) as Level;
                        var nt = _doc.GetElement(f.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_PARAM)?.AsElementId() ?? ElementId.InvalidElementId) as Level;
                        if (nb != null && nt != null)
                        {
                            zb = nb.Elevation + (f.get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM)?.AsDouble() ?? 0);
                            zt = nt.Elevation + (f.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM)?.AsDouble() ?? 0);
                            if (zt <= zb) { zb = bb.Min.Z; zt = bb.Max.Z; }
                        }
                        pb = new XYZ(lp.Point.X, lp.Point.Y, zb); pt = new XYZ(lp.Point.X, lp.Point.Y, zt);
                        giro = lp.Rotation * 180 / Math.PI;
                    }
                    else if (f.Location is LocationCurve lcc) { pb = lcc.Curve.GetEndPoint(0); pt = lcc.Curve.GetEndPoint(1); }
                    else { Omitir(f, "sin ubicación"); continue; }
                    if (Material3D(f) == "hormigon") { Omitir(f, "hormigón"); continue; }
                    pilares.Add(new
                    {
                        id = f.Id.ToString(), seccion = f.Symbol.Name, familia = f.Symbol.FamilyName, material = Material3D(f),
                        p0 = P(pb), p1 = P(pt), giro = R(giro), dims = Dims3D(f),
                    });
                }
                else if (e is FamilyInstance fv && (fv.StructuralType == StructuralType.Beam || fv.StructuralType == StructuralType.Brace ||
                         EsCat(fv, BuiltInCategory.OST_StructuralFraming)))
                {
                    if (!(fv.Location is LocationCurve lc)) { Omitir(fv, "sin línea de ubicación"); continue; }
                    // vigas curvas: la cuerda entre sus extremos
                    var lv = lc.Curve as Line ?? Line.CreateBound(lc.Curve.GetEndPoint(0), lc.Curve.GetEndPoint(1));
                    if (Material3D(fv) == "hormigon") { Omitir(fv, "hormigón"); continue; }
                    var dims = Dims3D(fv);
                    // eje de la sección: la línea de Revit suele ir en la cara superior (justificación «Top»)
                    double bajar = 0;
                    var zj = fv.get_Parameter(BuiltInParameter.Z_JUSTIFICATION)?.AsInteger();
                    if (dims.TryGetValue("d", out var dd) && zj == (int)ZJustification.Top) bajar = dd / 2;
                    else if (dims.TryGetValue("d", out dd) && zj == (int)ZJustification.Bottom) bajar = -dd / 2;
                    XYZ a = lv.GetEndPoint(0), b = lv.GetEndPoint(1);
                    // la línea de ubicación puede ser más larga (extensiones negativas, uniones) o más corta (Start/End
                    // Extension) que la viga dibujada: se lleva a lo que ocupa su sólido a lo largo de la línea
                    var (ra, rb) = RecortarAGeometria(fv, a, b);
                    double recorte = Mt(ra.DistanceTo(a) + rb.DistanceTo(b));
                    if (recorte > 0.02)
                        recortadas.Add($"{fv.Id} ({Mt(lv.Length):0.00} → {Mt(ra.DistanceTo(rb)):0.00} m)");
                    a = ra; b = rb;
                    var pa = P(a); var pb2 = P(b);
                    pa[2] = R(pa[2] - bajar); pb2[2] = R(pb2[2] - bajar);
                    var giro = (fv.get_Parameter(BuiltInParameter.STRUCTURAL_BEND_DIR_ANGLE)?.AsDouble() ?? 0) * 180 / Math.PI;
                    vigas.Add(new
                    {
                        id = fv.Id.ToString(), seccion = fv.Symbol.Name, familia = fv.Symbol.FamilyName, material = Material3D(fv),
                        tipo = fv.StructuralType == StructuralType.Brace ? "diagonal" : "viga",
                        costanera = enSistemas.Contains(fv.Id), p0 = pa, p1 = pb2, giro = R(giro), dims,
                    });
                }
                else if (e is Wall w && w.Location is LocationCurve lcw && lcw.Curve is Line lw)
                {
                    XYZ a = lw.GetEndPoint(0), b = lw.GetEndPoint(1);
                    double[] aa = { Mt(a.X), Mt(a.Y) };
                    double lm = Mt(lw.Length);
                    double wux = (Mt(b.X) - aa[0]) / lm, wuy = (Mt(b.Y) - aa[1]) / lm;
                    var (zBase, zTop) = AlturaMuro(w);
                    var perfil = PerfilSuperior(w, aa, wux, wuy, lm, zTop).Select(q => new[] { R(q.t), R(q.z) }).OrderBy(q => q[0]).ToList();
                    if (perfil.Count < 2) perfil = new List<double[]> { new[] { 0.0, R(zTop) }, new[] { R(lm), R(zTop) } };
                    var vanos = VanosDelMuro(w, aa, wux, wuy, lm, zBase, todosMuros)
                        .Select(v => new { t0 = R(Math.Min(v.T0, v.T1)), t1 = R(Math.Max(v.T0, v.T1)), z0 = R(v.ZInf), z1 = R(v.ZSup), puerta = v.Puerta })
                        .ToList();
                    string nombreTipo = w.WallType?.Name ?? "";
                    var cs = w.WallType?.GetCompoundStructure();
                    var capaEst = cs?.GetLayers().FirstOrDefault(l => l.Function == MaterialFunctionAssignment.Structure);
                    var matEst = capaEst != null ? _doc.GetElement(capaEst.MaterialId) as Material : null;
                    string texto = nombreTipo + " " + (matEst?.Name ?? "");
                    string material = texto.IndexOf("hormig", StringComparison.OrdinalIgnoreCase) >= 0 || texto.IndexOf("concret", StringComparison.OrdinalIgnoreCase) >= 0
                        ? "hormigon"
                        : texto.IndexOf("madera", StringComparison.OrdinalIgnoreCase) >= 0 || texto.IndexOf("plataforma", StringComparison.OrdinalIgnoreCase) >= 0 ||
                          texto.IndexOf("pino", StringComparison.OrdinalIgnoreCase) >= 0 || texto.IndexOf("wood", StringComparison.OrdinalIgnoreCase) >= 0
                            ? "madera" : "otro";
                    if (material == "hormigon") { Omitir(w, "muro de hormigón"); continue; }
                    muros.Add(new
                    {
                        id = w.Id.ToString(), tipo = nombreTipo, material,
                        a = new[] { R(aa[0]), R(aa[1]) }, b = new[] { R(Mt(b.X)), R(Mt(b.Y)) }, largo = R(lm), z0 = R(zBase),
                        espesor = R(Mt(w.Width)), nucleo = R(capaEst != null ? Mt(capaEst.Width) : Mt(w.Width)), perfil, vanos,
                    });
                }
                else Omitir(e, "categoría no estructural");
            }
            var niveles = new FilteredElementCollector(_doc).OfClass(typeof(Level)).Cast<Level>().OrderBy(l => l.Elevation)
                .Select(l => new { nombre = l.Name, z = R(Mt(l.Elevation)) }).ToList();
            var archivo = new
            {
                tipo_archivo = "cad2revit-modelo-3d", version = 1, unidades = "m", nombre, proyecto = _doc.Title, niveles,
                omitidos = motivos.Select(kv => new { motivo = kv.Key, n = kv.Value }).ToList(), seleccionados = seleccion.Count,
                pilares, vigas, muros,
            };
            File.WriteAllText(ruta, JsonSerializer.Serialize(archivo, new JsonSerializerOptions { WriteIndented = true }));
            _inf.Contar("Pilares", pilares.Count);
            _inf.Contar("Vigas y diagonales", vigas.Count);
            _inf.Contar("Muros", muros.Count);
            if (omitidos > 0) _inf.Advertencia($"{omitidos} elemento(s) seleccionados no entran al modelo 3D: " +
                string.Join("; ", motivos.Select(kv => $"{kv.Value} {kv.Key}")));
            if (recortadas.Count > 0)
                _inf.Advertencia($"{recortadas.Count} viga(s) con la línea de ubicación distinta de la viga dibujada (extensiones o recortes); se usó su largo real: " +
                    string.Join(", ", recortadas.Take(8)) + (recortadas.Count > 8 ? "…" : ""));
            if (pilares.Count + vigas.Count + muros.Count == 0) _inf.Error("La selección no tiene pilares, vigas ni muros");
            _inf.Advertencia($"Archivo: {ruta}");
            return _inf;
        }
    }
}

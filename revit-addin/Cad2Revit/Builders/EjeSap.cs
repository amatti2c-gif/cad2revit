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
    /// Exporta lo que hay sobre un eje (grilla) del modelo de Revit a un archivo «eje-*.sap.json» para armar un modelo
    /// 2D simplificado en SAP2000 con sap2000-toolkit: pilares y vigas de acero (con sus secciones) y muros (con su
    /// borde superior y sus vanos), en coordenadas del plano del eje: x a lo largo del eje (m desde su inicio) y z (m).
    /// </summary>
    public partial class Constructor
    {
        public Informe ExportarEjeSap(Grid eje, string ruta, double tolerancia = 0.30)
        {
            if (!(eje.Curve is Line linea)) { _inf.Error("El eje no es recto"); return _inf; }
            double ox = U.OrigenX, oy = U.OrigenY;
            U.OrigenX = U.OrigenY = 0;
            try
            {
                XYZ p0 = linea.GetEndPoint(0), p1 = linea.GetEndPoint(1);
                double[] o = { Mt(p0.X), Mt(p0.Y) };
                double L = Math.Sqrt(Math.Pow(Mt(p1.X) - o[0], 2) + Math.Pow(Mt(p1.Y) - o[1], 2));
                double ux = (Mt(p1.X) - o[0]) / L, uy = (Mt(p1.Y) - o[1]) / L;
                double X(XYZ q) => (Mt(q.X) - o[0]) * ux + (Mt(q.Y) - o[1]) * uy;
                double D(XYZ q) => Math.Abs((Mt(q.X) - o[0]) * uy - (Mt(q.Y) - o[1]) * ux);
                double R(double v) => Math.Round(v, 4);
                bool Entramado(Element e)
                {
                    var c = e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.AsString() ?? "";
                    return c.StartsWith("cad2revit-entramado") || c.StartsWith(Marca + "entramado");
                }
                string MaterialDe(Element e)
                {
                    var id = e.get_Parameter(BuiltInParameter.STRUCTURAL_MATERIAL_PARAM)?.AsElementId();
                    var m = id != null && id != ElementId.InvalidElementId ? _doc.GetElement(id) as Material : null;
                    var n = (m?.Name ?? "") + " " + (m?.MaterialClass ?? "");
                    if (n.IndexOf("madera", StringComparison.OrdinalIgnoreCase) >= 0 || n.IndexOf("wood", StringComparison.OrdinalIgnoreCase) >= 0 ||
                        n.IndexOf("pino", StringComparison.OrdinalIgnoreCase) >= 0) return "madera";
                    if (n.IndexOf("hormig", StringComparison.OrdinalIgnoreCase) >= 0 || n.IndexOf("concrete", StringComparison.OrdinalIgnoreCase) >= 0) return "hormigon";
                    return "acero";
                }
                Dictionary<string, double> Dims(FamilyInstance f)
                {
                    var r = new Dictionary<string, double>();
                    void Leer(string clave, params string[] nombres)
                    {
                        foreach (var n in nombres)
                        {
                            var p = f.Symbol.LookupParameter(n) ?? f.LookupParameter(n);
                            if (p != null && p.StorageType == StorageType.Double && p.AsDouble() > 0) { r[clave] = R(Mt(p.AsDouble())); return; }
                        }
                    }
                    Leer("d", "d", "h", "Height", "Altura", "Ht", "H");
                    Leer("bf", "bf", "b", "Width", "Ancho", "B");
                    Leer("tw", "tw", "Web Thickness", "s", "Espesor alma");
                    Leer("tf", "tf", "Flange Thickness", "Espesor ala");
                    Leer("t", "t", "tdes", "Wall Thickness", "Espesor");
                    return r;
                }

                var pilares = new List<object>();
                var vigas = new List<object>();
                var muros = new List<object>();
                var instancias = new FilteredElementCollector(_doc).OfClass(typeof(FamilyInstance)).Cast<FamilyInstance>()
                    .Where(f => !Entramado(f)).ToList();
                foreach (var f in instancias.Where(f => f.StructuralType == StructuralType.Column))
                {
                    var bb = f.get_BoundingBox(null);
                    if (bb == null) continue;
                    XYZ pb, pt;
                    if (f.Location is LocationPoint lp) { pb = new XYZ(lp.Point.X, lp.Point.Y, bb.Min.Z); pt = new XYZ(lp.Point.X, lp.Point.Y, bb.Max.Z); }
                    else if (f.Location is LocationCurve lcc) { pb = lcc.Curve.GetEndPoint(0); pt = lcc.Curve.GetEndPoint(1); }
                    else continue;
                    if (D(pb) > tolerancia || D(pt) > tolerancia) continue;
                    pilares.Add(new
                    {
                        id = f.Id.ToString(), seccion = f.Symbol.Name, familia = f.Symbol.FamilyName, material = MaterialDe(f),
                        x0 = R(X(pb)), z0 = R(Mt(pb.Z)), x1 = R(X(pt)), z1 = R(Mt(pt.Z)), dims = Dims(f),
                    });
                }
                foreach (var f in instancias.Where(f => f.StructuralType == StructuralType.Beam || f.StructuralType == StructuralType.Brace))
                {
                    if (!(f.Location is LocationCurve lc) || !(lc.Curve is Line lv)) continue;
                    XYZ a = lv.GetEndPoint(0), b = lv.GetEndPoint(1);
                    if (D(a) > tolerancia || D(b) > tolerancia || Math.Abs(X(a) - X(b)) < 0.05 && Math.Abs(a.Z - b.Z) < U.M(0.05)) continue;
                    // eje de la sección: la línea de Revit suele ir en la cara superior (justificación «Top»)
                    var dims = Dims(f);
                    double bajar = 0;
                    var zj = f.get_Parameter(BuiltInParameter.Z_JUSTIFICATION)?.AsInteger();
                    if (dims.TryGetValue("d", out var dd) && zj == (int)ZJustification.Top) bajar = dd / 2;
                    else if (dims.TryGetValue("d", out dd) && zj == (int)ZJustification.Bottom) bajar = -dd / 2;
                    vigas.Add(new
                    {
                        id = f.Id.ToString(), seccion = f.Symbol.Name, familia = f.Symbol.FamilyName, material = MaterialDe(f),
                        tipo = f.StructuralType == StructuralType.Brace ? "diagonal" : "viga",
                        x0 = R(X(a)), z0 = R(Mt(a.Z) - bajar), x1 = R(X(b)), z1 = R(Mt(b.Z) - bajar), dims,
                    });
                }
                var todosMuros = new FilteredElementCollector(_doc).OfClass(typeof(Wall)).Cast<Wall>()
                    .Where(w => w.Location is LocationCurve lc2 && lc2.Curve is Line).ToList();
                foreach (var w in todosMuros)
                {
                    var lw = (Line)((LocationCurve)w.Location).Curve;
                    XYZ a = lw.GetEndPoint(0), b = lw.GetEndPoint(1);
                    if (D(a) > tolerancia || D(b) > tolerancia) continue;
                    double[] aa = { Mt(a.X), Mt(a.Y) };
                    double lm = Mt(lw.Length);
                    double wux = (Mt(b.X) - aa[0]) / lm, wuy = (Mt(b.Y) - aa[1]) / lm;
                    var (zBase, zTop) = AlturaMuro(w);
                    double XT(double t) => (aa[0] + wux * t - o[0]) * ux + (aa[1] + wuy * t - o[1]) * uy;
                    var perfil = PerfilSuperior(w, aa, wux, wuy, lm, zTop).Select(q => new[] { R(XT(q.t)), R(q.z) }).OrderBy(q => q[0]).ToList();
                    if (perfil.Count < 2) perfil = new List<double[]> { new[] { R(XT(0)), R(zTop) }, new[] { R(XT(lm)), R(zTop) } };
                    var vanos = VanosDelMuro(w, aa, wux, wuy, lm, zBase, todosMuros)
                        .Select(v => new { x0 = R(Math.Min(XT(v.T0), XT(v.T1))), x1 = R(Math.Max(XT(v.T0), XT(v.T1))), z0 = R(v.ZInf), z1 = R(v.ZSup), puerta = v.Puerta })
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
                    bool conEntramado = new FilteredElementCollector(_doc).WhereElementIsNotElementType()
                        .Any(e => e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.AsString()?.StartsWith(MarcaEntramadoRevit + w.Id + " ") == true);
                    muros.Add(new
                    {
                        id = w.Id.ToString(), tipo = nombreTipo, material, entramado = conEntramado,
                        x0 = R(Math.Min(XT(0), XT(lm))), x1 = R(Math.Max(XT(0), XT(lm))), z0 = R(zBase),
                        espesor = R(Mt(w.Width)), nucleo = R(capaEst != null ? Mt(capaEst.Width) : Mt(w.Width)),
                        perfil, vanos,
                    });
                }
                var niveles = new FilteredElementCollector(_doc).OfClass(typeof(Level)).Cast<Level>().OrderBy(l => l.Elevation)
                    .Select(l => new { nombre = l.Name, z = R(Mt(l.Elevation)) }).ToList();
                var archivo = new
                {
                    tipo_archivo = "cad2revit-eje-sap", version = 1, unidades = "m", eje = eje.Name,
                    origen = new[] { R(o[0]), R(o[1]) }, direccion = new[] { Math.Round(ux, 6), Math.Round(uy, 6) },
                    largo_eje = R(L), proyecto = _doc.Title, niveles, pilares, vigas, muros,
                };
                File.WriteAllText(ruta, JsonSerializer.Serialize(archivo, new JsonSerializerOptions { WriteIndented = true }));
                _inf.Contar("Pilares en el eje", pilares.Count);
                _inf.Contar("Vigas y diagonales en el eje", vigas.Count);
                _inf.Contar("Muros en el eje", muros.Count);
                _inf.Advertencia($"Archivo: {ruta}");
                if (pilares.Count + vigas.Count + muros.Count == 0)
                    _inf.Advertencia($"No hay elementos a menos de {tolerancia:0.00} m del eje {eje.Name}");
            }
            finally { U.OrigenX = ox; U.OrigenY = oy; }
            return _inf;
        }
    }
}

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
    /// Exporta las fundaciones seleccionadas en Revit a «*.fundaciones.json» para la pestaña Fundaciones de la app:
    /// zapatas corridas (WallFoundation), sobrecimientos o pedestales (muros), zapatas aisladas, vigas y losas de
    /// fundación. De cada una, su caja orientada: eje en planta (a → b, por el centro del ancho), largo, ancho, cota
    /// inferior y superior (m, coordenadas absolutas del modelo, como el modelo 3D).
    /// </summary>
    public partial class Constructor
    {
        private static List<XYZ> PuntosSolidos(Element e)
        {
            var pts = new List<XYZ>();
            void Rec(GeometryElement ge)
            {
                if (ge == null) return;
                foreach (var go in ge)
                {
                    if (go is Solid so && so.Volume > 1e-9)
                        foreach (Edge ed in so.Edges) pts.AddRange(ed.Tessellate());
                    else if (go is GeometryInstance gi) Rec(gi.GetInstanceGeometry());
                }
            }
            try { Rec(e.get_Geometry(new Options { DetailLevel = ViewDetailLevel.Fine })); } catch { }
            return pts;
        }

        /// <summary>Contornos (lazos) de la cara superior más grande del elemento, en m: el exterior y los huecos (p. ej. un
        /// cimiento en anillo modelado como losa de fundación).</summary>
        private List<List<double[]>> ContornosSuperiores(Element e)
        {
            Face mejor = null;
            double area = 0;
            void Buscar(GeometryElement ge)
            {
                if (ge == null) return;
                foreach (var go in ge)
                {
                    if (go is Solid so && so.Volume > 1e-9)
                        foreach (Face f in so.Faces)
                            if (f is PlanarFace pf && pf.FaceNormal.Z > 0.9 && f.Area > area) { mejor = f; area = f.Area; }
                    if (go is GeometryInstance gi) Buscar(gi.GetInstanceGeometry());
                }
            }
            try { Buscar(e.get_Geometry(new Options { DetailLevel = ViewDetailLevel.Fine })); } catch { }
            var out_ = new List<List<double[]>>();
            if (mejor == null) return out_;
            try
            {
                foreach (var lazo in mejor.GetEdgesAsCurveLoops())
                {
                    var pts = new List<double[]>();
                    foreach (var c in lazo)
                    {
                        var tes = c.Tessellate();
                        for (int i = 0; i < tes.Count - 1; i++)
                            pts.Add(new[] { Math.Round(Mt(tes[i].X), 4), Math.Round(Mt(tes[i].Y), 4), Math.Round(Mt(tes[i].Z), 4) });
                    }
                    if (pts.Count >= 3) out_.Add(pts);
                }
            }
            catch { }
            return out_;
        }

        public Informe ExportarFundaciones(ICollection<ElementId> seleccion, string ruta, string nombre)
        {
            double R(double v) => Math.Round(v, 4);
            var elementos = new List<object>();
            var motivos = new Dictionary<string, int>();
            bool EsCat(Element el, BuiltInCategory c) => el?.Category != null && el.Category.Id.Value == (long)c;
            foreach (var id in seleccion)
            {
                var e = _doc.GetElement(id);
                if (e == null) continue;
                string clase;
                if (e is WallFoundation) clase = "zapata_corrida";
                else if (e is Wall) clase = "muro";
                else if (e is Floor && EsCat(e, BuiltInCategory.OST_StructuralFoundation)) clase = "losa";
                else if (e is FamilyInstance fi && EsCat(fi, BuiltInCategory.OST_StructuralFoundation)) clase = "zapata";
                else if (e is FamilyInstance fv && (EsCat(fv, BuiltInCategory.OST_StructuralFraming))) clase = "viga";
                else if (EsCat(e, BuiltInCategory.OST_StructuralFoundation)) clase = "zapata";
                else
                {
                    var k = $"no es fundación ({e.Category?.Name ?? "sin categoría"})";
                    motivos[k] = motivos.TryGetValue(k, out var n) ? n + 1 : 1;
                    continue;
                }
                var pts = PuntosSolidos(e);
                if (pts.Count < 4)
                {
                    var k = $"sin geometría ({e.Category?.Name})";
                    motivos[k] = motivos.TryGetValue(k, out var n) ? n + 1 : 1;
                    continue;
                }
                // dirección del eje: la línea de ubicación (muro, viga, muro anfitrión de la zapata corrida), la
                // orientación de la familia, o la dirección más larga de la planta
                XYZ u = null;
                Curve linea = (e.Location as LocationCurve)?.Curve;
                if (linea == null && e is WallFoundation wf && _doc.GetElement(wf.WallId) is Wall anf) linea = (anf.Location as LocationCurve)?.Curve;
                if (linea != null) u = (linea.GetEndPoint(1) - linea.GetEndPoint(0));
                else if (e is FamilyInstance f2) u = f2.HandOrientation;
                if (u == null || new XYZ(u.X, u.Y, 0).GetLength() < 1e-9)
                {
                    double best = -1;
                    for (int ang = 0; ang < 180; ang += 5)
                    {
                        var d = new XYZ(Math.Cos(ang * Math.PI / 180), Math.Sin(ang * Math.PI / 180), 0);
                        var ts = pts.Select(q => q.DotProduct(d)).ToList();
                        if (ts.Max() - ts.Min() > best) { best = ts.Max() - ts.Min(); u = d; }
                    }
                }
                u = new XYZ(u.X, u.Y, 0).Normalize();
                var nrm = new XYZ(-u.Y, u.X, 0);
                var tu = pts.Select(q => q.DotProduct(u)).ToList();
                var tn = pts.Select(q => q.DotProduct(nrm)).ToList();
                double t0 = tu.Min(), t1 = tu.Max(), n0 = tn.Min(), n1 = tn.Max();
                double zi = pts.Min(q => q.Z), zs = pts.Max(q => q.Z);
                double nc = (n0 + n1) / 2;
                XYZ A = u * t0 + nrm * nc, B = u * t1 + nrm * nc;
                var tipo = (_doc.GetElement(e.GetTypeId()) as ElementType)?.Name ?? e.Name;
                elementos.Add(new
                {
                    id = e.Id.ToString(), clase, categoria = e.Category?.Name ?? "", tipo, material = Material3D(e),
                    a = new[] { R(Mt(A.X)), R(Mt(A.Y)) }, b = new[] { R(Mt(B.X)), R(Mt(B.Y)) },
                    largo = R(Mt(t1 - t0)), ancho = R(Mt(n1 - n0)), z_inf = R(Mt(zi)), z_sup = R(Mt(zs)),
                    contornos = clase == "losa" || clase == "zapata_corrida" ? ContornosSuperiores(e) : new List<List<double[]>>(),
                });
            }
            var niveles = new FilteredElementCollector(_doc).OfClass(typeof(Level)).Cast<Level>().OrderBy(l => l.Elevation)
                .Select(l => new { nombre = l.Name, z = R(Mt(l.Elevation)) }).ToList();
            var archivo = new
            {
                tipo_archivo = "cad2revit-fundaciones", version = 1, unidades = "m", nombre, proyecto = _doc.Title, niveles,
                omitidos = motivos.Select(kv => new { motivo = kv.Key, n = kv.Value }).ToList(), elementos,
            };
            File.WriteAllText(ruta, JsonSerializer.Serialize(archivo, new JsonSerializerOptions { WriteIndented = true }));
            foreach (var g in elementos.GroupBy(x => (string)x.GetType().GetProperty("clase").GetValue(x)))
                _inf.Contar(g.Key switch
                {
                    "zapata_corrida" => "Zapatas corridas", "muro" => "Sobrecimientos o pedestales (muros)", "zapata" => "Zapatas aisladas",
                    "viga" => "Vigas de fundación", "losa" => "Losas de fundación", _ => g.Key,
                }, g.Count());
            if (motivos.Count > 0)
                _inf.Advertencia("No se exportaron: " + string.Join("; ", motivos.Select(kv => $"{kv.Value} {kv.Key}")));
            if (elementos.Count == 0) _inf.Error("La selección no tiene fundaciones (zapatas, sobrecimientos, vigas o losas de fundación)");
            _inf.Advertencia($"Archivo: {ruta}");
            return _inf;
        }
    }
}

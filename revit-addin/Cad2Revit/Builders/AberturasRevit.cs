using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.Json;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.ExtensibleStorage;

namespace Cad2Revit
{
    /// <summary>aberturas.json de la app: puertas y ventanas con su posición (centro en el eje del muro) y medidas, en m.</summary>
    public class ArchivoAberturas
    {
        public string TipoArchivo { get; set; }
        public string Unidades { get; set; }
        public string Proyecto { get; set; }
        public int? Corrida { get; set; }
        public double[] OrigenSugerido { get; set; }
        public List<AberturaApp> Aberturas { get; set; } = new();

        public static ArchivoAberturas Leer(string ruta)
        {
            var op = new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower, PropertyNameCaseInsensitive = true };
            var a = JsonSerializer.Deserialize<ArchivoAberturas>(File.ReadAllText(ruta), op);
            if (a == null || a.TipoArchivo != "cad2revit-aberturas")
                throw new InvalidDataException("No es un aberturas.json de cad2revit (descárgalo en Resultado → ⬇ aberturas.json).");
            return a;
        }
    }

    public class AberturaApp
    {
        public string Id { get; set; }
        public string Categoria { get; set; }
        public string Nivel { get; set; }
        public double CotaNivel { get; set; }
        public string Eje { get; set; }
        public string Muro { get; set; }
        public double[] DireccionMuro { get; set; }
        public double[] Centro { get; set; }
        public double Ancho { get; set; }
        public double Alto { get; set; }
        public double Antepecho { get; set; }
        /// <summary>Posición a lo largo del muro medida desde los ejes perpendiculares vecinos (m, con signo).</summary>
        public List<ReferenciaEje> Referencias { get; set; } = new();
    }

    public class ReferenciaEje
    {
        public string Eje { get; set; }
        public double Desde { get; set; }
    }

    public partial class Constructor
    {
        private static readonly Guid GuidAberturas = new Guid("b2f4c7d1-5a3e-4e8b-9c61-7d2a0e4f8b13");

        private static Schema EsquemaAberturas()
        {
            var e = Schema.Lookup(GuidAberturas);
            if (e != null) return e;
            var sb = new SchemaBuilder(GuidAberturas);
            sb.SetSchemaName("cad2revit_aberturas");
            sb.SetReadAccessLevel(AccessLevel.Public);
            sb.SetWriteAccessLevel(AccessLevel.Public);
            sb.AddSimpleField("reg", typeof(string));
            return sb.Finish();
        }

        /// <summary>Vano creado por «Puertas y ventanas» (registro en la información del proyecto).</summary>
        private class VanoRegistrado
        {
            public ElementId Elemento, Muro;
            public string Categoria;
            public double X, Y, Z0, Ancho, Alto;
            public bool Usado;
            public override string ToString() => string.Join("|", Elemento.Value.ToString(CultureInfo.InvariantCulture),
                Muro.Value.ToString(CultureInfo.InvariantCulture), Categoria,
                F(X), F(Y), F(Z0), F(Ancho), F(Alto));
            private static string F(double v) => v.ToString("0.####", CultureInfo.InvariantCulture);
            public static VanoRegistrado De(string s)
            {
                var p = s.Split('|');
                if (p.Length < 8) return null;
                double D(int i) => double.Parse(p[i], CultureInfo.InvariantCulture);
                return new VanoRegistrado
                {
                    Elemento = new ElementId(long.Parse(p[0], CultureInfo.InvariantCulture)),
                    Muro = new ElementId(long.Parse(p[1], CultureInfo.InvariantCulture)),
                    Categoria = p[2], X = D(3), Y = D(4), Z0 = D(5), Ancho = D(6), Alto = D(7),
                };
            }
        }

        /// <summary>Altura de un muro de Revit: base y tope absolutos (m).</summary>
        private (double zBase, double zTope) AlturaMuro(Wall w)
        {
            var nivel = _doc.GetElement(w.LevelId) as Level;
            double zBase = Mt((nivel?.Elevation ?? 0) + (w.get_Parameter(BuiltInParameter.WALL_BASE_OFFSET)?.AsDouble() ?? 0));
            var tope = w.get_Parameter(BuiltInParameter.WALL_HEIGHT_TYPE)?.AsElementId();
            double alto;
            if (tope != null && tope != ElementId.InvalidElementId && _doc.GetElement(tope) is Level nt)
                alto = Mt(nt.Elevation + (w.get_Parameter(BuiltInParameter.WALL_TOP_OFFSET)?.AsDouble() ?? 0)) - zBase;
            else
                alto = Mt(w.get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM)?.AsDouble() ?? 0);
            return (zBase, zBase + alto);
        }

        /// <summary>
        /// Abre en los muros de Revit los vanos de las puertas y ventanas de la app (aberturas.json): busca el muro que
        /// pasa por cada una (paralelo, a menos de su medio espesor + 30 cm), crea un vano rectangular con su ancho, alto
        /// y antepecho, y sincroniza con la carga anterior (ajusta lo que cambió, borra lo que ya no está; solo lo que
        /// creó este botón). Después rehace el entramado de los muros que lo tenían.
        /// </summary>
        public Informe CargarAberturas(ArchivoAberturas archivo)
        {
            double ox = U.OrigenX, oy = U.OrigenY;
            try
            {
                // mismo origen que la importación (guardado en el proyecto) o el que trae el archivo
                string guardado = null;
                try
                {
                    var ent = _doc.ProjectInformation.GetEntity(EsquemaOrigen());
                    guardado = ent != null && ent.IsValid() ? ent.Get<string>("xy") : null;
                }
                catch { /* sin origen guardado */ }
                if (!string.IsNullOrEmpty(guardado))
                {
                    var p = guardado.Split(';');
                    U.OrigenX = double.Parse(p[0], CultureInfo.InvariantCulture);
                    U.OrigenY = double.Parse(p[1], CultureInfo.InvariantCulture);
                }
                else
                {
                    U.OrigenX = archivo.OrigenSugerido?.ElementAtOrDefault(0) ?? 0;
                    U.OrigenY = archivo.OrigenSugerido?.ElementAtOrDefault(1) ?? 0;
                    _inf.Advertencia($"Este archivo de Revit no tiene el origen de una importación de cad2revit; se usó el del " +
                                     $"archivo ({U.OrigenX:0}, {U.OrigenY:0}) en 0, 0 de Revit.");
                }

                var muros = new FilteredElementCollector(_doc).OfClass(typeof(Wall)).Cast<Wall>()
                    .Where(w => w.Location is LocationCurve lc && lc.Curve is Line).ToList();
                var niveles = new FilteredElementCollector(_doc).OfClass(typeof(Level)).Cast<Level>().ToList();

                // registro anterior
                var registro = new List<VanoRegistrado>();
                try
                {
                    var ent = _doc.ProjectInformation.GetEntity(EsquemaAberturas());
                    var txt = ent != null && ent.IsValid() ? ent.Get<string>("reg") : null;
                    foreach (var linea in (txt ?? "").Split('\n', StringSplitOptions.RemoveEmptyEntries))
                    {
                        var r = VanoRegistrado.De(linea);
                        if (r != null && _doc.GetElement(r.Elemento) != null) registro.Add(r);
                    }
                }
                catch { /* sin registro */ }

                // ejes de Revit por nombre: cada abertura se ubica desde los ejes del modelo de Revit (donde estén),
                // no desde coordenadas absolutas
                var ejesRevit = new FilteredElementCollector(_doc).OfClass(typeof(Grid)).Cast<Grid>()
                    .Where(g => g.Curve is Line).GroupBy(g => g.Name).ToDictionary(x => x.Key, x => (Line)x.First().Curve);
                int desdeEjes = 0;
                var sinEjes = new List<string>();
                foreach (var a in archivo.Aberturas)
                {
                    var u = new XYZ(a.DireccionMuro?[0] ?? 1, a.DireccionMuro?[1] ?? 0, 0);
                    var refe = (a.Referencias ?? new List<ReferenciaEje>()).OrderBy(r => Math.Abs(r.Desde))
                        .FirstOrDefault(r => r.Eje != null && ejesRevit.TryGetValue(r.Eje, out var lr) &&
                                             Math.Abs(lr.Direction.X * u.X + lr.Direction.Y * u.Y) < 0.1);
                    if (refe == null) { sinEjes.Add(a.Id); continue; }
                    var lref = ejesRevit[refe.Eje];
                    // recta del muro: el eje de Revit donde va la abertura, o la posición del archivo
                    XYZ p0; XYZ dir = u;
                    if (a.Eje != null && ejesRevit.TryGetValue(a.Eje, out var lhost) &&
                        Math.Abs(lhost.Direction.X * u.Y - lhost.Direction.Y * u.X) < 0.1)
                        p0 = new XYZ(lhost.GetEndPoint(0).X, lhost.GetEndPoint(0).Y, 0);
                    else
                        p0 = new XYZ(U.M(a.Centro[0] - U.OrigenX), U.M(a.Centro[1] - U.OrigenY), 0);
                    // cruce de la recta del muro con el eje de referencia, y desde ahí la distancia del archivo
                    var q0 = new XYZ(lref.GetEndPoint(0).X, lref.GetEndPoint(0).Y, 0); var k = lref.Direction;
                    double den = dir.X * k.Y - dir.Y * k.X;
                    if (Math.Abs(den) < 1e-9) { sinEjes.Add(a.Id); continue; }
                    double s_ = ((q0.X - p0.X) * k.Y - (q0.Y - p0.Y) * k.X) / den;
                    var c = new XYZ(p0.X + dir.X * s_, p0.Y + dir.Y * s_, 0) + dir * U.M(refe.Desde);
                    a.Centro = new[] { Mt(c.X) + U.OrigenX, Mt(c.Y) + U.OrigenY };
                    desdeEjes++;
                }
                _inf.Contar("Vanos ubicados desde los ejes de Revit", desdeEjes);
                if (sinEjes.Count > 0)
                    _inf.Advertencia("Sin sus ejes en Revit (se usó la posición del archivo): " + string.Join(", ", sinEjes));

                var nuevoRegistro = new List<VanoRegistrado>();
                var tocados = new HashSet<ElementId>();
                var sinMuro = new List<string>();
                var yaTenia = new List<string>();
                int creados = 0, iguales = 0, ajustados = 0, borrados = 0;

                using (var grupo = new TransactionGroup(_doc, "cad2revit: puertas y ventanas"))
                {
                    grupo.Start();
                    Etapa("Vanos de puertas y ventanas", () =>
                    {
                        foreach (var a in archivo.Aberturas)
                        {
                            double zNivel = niveles.FirstOrDefault(l => l.Name == U.NombreSeguro(a.Nivel)) is Level lv
                                ? Mt(lv.Elevation) : a.CotaNivel;
                            double z0 = zNivel + a.Antepecho, z1 = z0 + a.Alto;
                            double du = a.DireccionMuro?[0] ?? 1, dv = a.DireccionMuro?[1] ?? 0;

                            // muro de Revit que pasa por la abertura
                            Wall host = null; double mejor = double.MaxValue, tHost = 0, largoHost = 0;
                            double[] aHost = null, uHost = null;
                            foreach (var w in muros)
                            {
                                var ln = (Line)((LocationCurve)w.Location).Curve;
                                XYZ p0 = ln.GetEndPoint(0), p1 = ln.GetEndPoint(1);
                                double[] pa = { Mt(p0.X) + U.OrigenX, Mt(p0.Y) + U.OrigenY }, pb = { Mt(p1.X) + U.OrigenX, Mt(p1.Y) + U.OrigenY };
                                double L = Math.Sqrt(Math.Pow(pb[0] - pa[0], 2) + Math.Pow(pb[1] - pa[1], 2));
                                if (L < 0.3) continue;
                                double ux = (pb[0] - pa[0]) / L, uy = (pb[1] - pa[1]) / L;
                                if (Math.Abs(ux * dv - uy * du) > 0.05) continue;                  // paralelo al muro de la app
                                double t = (a.Centro[0] - pa[0]) * ux + (a.Centro[1] - pa[1]) * uy;
                                double d = Math.Abs((a.Centro[0] - pa[0]) * uy - (a.Centro[1] - pa[1]) * ux);
                                if (d > Mt(w.Width) / 2 + 0.30 || t < -0.05 || t > L + 0.05) continue;
                                var (zb, zt) = AlturaMuro(w);
                                if (zb > z0 + 0.05 || zt < z1 - 0.05) continue;                    // el vano debe caber en altura
                                if (d < mejor) { mejor = d; host = w; tHost = t; largoHost = L; aHost = pa; uHost = new[] { ux, uy }; }
                            }
                            if (host == null)
                            {
                                sinMuro.Add($"{a.Id} ({a.Categoria}, eje {a.Eje ?? "?"}, {a.Centro[0]:0.00}; {a.Centro[1]:0.00})");
                                continue;
                            }
                            // centro sobre el eje del muro de Revit
                            double cx = aHost[0] + uHost[0] * tHost, cy = aHost[1] + uHost[1] * tHost;

                            // ¿ya lo creó este botón?
                            var previo = registro.FirstOrDefault(r => !r.Usado && r.Categoria == a.Categoria &&
                                                                 Math.Sqrt(Math.Pow(r.X - cx, 2) + Math.Pow(r.Y - cy, 2)) < 0.30);
                            if (previo != null)
                            {
                                previo.Usado = true;
                                bool igual = previo.Muro == host.Id && Math.Abs(previo.X - cx) < 0.005 && Math.Abs(previo.Y - cy) < 0.005 &&
                                             Math.Abs(previo.Z0 - z0) < 0.005 && Math.Abs(previo.Ancho - a.Ancho) < 0.005 &&
                                             Math.Abs(previo.Alto - a.Alto) < 0.005;
                                if (igual) { nuevoRegistro.Add(previo); iguales++; continue; }
                                _doc.Delete(previo.Elemento);
                                tocados.Add(previo.Muro);
                                ajustados++;
                            }
                            else
                            {
                                // ¿el muro ya tiene ahí una puerta, ventana o vano (de la importación o hecho a mano)?
                                bool ocupado = false;
                                foreach (var id in host.FindInserts(true, false, true, true))
                                {
                                    if (registro.Any(r => r.Elemento == id)) continue;
                                    var e = _doc.GetElement(id);
                                    XYZ c = null;
                                    if (e is FamilyInstance fi && fi.Location is LocationPoint lp) c = lp.Point;
                                    else if (e is Opening op && op.BoundaryRect?.Count >= 2) c = (op.BoundaryRect[0] + op.BoundaryRect[1]) / 2;
                                    if (c == null) continue;
                                    double tc = (Mt(c.X) + U.OrigenX - aHost[0]) * uHost[0] + (Mt(c.Y) + U.OrigenY - aHost[1]) * uHost[1];
                                    if (Math.Abs(tc - tHost) < a.Ancho / 2) { ocupado = true; break; }
                                }
                                if (ocupado) { yaTenia.Add(a.Id); continue; }
                            }

                            ElementId nuevo = null;
                            var ok = Intentar($"{a.Id} (vano)", () =>
                            {
                                var p1 = U.P3(new[] { cx - uHost[0] * a.Ancho / 2, cy - uHost[1] * a.Ancho / 2, z0 });
                                var p2 = U.P3(new[] { cx + uHost[0] * a.Ancho / 2, cy + uHost[1] * a.Ancho / 2, z1 });
                                var op = _doc.Create.NewOpening(host, p1, p2);
                                op.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.Set($"cad2revit-vano {a.Id} {a.Categoria}");
                                nuevo = op.Id;
                            });
                            if (!ok || nuevo == null) continue;
                            if (previo == null) creados++;
                            tocados.Add(host.Id);
                            nuevoRegistro.Add(new VanoRegistrado
                            {
                                Elemento = nuevo, Muro = host.Id, Categoria = a.Categoria, X = cx, Y = cy, Z0 = z0, Ancho = a.Ancho, Alto = a.Alto,
                            });
                        }
                        // lo que creó este botón y ya no está en la app
                        foreach (var r in registro.Where(r => !r.Usado))
                        {
                            if (_doc.GetElement(r.Elemento) == null) continue;
                            _doc.Delete(r.Elemento);
                            tocados.Add(r.Muro);
                            borrados++;
                        }
                    });

                    // entramado de los muros tocados que ya lo tenían (con jambas, dinteles y alféizares en los vanos)
                    var conEntramado = tocados.Select(id => _doc.GetElement(id) as Wall).Where(w => w != null &&
                        new FilteredElementCollector(_doc).WhereElementIsNotElementType()
                            .Any(e => e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.AsString()?
                                .StartsWith(MarcaEntramadoRevit + w.Id + " ") == true)).ToList();
                    if (conEntramado.Count > 0)
                    {
                        var todos = muros;
                        var conf = new ConfEntramado { H = 0 };
                        double gx = U.OrigenX, gy = U.OrigenY;
                        U.OrigenX = U.OrigenY = 0;            // el entramado trabaja en coordenadas de Revit tal cual
                        try
                        {
                            Etapa("Entramado de los muros con vanos nuevos", () =>
                            {
                                foreach (var w in conEntramado) Intentar($"muro {w.Id} (entramado)", () => EntramadoDeMuro(w, todos, conf));
                            });
                        }
                        finally { U.OrigenX = gx; U.OrigenY = gy; }
                        _inf.Contar("Muros con entramado rehecho", conEntramado.Count);
                    }

                    Etapa("Registro de vanos", () =>
                    {
                        var ent = new Entity(EsquemaAberturas());
                        ent.Set("reg", string.Join("\n", nuevoRegistro.Select(r => r.ToString())));
                        _doc.ProjectInformation.SetEntity(ent);
                    });
                    grupo.Assimilate();
                }

                _inf.Contar("Vanos nuevos", creados);
                if (ajustados > 0) _inf.Contar("Vanos ajustados (cambiaron en la app)", ajustados);
                if (iguales > 0) _inf.Contar("Vanos sin cambios", iguales);
                if (borrados > 0) _inf.Contar("Vanos eliminados (ya no están en la app)", borrados);
                foreach (var s in sinMuro)
                    _inf.Advertencia($"Sin muro en Revit para {s}: no hay un muro paralelo que pase por ahí y alcance su altura");
                if (yaTenia.Count > 0)
                    _inf.Advertencia($"Ya tenían puerta, ventana o vano en ese lugar (no se tocaron): {string.Join(", ", yaTenia)}");
            }
            finally
            {
                U.OrigenX = ox; U.OrigenY = oy;
            }
            return _inf;
        }
    }
}

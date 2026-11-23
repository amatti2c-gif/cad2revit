using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;
using Autodesk.Revit.UI;

namespace Cad2Revit
{
    /// <summary>
    /// «Costaneras a análisis»: exporta los sistemas de vigas (Structural Framing Systems) con sus costaneras (línea,
    /// tipo y parámetros de la sección) y las demás vigas estructurales (las que las soportan por debajo), y lo sube a la
    /// pestaña «8 · Elementos secundarios» de la app, que busca los apoyos, las luces libres y el ancho colaborante y
    /// verifica las costaneras (AISI S100-16 / NCh427/2).
    /// </summary>
    [Transaction(TransactionMode.ReadOnly)]
    public class ComandoCostaneras : IExternalCommand
    {
        private static double Mt(double pies) => UnitUtils.ConvertFromInternalUnits(pies, UnitTypeId.Meters);
        private static double[] P(XYZ p) => new[] { Mt(p.X), Mt(p.Y), Mt(p.Z) };

        /// <summary>Parámetros de longitud del tipo (y de la instancia), en mm, por nombre.</summary>
        private static Dictionary<string, double> Dimensiones(Element e)
        {
            var d = new Dictionary<string, double>();
            foreach (var el in new[] { e.Document.GetElement(e.GetTypeId()), e })
            {
                if (el == null) continue;
                foreach (Parameter p in el.Parameters)
                {
                    try
                    {
                        if (p.StorageType != StorageType.Double || !p.HasValue) continue;
                        if (p.Definition.GetDataType() != SpecTypeId.Length) continue;
                        var n = p.Definition.Name;
                        if (!d.ContainsKey(n)) d[n] = UnitUtils.ConvertFromInternalUnits(p.AsDouble(), UnitTypeId.Millimeters);
                    }
                    catch { /* parámetro sin tipo de dato */ }
                }
            }
            return d;
        }

        private static object Barra(FamilyInstance fi, long? sistema)
        {
            if (!(fi.Location is LocationCurve lc)) return null;
            var c = lc.Curve;
            var sym = fi.Symbol;
            return new Dictionary<string, object>
            {
                ["id"] = fi.Id.Value,
                ["sistema"] = sistema,
                ["tipo"] = sym != null ? $"{sym.FamilyName}: {sym.Name}" : fi.Name,
                ["familia"] = sym?.FamilyName,
                ["p0"] = P(c.GetEndPoint(0)),
                ["p1"] = P(c.GetEndPoint(1)),
                ["parametros"] = sistema != null ? Dimensiones(fi) : null,
            };
        }

        public Result Execute(ExternalCommandData data, ref string message, ElementSet elements)
        {
            var uidoc = data.Application.ActiveUIDocument;
            var doc = uidoc?.Document;
            if (doc == null || doc.IsFamilyDocument) { message = "Abre un proyecto de Revit."; return Result.Failed; }
            // sistemas: los seleccionados (o los que contienen las vigas seleccionadas); si no hay, todos
            var todos = new FilteredElementCollector(doc).OfClass(typeof(BeamSystem)).Cast<BeamSystem>().ToList();
            var selIds = uidoc.Selection.GetElementIds().Select(i => i.Value).ToHashSet();
            var sistemas = todos.Where(b => selIds.Contains(b.Id.Value) || b.GetBeamIds().Any(i => selIds.Contains(i.Value))).ToList();
            if (sistemas.Count == 0) sistemas = todos;
            if (sistemas.Count == 0)
            {
                TaskDialog.Show("cad2revit", "El modelo no tiene sistemas de vigas (Structural Framing Systems): las costaneras " +
                                             "se exportan desde ellos.");
                return Result.Cancelled;
            }
            var enSistemas = todos.SelectMany(b => b.GetBeamIds()).Select(i => i.Value).ToHashSet();
            var listaSis = new List<object>();
            var costaneras = new List<object>();
            foreach (var bs in sistemas)
            {
                var contorno = new List<double[]>();
                try
                {
                    foreach (Curve c in bs.Profile) contorno.Add(P(c.GetEndPoint(0)));
                }
                catch { /* sin contorno */ }
                var ids = bs.GetBeamIds();
                listaSis.Add(new Dictionary<string, object>
                {
                    ["id"] = bs.Id.Value, ["nombre"] = string.IsNullOrWhiteSpace(bs.Name) ? $"Sistema {bs.Id.Value}" : $"{bs.Name} ({bs.Id.Value})",
                    ["contorno"] = contorno, ["costaneras"] = ids.Select(i => i.Value).ToList(),
                });
                foreach (var id in ids)
                    if (doc.GetElement(id) is FamilyInstance fi)
                        costaneras.Add(Barra(fi, bs.Id.Value));
            }
            // vigas que no son de un sistema: candidatas a apoyo (la app usa las que pasan bajo cada costanera)
            var vigas = new FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_StructuralFraming)
                .WhereElementIsNotElementType().OfType<FamilyInstance>()
                .Where(f => !enSistemas.Contains(f.Id.Value))
                .Select(f => Barra(f, null)).Where(x => x != null).ToList();
            // base del modelo (para la altura del techo en el viento): pie de los pilares, o el nivel más bajo
            double? zBase = null;
            foreach (var col in new FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_StructuralColumns).WhereElementIsNotElementType())
            {
                var bb = col.get_BoundingBox(null);
                if (bb != null) zBase = zBase == null ? Mt(bb.Min.Z) : System.Math.Min(zBase.Value, Mt(bb.Min.Z));
            }
            if (zBase == null)
            {
                var niv = new FilteredElementCollector(doc).OfClass(typeof(Level)).Cast<Level>().ToList();
                if (niv.Count > 0) zBase = Mt(niv.Min(l => l.Elevation));
            }
            var datos = new Dictionary<string, object>
            {
                ["tipo_archivo"] = "cad2revit-costaneras", ["version"] = 1, ["proyecto"] = doc.Title, ["nombre"] = doc.Title,
                ["unidades"] = "m", ["z_base"] = zBase, ["sistemas"] = listaSis,
                ["costaneras"] = costaneras.Where(x => x != null).ToList(), ["vigas"] = vigas,
                // costaneras seleccionadas en Revit: quedan elegidas para verificar en la app
                ["elegidas"] = sistemas.SelectMany(b => b.GetBeamIds()).Select(i => i.Value).Where(selIds.Contains).ToList(),
            };
            string ruta;
            using (var dlg = new System.Windows.Forms.SaveFileDialog
            {
                Title = "Guardar las costaneras para análisis",
                Filter = "Costaneras de cad2revit (*.costaneras.json)|*.costaneras.json",
                FileName = $"{U.NombreSeguro(doc.Title)}.costaneras.json",
            })
            {
                if (dlg.ShowDialog() != System.Windows.Forms.DialogResult.OK) return Result.Cancelled;
                ruta = dlg.FileName;
            }
            System.IO.File.WriteAllText(ruta, System.Text.Json.JsonSerializer.Serialize(datos));
            string abierto = null, falla = null;
            try { abierto = Subir(ruta); }
            catch (System.Exception ex) { falla = ex.Message; }
            int nCost = ((List<object>)datos["costaneras"]).Count;
            new TaskDialog("cad2revit")
            {
                MainInstruction = $"{sistemas.Count} sistema(s) de vigas con {nCost} costanera(s) y {vigas.Count} viga(s) candidatas a apoyo",
                MainContent = abierto != null
                    ? "Se abrió la app en la pestaña «8 · Elementos secundarios»: ahí se verifican las costaneras."
                    : $"No se pudo subir a la app ({falla ?? "cancelado"}). Súbelo a mano en la pestaña 8 · Elementos secundarios: {ruta}",
            }.Show();
            return Result.Succeeded;
        }

        /// <summary>Sube el archivo a un proyecto de la app (se elige; se recuerda el último) y abre la pestaña 8.</summary>
        private static string Subir(string ruta)
        {
            var c = ComandoArmaduras.CargarConexion(false);
            for (int intento = 0; c != null && intento < 3; intento++)
            {
                using var http = new System.Net.Http.HttpClient { Timeout = System.TimeSpan.FromSeconds(120) };
                if (!string.IsNullOrEmpty(c.Usuario))
                    http.DefaultRequestHeaders.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Basic",
                        System.Convert.ToBase64String(System.Text.Encoding.UTF8.GetBytes(c.Usuario + ":" + c.Clave)));
                var r = http.GetAsync(c.Url + "/api/proyectos").GetAwaiter().GetResult();
                if ((int)r.StatusCode == 401) { c = ComandoArmaduras.CargarConexion(true); continue; }
                r.EnsureSuccessStatusCode();
                using var lista = System.Text.Json.JsonDocument.Parse(r.Content.ReadAsStringAsync().GetAwaiter().GetResult());
                var proyectos = lista.RootElement.EnumerateArray()
                    .Select(p => (id: p.GetProperty("id").GetString(), nombre: p.GetProperty("nombre").GetString())).ToList();
                if (proyectos.Count == 0) throw new System.Exception("la app no tiene proyectos");
                var archivoUltimo = System.IO.Path.Combine(System.Environment.GetFolderPath(System.Environment.SpecialFolder.ApplicationData),
                                                           "cad2revit", "ultimo-proyecto.txt");
                string ultimo = System.IO.File.Exists(archivoUltimo) ? System.IO.File.ReadAllText(archivoUltimo).Trim() : null;
                using (var f = new System.Windows.Forms.Form
                {
                    Text = "Proyecto de la app para estas costaneras", Width = 460, Height = 150,
                    FormBorderStyle = System.Windows.Forms.FormBorderStyle.FixedDialog,
                    StartPosition = System.Windows.Forms.FormStartPosition.CenterScreen, MinimizeBox = false, MaximizeBox = false,
                })
                {
                    var cb = new System.Windows.Forms.ComboBox { Left = 12, Top = 15, Width = 420, DropDownStyle = System.Windows.Forms.ComboBoxStyle.DropDownList };
                    foreach (var p in proyectos) cb.Items.Add(p.nombre);
                    int k = proyectos.FindIndex(p => p.id == ultimo);
                    cb.SelectedIndex = k >= 0 ? k : 0;
                    var ok = new System.Windows.Forms.Button { Text = "Subir", Left = 240, Top = 60, Width = 90, DialogResult = System.Windows.Forms.DialogResult.OK };
                    var no = new System.Windows.Forms.Button { Text = "Cancelar", Left = 340, Top = 60, Width = 90, DialogResult = System.Windows.Forms.DialogResult.Cancel };
                    f.Controls.Add(cb); f.Controls.Add(ok); f.Controls.Add(no); f.AcceptButton = ok; f.CancelButton = no;
                    if (f.ShowDialog() != System.Windows.Forms.DialogResult.OK) return null;
                    ultimo = proyectos[cb.SelectedIndex].id;
                }
                try { System.IO.Directory.CreateDirectory(System.IO.Path.GetDirectoryName(archivoUltimo)); System.IO.File.WriteAllText(archivoUltimo, ultimo); } catch { }
                using var cuerpo = new System.Net.Http.MultipartFormDataContent();
                var bytes = new System.Net.Http.ByteArrayContent(System.IO.File.ReadAllBytes(ruta));
                bytes.Headers.ContentType = new System.Net.Http.Headers.MediaTypeHeaderValue("application/json");
                cuerpo.Add(bytes, "archivo", System.IO.Path.GetFileName(ruta));
                var s = http.PostAsync($"{c.Url}/api/proyectos/{System.Uri.EscapeDataString(ultimo)}/costaneras", cuerpo).GetAwaiter().GetResult();
                var texto = s.Content.ReadAsStringAsync().GetAwaiter().GetResult();
                if (!s.IsSuccessStatusCode) throw new System.Exception($"la app respondió {(int)s.StatusCode}: {texto}");
                var nombre = System.Text.Json.JsonDocument.Parse(texto).RootElement.GetProperty("nombre").GetString();
                var url = $"{c.Url}/?proyecto={System.Uri.EscapeDataString(ultimo)}&tab=secundarios&techo={System.Uri.EscapeDataString(nombre)}";
                System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo { FileName = url, UseShellExecute = true });
                return url;
            }
            return null;
        }
    }
}

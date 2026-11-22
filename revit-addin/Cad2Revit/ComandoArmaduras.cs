using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using WF = System.Windows.Forms;

namespace Cad2Revit
{
    /// <summary>
    /// Armaduras de pedestal y fundación desde un croquis: la app (Railway) lee la imagen con Claude, se revisan los
    /// valores, se eligen el muro (pedestal) y su zapata corrida (fundación) y se crean las barras y estribos.
    /// </summary>
    [Transaction(TransactionMode.Manual)]
    [Regeneration(RegenerationOption.Manual)]
    public class ComandoArmaduras : IExternalCommand
    {
        private class Filtro : Autodesk.Revit.UI.Selection.ISelectionFilter
        {
            private readonly Func<Element, bool> _f;
            public Filtro(Func<Element, bool> f) { _f = f; }
            public bool AllowElement(Element e) => _f(e);
            public bool AllowReference(Reference r, XYZ p) => false;
        }

        internal static readonly JsonSerializerOptions Json = new()
        {
            PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower, PropertyNameCaseInsensitive = true, WriteIndented = true,
        };

        public Result Execute(ExternalCommandData data, ref string message, ElementSet elements)
        {
            var uidoc = data.Application.ActiveUIDocument;
            var doc = uidoc?.Document;
            if (doc == null || doc.IsFamilyDocument) { message = "Abre un proyecto de Revit."; return Result.Failed; }

            // 1. cómo se llenan las armaduras
            var modo = new TaskDialog("cad2revit")
            {
                MainInstruction = "Armaduras de pedestal y fundación",
                MainContent = "Llena las barras y estribos del croquis en un formulario (prellenado con valores típicos: " +
                              "2 φ12 por cara en el pedestal, 4 + 4 φ12 en la fundación, E φ8@20).",
                CommonButtons = TaskDialogCommonButtons.Cancel,
            };
            modo.AddCommandLink(TaskDialogCommandLinkId.CommandLink1, "Llenar a mano viendo el croquis",
                                "Eliges la foto o imagen del croquis y la ves al lado del formulario");
            modo.AddCommandLink(TaskDialogCommandLinkId.CommandLink2, "Llenar a mano sin imagen");
            modo.AddCommandLink(TaskDialogCommandLinkId.CommandLink3, "Abrir una lectura guardada (.armaduras.json)",
                                "La que se guardó junto a la imagen la vez anterior");
            var eleccion = modo.Show();
            if (eleccion != TaskDialogResult.CommandLink1 && eleccion != TaskDialogResult.CommandLink2 &&
                eleccion != TaskDialogResult.CommandLink3)
                return Result.Cancelled;

            string ruta = null;
            if (eleccion != TaskDialogResult.CommandLink2)
            {
                bool json = eleccion == TaskDialogResult.CommandLink3;
                using var dlg = new WF.OpenFileDialog
                {
                    Title = json ? "Lectura de armaduras guardada" : "Croquis de armaduras de pedestal y fundación",
                    Filter = json ? "Lectura de armaduras (*.json)|*.json" : "Imagen (*.jpg;*.jpeg;*.png;*.webp;*.bmp)|*.jpg;*.jpeg;*.png;*.webp;*.bmp",
                };
                if (dlg.ShowDialog() != WF.DialogResult.OK) return Result.Cancelled;
                ruta = dlg.FileName;
            }

            // 2. valores de partida
            LecturaArmaduras lectura;
            try
            {
                if (eleccion == TaskDialogResult.CommandLink3)
                    lectura = JsonSerializer.Deserialize<LecturaArmaduras>(File.ReadAllText(ruta), Json);
                else
                    lectura = new LecturaArmaduras
                    {
                        TotalBarrasLongitudinales = 0,
                        Observaciones = "Valores típicos de partida: corrígelos según el croquis.",
                    };
                if (lectura == null) return Result.Cancelled;
            }
            catch (Exception ex)
            {
                TaskDialog.Show("cad2revit", "No se pudo leer el croquis:\n" + ex.Message);
                return Result.Failed;
            }

            // 3. revisión
            if (!Revisar(lectura, ruta)) return Result.Cancelled;
            if (ruta != null && !ruta.EndsWith(".json", StringComparison.OrdinalIgnoreCase))
            {
                try { File.WriteAllText(Path.ChangeExtension(ruta, ".armaduras.json"), JsonSerializer.Serialize(lectura, Json)); }
                catch { /* carpeta de solo lectura: no importa */ }
            }

            // 4. elementos
            Wall muro; Element fundacion;
            try
            {
                var r1 = uidoc.Selection.PickObject(Autodesk.Revit.UI.Selection.ObjectType.Element,
                    new Filtro(e => e is Wall), "Elige el MURO que es el pedestal (parte vertical)");
                muro = (Wall)doc.GetElement(r1);
                var r2 = uidoc.Selection.PickObject(Autodesk.Revit.UI.Selection.ObjectType.Element,
                    new Filtro(e => e is WallFoundation || e.Category?.Id == new ElementId(BuiltInCategory.OST_StructuralFoundation)),
                    "Elige la FUNDACIÓN (zapata corrida bajo el muro)");
                fundacion = doc.GetElement(r2);
            }
            catch (Autodesk.Revit.Exceptions.OperationCanceledException) { return Result.Cancelled; }

            var informe = new Constructor(doc, new Modelo { Unidades = "m" }).ArmadurasPedestalFundacion(muro, fundacion, lectura);
            // la lectura queda asociada al muro: «Eje a análisis» la lleva a la pestaña 7 · Fundaciones de la app
            if (informe.Errores.Count == 0)
            {
                foreach (var destino in RutasLecturaMuro(doc, muro.Id))
                {
                    try
                    {
                        Directory.CreateDirectory(Path.GetDirectoryName(destino));
                        File.WriteAllText(destino, JsonSerializer.Serialize(lectura, Json));
                        break;
                    }
                    catch { /* carpeta sin permiso: se prueba la siguiente */ }
                }
            }
            new TaskDialog("cad2revit")
            {
                MainInstruction = informe.Errores.Count == 0 ? "Armaduras creadas" : "Armaduras con observaciones (ver detalle)",
                MainContent = informe.Resumen(),
                ExpandedContent = informe.Detalle(),
            }.Show();
            return Result.Succeeded;
        }

        /// <summary>Dónde se guarda la lectura de armaduras de un muro: en una carpeta junto al archivo de Revit
        /// («modelo.armaduras/id.json», compartida con quien abra el modelo) o, si no se puede, en la carpeta de datos
        /// del usuario.</summary>
        internal static List<string> RutasLecturaMuro(Document doc, ElementId muro)
        {
            var rutas = new List<string>();
            try
            {
                if (!string.IsNullOrEmpty(doc.PathName) && Path.IsPathRooted(doc.PathName) && Directory.Exists(Path.GetDirectoryName(doc.PathName)))
                    rutas.Add(Path.Combine(Path.GetDirectoryName(doc.PathName),
                                           Path.GetFileNameWithoutExtension(doc.PathName) + ".armaduras", muro + ".json"));
            }
            catch { /* ruta de nube u otra: se usa la carpeta del usuario */ }
            string titulo = string.Concat((doc.Title ?? "modelo").Select(ch => Path.GetInvalidFileNameChars().Contains(ch) ? '_' : ch));
            rutas.Add(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "cad2revit", "armaduras",
                                   titulo, muro + ".json"));
            return rutas;
        }

        // ---------------- conexión con la app ----------------

        internal class Conexion
        {
            public string Url { get; set; } = "https://cad2revit-production.up.railway.app";
            public string Usuario { get; set; } = "";
            public string Clave { get; set; } = "";
        }

        private static string RutaConexion => Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "cad2revit", "conexion.json");

        internal static Conexion CargarConexion(bool preguntar)
        {
            Conexion c = null;
            try { if (File.Exists(RutaConexion)) c = JsonSerializer.Deserialize<Conexion>(File.ReadAllText(RutaConexion), Json); }
            catch { c = null; }
            if (c != null && !preguntar && !string.IsNullOrEmpty(c.Url)) return c;
            c ??= new Conexion();
            using var f = new WF.Form
            {
                Text = "Conexión con la app cad2revit", Width = 520, Height = 230, FormBorderStyle = WF.FormBorderStyle.FixedDialog,
                StartPosition = WF.FormStartPosition.CenterScreen, MinimizeBox = false, MaximizeBox = false,
            };
            WF.TextBox Campo(string etiqueta, int y, string valor, bool clave = false)
            {
                f.Controls.Add(new WF.Label { Text = etiqueta, Left = 12, Top = y + 3, Width = 110 });
                var t = new WF.TextBox { Left = 125, Top = y, Width = 365, Text = valor, UseSystemPasswordChar = clave };
                f.Controls.Add(t);
                return t;
            }
            var tu = Campo("Dirección", 15, c.Url);
            var us = Campo("Usuario", 45, c.Usuario);
            var cl = Campo("Clave", 75, c.Clave, true);
            f.Controls.Add(new WF.Label { Text = "Las mismas de la página de la app. Se guardan en tu perfil de Windows.", Left = 12, Top = 108, Width = 480 });
            var ok = new WF.Button { Text = "Guardar", Left = 300, Top = 140, Width = 90, DialogResult = WF.DialogResult.OK };
            var no = new WF.Button { Text = "Cancelar", Left = 400, Top = 140, Width = 90, DialogResult = WF.DialogResult.Cancel };
            f.Controls.Add(ok); f.Controls.Add(no); f.AcceptButton = ok; f.CancelButton = no;
            if (f.ShowDialog() != WF.DialogResult.OK) return null;
            c = new Conexion { Url = tu.Text.Trim().TrimEnd('/'), Usuario = us.Text.Trim(), Clave = cl.Text };
            Directory.CreateDirectory(Path.GetDirectoryName(RutaConexion));
            File.WriteAllText(RutaConexion, JsonSerializer.Serialize(c, Json));
            return c;
        }

        // ---------------- revisión ----------------

        private static bool Revisar(LecturaArmaduras a, string imagen)
        {
            using var f = new WF.Form
            {
                Text = "Armaduras de pedestal y fundación — revisa y corrige", Width = 1000, Height = 640,
                FormBorderStyle = WF.FormBorderStyle.FixedDialog, StartPosition = WF.FormStartPosition.CenterScreen,
                MinimizeBox = false, MaximizeBox = false,
            };
            if (imagen != null && !imagen.EndsWith(".json", StringComparison.OrdinalIgnoreCase))
            {
                try
                {
                    f.Controls.Add(new WF.PictureBox
                    {
                        Left = 10, Top = 10, Width = 430, Height = 580, SizeMode = WF.PictureBoxSizeMode.Zoom,
                        Image = System.Drawing.Image.FromFile(imagen), BorderStyle = WF.BorderStyle.FixedSingle,
                    });
                }
                catch { /* sin vista previa */ }
            }
            var campos = new List<(WF.Control c, Action leer)>();
            int y = 10, x0 = 460;
            void Titulo(string t) { f.Controls.Add(new WF.Label { Text = t, Left = x0, Top = y, Width = 500, Font = new System.Drawing.Font(WF.Control.DefaultFont, System.Drawing.FontStyle.Bold) }); y += 26; }
            void Num(string etiqueta, double valor, Action<double> poner)
            {
                f.Controls.Add(new WF.Label { Text = etiqueta, Left = x0 + 10, Top = y + 3, Width = 330 });
                var t = new WF.TextBox { Left = x0 + 350, Top = y, Width = 100, Text = valor.ToString("0.##", CultureInfo.InvariantCulture) };
                f.Controls.Add(t);
                campos.Add((t, () =>
                {
                    if (!double.TryParse(t.Text.Trim().Replace(',', '.'), NumberStyles.Float, CultureInfo.InvariantCulture, out var v) || v < 0)
                        throw new FormatException($"«{etiqueta}»: escribe un número");
                    poner(v);
                }));
                y += 28;
            }
            Titulo("Pedestal (muro)");
            Num("Barras longitudinales en cada cara", a.Pedestal.BarrasPorCara, v => a.Pedestal.BarrasPorCara = (int)Math.Round(v));
            Num("Diámetro de las barras (mm)", a.Pedestal.DiametroMm, v => a.Pedestal.DiametroMm = v);
            Num("Diámetro del estribo (mm)", a.Pedestal.EstriboDiametroMm, v => a.Pedestal.EstriboDiametroMm = v);
            Num("Separación de estribos (cm)", a.Pedestal.EstriboSeparacionCm, v => a.Pedestal.EstriboSeparacionCm = v);
            var fondo = new WF.CheckBox { Text = "El estribo del pedestal baja hasta el fondo de la fundación", Left = x0 + 10, Top = y, Width = 480, Checked = a.Pedestal.EstriboHastaFondoFundacion };
            f.Controls.Add(fondo); y += 30;
            Num("Recubrimiento (cm)", a.RecubrimientoPedestalCm, v => a.RecubrimientoPedestalCm = v);
            y += 8;
            Titulo("Fundación (zapata corrida)");
            Num("Barras en la cara superior", a.Fundacion.BarrasSuperiores, v => a.Fundacion.BarrasSuperiores = (int)Math.Round(v));
            Num("Barras en la cara inferior", a.Fundacion.BarrasInferiores, v => a.Fundacion.BarrasInferiores = (int)Math.Round(v));
            Num("Diámetro de las barras (mm)", a.Fundacion.DiametroMm, v => a.Fundacion.DiametroMm = v);
            Num("Diámetro del estribo (mm)", a.Fundacion.EstriboDiametroMm, v => a.Fundacion.EstriboDiametroMm = v);
            Num("Separación de estribos (cm)", a.Fundacion.EstriboSeparacionCm, v => a.Fundacion.EstriboSeparacionCm = v);
            Num("Recubrimiento (cm)", a.RecubrimientoFundacionCm, v => a.RecubrimientoFundacionCm = v);
            y += 8;
            int total = 2 * a.Pedestal.BarrasPorCara + a.Fundacion.BarrasSuperiores + a.Fundacion.BarrasInferiores;
            var obs = new WF.TextBox
            {
                Left = x0, Top = y, Width = 500, Height = 590 - y - 50, Multiline = true, ReadOnly = true, ScrollBars = WF.ScrollBars.Vertical,
                Text = $"Total de barras longitudinales: {total}" +
                       (a.TotalBarrasLongitudinales > 0 ? $" (el croquis indica {a.TotalBarrasLongitudinales})" : "") +
                       (string.IsNullOrWhiteSpace(a.Observaciones) ? "" : "\r\n\r\nObservaciones de la lectura: " + a.Observaciones),
            };
            f.Controls.Add(obs);
            var ok = new WF.Button { Text = "Elegir muro y fundación", Left = x0 + 210, Top = 560, Width = 180, DialogResult = WF.DialogResult.OK };
            var no = new WF.Button { Text = "Cancelar", Left = x0 + 400, Top = 560, Width = 100, DialogResult = WF.DialogResult.Cancel };
            f.Controls.Add(ok); f.Controls.Add(no); f.AcceptButton = ok; f.CancelButton = no;
            while (f.ShowDialog() == WF.DialogResult.OK)
            {
                try
                {
                    foreach (var (_, leer) in campos) leer();
                    a.Pedestal.EstriboHastaFondoFundacion = fondo.Checked;
                    if (a.Pedestal.DiametroMm <= 0 || a.Fundacion.DiametroMm <= 0 || a.Pedestal.EstriboSeparacionCm <= 0 || a.Fundacion.EstriboSeparacionCm <= 0)
                        throw new FormatException("Los diámetros y separaciones deben ser mayores que cero");
                    return true;
                }
                catch (FormatException ex) { WF.MessageBox.Show(ex.Message); }
            }
            return false;
        }
    }
}

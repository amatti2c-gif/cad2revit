using System.Linq;
using System.Reflection;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;

namespace Cad2Revit
{
    /// <summary>Crea la pestaña "cad2revit" con el botón de importación.</summary>
    public class App : IExternalApplication
    {
        public Result OnStartup(UIControlledApplication app)
        {
            const string pestana = "cad2revit";
            try { app.CreateRibbonTab(pestana); } catch { /* ya existe */ }
            var panel = app.CreateRibbonPanel(pestana, "Modelo desde CAD");
            var boton = new PushButtonData("Cad2RevitImportar", "Construir\nmodelo",
                Assembly.GetExecutingAssembly().Location, typeof(ComandoImportar).FullName)
            {
                ToolTip = "Lee un modelo.json generado por el parser cad2revit y construye niveles, " +
                          "muros, aberturas, losas, estructura y recintos según el nivel de detalle elegido."
            };
            panel.AddItem(boton);
            panel.AddItem(new PushButtonData("Cad2RevitGuardarFamilias", "Guardar\nfamilias",
                Assembly.GetExecutingAssembly().Location, typeof(ComandoGuardarFamilias).FullName)
            {
                ToolTip = "Guarda en Documentos\\cad2revit\\Familias las familias de pilares, vigas y fundaciones " +
                          "cargadas en este proyecto. Desde ahí cad2revit las carga solo en cualquier proyecto nuevo. " +
                          "Basta hacerlo una vez."
            });
            return Result.Succeeded;
        }

        public Result OnShutdown(UIControlledApplication app) => Result.Succeeded;
    }

    [Transaction(TransactionMode.Manual)]
    [Regeneration(RegenerationOption.Manual)]
    public class ComandoImportar : IExternalCommand
    {
        public Result Execute(ExternalCommandData data, ref string message, ElementSet elements)
        {
            var doc = data.Application.ActiveUIDocument?.Document;
            if (doc == null || doc.IsFamilyDocument)
            {
                message = "Abre un proyecto de Revit (no una familia) antes de ejecutar cad2revit.";
                return Result.Failed;
            }

            var dlg = new FileOpenDialog("Modelo cad2revit (*.json)|*.json") { Title = "Selecciona modelo.json" };
            if (dlg.Show() != ItemSelectionDialogResult.Confirmed) return Result.Cancelled;
            var ruta = ModelPathUtils.ConvertModelPathToUserVisiblePath(dlg.GetSelectedModelPath());

            Modelo modelo;
            try { modelo = Modelo.Leer(ruta); }
            catch (System.Exception ex)
            {
                message = "No se pudo leer el JSON: " + ex.Message;
                return Result.Failed;
            }

            var constructor = new Constructor(doc, modelo);
            var previos = Constructor.ElementosPrevios(doc).Count;
            if (previos > 0 && Constructor.TieneSincronizacion(doc))
            {
                var pregunta = new TaskDialog("cad2revit")
                {
                    MainInstruction = "Actualizar el modelo con los cambios de la app",
                    MainContent = $"Este archivo ya tiene {previos} elementos de cad2revit. Al actualizar se crean los nuevos, se " +
                                  "actualizan los que cambiaron en la app y se eliminan los que ya no están, SIN tocar lo que " +
                                  "modificaste en Revit (eso queda como conflicto en el informe) ni lo que creaste a mano.",
                    CommonButtons = TaskDialogCommonButtons.Cancel,
                };
                pregunta.AddCommandLink(TaskDialogCommandLinkId.CommandLink1, "Actualizar (recomendado)",
                    "Respeta tus cambios en Revit. Para que la app no toque nunca un elemento, borra «cad2revit» de su Comentario.");
                pregunta.AddCommandLink(TaskDialogCommandLinkId.CommandLink2, "Reconstruir todo",
                    "Borra todo lo importado por cad2revit (con tus cambios sobre esos elementos) y lo crea de nuevo.");
                var r = pregunta.Show();
                if (r == TaskDialogResult.CommandLink1) constructor.Actualizar = true;
                else if (r == TaskDialogResult.CommandLink2) constructor.ReemplazarAnterior = true;
                else return Result.Cancelled;
            }
            else if (previos > 0)
            {
                var pregunta = new TaskDialog("cad2revit")
                {
                    MainInstruction = "Activar la actualización que respeta tus cambios",
                    MainContent = $"Hay {previos} elementos de una importación hecha con una versión anterior del complemento, sin " +
                                  "datos de sincronización. Hay que reconstruir UNA vez: lo importado se borra y se crea de nuevo. " +
                                  "Desde la próxima importación, «Actualizar» respetará lo que modifiques en Revit.",
                    CommonButtons = TaskDialogCommonButtons.Cancel,
                };
                pregunta.AddCommandLink(TaskDialogCommandLinkId.CommandLink1, "Reconstruir y activar la sincronización",
                    "Se puede deshacer con Ctrl+Z.");
                pregunta.AddCommandLink(TaskDialogCommandLinkId.CommandLink2, "Construir encima",
                    "Mantiene lo anterior y agrega el modelo nuevo (quedarán duplicados).");
                var r = pregunta.Show();
                if (r == TaskDialogResult.CommandLink1) constructor.ReemplazarAnterior = true;
                else if (r != TaskDialogResult.CommandLink2) return Result.Cancelled;
            }
            var informe = constructor.Construir();
            informe.Guardar(System.IO.Path.ChangeExtension(ruta, ".informe.txt"));
            // que el modelo se vea: ajustar las vistas abiertas a lo construido
            try { foreach (var v in data.Application.ActiveUIDocument.GetOpenUIViews()) v.ZoomToFit(); }
            catch { /* sin vistas abiertas */ }

            var td = new TaskDialog("cad2revit")
            {
                MainInstruction = informe.Errores.Count == 0 && informe.CantidadFaltantes == 0
                    ? "Modelo construido" : "Modelo construido con observaciones (ver detalle)",
                MainContent = informe.Resumen(),
                ExpandedContent = informe.Detalle(),
            };
            td.Show();
            return Result.Succeeded;
        }
    }

    /// <summary>
    /// Exporta (una sola vez) las familias estructurales cargadas en el proyecto abierto a
    /// Documentos\cad2revit\Familias\&lt;categoría&gt;\, carpeta donde el complemento busca las
    /// familias que faltan en un proyecto.
    /// </summary>
    [Transaction(TransactionMode.Manual)]
    public class ComandoGuardarFamilias : IExternalCommand
    {
        public Result Execute(ExternalCommandData data, ref string message, ElementSet elements)
        {
            var doc = data.Application.ActiveUIDocument?.Document;
            if (doc == null || doc.IsFamilyDocument) { message = "Abre un proyecto de Revit."; return Result.Failed; }
            var raiz = System.IO.Path.Combine(System.Environment.GetFolderPath(System.Environment.SpecialFolder.MyDocuments),
                                              "cad2revit", "Familias");
            var categorias = new (BuiltInCategory cat, string carpeta)[]
            {
                (BuiltInCategory.OST_StructuralColumns, "Structural Columns"),
                (BuiltInCategory.OST_StructuralFraming, "Structural Framing"),
                (BuiltInCategory.OST_StructuralFoundation, "Structural Foundations"),
                (BuiltInCategory.OST_Doors, "Doors"),
                (BuiltInCategory.OST_Windows, "Windows"),
            };
            var guardadas = new System.Collections.Generic.List<string>();
            var fallidas = new System.Collections.Generic.List<string>();
            foreach (var (cat, carpeta) in categorias)
            {
                var familias = new FilteredElementCollector(doc).OfClass(typeof(Family)).Cast<Family>()
                    .Where(f => f.FamilyCategory?.Id == new ElementId(cat) && f.IsEditable && !f.IsInPlace).ToList();
                foreach (var f in familias)
                {
                    try
                    {
                        var dir = System.IO.Path.Combine(raiz, carpeta);
                        System.IO.Directory.CreateDirectory(dir);
                        var fam = doc.EditFamily(f);
                        fam.SaveAs(System.IO.Path.Combine(dir, U.NombreSeguro(f.Name) + ".rfa"),
                                   new SaveAsOptions { OverwriteExistingFile = true });
                        fam.Close(false);
                        guardadas.Add($"{carpeta}: {f.Name}");
                    }
                    catch (System.Exception ex) { fallidas.Add($"{f.Name}: {ex.Message}"); }
                }
            }
            new TaskDialog("cad2revit")
            {
                MainInstruction = $"{guardadas.Count} familias guardadas para cad2revit",
                MainContent = $"Carpeta: {raiz}\n\nDesde ahora, si un proyecto no tiene alguna de estas familias, " +
                              "cad2revit la carga sola desde esa carpeta.",
                ExpandedContent = string.Join("\n", guardadas) +
                                  (fallidas.Count > 0 ? "\n\nNo se pudieron guardar:\n" + string.Join("\n", fallidas) : ""),
            }.Show();
            return Result.Succeeded;
        }
    }
}

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
            if (previos > 0)
            {
                var pregunta = new TaskDialog("cad2revit")
                {
                    MainInstruction = "Este proyecto ya tiene una importación de cad2revit",
                    MainContent = $"Hay {previos} elementos creados por una importación anterior. " +
                                  "Si construyes encima, quedarán muros y recintos duplicados.",
                    CommonButtons = TaskDialogCommonButtons.Cancel,
                };
                pregunta.AddCommandLink(TaskDialogCommandLinkId.CommandLink1, "Reemplazar la importación anterior",
                    "Borra los elementos anteriores de cad2revit y construye el modelo nuevo (se puede deshacer con Ctrl+Z).");
                pregunta.AddCommandLink(TaskDialogCommandLinkId.CommandLink2, "Construir encima",
                    "Mantiene lo anterior y agrega el modelo nuevo.");
                var r = pregunta.Show();
                if (r == TaskDialogResult.CommandLink1) constructor.ReemplazarAnterior = true;
                else if (r != TaskDialogResult.CommandLink2) return Result.Cancelled;
            }
            var informe = constructor.Construir();
            informe.Guardar(System.IO.Path.ChangeExtension(ruta, ".informe.txt"));

            var td = new TaskDialog("cad2revit")
            {
                MainInstruction = informe.Errores.Count == 0 ? "Modelo construido" : "Modelo construido con observaciones",
                MainContent = informe.Resumen(),
                ExpandedContent = informe.Detalle(),
            };
            td.Show();
            return Result.Succeeded;
        }
    }
}

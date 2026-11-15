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
            panel.AddItem(new PushButtonData("Cad2RevitEntramado", "Entramado\nen muros",
                Assembly.GetExecutingAssembly().Location, typeof(ComandoEntramado).FullName)
            {
                ToolTip = "Genera el entramado de madera de cad2revit (pies derechos cada 40 cm, soleras, cadenetas, jambas, " +
                          "dinteles y alféizares) en los muros seleccionados, o en todos los de tipo «marco plataforma». " +
                          "Lee el largo, la altura, los encuentros y las puertas y ventanas de cada muro. Si el muro ya tenía " +
                          "entramado generado así, se reemplaza."
            });
            panel.AddItem(new PushButtonData("Cad2RevitAberturas", "Puertas y\nventanas",
                Assembly.GetExecutingAssembly().Location, typeof(ComandoAberturas).FullName)
            {
                ToolTip = "Lee aberturas.json (app → Resultado → ⬇ aberturas.json) y abre en tus muros de Revit el vano exacto " +
                          "de cada puerta y ventana (ancho, alto y antepecho). Al volver a cargarlo sincroniza: ajusta lo que " +
                          "cambió y borra lo que ya no está, solo en lo que creó este botón. Rehace el entramado de los muros que lo tenían."
            });
            panel.AddItem(new PushButtonData("Cad2RevitMoverEje", "Mover\neje",
                Assembly.GetExecutingAssembly().Location, typeof(ComandoMoverEje).FullName)
            {
                ToolTip = "Eliges un eje y la distancia: mueve el eje con los muros (y sus vanos), pilares y vigas que están sobre él, " +
                          "alarga o acorta los muros y vigas que llegan a él y rehace el entramado de los muros afectados. " +
                          "Antes muestra qué se va a mover."
            });
            panel.AddItem(new PushButtonData("Cad2RevitRevisarEjes", "Revisar\nejes",
                Assembly.GetExecutingAssembly().Location, typeof(ComandoRevisarEjes).FullName)
            {
                ToolTip = "Muestra qué hay en cada eje (muros, vanos, pilares, vigas, entramados) y permite amarrar los elementos a " +
                          "los ejes con restricciones de Revit, para moverlos con la herramienta Mover."
            });
            panel.AddItem(new PushButtonData("Cad2RevitUnirPilares", "Unir pilares\na vigas",
                Assembly.GetExecutingAssembly().Location, typeof(ComandoUnirPilares).FullName)
            {
                ToolTip = "Une cada pilar a la viga que llega a su parte superior (también las inclinadas de los marcos a dos " +
                          "aguas): quita la unión de geometría que lo impide y enlaza el pilar a la viga, recortándolo con ella. " +
                          "En los pilares seleccionados o, si no hay selección, en todos."
            });
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
            var previos = modelo.EsParcial ? 0 : Constructor.ElementosPrevios(doc).Count;   // parcial: no pregunta ni borra
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

    /// <summary>Entramado de madera de cad2revit en muros dibujados en Revit (p. ej. tipo «marco plataforma»).</summary>
    [Transaction(TransactionMode.Manual)]
    [Regeneration(RegenerationOption.Manual)]
    public partial class ComandoEntramado : IExternalCommand
    {
        private class SoloMuros : Autodesk.Revit.UI.Selection.ISelectionFilter
        {
            public bool AllowElement(Element e) => e is Wall;
            public bool AllowReference(Reference r, XYZ p) => false;
        }

        public Result Execute(ExternalCommandData data, ref string message, ElementSet elements)
        {
            var uidoc = data.Application.ActiveUIDocument;
            var doc = uidoc?.Document;
            if (doc == null || doc.IsFamilyDocument) { message = "Abre un proyecto de Revit."; return Result.Failed; }

            // escuadría de las piezas
            var dEsc = new TaskDialog("cad2revit")
            {
                MainInstruction = "¿Qué piezas uso para el entramado de madera?",
                MainContent = "Pies derechos, soleras, cadenetas, jambas, dinteles y alféizares, todos con la misma escuadría.",
                CommonButtons = TaskDialogCommonButtons.Cancel,
            };
            dEsc.AddCommandLink(TaskDialogCommandLinkId.CommandLink1, "2x3", "50 x 76 mm (2\" x 3\")");
            dEsc.AddCommandLink(TaskDialogCommandLinkId.CommandLink2, "2x4", "50 x 102 mm (2\" x 4\")");
            dEsc.AddCommandLink(TaskDialogCommandLinkId.CommandLink3, "Según el núcleo de cada muro",
                                "2\" de cara y el espesor de la capa estructural del muro");
            var conf = new ConfEntramado { H = 0 };
            switch (dEsc.Show())
            {
                case TaskDialogResult.CommandLink1: conf.H = 0.0762; conf.B = 0.05; Escuadria(conf, "2x3"); break;
                case TaskDialogResult.CommandLink2: conf.H = 0.1016; conf.B = 0.05; Escuadria(conf, "2x4"); break;
                case TaskDialogResult.CommandLink3: break;
                default: return Result.Cancelled;
            }

            var muros = uidoc.Selection.GetElementIds().Select(doc.GetElement).OfType<Wall>().ToList();
            if (muros.Count == 0)
            {
                var deTipo = new FilteredElementCollector(doc).OfClass(typeof(Wall)).Cast<Wall>()
                    .Where(w => w.WallType?.Name?.IndexOf("plataforma", System.StringComparison.OrdinalIgnoreCase) >= 0).ToList();
                var tipos = string.Join(", ", deTipo.Select(w => w.WallType.Name).Distinct());
                var td = new TaskDialog("cad2revit")
                {
                    MainInstruction = "¿En qué muros genero el entramado de madera?",
                    MainContent = "Pies derechos cada 40 cm, soleras, cadenetas cada 40 cm, jambas, dinteles y alféizares. " +
                                  "Si un muro ya tenía este entramado, se reemplaza.",
                    CommonButtons = TaskDialogCommonButtons.Cancel,
                };
                if (deTipo.Count > 0)
                    td.AddCommandLink(TaskDialogCommandLinkId.CommandLink1, $"Todos los muros «plataforma» ({deTipo.Count})", tipos);
                td.AddCommandLink(TaskDialogCommandLinkId.CommandLink2, "Elegir los muros en la vista",
                                  "Selecciónalos y presiona Finalizar");
                var r = td.Show();
                if (r == TaskDialogResult.CommandLink1) muros = deTipo;
                else if (r == TaskDialogResult.CommandLink2)
                {
                    try
                    {
                        muros = uidoc.Selection.PickObjects(Autodesk.Revit.UI.Selection.ObjectType.Element, new SoloMuros(),
                                                            "Elige los muros para el entramado y presiona Finalizar")
                                     .Select(x => doc.GetElement(x)).OfType<Wall>().ToList();
                    }
                    catch (Autodesk.Revit.Exceptions.OperationCanceledException) { return Result.Cancelled; }
                }
                else return Result.Cancelled;
            }
            if (muros.Count == 0) { message = "No hay muros para el entramado."; return Result.Cancelled; }

            var informe = new Constructor(doc, new Modelo { Unidades = "m" }).EntramadoEnMuros(muros, conf);
            new TaskDialog("cad2revit")
            {
                MainInstruction = informe.Errores.Count == 0 && informe.CantidadFaltantes == 0
                    ? "Entramado creado" : "Entramado creado con observaciones (ver detalle)",
                MainContent = informe.Resumen(),
                ExpandedContent = informe.Detalle(),
            }.Show();
            return Result.Succeeded;
        }
    }

    public partial class ComandoEntramado
    {
        private static void Escuadria(ConfEntramado c, string tipo)
        {
            c.FamiliaVertical = new FamiliaTipoDef { Familia = c.FamiliaVertical?.Familia, Tipo = tipo };
            c.FamiliaHorizontal = new FamiliaTipoDef { Familia = c.FamiliaHorizontal?.Familia, Tipo = tipo };
        }
    }

    /// <summary>Vanos de puertas y ventanas de la app (aberturas.json) en los muros del modelo de Revit.</summary>
    [Transaction(TransactionMode.Manual)]
    [Regeneration(RegenerationOption.Manual)]
    public class ComandoAberturas : IExternalCommand
    {
        public Result Execute(ExternalCommandData data, ref string message, ElementSet elements)
        {
            var doc = data.Application.ActiveUIDocument?.Document;
            if (doc == null || doc.IsFamilyDocument) { message = "Abre un proyecto de Revit."; return Result.Failed; }
            var dlg = new FileOpenDialog("Puertas y ventanas cad2revit (*.json)|*.json") { Title = "Selecciona aberturas.json" };
            if (dlg.Show() != ItemSelectionDialogResult.Confirmed) return Result.Cancelled;
            var ruta = ModelPathUtils.ConvertModelPathToUserVisiblePath(dlg.GetSelectedModelPath());
            ArchivoAberturas archivo;
            try { archivo = ArchivoAberturas.Leer(ruta); }
            catch (System.Exception ex) { message = "No se pudo leer el archivo: " + ex.Message; return Result.Failed; }

            var informe = new Constructor(doc, new Modelo { Unidades = "m" }).CargarAberturas(archivo);
            try { informe.Guardar(System.IO.Path.ChangeExtension(ruta, ".informe.txt")); } catch { /* carpeta sin permiso */ }
            new TaskDialog("cad2revit")
            {
                MainInstruction = informe.Errores.Count == 0 && informe.Advertencias.Count == 0
                    ? "Vanos de puertas y ventanas listos" : "Vanos listos, con observaciones (ver detalle)",
                MainContent = informe.Resumen(),
                ExpandedContent = informe.Detalle(),
            }.Show();
            return Result.Succeeded;
        }
    }

    /// <summary>Mueve un eje y todo lo que está sobre él o llega a él.</summary>
    [Transaction(TransactionMode.Manual)]
    [Regeneration(RegenerationOption.Manual)]
    public class ComandoMoverEje : IExternalCommand
    {
        private class SoloEjes : Autodesk.Revit.UI.Selection.ISelectionFilter
        {
            public bool AllowElement(Element e) => e is Grid;
            public bool AllowReference(Reference r, XYZ p) => false;
        }

        public Result Execute(ExternalCommandData data, ref string message, ElementSet elements)
        {
            var uidoc = data.Application.ActiveUIDocument;
            var doc = uidoc?.Document;
            if (doc == null || doc.IsFamilyDocument) { message = "Abre un proyecto de Revit."; return Result.Failed; }
            var eje = uidoc.Selection.GetElementIds().Select(doc.GetElement).OfType<Grid>().FirstOrDefault();
            if (eje == null)
            {
                try { eje = doc.GetElement(uidoc.Selection.PickObject(Autodesk.Revit.UI.Selection.ObjectType.Element, new SoloEjes(), "Elige el eje a mover")) as Grid; }
                catch (Autodesk.Revit.Exceptions.OperationCanceledException) { return Result.Cancelled; }
            }
            if (eje == null) return Result.Cancelled;
            var constructor = new Constructor(doc, new Modelo { Unidades = "m" });
            var c = constructor.ContenidoDe(eje);
            var vertical = c.Linea != null && System.Math.Abs(c.Linea.Direction.Y) > System.Math.Abs(c.Linea.Direction.X);
            var metros = PedirDistancia($"Mover el eje {eje.Name}",
                Constructor.Resumen(c) + "\n\nDistancia en metros (" + (vertical ? "positiva hacia la derecha, negativa hacia la izquierda"
                                                                                  : "positiva hacia arriba, negativa hacia abajo") + "):");
            if (metros == null || System.Math.Abs(metros.Value) < 1e-6) return Result.Cancelled;
            var informe = constructor.MoverEje(eje, metros.Value);
            new TaskDialog("cad2revit")
            {
                MainInstruction = informe.Errores.Count == 0 ? $"Eje {eje.Name} movido {metros.Value:+0.000;-0.000} m" : "Eje movido con observaciones",
                MainContent = informe.Resumen(),
                ExpandedContent = informe.Detalle(),
            }.Show();
            return Result.Succeeded;
        }

        private static double? PedirDistancia(string titulo, string texto)
        {
            using var f = new System.Windows.Forms.Form
            {
                Text = titulo, Width = 520, Height = 300, FormBorderStyle = System.Windows.Forms.FormBorderStyle.FixedDialog,
                StartPosition = System.Windows.Forms.FormStartPosition.CenterScreen, MinimizeBox = false, MaximizeBox = false,
            };
            var lbl = new System.Windows.Forms.Label { Text = texto, Left = 12, Top = 12, Width = 480, Height = 150 };
            var caja = new System.Windows.Forms.TextBox { Left = 12, Top = 170, Width = 160, Text = "0.00" };
            var ok = new System.Windows.Forms.Button { Text = "Mover", Left = 300, Top = 210, Width = 90, DialogResult = System.Windows.Forms.DialogResult.OK };
            var no = new System.Windows.Forms.Button { Text = "Cancelar", Left = 400, Top = 210, Width = 90, DialogResult = System.Windows.Forms.DialogResult.Cancel };
            f.Controls.AddRange(new System.Windows.Forms.Control[] { lbl, caja, ok, no });
            f.AcceptButton = ok; f.CancelButton = no;
            while (f.ShowDialog() == System.Windows.Forms.DialogResult.OK)
            {
                if (double.TryParse(caja.Text.Trim().Replace(',', '.'), System.Globalization.NumberStyles.Float,
                                    System.Globalization.CultureInfo.InvariantCulture, out var v))
                    return v;
                System.Windows.Forms.MessageBox.Show("Escribe un número en metros, por ejemplo 0.30 o -1,25");
            }
            return null;
        }
    }

    /// <summary>Qué hay en cada eje y, si se quiere, amarrar los elementos a los ejes con restricciones de Revit.</summary>
    [Transaction(TransactionMode.Manual)]
    [Regeneration(RegenerationOption.Manual)]
    public class ComandoRevisarEjes : IExternalCommand
    {
        public Result Execute(ExternalCommandData data, ref string message, ElementSet elements)
        {
            var uidoc = data.Application.ActiveUIDocument;
            var doc = uidoc?.Document;
            if (doc == null || doc.IsFamilyDocument) { message = "Abre un proyecto de Revit."; return Result.Failed; }
            var constructor = new Constructor(doc, new Modelo { Unidades = "m" });
            var reporte = constructor.ReporteEjes();
            var td = new TaskDialog("cad2revit")
            {
                MainInstruction = "Qué hay en cada eje",
                MainContent = "Elementos sobre cada eje (a menos de 2 cm) y los que llegan a él. Abre el detalle para ver la lista.",
                ExpandedContent = reporte,
                CommonButtons = TaskDialogCommonButtons.Close,
            };
            td.AddCommandLink(TaskDialogCommandLinkId.CommandLink1, "Amarrar los elementos a los ejes",
                "Pilares: «se mueven con los ejes». Muros y vigas sobre un eje: candado de alineación (en esta vista de planta). " +
                "Después, al mover un eje con la herramienta Mover de Revit, lo amarrado lo sigue.");
            if (td.Show() != TaskDialogResult.CommandLink1) return Result.Succeeded;
            var informe = constructor.VincularAEjes(uidoc.ActiveView);
            new TaskDialog("cad2revit")
            {
                MainInstruction = "Elementos amarrados a los ejes",
                MainContent = informe.Resumen(),
                ExpandedContent = informe.Detalle(),
            }.Show();
            return Result.Succeeded;
        }
    }

    /// <summary>Une los pilares a las vigas que llegan a su parte superior (enlace de Revit, cortando el pilar).</summary>
    [Transaction(TransactionMode.Manual)]
    [Regeneration(RegenerationOption.Manual)]
    public class ComandoUnirPilares : IExternalCommand
    {
        public Result Execute(ExternalCommandData data, ref string message, ElementSet elements)
        {
            var uidoc = data.Application.ActiveUIDocument;
            var doc = uidoc?.Document;
            if (doc == null || doc.IsFamilyDocument) { message = "Abre un proyecto de Revit."; return Result.Failed; }
            var constructor = new Constructor(doc, new Modelo { Unidades = "m" });
            bool EsPilar(Element e) => e is FamilyInstance f && f.StructuralType == Autodesk.Revit.DB.Structure.StructuralType.Column &&
                !(f.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.AsString() ?? "").Contains("entramado");
            var pilares = uidoc.Selection.GetElementIds().Select(doc.GetElement).Where(EsPilar).Cast<FamilyInstance>().ToList();
            if (pilares.Count == 0)
                pilares = new FilteredElementCollector(doc).OfClass(typeof(FamilyInstance)).Where(EsPilar).Cast<FamilyInstance>().ToList();
            if (pilares.Count == 0) { message = "No hay pilares estructurales en el modelo."; return Result.Cancelled; }
            var informe = constructor.UnirPilaresAVigas(pilares);
            new TaskDialog("cad2revit")
            {
                MainInstruction = "Pilares y vigas",
                MainContent = informe.Resumen(),
                ExpandedContent = informe.Detalle(),
            }.Show();
            return Result.Succeeded;
        }
    }
}

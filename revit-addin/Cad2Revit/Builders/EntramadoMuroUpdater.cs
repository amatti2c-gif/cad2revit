using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;

namespace Cad2Revit
{
    /// <summary>
    /// Mantiene el entramado de muro unido a su muro: si el muro se elimina, se elimina su entramado; si cambia de
    /// altura (nivel o desfase de base), el entramado se mueve en vertical lo mismo que cambió la base del muro (cada pieza
    /// guarda en su comentario la cota de base con que se generó: «cad2revit-entramado muro {id} {rol} base=…»).
    /// (Si el muro se mueve en planta o cambia de largo, el entramado se vuelve a generar con el botón.)
    /// </summary>
    public class EntramadoMuroUpdater : IUpdater
    {
        private readonly UpdaterId _id;
        public const string EtiquetaBase = "base=";

        /// <summary>Cota de base del muro (pies) guardada en el comentario de la pieza al generarla; null si no tiene.</summary>
        private static double? BaseGuardada(Element e, out string comentario)
        {
            comentario = e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.AsString() ?? "";
            int i = comentario.LastIndexOf(" " + EtiquetaBase, StringComparison.Ordinal);
            if (i < 0) return null;
            var txt = comentario.Substring(i + 1 + EtiquetaBase.Length).Trim();
            return double.TryParse(txt, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var v)
                ? v : (double?)null;
        }

        public EntramadoMuroUpdater(AddInId addin)
        {
            _id = new UpdaterId(addin, new Guid("3d9a7e51-2c4b-4f8e-a6d1-7b0c5e2f9a34"));
        }

        public static void Registrar(AddInId addin)
        {
            var u = new EntramadoMuroUpdater(addin);
            if (UpdaterRegistry.IsUpdaterRegistered(u.GetUpdaterId())) return;
            UpdaterRegistry.RegisterUpdater(u, true);                // opcional: el modelo abre igual sin el complemento
            var muros = new ElementClassFilter(typeof(Wall));
            UpdaterRegistry.AddTrigger(u.GetUpdaterId(), muros, Element.GetChangeTypeElementDeletion());
            UpdaterRegistry.AddTrigger(u.GetUpdaterId(), muros, Element.GetChangeTypeGeometry());
        }

        private static List<Element> Piezas(Document doc, ElementId muro)
        {
            var marca = Constructor.MarcaEntramadoRevit + muro + " ";
            var cats = new ElementMulticategoryFilter(new List<BuiltInCategory>
                { BuiltInCategory.OST_StructuralColumns, BuiltInCategory.OST_StructuralFraming });
            return new FilteredElementCollector(doc).WherePasses(cats).WhereElementIsNotElementType()
                .Where(e => e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.AsString()?.StartsWith(marca) == true)
                .ToList();
        }

        public void Execute(UpdaterData data)
        {
            var doc = data.GetDocument();
            try
            {
                // muro eliminado: su entramado también
                var borrar = new List<ElementId>();
                foreach (var id in data.GetDeletedElementIds())
                    borrar.AddRange(Piezas(doc, id).Select(e => e.Id));
                if (borrar.Count > 0) doc.Delete(borrar);

                // muro que cambió de altura (nivel o desfase de base): el entramado se mueve lo mismo que cambió la base,
                // según la cota de base que cada pieza guardó al generarse (las piezas antiguas, sin ella, no se mueven)
                foreach (var id in data.GetModifiedElementIds())
                {
                    if (!(doc.GetElement(id) is Wall muro)) continue;
                    var nivel = doc.GetElement(muro.LevelId) as Level;
                    if (nivel == null) continue;
                    double baseActual = nivel.Elevation + (muro.get_Parameter(BuiltInParameter.WALL_BASE_OFFSET)?.AsDouble() ?? 0);
                    var mover = new List<ElementId>();
                    double dz = 0;
                    var renombrar = new List<(Element e, string comentario)>();
                    foreach (var e in Piezas(doc, id))
                    {
                        var b = BaseGuardada(e, out var com);
                        if (b == null || Math.Abs(baseActual - b.Value) < 1e-6) continue;
                        dz = baseActual - b.Value;
                        mover.Add(e.Id);
                        renombrar.Add((e, com));
                    }
                    if (mover.Count == 0) continue;
                    ElementTransformUtils.MoveElements(doc, mover, new XYZ(0, 0, dz));
                    string nueva = baseActual.ToString("R", System.Globalization.CultureInfo.InvariantCulture);
                    foreach (var (e, com) in renombrar)
                    {
                        int i = com.LastIndexOf(" " + EtiquetaBase, StringComparison.Ordinal);
                        e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.Set(com.Substring(0, i) + " " + EtiquetaBase + nueva);
                    }
                }
            }
            catch
            {
                // nunca impedir la edición del usuario por el entramado
            }
        }

        public UpdaterId GetUpdaterId() => _id;
        public ChangePriority GetChangePriority() => ChangePriority.FloorsRoofsStructuralWalls;
        public string GetUpdaterName() => "cad2revit: entramado de muro unido al muro";
        public string GetAdditionalInformation() =>
            "Elimina el entramado de madera cuando se elimina su muro y lo mueve cuando el muro cambia de altura.";
    }
}

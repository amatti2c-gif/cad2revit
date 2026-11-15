using System;
using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.DB;

namespace Cad2Revit
{
    /// <summary>
    /// Mantiene el entramado de piso unido a su piso: si el piso se elimina, se elimina su entramado; si cambia de
    /// altura (nivel, desfase o espesor), el entramado se mueve con él para seguir con su cara superior en la del piso.
    /// Las piezas se reconocen por su comentario «cad2revit-entramado piso {id del piso} …».
    /// </summary>
    public class EntramadoPisoUpdater : IUpdater
    {
        private readonly UpdaterId _id;

        public EntramadoPisoUpdater(AddInId addin)
        {
            _id = new UpdaterId(addin, new Guid("6b1f4c2e-8d1a-4b6e-9c3a-2f7e5d4a9b10"));
        }

        public static void Registrar(AddInId addin)
        {
            var u = new EntramadoPisoUpdater(addin);
            if (UpdaterRegistry.IsUpdaterRegistered(u.GetUpdaterId())) return;
            UpdaterRegistry.RegisterUpdater(u, true);                // opcional: el modelo abre igual sin el complemento
            var pisos = new ElementClassFilter(typeof(Floor));
            UpdaterRegistry.AddTrigger(u.GetUpdaterId(), pisos, Element.GetChangeTypeElementDeletion());
            UpdaterRegistry.AddTrigger(u.GetUpdaterId(), pisos, Element.GetChangeTypeGeometry());
        }

        private static List<Element> Piezas(Document doc, ElementId piso)
        {
            var marca = Constructor.MarcaEntramadoPiso + piso + " ";
            return new FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_StructuralFraming)
                .WhereElementIsNotElementType()
                .Where(e => e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.AsString()?.StartsWith(marca) == true)
                .ToList();
        }

        public void Execute(UpdaterData data)
        {
            var doc = data.GetDocument();
            try
            {
                // piso eliminado: su entramado también
                var borrar = new List<ElementId>();
                foreach (var id in data.GetDeletedElementIds())
                    borrar.AddRange(Piezas(doc, id).Select(e => e.Id));
                if (borrar.Count > 0) doc.Delete(borrar);

                // piso que cambió de altura: el entramado lo sigue (cara superior de las piezas = cara superior del piso)
                foreach (var id in data.GetModifiedElementIds())
                {
                    if (!(doc.GetElement(id) is Floor piso)) continue;
                    var cajaPiso = piso.get_BoundingBox(null);
                    if (cajaPiso == null) continue;
                    var piezas = Piezas(doc, id);
                    if (piezas.Count == 0) continue;
                    double arriba = piezas.Select(e => e.get_BoundingBox(null)).Where(b => b != null).Select(b => b.Max.Z)
                                          .DefaultIfEmpty(double.NaN).Max();
                    if (double.IsNaN(arriba)) continue;
                    double dz = cajaPiso.Max.Z - arriba;
                    if (Math.Abs(dz) < 1e-4) continue;
                    ElementTransformUtils.MoveElements(doc, piezas.Select(e => e.Id).ToList(), new XYZ(0, 0, dz));
                }
            }
            catch
            {
                // nunca impedir la edición del usuario por el entramado
            }
        }

        public UpdaterId GetUpdaterId() => _id;
        public ChangePriority GetChangePriority() => ChangePriority.FloorsRoofsStructuralWalls;
        public string GetUpdaterName() => "cad2revit: entramado de piso unido al piso";
        public string GetAdditionalInformation() =>
            "Elimina el entramado de madera cuando se elimina su piso y lo mueve cuando el piso cambia de altura.";
    }
}
